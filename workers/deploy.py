#!/usr/bin/env python3
"""Deploy with persistent configuration outside Git; keep the same OAuth namespace."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
WRANGLER = str(ROOT / "node_modules/.bin/wrangler")


def run(*arguments):
    result = subprocess.run([WRANGLER, *arguments], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if result.returncode:
        raise SystemExit(result.returncode)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Deployment config outside the repository")
    parser.add_argument("--account-id", help="Cloudflare account ID, required on first deploy")
    args = parser.parse_args()
    path = args.config.expanduser().resolve()
    if ROOT.parent == path or ROOT.parent in path.parents:
        raise SystemExit("Keep the deployment config outside the repository.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        config = json.loads(path.read_text())
    else:
        if not args.account_id or not re.fullmatch(r"[a-f0-9]{32}", args.account_id):
            raise SystemExit("Provide --account-id on first deployment. See: npx wrangler whoami")
        config = json.loads((ROOT / "wrangler.jsonc").read_text())
        config["main"] = str(ROOT / "src/index.ts")
        config["account_id"] = args.account_id
        config.pop("$schema", None)
        # A first deploy must work before secrets are imported; protected tools fail closed.
        config.pop("secrets", None)
        path.write_text(json.dumps(config, indent=2) + "\n")
    config.pop("tsconfig", None)
    path.write_text(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)
    if not config["kv_namespaces"][0].get("id"):
        run("kv", "namespace", "create", "quick-wise-finance-oauth", "--binding", "OAUTH_KV", "--update-config", "--config", str(path))
        config = json.loads(path.read_text())
        if not config["kv_namespaces"][0].get("id"):
            raise SystemExit("OAuth storage was not saved. Stop here; do not create another namespace.")
    output = run("deploy", "--config", str(path))
    matches = re.findall(r"https://quick-wise-finance\.[a-z0-9-]+\.workers\.dev", output)
    if not matches:
        raise SystemExit("No workers.dev URL returned. Configure your free Workers subdomain, then run again.")
    origin = matches[-1]
    if config["vars"]["PUBLIC_ORIGIN"] != origin:
        config["vars"]["PUBLIC_ORIGIN"] = origin
        path.write_text(json.dumps(config, indent=2) + "\n")
        path.chmod(0o600)
        run("deploy", "--config", str(path))
    print(f"Stable MCP URL: {origin}/mcp")
    print("Import your approved bank session next. Keep this deployment config for every update.")


if __name__ == "__main__":
    main()
