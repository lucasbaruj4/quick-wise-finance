#!/usr/bin/env python3
"""One-time Enable Banking account-information probe. Uses Python and OpenSSL."""

import argparse
import base64
import datetime as dt
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from local_tls import server_context
from settings import CALLBACK, CALLBACK_PORT

BASE = "https://api.enablebanking.com"


def private_write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        handle.write(value)
    temporary.replace(path)


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, app_id, key):
        self.app_id = str(uuid.UUID(app_id))
        self.key = key

    def request(self, method, path, body=None):
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        account_read = (
            not parsed.scheme and not parsed.netloc and not parsed.fragment
            and re.fullmatch(r"/accounts/[0-9a-f-]+/(balances|transactions)", parsed.path)
            and set(query) <= {"date_from", "date_to", "continuation_key"}
            and all(len(values) == 1 for values in query.values())
            and (parsed.path.endswith("/transactions") or not query)
        )
        allowed = (
            method == "POST" and path in {"/auth", "/sessions"}
            or method == "GET" and (
                path in {"/application", "/aspsps?psu_type=personal&service=AIS"}
                or account_read
            )
        )
        if not allowed:
            raise RuntimeError("This checker permits account-information requests only.")
        now = int(time.time())
        header = {"typ": "JWT", "alg": "RS256", "kid": self.app_id}
        claims = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 300}
        signed = b".".join(encode(json.dumps(x).encode()) for x in (header, claims))
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(self.key)],
            input=signed, capture_output=True, check=True,
        ).stdout
        token = (signed + b"." + encode(signature)).decode()
        request = urllib.request.Request(
            BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                code = json.load(error).get("error", "UNKNOWN")
            except (ValueError, AttributeError):
                code = "UNKNOWN"
            raise RuntimeError(f"Enable Banking returned HTTP {error.code}, {code}, for {method} {path}. No payment was requested.") from None


def validate_app(client, active=False):
    app = client.request("GET", "/application")
    if app.get("environment") != "PRODUCTION":
        raise RuntimeError("This is a sandbox app. Register a Production app for your real account.")
    if CALLBACK not in app.get("redirect_urls", []):
        raise RuntimeError(f"Add this exact redirect URL to your app: {CALLBACK}")
    if "AIS" not in app.get("services", []):
        raise RuntimeError("Enable the Account Information service on the app.")
    if active and not app.get("active"):
        raise RuntimeError("The app is inactive. Complete 'Activate by linking accounts' first.")
    print("App ID and private key match. Production and Account Information are configured.", flush=True)


def authorization(client, directory):
    validate_app(client, active=True)
    banks = client.request("GET", "/aspsps?psu_type=personal&service=AIS").get("aspsps", [])
    wise = [b for b in banks if b["name"].lower() in {"wise", "wisev2"} and "personal" in b.get("psu_types", [])]
    if not wise:
        raise RuntimeError("The live bank list does not expose Wise personal accounts. Stop here so we can inspect coverage.")
    wise.sort(key=lambda b: (b["country"] != "BE", b["country"] != "DE", b["name"].lower() != "wise", b["country"]))
    bank = wise[0]
    state = secrets.token_urlsafe(32)
    received = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            supplied = query.get("state", [""])[0]
            if not secrets.compare_digest(supplied, state):
                self.send_error(400, "Incorrect callback state")
                return
            received.update(code=query.get("code", [""])[0], error=query.get("error", [""])[0])
            message = b"Wise approval returned. Go back to the setup terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    try:
        server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), Callback)
        server.socket = server_context().wrap_socket(server.socket, server_side=True)
    except OSError:
        raise RuntimeError(f"Local port {CALLBACK_PORT} is in use. Close the earlier access check before retrying.") from None
    url_file = directory / "approval.url"
    try:
        lifetime = int(bank["maximum_consent_validity"])
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(1, lifetime - 30))).isoformat()
        result = client.request("POST", "/auth", {
            "access": {"valid_until": until, "balances": True, "transactions": True},
            "aspsp": {"name": bank["name"], "country": bank["country"]},
            "state": state, "redirect_url": CALLBACK, "psu_type": "personal", "language": "en",
        })
        url = result["url"]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"auth.enablebanking.com", "tilisy.enablebanking.com"}:
            raise RuntimeError("Unexpected approval host. Stop here so we can inspect the response.")
        private_write(url_file, url)
        print(f"Using {bank['name']} through {bank['country']}. Waiting up to 15 minutes for browser approval.", flush=True)
        server.timeout = 1
        deadline = time.monotonic() + 900
        while not received and time.monotonic() < deadline:
            server.handle_request()
        if not received:
            raise RuntimeError("Approval timed out. Re-run the wizard to start a new approval.")
        if received.get("error") or not received.get("code"):
            raise RuntimeError("Wise approval was cancelled or returned no authorization code. Re-run when ready.")
        session = client.request("POST", "/sessions", {"code": received["code"]})
    finally:
        server.server_close()
        url_file.unlink(missing_ok=True)

    accounts = session.get("accounts", [])
    if not accounts:
        raise RuntimeError("Wise returned no linked accounts. Link each desired account using the same Wise connector, then retry.")
    minimal_accounts = [{k: a[k] for k in ("uid", "name", "details", "product", "currency") if k in a} for a in accounts]
    private_write(directory / "session.json", json.dumps({
        "session_id": session["session_id"], "access": session.get("access"),
        "aspsp": session.get("aspsp"), "accounts": minimal_accounts,
    }, indent=2))
    report = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "sample_only": True, "accounts": []}
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).date().isoformat()
    failures = 0
    for account in minimal_accounts:
        label = account.get("details") or account.get("product") or account.get("name") or "Unnamed account"
        print(f"\n{label} [{account.get('currency', '?')}]", flush=True)
        row = dict(account)
        for operation in ("balances", "transactions"):
            try:
                uid = str(uuid.UUID(account.get("uid", "")))
                path = f"/accounts/{uid}/{operation}"
                if operation == "transactions":
                    path += "?date_from=" + since
                data = client.request("GET", path)
                if operation == "balances":
                    row[operation] = data.get("balances", [])
                    for balance in row[operation]:
                        amount = balance.get("balance_amount", {})
                        print(f"  {balance.get('balance_type', 'Balance')}: {amount.get('amount', '?')} {amount.get('currency', '')}", flush=True)
                else:
                    sample = data.get("transactions", [])[:5]
                    row[operation] = [{k: t[k] for k in (
                        "booking_date", "transaction_date", "transaction_amount", "credit_debit_indicator",
                        "status", "remittance_information", "entry_reference",
                    ) if k in t} for t in sample]
                    row["more_transactions_available"] = bool(data.get("continuation_key")) or len(data.get("transactions", [])) > 5
                    print(f"  Transaction access works: {len(sample)} sample entries saved from the last 30 days.", flush=True)
            except (RuntimeError, ValueError) as error:
                failures += 1
                row[operation + "_error"] = str(error)
                print(f"  {operation}: {error}", flush=True)
        report["accounts"].append(row)
    private_write(directory / "access-report.json", json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nSaved a private sample report to {directory / 'access-report.json'}", flush=True)
    print("Compare every returned account and Jar with the accounts you selected in Wise.", flush=True)
    print("This does not prove Jar coverage or a complete transaction history. ChatGPT is not connected yet.", flush=True)
    if failures:
        raise RuntimeError("Some data requests failed. The saved report shows which ones; do not treat this as complete access.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate", "authorize"])
    parser.add_argument("app_id")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    client = Client(args.app_id, args.directory / "private.key")
    if args.command == "validate":
        validate_app(client)
    else:
        authorization(client, args.directory)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Access check stopped: {error}", file=sys.stderr)
        sys.exit(1)
