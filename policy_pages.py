#!/usr/bin/env python3
"""Serve only the personal app's policy documents on the local computer."""
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
from local_tls import server_context
from settings import POLICY_PORT, TLS_DIR

HOST = "127.0.0.1"
PORT = POLICY_PORT
BASE = f"https://localhost:{PORT}"
MARKER = b"wise-finances-personal-policy-pages-v1"
DOCUMENTS = {
    "/privacy": ("Privacy policy", [
        "Wise finances is a private application operated by its sole user to read their own Wise accounts through Enable Banking.",
        "With the account owner's consent, the access test reads available account labels, currencies, balances and a first page of transactions from a 30-day window. It stores a sample of up to five transactions per account. The optional ChatGPT connection can fetch current balances and transaction pages for a requested date range. Jar availability depends on the bank connection.",
        "The application stores its private key, session details and sample report on the owner's computer, in the quick-wise-finance folder under the local state directory. These files remain until the owner deletes them. This local document server cannot read or serve those files.",
        "Wise handles bank sign-in. This application does not collect the owner's Wise password. Wise and Enable Banking process information under their own policies. When the owner connects and uses the ChatGPT plugin, requested account names, balances, transaction descriptions and counterparty names are sent through Cloudflare to ChatGPT. Structured bank account numbers and postal addresses are excluded from transaction results; free-text descriptions can still contain personal information. OpenAI and Cloudflare process that traffic under their own terms and privacy policies. The bridge uses a separate local connection password and OAuth tokens; it does not expose the bank private key or bank session identifier to ChatGPT.",
        "The owner can end data sharing through Wise or Enable Banking's data-sharing-consents page and delete local files. Revoking consent does not delete existing local copies. This app is for the owner's use only; its data protection contact is the email supplied in its Enable Banking registration.",
    ]),
    "/terms": ("Terms of use", [
        "Wise finances is a private account-information tool for its sole owner's personal use. It is not offered to other users or as a commercial service.",
        "The owner must authorize access only to accounts they own. The application requests account-information access and cannot initiate payments or transfers.",
        "The setup test stores a limited sample. The optional ChatGPT plugin reads data on request through a temporary tunnel while this computer and the bridge are running. Bank history limits, consent expiry and pagination can limit results. It does not continuously synchronize ChatGPT project files.",
        "Account data can be delayed, incomplete or unavailable. Verify balances and transactions in Wise before making a financial decision. This tool does not provide financial, tax or legal advice.",
        "Use of Wise and Enable Banking remains subject to their respective terms. The owner can stop using this application, revoke consent and remove its local files at any time.",
    ]),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body, content_type = MARKER, "text/plain; charset=utf-8"
        elif self.path in DOCUMENTS:
            title, paragraphs = DOCUMENTS[self.path]
            body = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    f"<title>Wise finances — {html.escape(title)}</title>"
                    "<style>body{font:18px/1.6 system-ui;max-width:760px;margin:40px auto;padding:0 24px;color:#20242a}"
                    "a{color:#1258a5}</style><main><p>wise finances · personal app</p>"
                    f"<h1>{html.escape(title)}</h1>"
                    + "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
                    + "<p><a href='/privacy'>Privacy policy</a> · <a href='/terms'>Terms of use</a></p></main></html>").encode()
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def running():
    try:
        import ssl
        context = ssl.create_default_context(cafile=str(TLS_DIR / "localhost.crt"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        with opener.open(f"https://{HOST}:{PORT}/health", timeout=1) as response:
            return response.read(128) == MARKER
    except (OSError, urllib.error.URLError):
        return False


def main():
    if sys.argv[1:] == ["serve"]:
        server = HTTPServer((HOST, PORT), Handler)
        server.socket = server_context().wrap_socket(server.socket, server_side=True)
        server.serve_forever()
    elif sys.argv[1:] == ["start"]:
        server_context()
        if not running():
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "serve"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            for _ in range(30):
                if running():
                    break
                if process.poll() is not None:
                    raise SystemExit(f"Policy pages could not start on port {PORT}.")
                time.sleep(0.1)
            else:
                process.terminate()
                raise SystemExit("Policy pages did not start. Check whether the configured port is available.")
        print(f"Privacy URL: {BASE}/privacy\nTerms URL: {BASE}/terms")
    else:
        raise SystemExit("Usage: policy_pages.py start|serve")


if __name__ == "__main__":
    main()
