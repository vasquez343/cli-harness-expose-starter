import argparse
import secrets

import uvicorn


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("token", help="Generate an API token")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()
    if args.command == "token":
        print(secrets.token_urlsafe(32))
    else:
        uvicorn.run("agentd.app:create_app", factory=True, host=args.host, port=args.port,
                    workers=1, proxy_headers=False)
