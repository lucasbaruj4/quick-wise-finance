"""Create a separate TLS certificate for this computer's local servers."""
import os
from pathlib import Path
import ssl
import subprocess

from settings import TLS_DIR


def server_context():
    directory = TLS_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key, certificate = directory / "localhost.key", directory / "localhost.crt"
    if not key.exists() or not certificate.exists():
        subprocess.run([
            "openssl", "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "365", "-keyout", str(key), "-out", str(certificate),
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ], check=True, capture_output=True)
        os.chmod(key, 0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context
