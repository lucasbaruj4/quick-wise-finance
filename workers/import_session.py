#!/usr/bin/env python3
"""Upload an existing approved bank session as Worker secrets, without printing it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path, help="Private state directory of your working local installation")
    parser.add_argument("--config", type=Path, required=True, help="Private deployment wrangler.jsonc")
    args = parser.parse_args()
    state = args.state.expanduser().resolve()
    config = {}
    for line in (state / "config.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            config[key] = shlex.split(value)[0]
    session = json.loads((state / "session.json").read_text())
    minimal = {"access": session["access"], "accounts": [
        {"uid": str(uuid.UUID(a["uid"])),
         "name": (a.get("details") or a.get("product") or a.get("name") or "Unnamed account").strip(),
         "currency": a.get("currency")}
        for a in session["accounts"]
    ]}
    key = subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt", "-in", str(state / "private.key")],
                         capture_output=True, check=True).stdout.decode()
    secrets = {"ENABLE_BANKING_APP_ID": str(uuid.UUID(config["ENABLE_BANKING_APP_ID"])),
               "BANK_PRIVATE_KEY": key, "BANK_SESSION": json.dumps(minimal),
               "OWNER_PASSWORD_SHA256": hashlib.sha256((state / "bridge-password").read_text().strip().encode()).hexdigest()}
    if any(len(value.encode()) > 5000 for value in secrets.values()):
        raise SystemExit("A secret exceeds the free plan's value size. No secret was uploaded.")
    with tempfile.TemporaryDirectory(prefix="wise-worker-secrets-") as directory:
        path = Path(directory) / "secrets.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(secrets, handle)
        subprocess.run([str(Path(__file__).parent / "node_modules/.bin/wrangler"), "secret", "bulk", str(path),
                        "--config", str(args.config.resolve())], check=True)
    print("Approved bank session imported. Existing OAuth storage and connection password were preserved.")


if __name__ == "__main__":
    main()
