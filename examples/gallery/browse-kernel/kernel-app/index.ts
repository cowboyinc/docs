// Cowboy browse executor — a THIN Kernel app: one Playwright step per
// invocation against a PERSISTENT Kernel browser session. No LLM here; the
// agent loop lives in the on-chain assistant (runner.llm decides each step).
import Kernel, { type KernelContext } from '@onkernel/sdk';
import { chromium } from 'playwright';

const kernel = new Kernel();
const app = kernel.app('cowboy-browse');

const API = 'https://api.onkernel.com';
const KEY = process.env.KERNEL_API_KEY || '';
const H = { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' };
const CAP = 4000;                      // bytes of page text returned (delivery budget downstream)
const RESULT_CAP = 32000;              // leaves room for the invocation envelope

function bounded(result: any) {
  if (new TextEncoder().encode(JSON.stringify(result)).length > RESULT_CAP)
    return { ok: false, error: `page result exceeds ${RESULT_CAP} bytes` };
  return result;
}

async function closeBrowser(sid: string): Promise<void> {
  const r = await fetch(`${API}/browsers/${sid}`, { method: 'DELETE', headers: H });
  if (!r.ok && r.status !== 404) throw new Error(`session ${sid} close failed (${r.status})`);
}

async function freshCdp(sid: string): Promise<string> {
  const g = await fetch(`${API}/browsers/${sid}`, { headers: H });
  if (!g.ok) throw new Error(`session ${sid} not found (${g.status})`);
  const b: any = await g.json();
  return b.cdp_ws_url;
}

async function pageState(page: any) {
  // Visible text, bounded — this is what the agent reasons over next turn.
  const text: string = await page.evaluate(() => (document.body?.innerText || '').slice(0, 20000));
  const links: any[] = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href]')).slice(0, 40).map((a: any) =>
      ({ t: (a.innerText || '').trim().slice(0, 60), href: a.href })).filter((l: any) => l.t));
  return bounded({ url: page.url(), title: await page.title().catch(() => ''),
                   text: text.slice(0, CAP), links });
}

app.action('step', async (_ctx: KernelContext, payload: any) => {
  const p = typeof payload === 'string' ? JSON.parse(payload) : (payload || {});
  const op = String(p.op || 'extract');

  // open: create a PERSISTENT browser (no invocation_id => survives across steps)
  if (op === 'open') {
    const r = await fetch(`${API}/browsers`, { method: 'POST', headers: H,
      body: JSON.stringify({ headless: true, stealth: !!p.stealth,
                             timeout_seconds: Math.min(Math.max(p.timeout_seconds || 300, 60), 3600) }) });
    if (!r.ok) throw new Error(`session create failed (${r.status})`);
    const b: any = await r.json();
    return { ok: true, session_id: b.session_id };
  }
  let sid = String(p.session_id || '');
  if (op === 'close') {
    if (!sid) return { ok: false, error: 'session_id required' };
    await closeBrowser(sid);
    return { ok: true, closed: sid };
  }

  // AUTO-OPEN on goto. The on-chain model does not reliably call open before
  // goto (one run went goto-before-open and left an empty session). goto with
  // no session_id therefore CREATES a persistent browser here and returns its
  // session_id, which the actor remembers -- so the model just says "go here"
  // and the lifecycle can't be fumbled. Other ops still need an existing
  // session (there is nothing to click on a page never navigated to).
  let cdp: string;
  let newSession = '';
  if (!sid) {
    if (op !== 'goto')
      return { ok: false, error: 'session_id required (goto a page first)' };
    const r = await fetch(`${API}/browsers`, { method: 'POST', headers: H,
      body: JSON.stringify({ headless: true, stealth: !!p.stealth,
                             timeout_seconds: Math.min(Math.max(p.timeout_seconds || 300, 60), 3600) }) });
    if (!r.ok) throw new Error(`session create failed (${r.status})`);
    const b: any = await r.json();
    sid = b.session_id;
    newSession = sid;
    cdp = b.cdp_ws_url;
  } else {
    cdp = await freshCdp(sid);
  }

  const browser = await chromium.connectOverCDP(cdp);
  try {
    const ctx = browser.contexts()[0];
    const page = ctx.pages()[0] || await ctx.newPage();
    if (op === 'goto')       await page.goto(String(p.url || ''), { waitUntil: 'domcontentloaded', timeout: 45000 });
    else if (op === 'click') await page.click(String(p.selector || ''), { timeout: 15000 });
    else if (op === 'type')  await page.fill(String(p.selector || ''), String(p.text || ''), { timeout: 15000 });
    else if (op === 'press') await page.keyboard.press(String(p.key || 'Enter'));
    else if (op === 'scroll')await page.mouse.wheel(0, Number(p.dy || 800));
    else if (op !== 'extract') return { ok: false, error: `unknown op ${op}` };
    if (['click','type','press','scroll'].includes(op))
      await page.waitForLoadState('domcontentloaded', { timeout: 15000 }).catch(() => {});
    // Return the new session_id when we auto-opened, so the actor remembers it.
    const state = await pageState(page);
    if (state.ok === false) return { ...state, ...(newSession ? { session_id: newSession } : {}) };
    return { ok: true, ...(newSession ? { session_id: newSession } : {}), ...state };
  } finally {
    await browser.close();   // disconnect Playwright; the Kernel SESSION stays alive
  }
});

// One-shot read-only browse: open -> goto -> extract -> close, all server-side,
// so the on-chain actor's `visit` handler is a SINGLE invocation (one await).
app.action('visit', async (_ctx: KernelContext, payload: any) => {
  const p = typeof payload === 'string' ? JSON.parse(payload) : (payload || {});
  const url = String(p.url || '');
  if (!url) return { ok: false, error: 'url required' };
  const r = await fetch(`${API}/browsers`, { method: 'POST', headers: H,
    body: JSON.stringify({ headless: true, timeout_seconds: 120 }) });
  if (!r.ok) throw new Error(`session create failed (${r.status})`);
  const b: any = await r.json();
  const sid = b.session_id;
  try {
    const browser = await chromium.connectOverCDP(b.cdp_ws_url);
    try {
      const ctx = browser.contexts()[0];
      const page = ctx.pages()[0] || await ctx.newPage();
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
      const state = await pageState(page);
      return state.ok === false ? state : { ok: true, ...state };
    } finally { await browser.close(); }
  } finally {
    try { await closeBrowser(sid); } catch {}
  }
});
