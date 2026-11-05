from __future__ import annotations

import argparse
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser(description="DownloadBot API and worker")
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker")
    init = sub.add_parser("init", help="Generate a local .env without displaying secrets")
    init.add_argument("--env-file", default=".env")
    init.add_argument("--database-url-env", default="DOWNLOADBOT_DATABASE_URL",
                      help="Environment variable holding the PostgreSQL DSN")
    args = parser.parse_args()
    if args.command == "init":
        dsn = os.environ.get(args.database_url_env)
        if not dsn or "\n" in dsn or "'" in dsn:
            parser.error("Set the database URL environment variable to a valid PostgreSQL DSN first")
        path = Path(args.env_file)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"DOWNLOADBOT_DATABASE_URL='{dsn}'\n")
            handle.write(f"DOWNLOADBOT_API_KEY={secrets.token_urlsafe(32)}\n")
            handle.write(f"DOWNLOADBOT_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n")
            handle.write("DOWNLOADBOT_DATA_DIR=data\n")
        print(f"Created {path}; credentials were not printed. Keep this file private.")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "api":
        import uvicorn
        uvicorn.run("downloadbot.api:create_app", factory=True, host=args.host, port=args.port,
                    access_log=False, log_level="info")
    else:
        from downloadbot.config import Settings
        from downloadbot.worker import run_worker
        run_worker(Settings())


if __name__ == "__main__":
    main()
