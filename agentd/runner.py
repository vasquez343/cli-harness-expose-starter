"""Fixed CLI argv, stdin prompts, bounded JSONL and POSIX process-group cleanup."""
import asyncio
from contextlib import aclosing, suppress
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from anyio import CancelScope

from .config import Config


class HarnessError(Exception):
    pass


def child_env() -> dict[str, str]:
    names = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    names.update(n.strip() for n in os.getenv("HARNESS_ENV_ALLOWLIST", "").split(",") if n.strip())
    names.discard("AGENTD_TOKEN")
    return {name: os.environ[name] for name in names if name in os.environ}


def command(config: Config, output: Path) -> list[str]:
    if not config.allow_unsafe_cli:
        raise HarnessError("CLI execution disabled: review isolation and set HARNESS_ALLOW_UNSAFE_CLI=1")
    if config.provider == "codex":
        return ["codex", "exec", "--json", "--color", "never", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", "-o", str(output), "-"]
    return ["claude", "--print", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--dangerously-skip-permissions"]


async def process_events(argv, *, config: Config, prompt: str):
    """Linux/macOS only. Killing a process group is cleanup, not a sandbox."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=config.workspace, env=child_env(), start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=min(config.max_output_bytes, 1024 * 1024),
    )
    deadline = time.monotonic() + config.total_timeout
    stderr_bytes = 0

    async def drain_stderr():
        nonlocal stderr_bytes
        while chunk := await proc.stderr.read(8192):
            stderr_bytes += len(chunk)
            if stderr_bytes > config.max_output_bytes:
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                return

    stderr_task = asyncio.create_task(drain_stderr())

    async def bounded(awaitable):
        remaining = deadline - time.monotonic()
        try:
            return await asyncio.wait_for(awaitable, max(0, min(config.idle_timeout, remaining)))
        except asyncio.TimeoutError as exc:
            raise HarnessError("CLI exceeded inactivity or total execution timeout") from exc

    try:
        proc.stdin.write(prompt.encode())
        await bounded(proc.stdin.drain())
        proc.stdin.close()
        received = 0
        while True:
            try:
                raw = await bounded(proc.stdout.readline())
            except ValueError as exc:
                raise HarnessError("CLI output line exceeds limit") from exc
            if not raw:
                break
            received += len(raw)
            if received + stderr_bytes > config.max_output_bytes:
                raise HarnessError("CLI output exceeds byte limit")
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                yield event
        await bounded(proc.wait())
        await bounded(stderr_task)
        if received + stderr_bytes > config.max_output_bytes:
            raise HarnessError("CLI output exceeds byte limit")
        if proc.returncode:
            raise HarnessError(f"CLI exited with code {proc.returncode}")
    finally:
        # Also terminate descendants when the parent exited normally.
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with CancelScope(shield=True):
            await proc.wait()
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task


async def run(config: Config, prompt: str):
    if not config.workspace.is_dir():
        raise HarnessError("HARNESS_WORKSPACE must be an existing directory")
    # Parent directory stays fixed server-side; callers cannot choose cwd/argv.
    with tempfile.TemporaryDirectory(prefix="agentd-") as tmp:
        output = Path(tmp) / "last.txt"
        final_text = ""
        failed = False
        saw_result = False
        async with aclosing(process_events(command(config, output), config=config, prompt=prompt)) as events:
            async for event in events:
                if config.provider == "claude" and event.get("type") == "result":
                    saw_result = True
                    final_text = event.get("result", "")
                    failed |= bool(event.get("is_error"))
                if event.get("type") in {"error", "turn.failed"}:
                    failed = True
                yield {"type": "event", "provider": config.provider, "data": event}
        if failed:
            raise HarnessError("Harness reported a failed turn")
        if config.provider == "claude" and not saw_result:
            raise HarnessError("Claude did not produce a result event")
        if config.provider == "codex":
            if not output.is_file():
                raise HarnessError("Codex did not produce its final-message file")
            with output.open("rb") as source:
                data = source.read(config.max_output_bytes + 1)
            if len(data) > config.max_output_bytes:
                raise HarnessError("Final answer exceeds byte limit")
            final_text = data.decode(errors="replace")
        if not isinstance(final_text, str):
            raise HarnessError("Unexpected final response format")
        yield {"type": "result", "ok": True, "provider": config.provider, "text": final_text}
