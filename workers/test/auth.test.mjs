import { after, test } from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes, generateKeyPairSync } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const origin = "https://wise.example.test";
const directory = await mkdtemp(join(tmpdir(), "wise-auth-test-"));
const password = randomBytes(32).toString("hex");
const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const options = convertV4MiniflareOptions({
  modules: true, scriptPath: resolve("dist/index.js"),
  compatibilityDate: "2026-10-30", compatibilityFlags: ["nodejs_compat"],
  kvNamespaces: ["OAUTH_KV"], resourcePersistencePath: directory,
  ratelimits: { AUTH_LIMIT: { namespace_id: "1", simple: { limit: 1000, period: 60 } } },
  outboundService: async request => {
    assert.equal(new URL(request.url).hostname, "api.enablebanking.com");
    assert.equal(request.method, "GET");
    return Response.json({ balances: [{ balance_amount: { amount: "12.34", currency: "EUR" }, balance_type: "ITAV",
      last_change_date_time: null, reference_date: "2026-01-01" }] });
  },
  bindings: {
    PUBLIC_ORIGIN: origin, OWNER_PASSWORD_SHA256: createHash("sha256").update(password).digest("hex"),
    BANK_SESSION: JSON.stringify({ accounts: [{ uid: "00000000-0000-4000-8000-000000000001", name: "Example Jar", currency: "EUR" }],
      access: { valid_until: "2099-01-01T00:00:00Z" } }),
    ENABLE_BANKING_APP_ID: "00000000-0000-4000-8000-000000000002", BANK_PRIVATE_KEY: privateKey.export({ type: "pkcs8", format: "pem" }),
  },
});
let worker = new Miniflare(options);
after(async () => { await worker.dispose(); await rm(directory, { recursive: true, force: true }); });
const request = (path, init) => worker.dispatchFetch(`${origin}${path}`, { redirect: "manual", ...init });
const form = (values, headers = {}) => ({ method: "POST", body: new URLSearchParams(values), headers });
const redirect = "https://chatgpt.com/connector_platform/oauth/callback";

test("real Worker OAuth: consent, PKCE, persistence, refresh, and revocation", async () => {
  assert.equal((await request("/mcp")).status, 401);
  assert.equal((await worker.dispatchFetch("https://attacker.example/mcp")).status, 421);
  assert.equal((await request("/health", { headers: { Origin: "https://attacker.example" } })).status, 403);
  assert.equal((await request("/oauth/register", { method: "POST", body: "x".repeat(33000) })).status, 413);
  const metadata = await (await request("/.well-known/oauth-authorization-server")).json();
  assert.equal(metadata.issuer, origin);
  const register = async redirect_uri => request("/oauth/register", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: "<script>bad</script>", redirect_uris: [redirect_uri],
      token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"] }),
  });
  assert.equal((await register("https://chatgpt.com.attacker.example/callback")).status, 400);
  const registered = await register(redirect);
  assert.equal(registered.status, 201);
  const client = await registered.json();
  const verifier = randomBytes(32).toString("base64url");
  const challenge = createHash("sha256").update(verifier).digest("base64url");
  const params = new URLSearchParams({ response_type: "code", client_id: client.client_id, redirect_uri: redirect,
    scope: "wise:read offline_access", state: "browser-test", resource: `${origin}/mcp`,
    code_challenge: challenge, code_challenge_method: "S256" });
  const consent = await request(`/authorize?${params}`);
  assert.equal(consent.status, 200);
  assert.equal(consent.headers.get("referrer-policy"), "same-origin");
  assert.ok(consent.headers.get("content-security-policy").includes("form-action 'self' https://chatgpt.com https://chat.openai.com;"));
  const html = await consent.text();
  assert.ok(!html.includes("<script>bad</script>"));
  const handle = html.match(/name="handle" value="([^"]+)"/)?.[1];
  assert.ok(handle);
  const cookie = consent.headers.get("set-cookie").split(";")[0];
  const approval = { handle, password, decision: "approve" };
  assert.equal((await request("/authorize", form(approval, { Origin: "null", Cookie: cookie }))).status, 403);
  assert.equal((await request("/authorize", form(approval, { Origin: "https://attacker.example", Cookie: cookie }))).status, 403);
  assert.equal((await request("/authorize", form(approval, { Origin: origin }))).status, 400);
  assert.equal((await request("/authorize", form({ ...approval, handle: "wrong" }, { Origin: origin, Cookie: cookie }))).status, 400);
  assert.equal((await request("/authorize", form({ ...approval, password: "wrong" }, { Origin: origin, Cookie: cookie }))).status, 403);
  const approved = await request("/authorize", form(approval, { Origin: origin, Cookie: cookie }));
  assert.equal(approved.status, 302);
  const callback = new URL(approved.headers.get("location"));
  assert.equal(callback.origin, "https://chatgpt.com");
  assert.equal(callback.searchParams.get("state"), "browser-test");
  assert.equal((await request("/authorize", form(approval, { Origin: origin, Cookie: cookie }))).status, 400);
  const exchange = { grant_type: "authorization_code", client_id: client.client_id,
    code: callback.searchParams.get("code"), redirect_uri: redirect, code_verifier: verifier, resource: `${origin}/mcp` };
  assert.equal((await request("/oauth/token", form({ ...exchange, code_verifier: "wrong".repeat(12) }))).status, 400);
  const tokensResponse = await request("/oauth/token", form(exchange));
  assert.equal(tokensResponse.status, 200);
  const tokens = await tokensResponse.json();
  assert.ok(tokens.refresh_token);
  const call = async (token, method, params = {}) => {
    const response = await request("/mcp", { method: "POST", headers: {
      Authorization: `Bearer ${token}`, "Content-Type": "application/json", Accept: "application/json, text/event-stream",
    }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
    if (response.status !== 200) assert.fail(`MCP ${response.status}: ${await response.text()}`);
    return response.json();
  };
  const tools = await call(tokens.access_token, "tools/list");
  assert.deepEqual(tools.result.tools.map(t => t.name).sort(), ["get_balances", "get_transactions", "list_accounts"]);
  await worker.dispose();
  worker = new Miniflare(options);
  const accounts = await call(tokens.access_token, "tools/call", { name: "list_accounts", arguments: {} });
  assert.equal(JSON.parse(accounts.result.content[0].text).accounts[0].name, "Example Jar");
  const balances = await call(tokens.access_token, "tools/call", { name: "get_balances", arguments: {} });
  assert.ok(!balances.result.isError);
  assert.equal(JSON.parse(balances.result.content[0].text).accounts[0].balances[0].balance_amount.amount, "12.34");
  const refresh = { grant_type: "refresh_token", client_id: client.client_id, refresh_token: tokens.refresh_token, resource: `${origin}/mcp` };
  assert.equal((await request("/oauth/token", form({ ...refresh, resource: "https://attacker.example/mcp" }))).status, 400);
  const refreshed = await request("/oauth/token", form(refresh));
  assert.equal(refreshed.status, 200);
  const rotated = await refreshed.json();
  assert.ok(rotated.refresh_token && rotated.refresh_token !== tokens.refresh_token);
  await call(rotated.access_token, "tools/list");
  const revoked = await request(new URL(metadata.revocation_endpoint).pathname, form({ client_id: client.client_id, token: rotated.refresh_token, token_type_hint: "refresh_token" }));
  assert.equal(revoked.status, 200);
  assert.equal((await request("/oauth/token", form({ ...refresh, refresh_token: rotated.refresh_token }))).status, 400);
});
