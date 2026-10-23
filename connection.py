#!/usr/bin/env python3
"""Start, inspect, and stop the personal Wise connection on Linux or WSL."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from settings import ROOT, STATE_DIR as STATE, BRIDGE_PORT


def cloudflared():
    binary = shutil.which("cloudflared") or str(STATE / "bin/cloudflared")
    if not Path(binary).is_file():
        raise SystemExit("cloudflared is missing. Run ./setup.sh install.")
    return binary


def alive(name):
    try:
        record = json.loads((STATE / (name + ".pid")).read_text())
        pid = int(record["pid"])
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        # A recorded PID alone can belong to a different process after a restart.
        started = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return pid if command[:-1] == [s.encode() for s in record["argv"]] and started == record["started"] else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


def start_process(name, arguments):
    with (STATE / (name + ".log")).open("w") as stream:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=stream, start_new_session=True)
    started = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    actual_argv = [part.decode() for part in Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0")[:-1]]
    (STATE / (name + ".pid")).write_text(json.dumps({"pid": process.pid, "argv": actual_argv, "started": started}))
    return process


def stop_one(name):
    pid = alive(name)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not alive(name):
                return
            time.sleep(.1)
        raise SystemExit(f"{name} is still stopping. Retry in a moment.")


def start():
    if not (STATE / "config.env").exists() or not (STATE / "session.json").exists():
        raise SystemExit("Bank setup is missing. Run ./setup.sh first.")
    if not alive("bridge"):
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", BRIDGE_PORT))
            except OSError:
                raise SystemExit(f"Port {BRIDGE_PORT} is busy. Choose another QWF_BRIDGE_PORT.") from None
    password = STATE / "bridge-password"
    if not password.exists():
        password.write_text(secrets.token_urlsafe(32))
    password.chmod(0o600)
    if not alive("tunnel"):
        start_process("tunnel", [cloudflared(), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{BRIDGE_PORT}"])
        for _ in range(100):
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", (STATE / "tunnel.log").read_text())
            if match:
                (STATE / "bridge-origin").write_text(match[0])
                break
            if not alive("tunnel"):
                raise SystemExit(f"Tunnel stopped. Check {STATE / 'tunnel.log'}.")
            time.sleep(.3)
        else:
            raise SystemExit(f"Tunnel URL not ready. Check {STATE / 'tunnel.log'}.")
        stop_one("bridge")
    origin = (STATE / "bridge-origin").read_text().strip()
    if not alive("bridge"):
        start_process("bridge", [sys.executable, str(ROOT / "bridge.py"), origin, "--state", str(STATE)])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            with opener.open(f"http://127.0.0.1:{BRIDGE_PORT}/health", timeout=1) as response:
                if json.load(response) != {"service": "wise-finances", "read_only": True}:
                    raise ValueError("Unexpected local service")
            print("Server URL: " + origin + "/mcp")
            print("Authentication: OAuth. Leave client ID and client secret empty for automatic registration.")
            print("Run ./setup.sh password locally to show the connection password.")
            return
        except (OSError, ValueError):
            if not alive("bridge"):
                raise SystemExit(f"Bridge stopped. Check {STATE / 'bridge.log'}.")
            time.sleep(.1)
    raise SystemExit("Local bridge did not become ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "status", "stop", "password", "reset-chatgpt"])
    command = parser.parse_args().command
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    if command == "start":
        start()
    elif command == "status":
        print("Bridge running:", bool(alive("bridge")), "Tunnel running:", bool(alive("tunnel")))
        if (STATE / "bridge-origin").exists():
            print("Server URL:", (STATE / "bridge-origin").read_text().strip() + "/mcp")
    elif command == "stop":
        for name in ("bridge", "tunnel"):
            stop_one(name)
        print("Local bridge and temporary tunnel stopped.")
    elif command == "password":
        if not (STATE / "bridge-password").exists():
            raise SystemExit("Start the connection first: ./setup.sh start")
        if not sys.stdout.isatty():
            raise SystemExit("Show the connection password in a local terminal, without redirecting or logging it.")
        print((STATE / "bridge-password").read_text().strip())
    elif command == "reset-chatgpt":
        stop_one("bridge")
        for name in ("bridge-clients.json", "bridge-tokens.json"):
            (STATE / name).unlink(missing_ok=True)
        print("ChatGPT access revoked. Bank consent is unchanged. Run ./setup.sh start and reconnect ChatGPT.")


if __name__ == "__main__":
    main()
