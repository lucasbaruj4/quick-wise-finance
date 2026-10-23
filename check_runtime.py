"""Check isolated paths, local TLS, and process lifecycle without a bank or tunnel."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def check():
    denied = subprocess.run([sys.executable, '-c', 'import settings'], cwd=ROOT,
                            env={**os.environ, 'XDG_STATE_HOME': str(ROOT / 'private')}, capture_output=True)
    assert denied.returncode != 0 and b'outside the repository' in denied.stderr
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        env = {**os.environ, 'XDG_STATE_HOME': str(directory)}
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            env['QWF_BRIDGE_PORT'] = str(listener.getsockname()[1])
        state = directory / 'quick-wise-finance'
        state.mkdir(mode=0o700)
        (state / 'config.env').write_text('ENABLE_BANKING_APP_ID=11111111-1111-4111-8111-111111111111\n')
        (state / 'session.json').write_text('{}')
        bin_dir = directory / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'cloudflared'
        # A local stub avoids an external tunnel or public endpoint during checks.
        fake.write_text('#!' + sys.executable + '\nimport time\nprint("https://test-runtime" + ".trycloudflare.com", flush=True)\ntime.sleep(120)\n')
        fake.chmod(0o700)
        env['PATH'] = str(bin_dir) + os.pathsep + env['PATH']

        def run(*arguments):
            result = subprocess.run([sys.executable, *arguments], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=40)
            if result.returncode:
                raise AssertionError(result.stderr + result.stdout)
            return result

        try:
            run('-c', 'from local_tls import server_context; server_context()')
            assert (state / 'local-tls/localhost.key').stat().st_mode & 0o777 == 0o600
            assert not (ROOT / 'local-tls').exists()
            run('connection.py', 'start')
            records = {name: json.loads((state / (name + '.pid')).read_text()) for name in ('bridge', 'tunnel')}
            run('connection.py', 'start')
            assert all(json.loads((state / (name + '.pid')).read_text()) == record for name, record in records.items()), 'Repeated start created duplicate processes'
            assert 'Bridge running: True Tunnel running: True' in run('connection.py', 'status').stdout
            run('connection.py', 'stop')
            assert 'Bridge running: False Tunnel running: False' in run('connection.py', 'status').stdout
            # A stale PID record must never authorize stopping an unrelated process.
            (state / 'bridge.pid').write_text(json.dumps({'pid': os.getpid(), 'argv': ['unrelated-command'], 'started': '0'}))
            assert 'Bridge running: False' in run('connection.py', 'status').stdout
            run('connection.py', 'stop')
        finally:
            run('connection.py', 'stop')
    print('PASS: state and TLS stay outside Git; start is repeatable; stop rejects unrelated PIDs. No bank or real tunnel contacted.')


if __name__ == '__main__':
    check()
