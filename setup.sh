#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command="${1:-setup}"
case "$command" in
  setup|install|renew|start|status|stop|password|reset-chatgpt|check) ;;
  *) printf 'Usage: ./setup.sh [install|renew|start|status|stop|password|reset-chatgpt|check]\n' >&2; exit 2 ;;
esac
for dependency in python3 openssl; do
  command -v "$dependency" >/dev/null || { printf 'Install %s first. See README.md.\n' "$dependency" >&2; exit 1; }
done
if [[ "$command" == setup || "$command" == install ]]; then
  python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11 or newer is required"'
  python3 -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
  .venv/bin/python install_tunnel.py
fi
[[ -x .venv/bin/python ]] || { printf 'Run ./setup.sh install first.\n' >&2; exit 1; }
export PATH="$PWD/.venv/bin:$PATH"
case "$command" in
  install) printf 'Dependencies installed. Run ./setup.sh to authorize your own Wise accounts.\n' ;;
  setup) bash wizard.sh; .venv/bin/python connection.py start ;;
  renew) .venv/bin/python - <<'PY'
from pathlib import Path
import os
from access_check import authorization
from wise_tools import WiseData
from settings import STATE_DIR
from subprocess import Popen, DEVNULL
import time
os.umask(0o077)
# Show only on this terminal; do not send the one-time bank link to a public log.
child = Popen([str(Path('.venv/bin/python')), 'access_check.py', 'authorize', WiseData(STATE_DIR).client.app_id, str(STATE_DIR)])
try:
    approval = STATE_DIR / 'approval.url'
    print('Approve access in a browser on this computer. Waiting for the approval URL...', flush=True)
    while child.poll() is None:
        if approval.exists():
            print(approval.read_text(), flush=True)
            break
        time.sleep(.2)
    raise SystemExit(child.wait())
finally:
    if child.poll() is None:
        child.terminate()
        child.wait()
PY
    ;;
  check) .venv/bin/python check_release.py; .venv/bin/python check_bridge.py; .venv/bin/python check_runtime.py ;;
  *) .venv/bin/python connection.py "$command" ;;
esac
