import { AuthorizationError, OAuthAuthorizationServer, type ConsentDescription } from "@cloudflare/workers-oauth-provider";

export const SCOPE = "wise:read";
const scopes = [SCOPE, "offline_access"];
const escape = (value: string) => value.replace(/[&<>"']/g, char => `&#${char.charCodeAt(0)};`);

function chatgptRedirect(value: unknown) {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && ["chatgpt.com", "chat.openai.com"].includes(url.hostname)
      && !url.port && !url.username && !url.password && !url.hash;
  } catch { return false; }
}

export function authorizationServer(env: Env) {
  return new OAuthAuthorizationServer<Env>({
    issuer: env.PUBLIC_ORIGIN, resources: [`${env.PUBLIC_ORIGIN}/mcp`], defaultResource: `${env.PUBLIC_ORIGIN}/mcp`,
    clientRegistrationEndpoint: "/oauth/register", scopesSupported: scopes,
    accessTokenTTL: 3600, refreshTokenTTL: 180 * 86400, refreshTokenIdleTTL: 180 * 86400,
    clientRegistrationTTL: 180 * 86400,
    clientRegistrationCallback: ({ clientMetadata }) => {
      const redirects = clientMetadata.redirect_uris;
      if (!Array.isArray(redirects) || !redirects.length || !redirects.every(chatgptRedirect)) {
        return { code: "invalid_redirect_uri", description: "Only official ChatGPT HTTPS callbacks are allowed." };
      }
      return undefined;
    },
    onError: error => { console.warn(JSON.stringify({ event: "oauth_error", code: error.code, status: error.status })); },
  });
}

function page(details: ConsentDescription, handle: string) {
  return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Wise finances authorization</title><style>body{font:18px system-ui;max-width:600px;margin:8vh auto;padding:24px;color:#182d22;background:#f6f9f5}input,button{box-sizing:border-box;font:inherit;padding:14px;width:100%;margin:12px 0}button{background:#163d26;color:white;border:0;border-radius:8px}p{line-height:1.5}</style>
<h1>Connect Wise Finances</h1><p><strong>${escape(details.clientName)}</strong> requests read access to account names, balances, and transactions. This connection cannot make payments or transfers.</p>
<p>Access will return to <strong>${escape(details.redirectHost)}</strong>. The client name is self-reported.</p>
<p>Permissions: ${details.scope.map(escape).join(", ")}.</p><form method="post" action="/authorize">
<input type="hidden" name="handle" value="${escape(handle)}"><label for="password">Connection password</label>
<input id="password" name="password" type="password" autocomplete="off" required maxlength="512">
<button name="decision" value="approve">Allow read access</button></form></html>`;
}

async function passwordMatches(password: unknown, expected: string) {
  if (typeof password !== "string" || password.length > 512 || !/^[0-9a-f]{64}$/.test(expected)) return false;
  const actual = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(password));
  const wanted = Uint8Array.from(expected.match(/../g) ?? [], pair => parseInt(pair, 16));
  return crypto.subtle.timingSafeEqual(actual, wanted);
}

export async function authorize(request: Request, env: Env, server: ReturnType<typeof authorizationServer>) {
  const oauth = server.getOAuthApi(env);
  try {
    if (request.method === "GET") {
      const auth = await oauth.parseAuthRequest(request);
      if (!chatgptRedirect(auth.redirectUri) || !auth.codeChallenge || auth.codeChallengeMethod !== "S256"
        || auth.scope.some(scope => !scopes.includes(scope))) {
        return new Response("Use ChatGPT with PKCE S256 and read-only scopes.", { status: 400 });
      }
      auth.scope = [...new Set([...auth.scope, SCOPE, "offline_access"])];
      const details = await oauth.describeConsent(auth);
      const consent = await oauth.beginConsent(auth);
      consent.headers.set("Content-Type", "text/html; charset=utf-8");
      consent.headers.set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com https://chat.openai.com; frame-ancestors 'none'; base-uri 'none'");
      consent.headers.set("Referrer-Policy", "same-origin");
      return new Response(page(details, consent.handle), { headers: consent.headers });
    }
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
    if (request.headers.get("Origin") !== env.PUBLIC_ORIGIN) return new Response("Incorrect form origin", { status: 403 });
    const form = await request.formData();
    if (!(await passwordMatches(form.get("password"), env.OWNER_PASSWORD_SHA256))) {
      return new Response("Incorrect connection password. Start Connect again in ChatGPT.", { status: 403 });
    }
    const approved = await oauth.approveConsent(request, String(form.get("handle") ?? ""), { scope: scopes });
    if (!chatgptRedirect(approved.request.redirectUri)) return new Response("Unsupported callback", { status: 400 });
    const { redirectTo } = await oauth.completeAuthorization({ request: approved.request, userId: "owner",
      metadata: {}, scope: scopes, props: { owner: true },
    });
    approved.headers.set("Location", redirectTo);
    approved.headers.set("Referrer-Policy", "no-referrer");
    console.info(JSON.stringify({ event: "owner_approved" }));
    return new Response(null, { status: 302, headers: approved.headers });
  } catch (error) {
    if (error instanceof AuthorizationError) return new Response("Approval expired or invalid. Start Connect again in ChatGPT.", { status: 400 });
    throw error;
  }
}
