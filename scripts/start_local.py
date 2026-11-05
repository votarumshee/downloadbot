"""Start local development API/worker as hidden child processes; PID files stay in .runtime."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parents[1]
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({k: v for k, v in dotenv_values(root / ".env").items() if v is not None})
    import socket
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            raise SystemExit("Port 8000 is already in use; stop the existing service before starting another.")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    children = {}
    for name, command in [("api", ["api"]), ("worker", ["worker"])]:
        with (runtime / f"{name}.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, "-m", "downloadbot", *command], cwd=root, env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
        children[name] = child.pid
        if name == "api":
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise SystemExit("API failed to start; see .runtime/api.log")
                try:
                    response = httpx.get("http://127.0.0.1:8000/healthz", timeout=1, trust_env=False)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            else:
                raise SystemExit("API startup timed out; inspect .runtime/api.log before retrying")
    (runtime / "processes.json").write_text(json.dumps(children), encoding="utf-8")
    print("Started local API and worker; PIDs saved in .runtime/processes.json")


if __name__ == "__main__":
    main()
