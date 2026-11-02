// The shared half of the browser smoke test: the browser, the page, and every helper a step uses. Defined once, here.
// smoke.mjs (the runner) calls createContext() and hands the result to each step as its only argument; the step files
// in steps/ destructure what they need from it. A step file never declares a helper of its own at the top level (the
// guard in guards.mjs fails the run if two files do, or if one shadows a name exported here): a helper two files need
// belongs in this file.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

export const LINE = "[data-panel=line] .panel[data-device=modem]";
export const ROUTER = "[data-panel=router] .panel[data-device=router]";
export const USUAL_PANELS = ["diagnosis", "latency", "line", "router", "services", "strips", "heat", "incidents", "report", "matrix"];
export const PHONE = { width: 390, height: 844, isMobile: true, hasTouch: true };
export const DESKTOP = { width: 1400, height: 1000 };
export const ALL_VARIANTS = ["full", "bare", "tworouters", "unknownline"];
export const SHOTS = process.env.PENGUMON_SMOKE_SHOTS === "1";

// The bus step subscribes a function that throws on purpose; the bus logs it (and must go on). The second: the states
// steps refuse the socket on purpose.
const KNOWN_CONSOLE_ERRORS = [/^bus: subscriber of "targets" threw: smoke-boom$/, /^WebSocket connection to .*\/ws-dead.* failed/];

export async function createContext({ base, outDir, variant }) {
  const require = createRequire(path.join(process.env.PGBROWSER, "package.json"));
  const puppeteer = require("puppeteer-core");
  fs.mkdirSync(outDir, { recursive: true });
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
  if (!executablePath) {
    console.error("PUPPETEER_EXECUTABLE_PATH is not set: point it at a Chrome/Chromium executable (scripts/test-ui.sh does this in the container).");
    process.exit(2);
  }
  const args = (process.env.PENGUMON_BROWSER_ARGS ?? "").split(/\s+/).filter(Boolean);
  const browser = await puppeteer.launch({ executablePath, args, headless: true });
  const page = await browser.newPage();
  await page.setViewport(DESKTOP);
  await page.evaluateOnNewDocument(() => {
    // a headless page has no focus, so the real clipboard refuses; a stand-in keeps the copy buttons testable
    let clip = "";
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (t) => { clip = t; }, readText: async () => clip } });
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
  // A WebSocket stand-in passes the real messages through window.__smokeMutate (the states steps set it);
  // sessionStorage.smokeWs=dead makes the page's socket one that never connects.
  await page.evaluateOnNewDocument(() => {
    const Native = window.WebSocket;
    window.__smokeSockets = [];
    window.WebSocket = class extends Native {
      constructor(url, ...rest) {
        super(sessionStorage.getItem("smokeWs") === "dead" ? url + "-dead" : url, ...rest);
        window.__smokeSockets.push(this);
      }
      set onmessage(fn) {
        super.onmessage = fn && ((ev) => {
          if (!window.__smokeMutate) return fn(ev);
          try { const d = JSON.parse(ev.data); window.__smokeMutate(d); return fn({ data: JSON.stringify(d) }); } catch (e) { return fn(ev); }
        });
      }
      get onmessage() { return super.onmessage; }
    };
  });
  const problems = [];
  const dialogs = [];
  // A step that ends the page's sign-in on purpose (expectRefusals(true)) expects 401s until it says otherwise.
  let refusalsExpected = false;
  const expectRefusals = (on) => { refusalsExpected = on; };
  const refusal = (text) => refusalsExpected && /status of 401/.test(text);
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && !KNOWN_CONSOLE_ERRORS.some((re) => re.test(m.text())) && !refusal(m.text())) problems.push(`console.error: ${m.text()}`);
  });
  page.on("requestfailed", (r) => problems.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => {
    if (r.url().startsWith(base) && r.status() >= 400 && !(refusalsExpected && r.status() === 401)) problems.push(`HTTP ${r.status()}: ${r.url()}`);
  });
  page.on("dialog", (d) => { dialogs.push(`${d.type()}: ${d.message()}`); d.dismiss(); });
  const client = await page.createCDPSession();
  await client.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: path.resolve(outDir) });

  const bare = variant === "bare"; // a run against a config with no devices: no device cards, the rest works
  const stats = { steps: [], failed: [], assertions: 0 };
  const report = { perf: null }; // a step may leave a note for the final output

  // ---- waiting and reading ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = (fn, ...a) => page.waitForFunction(fn, { timeout: 15000 }, ...a).catch((e) => {
    throw new Error(`${e.message} while waiting for ${String(fn).slice(0, 160)} with ${JSON.stringify(a)}`);
  });
  const text = (sel) => page.$eval(sel, (e) => e.textContent);
  const count = (sel) => page.$$eval(sel, (els) => els.length);
  const shown = (sel) => page.$eval(sel, (e) => getComputedStyle(e).display !== "none");
  const visible = (sel) => page.$eval(sel, (e) => { const r = e.getBoundingClientRect(); return getComputedStyle(e).display !== "none" && r.width > 0 && r.height > 0; });
  // el.click() instead of a mouse click: tables are rebuilt every second by the websocket, which can detach the target mid-click.
  const click = (sel) => page.evaluate((s) => document.querySelector(s).click(), sel);
  // SVG elements have no .click(); the topology groups get a real mouse click (hit testing included).
  const clickAt = (sel) => page.click(sel);
  const waitText = (sel, re) => until((s, r) => new RegExp(r).test(document.querySelector(s)?.textContent ?? ""), sel, re.source);
  const waitCount = (sel, n) => until((s, k) => document.querySelectorAll(s).length === k, sel, n);
  const chartLabels = (id) => page.evaluate((i) => Chart.getChart(document.getElementById(i))?.data.labels.length ?? -1, id);
  const hasChart = (id) => until((i) => !!Chart.getChart(document.getElementById(i)), id);
  const modalOpen = (id) => page.$eval(`#${id}`, (m) => m.classList.contains("active"));
  const eq = (what, got, want) => { stats.assertions++; if (got !== want) throw new Error(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); };
  const ok = (what, cond) => { stats.assertions++; if (!cond) throw new Error(what); };

  // ---- the page ----
  const panelOrder = () => page.$$eval("[data-panel]", (els) => els.map((e) => e.dataset.panel));
  const layoutMounted = () => until(() => document.querySelector("[data-panel=report]")?.dataset.layoutHidden !== undefined);
  const openLayout = async () => {
    await click("[data-action=openLayoutDialog]");
    await until(() => document.getElementById("layoutModal").classList.contains("active"));
  };
  const pageReady = () => until((l) => /^Live/.test(document.getElementById("wsText")?.textContent ?? "") && document.querySelector(l), `${LINE} [data-action=openChannelsModal]`);
  // Puppeteer reloads the page when the mobile emulation switches on or off, so dialogs are opened after each switch.
  const phone = async (fn) => {
    await page.setViewport(PHONE);
    try { await pageReady(); await fn(); } finally { await page.setViewport(DESKTOP); await pageReady(); }
  };
  const noSideScroll = (modal) => page.evaluate((id) => { const b = document.querySelector(`#${id} .modal-b`); return b.scrollWidth <= b.clientWidth + 1; }, modal);

  // ---- the theme, through the header's own button (so the charts re-theme as they do for a person); null: the system theme ----
  const systemTheme = await page.evaluate(() => (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  const setTheme = async (t) => {
    const want = t || systemTheme;
    const now = await page.evaluate(() => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
    if (now !== want) await click("#btnTheme");
    await until((w) => (document.documentElement.dataset.theme || w) === w, want);
  };

  // ---- screenshots ----
  const shot = (name, opts = {}) => page.screenshot({ path: path.join(outDir, `ps-${name}.png`), ...opts }); // the phone-and-states set (PENGUMON_SMOKE_SHOTS=1)
  const dialogShot = (name) => page.screenshot({ path: path.join(outDir, `ui-${name}.png`) });
  const snap = (file, opts = {}) => page.screenshot({ path: path.join(outDir, file), ...opts }); // a named file in outDir
  // a dialog photographed in the current theme, the other one, and a phone width (<name>-dark.png, -light.png, -phone-*.png)
  async function shots(name) {
    const dark = await page.evaluate(() => document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches));
    const flip = async () => { await click("#btnTheme"); await sleep(400); };
    await snap(`${name}-${dark ? "dark" : "light"}.png`);
    await flip();
    await snap(`${name}-${dark ? "light" : "dark"}.png`);
    await flip();
    const view = page.viewport();
    await page.setViewport({ width: 390, height: 844 });
    await sleep(400);
    await snap(`${name}-phone-${dark ? "dark" : "light"}.png`);
    await page.setViewport(view);
    await sleep(300);
  }

  // ---- incidents ----
  // opens an incident's inspector from the table row (the incident must be on the shown page)
  const inspect = (title) => page.evaluate((t) => {
    [...document.querySelectorAll("#incidentsBody tr")].find((tr) => tr.textContent.includes(t)).querySelector("button[data-action=inspectIncident]").click();
  }, title);
  // opens it the way a row's button does (the delegated action), whatever page the table is on
  const inspectNamed = (title, { suspectLoad = false } = {}) => page.evaluate(async (t, suspect) => {
    const list = await (await fetch("/api/incidents?limit=500&include_expected=true")).json();
    const inc = list.find((i) => i.title === t);
    if (!inc) throw new Error(`no incident titled ${t}`);
    const real = window.fetch;
    if (suspect) {
      window.fetch = async (url, ...rest) => {
        const res = await real(url, ...rest);
        if (!String(url).includes("/inspect")) return res;
        const data = await res.json();
        data.incident.load_pct = 912;
        data.incident.load_suspect = true;
        return new Response(JSON.stringify(data), { status: 200, headers: { "content-type": "application/json" } });
      };
      setTimeout(() => { window.fetch = real; }, 1500);
    }
    const b = document.createElement("button");
    b.dataset.action = "inspectIncident";
    b.dataset.arg = String(inc.id);
    document.body.appendChild(b);
    b.click();
    b.remove();
  }, title, suspectLoad);

  // ---- canvases ----
  const canvasSig = (sel) => page.$eval(sel, (c) => c.dataset.sig ?? "");
  const inkedPixels = (sel) => page.$eval(sel, (c) => {
    const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
    return n;
  });
  // the served URL of a dashboard module in js/ (a step imports it inside the page, to use the page's own copy and state)
  const moduleUrl = (name) => page.evaluate((n) => [...document.scripts].find((s) => s.src.endsWith("/js/main.js")).src.replace("main.js", n), name);

  // ---- help popovers ----
  const popoverText = () => page.$eval("#helpPopover", (e) => (e.hidden ? null : e.textContent.trim()));
  const openHelp = async (key) => {
    await click(`.info[data-help="${key}"]`);
    await until(() => !document.getElementById("helpPopover").hidden);
    return popoverText();
  };

  // ---- device setup: what a person does to give a device its password ----
  // Opens the setup dialog of a device card, types a password and saves; the real call stores it, which is what makes
  // the `clients` feature of a router available (a reload picks that up). Waits for the dialog to close again.
  const signIn = async (card) => {
    await click(`${card} [data-action=openDeviceSetup]`);
    await until(() => document.getElementById("deviceSetupModal").classList.contains("active"));
    await page.$eval("#setupPassword", (i) => { i.value = ""; });
    await page.type("#setupPassword", "pw");
    await page.evaluate(() => document.getElementById("btnSaveSetup").click());
    await waitText("#setupResult", /Success/);
    await until(() => !document.getElementById("deviceSetupModal").classList.contains("active"));
  };

  // ---- the access token's sign-in page (the `token` variant; PENGUMON_SMOKE_TOKEN is the token the app has) ----
  // Signs `p` (this page, or a page of another browser context) in the way a person does: open the dashboard, get the
  // sign-in page, type the token, land on the dashboard.
  const signInWithToken = async (p = page) => {
    await p.goto(base + "/", { waitUntil: "load" });
    if (new URL(p.url()).pathname !== "/login") throw new Error(`expected the sign-in page, got ${p.url()}`);
    await p.type("input[name=token]", process.env.PENGUMON_SMOKE_TOKEN ?? "");
    await Promise.all([p.waitForNavigation({ waitUntil: "load" }), p.click("button[type=submit]")]);
    if (new URL(p.url()).pathname !== "/") throw new Error(`the sign-in did not reach the dashboard: ${p.url()}`);
  };

  // ---- the page between step files: a known state (desktop, system theme, nothing stored, a fresh socket) ----
  async function resetPage() {
    await page.emulateMediaFeatures([]);
    await page.setViewport(DESKTOP);
    await page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); window.__smokeMutate = null; });
    await page.reload({ waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    await layoutMounted();
    await until(() => document.querySelectorAll("#hopMatrixBody tr [data-f=last]").length >= 5); // the first message has been drawn
  }

  // ---- a step: runs fn(ctx); a failure is recorded with a screenshot ----
  async function step(name, fn) {
    try {
      await fn(ctx);
      stats.steps.push(name);
      return true;
    } catch (e) {
      stats.failed.push({ step: name, error: String(e.message ?? e) });
      await page.screenshot({ path: path.join(outDir, stats.failed.length === 1 ? "failure.png" : `failure-${stats.failed.length}.png`) }).catch(() => {});
      return false;
    }
  }

  const ctx = {
    browser, page, client, base, outDir, variant, bare, problems, dialogs, stats, report,
    LINE, ROUTER, USUAL_PANELS, PHONE, DESKTOP, SHOTS, systemTheme, fs, path,
    sleep, until, text, count, shown, visible, click, clickAt, waitText, waitCount, chartLabels, hasChart, modalOpen, eq, ok,
    panelOrder, layoutMounted, openLayout, pageReady, phone, noSideScroll, setTheme,
    shot, dialogShot, snap, shots, inspect, inspectNamed, canvasSig, inkedPixels, moduleUrl,
    popoverText, openHelp, signIn, signInWithToken, expectRefusals, resetPage, step,
  };
  return ctx;
}
