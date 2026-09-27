import { createWalletClient } from "./wallet.js";
// A host may supply an equivalent signing bridge; otherwise use the Cowboy
// wallet provider. Keys stay in the wallet, never in this page.
const wallet = window.cowboyWallet || createWalletClient();
const $ = (id) => document.getElementById(id);
const slot = new URLSearchParams(location.search).get("slot");
let catalog = null;
let connected = false;
const status = (text) => { $("status").textContent = text; };
const clearSecret = () => { $("secret").textContent = ""; $("secret-panel").hidden = true; };
const readable = (value) => value === null ? "Unavailable" : BigInt(value).toLocaleString();
const cost = (value) => {
  if (value === null) return "Unavailable";
  const n = BigInt(value);
  return "$" + (n / 1000000000n).toLocaleString() + "." + (n % 1000000000n).toString().padStart(9, "0").replace(/0+$/, "").padEnd(2, "0");
};
const date = (seconds) => seconds === null ? "Unavailable" : new Date(seconds * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC");
const textCell = (row, text) => { const cell = document.createElement("td"); cell.textContent = text; row.append(cell); return cell; };
const option = (select, value, label) => { const item = document.createElement("option"); item.value = value; item.textContent = label; select.append(item); };
async function api(path, method = "GET", data, extra = {}) {
  if (!connected || !slot || !wallet?.signedFetch) throw new Error("Connect your hiring wallet to continue.");
  const query = new URLSearchParams({slot, ...extra});
  const target = path + "?" + query.toString();
  const body = data === undefined ? "" : JSON.stringify(data);
  const response = await wallet.signedFetch(target, {method, body});
  const value = await response.json();
  if (response.status === 401 || response.status === 403) {
    clearSecret(); connected = false; $("workspace").hidden = true;
  }
  if (!response.ok) throw new Error(value.error === "access_denied" ? "This wallet cannot manage this hire. Reconnect with your hiring wallet." : value.error.replaceAll("_", " "));
  return value;
}
async function run(button, task) {
  button.disabled = true;
  try { await task(); } catch (error) { status(error.message || "Request failed. Try again."); }
  finally { button.disabled = false; }
}
async function loadKeys() {
  const data = await api("/manage/api/keys");
  $("keys").replaceChildren();
  $("key-filter").replaceChildren();
  option($("key-filter"), "", "All keys");
  $("keys-empty").hidden = data.keys.length > 0;
  for (const key of data.keys) {
    option($("key-filter"), key.key_id, `${key.name} · ${key.prefix}${key.revoked_at === null ? "" : " (revoked)"}`);
    const row = document.createElement("tr");
    textCell(row, key.name); textCell(row, key.prefix); textCell(row, date(key.created_at)); textCell(row, key.revoked_at === null ? "Active" : "Revoked");
    const cell = textCell(row, "");
    if (key.revoked_at === null) {
      const button = document.createElement("button"); button.className = "secondary"; button.textContent = "Revoke";
      button.addEventListener("click", () => {
        if (!window.confirm(`Revoke ${key.name} (${key.prefix})? Clients using this key will lose access.`)) return;
        run(button, async () => { await api("/manage/api/keys/" + encodeURIComponent(key.key_id), "DELETE"); clearSecret(); await loadKeys(); status("Key revoked."); });
      });
      cell.append(button);
    }
    $("keys").append(row);
  }
  $("show-create").disabled = data.hire_status !== "active";
  if (data.hire_status !== "active") status(`Inference is not available for this hire (${data.hire_status}). You can still view usage and revoke keys.`);
}
async function loadUsage() {
  $("usage-state").textContent = "Loading usage…";
  const query = {from: $("from").value, to: $("to").value};
  if ($("key-filter").value) query.key_id = $("key-filter").value;
  if ($("model-filter").value) query.model = $("model-filter").value;
  try {
    const data = await api("/manage/api/usage", "GET", undefined, query);
    const totals = data.totals;
    $("requests").textContent = readable(totals.requests);
    $("input-tokens").textContent = readable(totals.input_tokens);
    $("output-tokens").textContent = readable(totals.output_tokens);
    $("cost").textContent = cost(totals.usage_cost_nano_usd);
    const notes = [];
    if (totals.partial) notes.push(`Partial: ${totals.unknown_usage_requests} requests have pending or unknown usage.`);
    if (totals.cost_partial) notes.push("Usage cost has incomplete coverage.");
    if (totals.requests === 0) notes.push("No usage in this range.");
    notes.push("Last committed segment: " + date(data.last_committed_at) + ".");
    $("usage-state").textContent = notes.join(" ");
    $("breakdown").replaceChildren();
    for (const item of data.breakdown) {
      const row = document.createElement("tr");
      [item.key_id.slice(0, 8), item.model, readable(item.requests), readable(item.input_tokens), readable(item.output_tokens), cost(item.usage_cost_nano_usd) + (item.cost_partial ? " (partial)" : "")].forEach((value) => textCell(row, value));
      $("breakdown").append(row);
    }
  } catch (error) {
    ["requests", "input-tokens", "output-tokens", "cost"].forEach((id) => { $(id).textContent = "—"; });
    $("breakdown").replaceChildren();
    $("usage-state").textContent = "Usage could not be loaded. Refresh to try again.";
    throw error;
  }
}
const shellQuote = (text) => "'" + text.replaceAll("'", "'\\''") + "'";
function quickstart() {
  if (!catalog || !$("quickstart-model").value) { $("quickstart").textContent = "No enabled models."; return; }
  const body = JSON.stringify({model: $("quickstart-model").value, messages: [{role: "user", content: "Hello"}]});
  $("quickstart").textContent = "export COWBOY_API_KEY='paste-your-key-here'\nexport COWBOY_API_BASE=" + shellQuote(catalog.public_base_url) + "\ncurl \"$COWBOY_API_BASE/v1/chat/completions\" \\\n  -H \"Authorization: Bearer $COWBOY_API_KEY\" \\\n  -H 'Content-Type: application/json' \\\n  -d " + shellQuote(body);
}
$("connect").addEventListener("click", () => run($("connect"), async () => {
  clearSecret(); $("workspace").hidden = true; connected = false;
  if (!slot) throw new Error("Open the management link for your hire to continue.");
  await wallet.connect(); connected = true;
  catalog = await api("/manage/api/catalog");
  $("direct-access").hidden = !catalog.direct_access_available;
  $("grant-state").textContent = ""; $("grant-connection").hidden = true; $("grant-connection").textContent = "";
  $("quickstart-model").replaceChildren(); $("model-filter").replaceChildren();
  option($("model-filter"), "", "All models");
  for (const model of catalog.models) { option($("quickstart-model"), model.id, model.id); option($("model-filter"), model.id, model.id); }
  quickstart(); status("Wallet connected.");
  await loadKeys(); await loadUsage(); $("workspace").hidden = false;
}));
$("grant-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(event.submitter, async () => {
    const clean = (id) => $(id).value.trim().replace(/^0x/, "").toLowerCase();
    const data = await api("/manage/api/read-grant", "PUT", {signing_key: clean("grant-signing"), encryption_key: clean("grant-encryption")});
    $("grant-signing").value = ""; $("grant-encryption").value = "";
    $("grant-state").textContent = "Read access enabled. Open this volume using your CLI delegation.";
    $("grant-connection").textContent = `Owner: 0x${data.owner}\nVolume: ${data.volume}\nAccess: Read only`;
    $("grant-connection").hidden = false;
  });
});
$("remove-grant").addEventListener("click", () => {
  if (!window.confirm("Remove your CLI's read access to this hire's files?")) return;
  run($("remove-grant"), async () => {
    await api("/manage/api/read-grant", "DELETE");
    $("grant-connection").hidden = true; $("grant-connection").textContent = "";
    $("grant-state").textContent = "Read grant removed. Previously downloaded files remain on your device.";
  });
});
$("show-create").addEventListener("click", () => { clearSecret(); $("create-form").hidden = false; $("key-name").focus(); });
$("cancel-create").addEventListener("click", () => { $("create-form").hidden = true; $("key-name").value = ""; });
$("create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = event.submitter;
  run(button, async () => {
    clearSecret();
    // Deliberately one call. A lost response cannot recover the secret.
    try {
      const data = await api("/manage/api/keys", "POST", {name: $("key-name").value.trim()});
      $("secret").textContent = data.secret; $("secret-panel").hidden = false;
      $("create-form").hidden = true; $("key-name").value = "";
    } catch (error) {
      status("Key creation was not confirmed. Refresh the key list before trying again; a key may have been created.");
      await loadKeys(); return;
    }
    await loadKeys(); status("Key created. Copy and store it now.");
  });
});
$("clear-secret").addEventListener("click", clearSecret);
$("copy-secret").addEventListener("click", () => run($("copy-secret"), async () => { await navigator.clipboard.writeText($("secret").textContent); status("Key copied."); }));
$("copy-example").addEventListener("click", () => run($("copy-example"), async () => { await navigator.clipboard.writeText($("quickstart").textContent); status("Example copied."); }));
$("usage-form").addEventListener("submit", (event) => { event.preventDefault(); run(event.submitter, loadUsage); });
$("quickstart-model").addEventListener("change", quickstart);
window.addEventListener("pagehide", clearSecret);
window.addEventListener("beforeunload", clearSecret);
const today = new Date();
$("to").value = today.toISOString().slice(0, 10);
$("from").value = new Date(today.getTime() - 6 * 86400000).toISOString().slice(0, 10);
if (!slot) status("Open the management link for your hire to continue.");
