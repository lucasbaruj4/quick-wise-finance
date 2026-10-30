# Persistent Cloudflare server

This optional server replaces the local bridge and temporary tunnel. Its `workers.dev` address stays the same across deployments. Your computer can be off while ChatGPT reads Wise.

It exposes the same three read-only tools. Cloudflare stores your bank application key and approved session as Worker secrets. Cloudflare KV stores OAuth clients and grants. Financial responses are fetched from Enable Banking when requested; the server does not save balances or transaction history.

## First deployment

Use Linux or WSL, Python 3.11+, OpenSSL, and Node.js 24+. Complete the repository's bank setup first. Keep its private state directory outside Git. Allow about 10–15 minutes for this deployment and ChatGPT connection.

1. Install the pinned dependencies and sign into your free Cloudflare account:

   ```bash
   cd workers
   npm ci
   npx wrangler login --device --browser=false
   npx wrangler whoami
   ```

   Complete device approval in your browser. If Cloudflare asks, create a free Workers subdomain. A custom domain and a paid Workers plan are not required. Do not enable billing for this setup.

2. Check the source, then deploy. Replace `YOUR_ACCOUNT_ID` with the account ID from `whoami`:

   ```bash
   npm run check
   npm test
   python3 deploy.py --account-id YOUR_ACCOUNT_ID \
     --config "$HOME/.local/state/quick-wise-finance-worker/wrangler.json"
   ```

   The script saves the account, fixed origin, and OAuth KV namespace in that private configuration file. It first publishes with a placeholder origin, then saves the assigned URL and republishes. Until that completes and secrets are imported, bank tools are unavailable. Keep the same configuration for all updates; deleting the KV namespace loses ChatGPT authorizations.

3. Import your existing approved bank session. Adjust the first path if your installation uses different private state:

   ```bash
   python3 import_session.py "$HOME/.local/state/quick-wise-finance" \
     --config "$HOME/.local/state/quick-wise-finance-worker/wrangler.json"
   ```

   This uploads only the application ID, PKCS8 private key, minimal approved account session, and connection-password hash. The temporary upload file is private and deleted afterward. It preserves your password and bank consent. Never commit the deployment configuration, private state, secrets, or generated files.

4. In ChatGPT, add a private custom MCP app named `Wise Finances` using the printed fixed `/mcp` URL. Select OAuth and leave static client credentials empty. Complete the owner approval with your existing connection password. If the old app's server URL cannot be edited, replace that app once with the new address. The bank login is separate; a valid bank session does not need renewed approval for this change.

5. In a new chat, select the app and ask it to call `list_accounts` and `get_balances`. Check that the account and Jar count matches Wise. Test a bounded transaction date range. Once these work through the new server, stop the old local connection:

   ```bash
   cd ..
   ./setup.sh stop
   ```

## Updates and renewal

Run `deploy.py` again with the **same** private `--config` path. It reuses the Worker and KV namespace. Deployments retain Worker secrets. Do not deploy the example `wrangler.jsonc` directly to production; its origin is a placeholder.

ChatGPT access tokens last one hour. Refresh tokens rotate, and successful refresh renews the grant's 180-day inactivity window. Reconnect after prolonged inactivity, revocation, password compromise, or deletion of storage. KV is eventually consistent, so revocation and single-use checks are not globally instantaneous.

Bank consent has its own expiry. OAuth refresh cannot extend it. When `list_accounts` reports expiry, run `./setup.sh renew` in the local installation, complete bank approval, and run `import_session.py` again with the same deployment configuration. This replaces bank consent without changing the server URL or OAuth storage. A temporary tunnel started by the local renewal command can be stopped after import.

If a key or connection password is compromised, rotate it and revoke existing OAuth access. To force all ChatGPT connections to sign in again, create a new OAuth namespace, bind it in the private configuration, and redeploy. Do not delete unrelated Cloudflare resources.

## Limits and verification

Cloudflare's free plan has CPU, request, storage, and write limits. This is a small personal server, not an uptime guarantee. Public registration and owner approval are limited per IP; an abusive client can still consume free quotas. Do not add payment tools or expose the owner's bank secrets to clients.

Logs contain tool names, success/failure, and sanitized error locations. Request invocation logs are disabled to avoid saving OAuth query strings. Do not add financial values, passwords, tokens, authorization headers, bank identifiers, or raw API responses to logs.

`npm test` builds the Worker and runs its actual runtime with fabricated keys and data. It checks browser-bound consent, origin checks, PKCE, resource binding, persistence across restart, refresh, revocation, and signed bank reads. Boundary checks also verify pagination, consent expiry, and removal of account numbers and addresses. These checks do not contact a real bank or Cloudflare account.

API references: [Cloudflare OAuth provider](https://github.com/cloudflare/workers-oauth-provider), [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [KV limits](https://developers.cloudflare.com/kv/platform/limits/), and [Enable Banking API](https://enablebanking.com/docs/api/reference/).
