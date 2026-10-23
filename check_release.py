"""Reject unreviewed files and common private artifacts before publication."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
# Explicit publication boundary: runtime files are never part of a release.
PUBLIC_FILES = {
    '.gitignore', '.github/workflows/check.yml', 'README.md', 'LICENSE',
    'requirements.txt', 'setup.sh', 'wizard.sh', 'settings.py', 'install_tunnel.py',
    'connection.py', 'local_tls.py', 'policy_pages.py', 'access_check.py',
    'bridge.py', 'bridge_auth.py', 'wise_tools.py', 'check_bridge.py', 'check_release.py', 'check_runtime.py',
}
PATTERNS = [
    (re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'), 'private key'),
    (re.compile(rb'https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com'), 'live tunnel URL'),
    (re.compile(rb'/home/(?!YOUR_USER(?:/|\b))[a-zA-Z0-9_.-]+/'), 'personal home path'),
    (re.compile(rb'\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b'), 'possible bank account number'),
]


def validate_files(files):
    failures = []
    for name, data in files.items():
        if name not in PUBLIC_FILES:
            failures.append(f'{name}: outside the publication allowlist')
        if len(data) > 200_000 or b'\x00' in data:
            failures.append(f'{name}: binary or oversized file')
        for pattern, reason in PATTERNS:
            if pattern.search(data):
                failures.append(f'{name}: {reason}')
    return failures


def check():
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    files = {name: subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT) for name in names if name}
    assert files, 'Stage the reviewed source files first: git add <files>'
    errors = validate_files(files)
    # Prove the boundary rejects both an accidental state file and key content.
    assert validate_files({'session.json': b'{"accounts":[]}'}), 'State guard failed'
    assert validate_files({'README.md': b'-----BEGIN ' + b'PRIVATE KEY-----'}), 'Key guard failed'
    assert not validate_files({'README.md': b'Only a generic example.'}), 'Safe file rejected'
    if errors:
        raise SystemExit('\n'.join(errors) + '\nRemove private content from Git before publishing.')
    print(f'PASS: {len(files)} allowlisted text files; no private artifacts detected. Manual review is still required.')


if __name__ == '__main__':
    check()
