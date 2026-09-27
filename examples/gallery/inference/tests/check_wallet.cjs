// Real browser codec and secp256k1 signatures, against a fake HTTP API.
// The wallet private key is the public test vector, never a user's wallet.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {execFileSync} = require("node:child_process");
const {chromium} = require("playwright");
const base = path.join(__dirname, "..");
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/account_proof.json")));
const config = {actor: fixture.request.actor_hex, chain_id: String(fixture.request.chain_id), network: fixture.request.network, handler: fixture.request.handler};
const captures = [];
const server = http.createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const pathname = new URL(req.url, "http://localhost").pathname;
  if (pathname === "/manage/config") { res.writeHead(200, {"content-type": "application/json"}); res.end(JSON.stringify(config)); return; }
  const files = ["manage.js", "wallet.js", "manage.css", "vendor/sha3.js", "vendor/_u64.js", "vendor/utils.js", "vendor/crypto.js"];
  const file = pathname === "/manage" ? "index.html" : files.find(name => pathname === "/manage/assets/" + name);
  if (file) {
    res.writeHead(200, {"content-type": file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html",
      "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"});
    res.end(fs.readFileSync(path.join(base, "offchain/page", file))); return;
  }
  captures.push({method: req.method, target: req.url, headers: req.headers, body_hex: Buffer.concat(chunks).toString("hex")});
  res.writeHead(200, {"content-type": "application/json"}); res.end("{}");
});
const python = (program, data) => execFileSync("uv", ["run", "--with", "coincurve", "--with", "pycryptodome", "python", "-c", program], {cwd: base, input: JSON.stringify(data), encoding: "utf8"}).trim();
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({channel: "chrome", headless: true});
  try {
    const context = await browser.newContext();
    await context.exposeFunction("testSignDigest", digest => JSON.parse(python(
      "import json,sys; from coincurve import PrivateKey; d=json.load(sys.stdin); s=PrivateKey(bytes.fromhex(d['key'])).sign_recoverable(bytes.fromhex(d['digest'][2:]),hasher=None); print(json.dumps(dict(r=s[:32].hex(),s=s[32:64].hex(),v=s[64])))",
      {key: fixture.private_key_hex, digest})));
    await context.addInitScript(() => {
      window.testRejectSign = false;
      window.testBadRecid = false;
      window.cowboy = {connect: async () => {}, signMessage: async digest => {
        if (window.testRejectSign) throw new Error("Rejected by wallet");
        const sig = await window.testSignDigest(digest);
        return {...sig, v: window.testBadRecid ? 27 : sig.v};
      }};
    });
    const page = await context.newPage();
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin + "/manage?slot=cloud");
    const golden = await page.evaluate(async fixture => {
      const wallet = await import("/manage/assets/wallet.js");
      const unhex = h => Uint8Array.from(h.match(/../g), b => parseInt(b, 16));
      const hex = b => Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
      const f = fixture.request;
      const context = {actor: f.actor_hex, chain_id: String(f.chain_id), network: f.network, handler: f.handler};
      const args = [context, f.verb, f.raw_target, new TextDecoder().decode(unhex(f.body_hex)), f.expiry, unhex(f.nonce_hex)];
      return {preimage: hex(await wallet.signingBytes(...args)), digest: hex(await wallet.signingDigest(...args))};
    }, fixture);
    assert.equal(golden.preimage, fixture.preimage_hex);
    assert.equal(golden.digest, fixture.digest_hex);
    await page.evaluate(async () => {
      const {createWalletClient} = await import("/manage/assets/wallet.js");
      window.testWallet = createWalletClient(); await window.testWallet.connect();
      await window.testWallet.signedFetch("/manage/api/keys?slot=cloud&encoded=a%2Fb", {method: "POST", body: '{"name":"café"}'});
      await window.testWallet.signedFetch("/manage/api/keys?slot=cloud", {method: "GET"});
    });
    assert.equal(captures.length, 2);
    assert.equal(captures[0].target, "/manage/api/keys?slot=cloud&encoded=a%2Fb");
    assert.equal(captures[0].body_hex, Buffer.from('{"name":"café"}').toString("hex"));
    assert.notEqual(captures[0].headers["x-cowboy-account-nonce"], captures[1].headers["x-cowboy-account-nonce"]);
    const result = JSON.parse(python("import json,sys,base64,time; from offchain import account_proof as ap; d=json.load(sys.stdin); out=[]\nfor r in d['requests']:\n h=r['headers']; dec=lambda x:base64.urlsafe_b64decode(x+'='*((-len(x))%4)); a=ap.verify(sig=dec(h['x-cowboy-account-sig']),chain_id=int(h['x-cowboy-account-chain-id']),network=h['x-cowboy-account-network'],actor=bytes.fromhex(d['config']['actor']),handler=d['config']['handler'],verb=r['method'],raw_target=r['target'],body=bytes.fromhex(r['body_hex']),expiry=int(h['x-cowboy-account-expiry']),nonce=dec(h['x-cowboy-account-nonce']),now=int(time.time())); out.append(a.address.hex())\nprint(json.dumps(out))", {requests: captures, config}));
    assert.deepEqual(result, [fixture.address_hex, fixture.address_hex]);
    for (const flag of ["testRejectSign", "testBadRecid"]) {
      await page.evaluate(flag => { window[flag] = true; }, flag);
      assert.equal(await page.evaluate(async () => { try { await window.testWallet.signedFetch("/manage/api/keys?slot=cloud", {method: "POST", body: "{}"}); return false; } catch { return true; } }), true);
      await page.evaluate(flag => { window[flag] = false; }, flag);
    }
    for (const target of ["https://example.com/steal", "//example.com/steal", "/manage/../api", "/manage#fragment"]) {
      assert.equal(await page.evaluate(async target => { try { await window.testWallet.signedFetch(target); return false; } catch { return true; } }, target), true);
    }
    assert.equal(captures.length, 2, "failed signing/unsafe targets must not send or retry");
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
    console.log("PASS: browser CBOR/Keccak golden vector, real wallet signatures recovered by Python verifier, exact target/body, fresh nonces, rejected signing, invalid recid, same-origin only, no persistence");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
