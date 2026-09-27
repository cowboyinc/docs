import { keccak_256 } from "./vendor/sha3.js";

const utf8 = (value) => new TextEncoder().encode(value);
const hex = (value) => Array.from(value, byte => byte.toString(16).padStart(2, "0")).join("");
const unhex = (value, length) => {
  if (typeof value !== "string") throw new Error("Invalid wallet signature.");
  const text = value.replace(/^0x/, "");
  if (!new RegExp(`^[0-9a-fA-F]{${length * 2}}$`).test(text)) throw new Error("Invalid wallet signature.");
  return Uint8Array.from(text.match(/../g), byte => parseInt(byte, 16));
};
const join = (...parts) => {
  const bytes = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.length; }
  return bytes;
};
const uint = (value) => {
  if (!/^[0-9]+$/.test(String(value))) throw new Error("Invalid signing context.");
  const n = BigInt(value);
  if (n < 0n || n >= 2n ** 64n) throw new Error("Invalid signing context.");
  if (n < 24n) return Uint8Array.of(Number(n));
  if (n <= 255n) return Uint8Array.of(24, Number(n));
  if (n <= 65535n) { const b = new Uint8Array(3); b[0] = 25; new DataView(b.buffer).setUint16(1, Number(n)); return b; }
  if (n <= 4294967295n) { const b = new Uint8Array(5); b[0] = 26; new DataView(b.buffer).setUint32(1, Number(n)); return b; }
  const bytes = new Uint8Array(9); bytes[0] = 27; new DataView(bytes.buffer).setBigUint64(1, n); return bytes;
};
const sized = (value, major) => {
  const length = uint(value.length); length[0] |= major << 5;
  return join(length, value);
};
const text = (value) => sized(utf8(value), 3);
const bytes = (value) => sized(value, 2);
const b64url = (bytes) => btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");

// The same exact target, UTF-8 body, and method are signed and sent. No URL
// normalization or JSON reserialization is allowed after constructing this.
export async function signingBytes(context, method, target, body, expiry, nonce) {
  if (typeof method !== "string" || !/^[A-Z]+$/.test(method)
      || typeof target !== "string" || !/^\/(?!\/)[\x21-\x7e]*$/.test(target)
      || target.includes("#") || /%(?![0-9A-Fa-f]{2})/.test(target)
      || target.length > 4096 || typeof body !== "string" || utf8(body).length > 4096
      || !(nonce instanceof Uint8Array) || nonce.length < 1 || nonce.length > 256
      || typeof context.network !== "string" || !context.network.trim()
      || !/^[\x21-\x7e]{1,64}$/.test(context.network)
      || typeof context.handler !== "string" || !context.handler || utf8(context.handler).length > 256
      || BigInt(context.chain_id) === 0n) throw new Error("Invalid signing context.");
  const actor = unhex(context.actor, 20);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", utf8(body)));
  return join(utf8("cowboy/cip15/runner-route-auth/v1"), Uint8Array.of(0x89), uint(context.chain_id),
    text(context.network), bytes(actor), text(method), text(context.handler),
    bytes(utf8(target)), bytes(digest), uint(expiry), bytes(nonce));
}

export const signingDigest = async (...args) => keccak_256(await signingBytes(...args));

export function createWalletClient(provider) {
  let context;
  return {
    async connect() {
      context = undefined;
      provider ||= window.cowboy;
      if (!provider?.connect || !provider?.signMessage) throw new Error("Wallet connection is unavailable on this page.");
      const response = await fetch("/manage/config", {credentials: "omit", cache: "no-store", redirect: "error"});
      if (!response.ok) throw new Error("Wallet configuration is unavailable.");
      const config = await response.json();
      await signingBytes(config, "GET", "/manage", "", 1, new Uint8Array(32));
      await provider.connect();
      context = Object.freeze(config);
    },
    async signedFetch(target, {method = "GET", body = ""} = {}) {
      if (!context) throw new Error("Connect your hiring wallet to continue.");
      const url = new URL(target, location.origin);
      if (url.origin !== location.origin || url.pathname + url.search !== target) throw new Error("Invalid request target.");
      const nonce = crypto.getRandomValues(new Uint8Array(32));
      const expiry = Math.floor(Date.now() / 1000) + 120;
      const digest = await signingDigest(context, method, target, body, expiry, nonce);
      const signed = await provider.signMessage("0x" + hex(digest));
      const recovery = signed?.v;
      if (!Number.isInteger(recovery) || recovery < 0 || recovery > 3) throw new Error("Invalid wallet signature.");
      const signature = join(unhex(signed.r, 32), unhex(signed.s, 32), Uint8Array.of(recovery));
      const s = BigInt("0x" + hex(signature.slice(32, 64)));
      if (s === 0n || s > 0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0n) throw new Error("Invalid wallet signature.");
      if (Math.floor(Date.now() / 1000) >= expiry) throw new Error("Signing expired. Try the action again.");
      // Fetch never retries or follows a redirect carrying the account proof.
      return fetch(target, {method, ...(body ? {body} : {}), credentials: "omit", cache: "no-store", redirect: "error",
        headers: {"content-type": "application/json", "x-cowboy-account-sig": b64url(signature),
          "x-cowboy-account-nonce": b64url(nonce), "x-cowboy-account-expiry": String(expiry),
          "x-cowboy-account-chain-id": String(context.chain_id), "x-cowboy-account-network": context.network}});
    }
  };
}
