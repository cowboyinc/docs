// Browser contract check with an explicitly fake wallet/API. This does not
// exercise wallet signatures, gateway admission, CBFS, or inference.
// Run with Playwright on NODE_PATH; uses an isolated headless Chrome profile.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {chromium} = require("playwright");
const assets = path.join(__dirname, "../offchain/page");
const server = http.createServer((req, res) => {
  const files = {"/manage": ["index.html", "text/html"], "/manage/assets/manage.js": ["manage.js", "text/javascript"], "/manage/assets/manage.css": ["manage.css", "text/css"]};
  for (const name of ["wallet.js", "vendor/sha3.js", "vendor/_u64.js", "vendor/utils.js", "vendor/crypto.js"]) files["/manage/assets/" + name] = [name, "text/javascript"];
  const file = files[new URL(req.url, "http://localhost").pathname];
  if (!file) { res.writeHead(404).end(); return; }
  res.writeHead(200, {"content-type": file[1], "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"});
  res.end(fs.readFileSync(path.join(assets, file[0])));
});
(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({channel: "chrome", headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1200, height: 1100}, permissions: ["clipboard-read", "clipboard-write"]});
    const page = await context.newPage();
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin + "/manage?slot=cloud");
    await page.getByRole("button", {name: "Connect wallet"}).click();
    await page.getByRole("status").filter({hasText: "Wallet connection is unavailable"}).waitFor();
    assert.equal(await page.locator("#workspace").isVisible(), false);
    await context.addInitScript(() => {
      const keys = [];
      window.testCalls = [];
      window.testLoseCreateResponse = false;
      window.cowboyWallet = {
        connect: async () => {},
        signedFetch: async (target, options) => {
          window.testCalls.push({target, method: options.method});
          const path = new URL(target, location.origin).pathname;
          const reply = (data, status = 200) => new Response(JSON.stringify(data), {status});
          if (path.endsWith("/catalog")) return reply({models: [{id: "cowboy-v1"}], public_base_url: "https://api.cowboy.example", hire_status: "active", direct_access_available: true});
          if (path.endsWith("/read-grant")) return reply(options.method === "PUT" ? {owner: "ab".repeat(20), volume: "hire-7", access: "read_only"} : {removed: true});
          if (path.endsWith("/keys") && options.method === "POST") {
            const key = {key_id: String(keys.length + 1).padStart(32, "0"), name: JSON.parse(options.body).name, prefix: "test0001", created_at: 1789080000, revoked_at: null};
            keys.push(key);
            if (window.testLoseCreateResponse) throw new Error("test lost response");
            return reply({key, secret: "cwir1.TEST_ONLY_NOT_A_REAL_KEY"}, 201);
          }
          if (path.endsWith("/keys")) return reply({keys, hire_status: "active"});
          if (options.method === "DELETE") { const key = keys.find(k => path.endsWith(k.key_id)); key.revoked_at = 1789080100; return reply({key}); }
          if (path.endsWith("/usage")) return reply({totals: {requests: 2, input_tokens: "123", output_tokens: "32", usage_cost_nano_usd: "12000000", unknown_usage_requests: 1, unpriced_requests: 1, partial: true, cost_partial: true}, breakdown: [{key_id: "test0001", model: "cowboy-v1", requests: 2, input_tokens: "123", output_tokens: "32", usage_cost_nano_usd: "12000000", cost_partial: true}], last_committed_at: 1789080000});
          return reply({error: "not_found"}, 404);
        }
      };
    });
    await page.reload();
    assert.equal(await page.evaluate(() => window.testCalls.length), 0);
    await page.getByRole("button", {name: "Connect wallet"}).click();
    await page.locator("#workspace").waitFor({state: "visible"});
    assert.match(await page.locator("#usage-state").innerText(), /Partial: 1 requests/);
    assert.equal(await page.locator("#cost").innerText(), "$0.012");
    await page.getByRole("button", {name: "Create key", exact: true}).click();
    await page.getByLabel("Key name").fill("<img src=x onerror=alert(1)>");
    await page.getByRole("button", {name: "Create", exact: true}).click();
    await page.locator("#secret-panel").waitFor({state: "visible"});
    assert.equal(await page.locator("#keys img").count(), 0);
    assert.match(await page.locator("#keys").innerText(), /<img src=x/);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.equal(await page.evaluate(() => window.testCalls.filter(c => c.method === "POST").length), 1);
    assert.doesNotMatch(await page.locator("#quickstart").innerText(), /TEST_ONLY/);
    await page.getByRole("button", {name: "Done", exact: true}).click();
    assert.equal(await page.locator("#secret").innerText(), "");
    page.once("dialog", dialog => dialog.accept());
    await page.getByRole("button", {name: "Revoke", exact: true}).click();
    await page.locator("#keys").getByText("Revoked", {exact: true}).waitFor();
    await page.evaluate(() => { window.testLoseCreateResponse = true; });
    await page.getByRole("button", {name: "Create key", exact: true}).click();
    await page.getByLabel("Key name").fill("Lost response");
    await page.getByRole("button", {name: "Create", exact: true}).click();
    await page.locator("#status").filter({hasText: "Key creation was not confirmed"}).waitFor();
    await page.locator("#keys").getByText("Lost response", {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.filter(c => c.method === "POST").length), 2);
    assert.equal(await page.locator("#secret-panel").isVisible(), false);
    await page.getByRole("button", {name: "Cancel", exact: true}).click();
    await page.getByLabel("Ed25519 signing public key").fill("0x" + "11".repeat(32));
    await page.getByLabel("X25519 encryption public key").fill("22".repeat(32));
    await page.getByRole("button", {name: "Enable read access"}).click();
    await page.locator("#grant-state").filter({hasText: "Read access enabled"}).waitFor();
    assert.match(await page.locator("#grant-connection").innerText(), /Volume: hire-7/);
    assert.equal(await page.locator("#grant-signing").inputValue(), "");
    page.once("dialog", dialog => dialog.accept());
    await page.getByRole("button", {name: "Remove read access"}).click();
    await page.locator("#grant-state").filter({hasText: "Read grant removed"}).waitFor();
    assert.equal(await page.locator("#grant-connection").isVisible(), false);
    await page.screenshot({path: "/tmp/cowboy-inference-management-desktop.png", fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: "/tmp/cowboy-inference-management-mobile.png", fullPage: true});
    assert.deepEqual(errors, []);
    console.log("PASS: public shell, wallet-unavailable, fake-wallet lifecycle, partial usage, XSS text, lost-response no-retry, no persistence, desktop/mobile layout");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
