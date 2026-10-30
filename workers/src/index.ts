import { OAuthResourceServer, insufficientScope } from "@cloudflare/workers-oauth-provider";
import { z } from "zod";
import { authorize, authorizationServer, SCOPE } from "./auth";
import { mcp } from "./mcp";

async function boundedRequest(request: Request) {
  if (!request.body) return request;
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > 32768) { await reader.cancel(); return null; }
    chunks.push(value);
  }
  const data = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length; }
  return new Request(request, { body: data });
}

export default {
  async fetch(incoming, env, ctx) {
    let request: Request = incoming;
    const url = new URL(request.url);
    if (url.origin !== env.PUBLIC_ORIGIN) return new Response("Incorrect server origin", { status: 421 });
    const origin = request.headers.get("Origin");
    if (origin && ![env.PUBLIC_ORIGIN, "https://chatgpt.com", "https://chat.openai.com"].includes(origin)) {
      return new Response("Unsupported origin", { status: 403 });
    }
    const responseHeaders = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };
    if (url.pathname === "/health" && request.method === "GET") {
      return Response.json({ service: "wise-finances", read_only: true }, { headers: responseHeaders });
    }
    const length = Number(request.headers.get("Content-Length") ?? 0);
    if (length > 32768) return new Response("Request too large", { status: 413 });
    if (["/authorize", "/oauth/register"].includes(url.pathname)) {
      const limit = await env.AUTH_LIMIT.limit({ key: `${url.pathname}:${request.headers.get("CF-Connecting-IP") ?? "local"}` });
      if (!limit.success) return new Response("Too many attempts. Try again in one minute.", { status: 429, headers: { "Retry-After": "60" } });
    }
    try {
      const bounded = await boundedRequest(request);
      if (!bounded) return new Response("Request too large", { status: 413 });
      request = bounded;
      const auth = authorizationServer(env);
      let response: Response;
      if (url.pathname === "/authorize") response = await authorize(request, env, auth);
      else if (url.pathname === "/mcp" || url.pathname.startsWith("/.well-known/oauth-protected-resource")) {
        const resource = new OAuthResourceServer<Env, unknown>({
          resourceMetadata: { resource: `${env.PUBLIC_ORIGIN}/mcp`, authorization_servers: [env.PUBLIC_ORIGIN] },
          requiredScopes: [SCOPE],
          validateToken: current => (audience, token) => auth.validateToken(audience, token, current),
          handler: { async fetch(req, current, context) {
            if (!context.auth.scope.includes(SCOPE)) return insufficientScope(context.auth, [SCOPE]);
            if (!z.object({ owner: z.literal(true) }).safeParse(context.props).success) return new Response("Forbidden", { status: 403 });
            return mcp(req, current);
          } },
        });
        response = await resource.fetch(request, env, ctx);
      } else if (url.pathname.startsWith("/.well-known/") || url.pathname.startsWith("/oauth/")) {
        response = await auth.fetch(request, env, ctx);
      } else response = new Response("Not found", { status: 404 });
      const secured = new Response(response.body, response);
      for (const [name, value] of Object.entries(responseHeaders)) secured.headers.set(name, value);
      return secured;
    } catch {
      console.error(JSON.stringify({ event: "request_failed", path: url.pathname }));
      return new Response("The Wise server could not complete this request. Try again.", { status: 503, headers: responseHeaders });
    }
  },
} satisfies ExportedHandler<Env>;
