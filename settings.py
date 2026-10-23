"""Own the locations and ports for this installation's private runtime state."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "quick-wise-finance"
STATE_DIR = STATE_DIR.expanduser().resolve()
if STATE_DIR == ROOT or ROOT in STATE_DIR.parents:
    raise ValueError("Keep XDG_STATE_HOME outside the repository; it holds private bank data.")
TLS_DIR = STATE_DIR / "local-tls"


def port(name, default):
    value = int(os.environ.get(name, default))
    if not 1024 <= value <= 65535:
        raise ValueError(f"{name} must be between 1024 and 65535.")
    return value


CALLBACK_PORT = port("QWF_CALLBACK_PORT", 53857)
POLICY_PORT = port("QWF_POLICY_PORT", 53858)
BRIDGE_PORT = port("QWF_BRIDGE_PORT", 53860)
if len({CALLBACK_PORT, POLICY_PORT, BRIDGE_PORT}) != 3:
    raise ValueError("The callback, policy, and bridge ports must be different.")
CALLBACK = f"https://localhost:{CALLBACK_PORT}/callback"

if __name__ == "__main__":
    print({"state": str(STATE_DIR), "callback-port": CALLBACK_PORT, "callback": CALLBACK}[sys.argv[1]])
