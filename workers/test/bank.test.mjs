import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, verify } from "node:crypto";
import { getBalances, getTransactions, listAccounts, transactionsInput } from "../src/bank.ts";

test("bank boundary: GET-only signed requests, private fields removed, paging, expiry", async () => {
  const { publicKey, privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const env = {
    ENABLE_BANKING_APP_ID: "00000000-0000-4000-8000-000000000001",
    BANK_PRIVATE_KEY: privateKey.export({ type: "pkcs8", format: "pem" }),
    BANK_SESSION: JSON.stringify({ accounts: [{ uid: "00000000-0000-4000-8000-000000000002", name: "Example account", currency: "EUR" }],
      access: { valid_until: "2099-01-01T00:00:00Z" } }),
  };
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (target, init) => {
    calls++;
    const url = new URL(target);
    assert.equal(url.origin, "https://api.enablebanking.com");
    assert.equal(init.method, "GET");
    assert.equal(init.redirect, "manual");
    const token = init.headers.Authorization.slice(7);
    const [header, body, signature] = token.split(".");
    assert.ok(verify("RSA-SHA256", Buffer.from(`${header}.${body}`), publicKey, Buffer.from(signature, "base64url")));
    assert.equal(JSON.parse(Buffer.from(body, "base64url")).aud, "api.enablebanking.com");
    const data = url.pathname.endsWith("/balances")
      ? { balances: [{ balance_amount: { amount: "12.34", currency: "EUR" }, balance_type: "ITAV", iban: "PRIVATE_ACCOUNT" }] }
      : { transactions: [{ transaction_amount: { amount: "3.00", currency: "EUR" }, credit_debit_indicator: "DBIT",
        balance_after_transaction: { amount: "9.34", currency: "EUR" },
        creditor: { name: "Example shop", account: "PRIVATE_ACCOUNT", postal_address: "PRIVATE_ADDRESS" },
        creditor_account: "PRIVATE_ACCOUNT", remittance_information: ["Untrusted text"] }], continuation_key: "next/page?x=1" };
    if (url.pathname.endsWith("/transactions")) assert.equal(url.searchParams.get("continuation_key"), "cursor?&=test");
    return Response.json(data);
  };
  try {
    const listed = listAccounts(env);
    assert.ok(!JSON.stringify(listed).includes("00000000"));
    assert.equal((await getBalances(env)).accounts.length, 1);
    const input = transactionsInput.parse({ account: "account_1", date_from: "2026-01-01", date_to: "2026-01-02", cursor: "cursor?&=test" });
    const transactions = await getTransactions(env, input);
    assert.equal(transactions.transactions[0].creditor_name, "Example shop");
    assert.equal(transactions.next_cursor, "next/page?x=1");
    assert.equal(transactions.complete_for_requested_range, false);
    assert.ok(!JSON.stringify(transactions).includes("PRIVATE_"));
    assert.throws(() => transactionsInput.parse({ ...input, account: "https://attacker.example" }));
    await assert.rejects(getTransactions(env, { ...input, date_to: "2028-01-01" }), /at most 366/);
    await assert.rejects(getTransactions(env, { ...input, account: "account_99" }), /account alias/);
    const expired = { ...env, BANK_SESSION: env.BANK_SESSION.replace("2099-01-01", "2000-01-01") };
    await assert.rejects(getBalances(expired), /consent expired/);
    assert.equal(calls, 2);
  } finally { globalThis.fetch = original; }
});
