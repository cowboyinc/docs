import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("./index.ts", import.meta.url), "utf8");
const executable = stripTypeScriptTypes(
  source
    .replace(
      "import Kernel, { type KernelContext } from '@onkernel/sdk';",
      "const Kernel = globalThis.KernelStub;",
    )
    .replace(
      "import { chromium } from 'playwright';",
      "const chromium = globalThis.chromiumStub;",
    ),
  { mode: "transform" },
);

function load(fetchImpl) {
  const actions = new Map();
  class KernelStub {
    app() {
      return { action: (name, handler) => actions.set(name, handler) };
    }
  }
  let evaluations = 0;
  const page = {
    evaluate: async () => (++evaluations === 1 ? "" : []),
    url: () => "https://example.com/",
    title: async () => "Example",
    goto: async () => {},
  };
  const chromiumStub = {
    connectOverCDP: async () => ({
      contexts: () => [{ pages: () => [page], newPage: async () => page }],
      close: async () => {},
    }),
  };
  vm.runInNewContext(executable, {
    KernelStub,
    chromiumStub,
    fetch: fetchImpl,
    process: { env: {} },
    TextEncoder,
  });
  return actions;
}

function response(status, body = {}) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test("visit keeps the page result when temporary-session cleanup fails", async () => {
  const actions = load(async (_url, options = {}) => {
    if (options.method === "POST")
      return response(200, { session_id: "TEMP1", cdp_ws_url: "ws://visit" });
    if (options.method === "DELETE") return response(503);
    throw new Error("unexpected request");
  });
  assert.deepEqual(
    JSON.parse(JSON.stringify(await actions.get("visit")({}, { url: "https://example.com" }))),
    { ok: true, url: "https://example.com/", title: "Example", text: "", links: [] },
  );
});

test("explicit close still reports cleanup failure", async () => {
  const actions = load(async () => response(503));
  await assert.rejects(
    actions.get("step")({}, { op: "close", session_id: "S1" }),
    /session S1 close failed \(503\)/,
  );
});
