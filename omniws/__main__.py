"""`python -m omniws` starts the server on OMNI_HOST:OMNI_PORT (127.0.0.1:8300 by default).

`python -m omniws passkey-link` prints a one-time link (15 minutes) that registers a passkey without signing in:
for the first device, or when every registered device is lost.
"""

import argparse
import os
import sys

import uvicorn

from .config import get_settings


def passkey_link(minutes: int) -> None:
    from sqlalchemy.orm import sessionmaker

    from .auth import create_enroll_token
    from .db import make_engine, migrate

    settings = get_settings()
    origins = settings.public_origins()
    if not origins:
        raise SystemExit("OMNI_PUBLIC_URLS を .env に書いてください（例: https://myserver.tail1234.ts.net）")
    engine = make_engine(settings.omni_data_dir)
    migrate(engine)
    token = create_enroll_token(sessionmaker(engine), minutes)
    print(f"パスキー登録用のリンク（{minutes}分・1回だけ有効）:")
    for origin in origins:
        print(f"  {origin}/#/enroll/{token}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m omniws")
    commands = parser.add_subparsers(dest="command")
    link = commands.add_parser("passkey-link", help="パスキー登録用の一回限りのリンクを作る")
    link.add_argument("--minutes", type=int, default=15)
    args = parser.parse_args()
    if args.command == "passkey-link":
        passkey_link(args.minutes)
        return
    settings = get_settings()
    from .main import app

    server = uvicorn.Server(uvicorn.Config(app, host=settings.omni_host, port=settings.omni_port, proxy_headers=True,
                                           forwarded_allow_ips="127.0.0.1", timeout_graceful_shutdown=10))
    # The settings screen stops the server through this flag; a signal would end the whole process afterwards
    app.state.server = server
    app.state.can_restart = True
    server.run()
    if getattr(app.state, "restart_requested", False):
        # The settings screen asked for a restart: start again as the same process (systemd keeps its PID)
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


if __name__ == "__main__":
    main()
