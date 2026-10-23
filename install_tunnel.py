"""Install an official, pinned Cloudflare binary into private local state."""
import hashlib
import os
import platform
import shutil
import urllib.request

from settings import STATE_DIR

VERSION = "2026.10.3"
HASHES = {
    "x86_64": ("amd64", "053b80b0a23031a038fdbf3c073f59f82b8f1f152aa67971d7383ffafc0dbab0"),
    "aarch64": ("arm64", "89297ebfb9e8c99c0dc013a1140601453cf3798d8306c5591651f4a3f5210be8"),
}


def install():
    if platform.system() != "Linux":
        raise SystemExit("This setup supports Linux and WSL. Use a Linux environment.")
    if shutil.which("cloudflared"):
        print("Using cloudflared already installed on PATH.")
        return
    if platform.machine() not in HASHES:
        raise SystemExit("Install cloudflared from Cloudflare's official downloads, then rerun setup.")
    arch, expected = HASHES[platform.machine()]
    directory = STATE_DIR / "bin"
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    binary = directory / "cloudflared"
    if binary.exists() and hashlib.sha256(binary.read_bytes()).hexdigest() == expected:
        binary.chmod(0o700)
        return
    url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-linux-{arch}"
    print(f"Downloading official cloudflared {VERSION} ({arch}); verifying SHA-256.")
    with urllib.request.urlopen(url, timeout=90) as response:
        data = response.read(100_000_000)
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit("Cloudflare download checksum failed; nothing was installed.")
    temporary = binary.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o700)
    temporary.replace(binary)


if __name__ == "__main__":
    install()
