"""Personal OAuth provider. The MCP SDK validates client, redirect, and PKCE."""
import html
import json
import logging
import secrets
import time
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, RegistrationError, TokenError, AuthorizeError
from mcp.shared.auth import OAuthToken, OAuthClientInformationFull
from starlette.responses import HTMLResponse, RedirectResponse
from access_check import private_write

SCOPE = "wise:read"
logger = logging.getLogger("wise.oauth")


class PersonalOAuth:
    def __init__(self, base, password, clients_path=None):
        self.base, self.password = base, password
        self.clients_path = clients_path
        self.clients, self.pending, self.codes, self.tokens = {}, {}, {}, {}
        self.registration_expiry = {}
        if clients_path and clients_path.exists():
            saved = json.loads(clients_path.read_text())
            if saved.get("issuer") == base:
                self.clients = {key: OAuthClientInformationFull.model_validate(value) for key, value in saved.get("clients", {}).items()}
                self.registration_expiry = saved.get("registration_expiry", {})
        self.refreshes, self.pairs = {}, {}
        self.tokens_path = clients_path.with_name("bridge-tokens.json") if clients_path else None
        if self.tokens_path and self.tokens_path.exists():
            saved = json.loads(self.tokens_path.read_text())
            if saved.get("issuer") == base:
                now = time.time()
                self.tokens = {key: AccessToken.model_validate(value) for key, value in saved.get("tokens", {}).items() if value.get("expires_at", 0) > now}
                self.refreshes = {key: RefreshToken.model_validate(value) for key, value in saved.get("refreshes", {}).items() if value.get("expires_at", 0) > now}
                self.pairs = {key: value for key, value in saved.get("pairs", {}).items() if key in self.tokens or key in self.refreshes}
        self.failed_attempts = []

    def save_tokens(self):
        if self.tokens_path:
            private_write(self.tokens_path, json.dumps({"issuer": self.base,
                "tokens": {key: value.model_dump(mode="json") for key, value in self.tokens.items()},
                "refreshes": {key: value.model_dump(mode="json") for key, value in self.refreshes.items()},
                "pairs": self.pairs}))

    def save_clients(self):
        if self.clients_path:
            private_write(self.clients_path, json.dumps({"issuer": self.base,
                "registration_expiry": self.registration_expiry,
                "clients": {key: value.model_dump(mode="json") for key, value in self.clients.items()}}))

    def prune_registrations(self):
        for key, expiry in list(self.registration_expiry.items()):
            if expiry <= time.time():
                self.clients.pop(key, None)
                self.registration_expiry.pop(key, None)

    async def get_client(self, client_id):
        self.prune_registrations()
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.prune_registrations()
        if len(self.clients) >= 30 and self.registration_expiry:
            oldest = min(self.registration_expiry, key=self.registration_expiry.get)
            self.registration_expiry.pop(oldest)
            self.clients.pop(oldest, None)
        if len(self.clients) >= 30:
            raise RegistrationError("invalid_client_metadata", "Registration limit reached.")
        for uri in client_info.redirect_uris or []:
            parsed = urlsplit(str(uri))
            if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "chat.openai.com"} or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
                raise RegistrationError("invalid_redirect_uri", "Only official ChatGPT HTTPS callbacks are allowed.")
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "A ChatGPT callback is required.")
        self.clients[client_info.client_id] = client_info
        self.registration_expiry[client_info.client_id] = time.time() + 600
        self.save_clients()

    async def authorize(self, client, params):
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        if sum(not item.get("approved", False) for item in self.pending.values()) >= 20:
            raise AuthorizeError("temporarily_unavailable", "Too many pending requests.")
        if params.resource and params.resource.rstrip("/") != self.base + "/mcp":
            raise AuthorizeError("invalid_request", "Incorrect MCP resource.")
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = {"client": client, "params": params, "expires": now + 600, "csrf": secrets.token_urlsafe(32)}
        logger.warning("Wise approval: created")
        return self.base + "/owner-approval?" + urlencode({"ticket": ticket})

    async def consent(self, request):
        if request.method != "GET" and request.headers.get("origin") != self.base:
            logger.warning("Wise approval: rejected origin")
            return self.page("Incorrect form origin.", 403)
        form = request.query_params if request.method == "GET" else await request.form()
        ticket = str(form.get("ticket", ""))
        pending = self.pending.get(ticket)
        if not ticket:
            logger.warning("Wise approval: missing form ticket")
            return self.page("The approval form is incomplete. Start Connect again from ChatGPT.", 400)
        if not pending:
            logger.warning("Wise approval: request no longer available")
            return self.page("This approval is no longer available. Start Connect again from ChatGPT.", 400)
        if pending["expires"] < time.time():
            logger.warning("Wise approval: timed out")
            return self.page("This approval timed out after 10 minutes. Start Connect again from ChatGPT.", 400)
        if pending.get("approved"):
            logger.warning("Wise approval: repeated form submission")
            return self.page("This form was already submitted. Return to ChatGPT to check the connection.", 409)
        if request.method == "GET":
            logger.warning("Wise approval: form displayed")
            client = pending["client"]
            body = ("<h1>Connect Wise finances</h1><p>Allow ChatGPT to read your Wise account names, balances, and transactions. No payments or transfers.</p>"
                    "<p>Bank data will be sent through Cloudflare to ChatGPT when you use these tools. Your computer must stay on.</p>"
                    f"<p>Client: {html.escape(client.client_name or 'ChatGPT')}<br>Return address: {html.escape(str(pending['params'].redirect_uri))}</p>"
                    "<p>Paste the connection password copied by the local setup. This is not your Wise password.</p>"
                    "<form method='post' action='/owner-approval'>"
                    f"<input type='hidden' name='ticket' value='{html.escape(ticket)}'>"
                    f"<input type='hidden' name='csrf' value='{pending['csrf']}'>"
                    "<label>Connection password <input type='password' name='password' required autocomplete='off'></label>"
                    "<p><button>Allow read access</button></p></form>")
            response = self.page(body)
            response.set_cookie("wise_consent", pending["csrf"], secure=True, httponly=True, samesite="lax", max_age=600, path="/owner-approval")
            return response
        csrf = str(form.get("csrf", ""))
        if not secrets.compare_digest(csrf, pending["csrf"]) or not secrets.compare_digest(request.cookies.get("wise_consent", ""), csrf):
            logger.warning("Wise approval: rejected form cookie or CSRF")
            return self.page("Invalid approval form. Reopen the connection.", 403)
        now = time.time()
        self.failed_attempts = [t for t in self.failed_attempts if t > now - 60]
        if len(self.failed_attempts) >= 5:
            return self.page("Too many attempts. Wait one minute and reopen the connection.", 429)
        if not secrets.compare_digest(str(form.get("password", "")), self.password):
            logger.warning("Wise approval: incorrect password")
            self.failed_attempts.append(now)
            return self.page("Incorrect connection password. Go back and try again.", 403)
        if await self.get_client(pending["client"].client_id) is None:
            return self.page("Registration expired. Reconnect from ChatGPT.", 400)
        self.registration_expiry.pop(pending["client"].client_id, None)
        self.save_clients()
        pending["approved"] = True
        logger.warning("Wise approval: approved; returning to ChatGPT")
        params, client = pending["params"], pending["client"]
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(code=code, scopes=params.scopes or [SCOPE], expires_at=now+120,
            client_id=client.client_id, code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.base + "/mcp")
        query = {"code": code}
        if params.state is not None:
            query["state"] = params.state
        redirect = str(params.redirect_uri)
        response = RedirectResponse(redirect + ("&" if "?" in redirect else "?") + urlencode(query), status_code=303)
        response.delete_cookie("wise_consent", path="/owner-approval")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @staticmethod
    def page(body, status=200):
        return HTMLResponse("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Wise finances authorization</title><style>body{font:18px/1.5 system-ui;max-width:680px;margin:40px auto;padding:20px}input{font:inherit;max-width:100%}button{padding:12px;font:inherit}</style>" + body + "</html>",
            status_code=status, headers={"Cache-Control":"no-store", "Referrer-Policy":"same-origin", "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com https://chat.openai.com; frame-ancestors 'none'"})

    async def load_authorization_code(self, client, code):
        item = self.codes.get(code)
        return item if item and item.client_id == client.client_id and item.expires_at > time.time() else None

    async def exchange_authorization_code(self, client, authorization_code):
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Code was already used.")
        logger.warning("Wise approval: authorization code exchanged")
        return self.issue(client.client_id, authorization_code.scopes, int(time.time()) + 180*86400)

    def issue(self, client_id, scopes, refresh_expiry):
        token, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        self.tokens[token] = AccessToken(token=token, client_id=client_id, scopes=scopes,
            expires_at=int(time.time())+3600, resource=self.base+"/mcp")
        self.refreshes[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes,
            expires_at=refresh_expiry)
        self.pairs[token], self.pairs[refresh] = refresh, token
        self.save_tokens()
        return OAuthToken(access_token=token, refresh_token=refresh, token_type="Bearer", expires_in=3600, scope=" ".join(scopes))

    async def load_access_token(self, token):
        item = self.tokens.get(token)
        return item if item and item.expires_at > time.time() else None

    async def load_refresh_token(self, client, refresh_token):
        item = self.refreshes.get(refresh_token)
        return item if item and item.client_id == client.client_id and item.expires_at > time.time() else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if refresh_token.token not in self.refreshes:
            raise TokenError("invalid_grant", "Refresh token was already used.")
        await self.revoke_token(refresh_token)
        return self.issue(client.client_id, scopes, refresh_token.expires_at)

    async def revoke_token(self, token):
        self.tokens.pop(token.token, None)
        self.refreshes.pop(token.token, None)
        paired = self.pairs.pop(token.token, None)
        if paired:
            self.tokens.pop(paired, None)
            self.refreshes.pop(paired, None)
            self.pairs.pop(paired, None)
        self.save_tokens()
