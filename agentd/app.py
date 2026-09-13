from contextlib import aclosing
import hmac
import json

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import Config
from .runner import HarnessError, run


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=16000)


def create_app(config: Config | None = None, runner=run) -> FastAPI:
    config = config or Config.from_env()
    app = FastAPI(title="CLI Harness Expose Starter", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.busy = False

    async def auth(authorization: str = Header(default="")):
        if not config.token:
            raise HTTPException(503, "AGENTD_TOKEN is not configured")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), config.token.encode()):
            raise HTTPException(401, "Invalid bearer token")

    def reserve():
        if not config.allow_unsafe_cli:
            raise HTTPException(503, "CLI disabled; evaluate isolation before enabling HARNESS_ALLOW_UNSAFE_CLI")
        if app.state.busy:
            raise HTTPException(409, "Workspace is busy; retry later")
        # No await between check/set; valid for ONE ASGI worker/event loop.
        app.state.busy = True

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/run", dependencies=[Depends(auth)])
    async def run_json(body: RunRequest):
        reserve()
        try:
            final = None
            async with aclosing(runner(config, body.prompt)) as events:
                async for event in events:
                    if event.get("type") == "result":
                        final = event
            if final is None:
                raise HarnessError("CLI returned no final result")
            return final
        except (HarnessError, OSError) as exc:
            # Avoid reflecting filesystem paths or OS environment details.
            detail = str(exc) if isinstance(exc, HarnessError) else "Unable to launch or read CLI"
            raise HTTPException(502, detail) from exc
        finally:
            app.state.busy = False

    @app.post("/stream", dependencies=[Depends(auth)])
    async def stream(body: RunRequest):
        reserve()

        async def generate():
            try:
                async with aclosing(runner(config, body.prompt)) as events:
                    async for event in events:
                        yield "data: " + json.dumps(event, ensure_ascii=True) + "\n\n"
            except (HarnessError, OSError) as exc:
                detail = str(exc) if isinstance(exc, HarnessError) else "Unable to launch or read CLI"
                yield "data: " + json.dumps({"type": "error", "ok": False, "error": detail}) + "\n\n"
            finally:
                app.state.busy = False

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
