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
export const USUAL_PANELS = ["diagnosis", "latency", "line", "speed", "router", "traffic", "services", "strips", "heat", "incidents", "report", "matrix"];
export const PHONE = { width: 390, height: 844, isMobile: true, hasTouch: true };
export const DESKTOP = { width: 1400, height: 1000 };
export const ALL_VARIANTS = ["full", "bare", "tworouters", "unknownline"];
// The host name the browser opens the app by (test_ui_smoke.py BROWSER_HOST), mapped to 127.0.0.1 below. Not a local name
// on purpose: a page on 127.0.0.1 or localhost is a secure context, where the Clipboard API, notifications and downloads
// work, which hides what fails for a person who opens http://homeserver:8000 from another machine.
export const BROWSER_HOST = "pengumon-test";
// The same address on 127.0.0.1, for requests made by node itself (a step's fetch), which cannot resolve BROWSER_HOST.
export const loopback = (url) => url.replace(BROWSER_HOST, "127.0.0.1");
export const SHOTS = process.env.PENGUMON_SMOKE_SHOTS === "1";

// The bus step subscribes a function that throws on purpose; the bus logs it (and must go on). The second: the states
// steps refuse the socket on purpose.
const KNOWN_CONSOLE_ERRORS = [/^bus: subscriber of "targets" threw: smoke-boom$/, /^WebSocket connection to .*\/ws-dead.* failed/,
  /^The theme's effect could not be started: smoke-effect-boom$/]; // (the effects step makes an effect throw on purpose)

export async function createContext({ base, outDir, variant }) {
  const require = createRequire(path.join(process.env.PGBROWSER, "package.json"));
  const puppeteer = require("puppeteer-core");
  fs.mkdirSync(outDir, { recursive: true });
  const firefox = process.env.PENGUMON_BROWSER === "firefox";
  const executablePath = firefox ? process.env.FIREFOX_EXECUTABLE_PATH : process.env.PUPPETEER_EXECUTABLE_PATH;
  if (!executablePath) {
    console.error(`${firefox ? "FIREFOX_EXECUTABLE_PATH" : "PUPPETEER_EXECUTABLE_PATH"} is not set: point it at the browser's executable (scripts/test-ui.sh does this in the container).`);
    process.exit(2);
  }
  // PENGUMON_BROWSER=firefox: Firefox over WebDriver BiDi (see docs/testing.md); the default is Chromium over CDP.
  const args = (process.env.PENGUMON_BROWSER_ARGS ?? "").split(/\s+/).filter(Boolean);
  const browser = firefox
    ? await puppeteer.launch({ browser: "firefox", protocol: "webDriverBiDi", executablePath, headless: true,
      // network.dns.localDomains: these names resolve to the loopback address (the container has no network and no DNS)
      extraPrefsFirefox: { "network.dns.localDomains": BROWSER_HOST } })
    : await puppeteer.launch({ executablePath, args: [...args, `--host-resolver-rules=MAP ${BROWSER_HOST} 127.0.0.1`], headless: true });
  const page = await browser.newPage();
  await page.setViewport(DESKTOP);
  await page.evaluateOnNewDocument(() => {
    // The page is not a secure context (BROWSER_HOST), so it has no navigator.clipboard, as for a person on the LAN: it
    // copies through a hidden textarea and execCommand("copy"). window.__clip() is what that last copied, taken from the
    // copy event (the system clipboard of a headless browser cannot be read).
    let clip = "";
    document.addEventListener("copy", () => {
      const box = [...document.querySelectorAll("textarea")].find((t) => t.selectionStart !== t.selectionEnd);
      clip = box ? box.value.slice(box.selectionStart, box.selectionEnd) : String(getSelection());
    }, true);
    window.__clip = () => clip;
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
        super(sessionStorage.getItem("smokeWs") === "dead" ? String(url).replace(/(\?|$)/, "-dead$1") : url, ...rest);
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
  // sessionStorage.smokeFetch = {"METHOD /path": {status, body}}: those calls are answered by the page itself from its first
  // script on (stubFetch below sets it and reloads), e.g. a state the test server is not in; window.__stubbed lists them,
  // and window.__fetched every call of that page load ([key, body]), stubbed or not.
  await page.evaluateOnNewDocument(() => {
    const raw = sessionStorage.getItem("smokeFetch");
    if (!raw) return;
    const stubs = JSON.parse(raw);
    const real = window.fetch.bind(window);
    window.__stubbed = [];
    window.__fetched = [];
    window.fetch = (url, opts = {}) => {
      const key = `${(opts.method || "GET").toUpperCase()} ${new URL(String(url), location.href).pathname}`;
      window.__fetched.push([key, opts.body ?? null]);
      const hit = stubs[key];
      if (!hit) return real(url, opts);
      window.__stubbed.push([key, opts.body ?? null]);
      return Promise.resolve(new Response(JSON.stringify(hit.body), { status: hit.status ?? 200, headers: { "content-type": "application/json" } }));
    };
  });
  const problems = [];
  const dialogs = [];
  // A step that ends the page's sign-in on purpose (expectRefusals(true)) expects 401s until it says otherwise.
  let refusalsExpected = false;
  const expectRefusals = (on) => { refusalsExpected = on; };
  // A step that makes the server answer a status on purpose (a 409 from Settings' Save) says which until it says none.
  let statusExpected = [];
  const expectStatus = (...codes) => { statusExpected = codes; };
  const refusal = (text) => (refusalsExpected && /status of 401/.test(text)) || statusExpected.some((c) => text.includes(`status of ${c}`));
  // Firefox reports, as errors the page did not make: the socket the states steps refuse on purpose (the page's own refusal is
  // /ws-dead), a module import that a reload canceled mid-load (a module that is really missing is an HTTP 404 as well,
  // which is still a problem), and a fetch a reload canceled: the page's .catch logs the error, which Firefox's console
  // gives as "undefined" (a request that really failed is a requestfailed or an HTTP status, still a problem).
  const FIREFOX_NOISE = [/ws-dead/, /error loading dynamically imported module/, /^undefined$/];
  const noise = (text) => firefox && FIREFOX_NOISE.some((re) => re.test(text));
  page.on("pageerror", (e) => { if (!noise(e.message)) problems.push(`pageerror: ${e.message}`); });
  page.on("console", async (m) => {
    if (m.type() !== "error") return;
    let text = m.text();
    // Firefox gives an Error argument as "JSHandle@error": read its message
    if (/^JSHandle@/.test(text)) text = (await Promise.all(m.args().map((a) => a.evaluate((v) => (v instanceof Error ? v.message : String(v))).catch(() => "")))).join(" ");
    if (!KNOWN_CONSOLE_ERRORS.some((re) => re.test(text)) && !refusal(text) && !noise(text)) problems.push(`console.error: ${text}`);
  });
  // Firefox also reports a request that a reload or navigation canceled (NS_BINDING_ABORTED), and the socket the states
  // steps refuse on purpose; Chromium reports neither here.
  page.on("requestfailed", (r) => {
    const why = r.failure()?.errorText;
    if (firefox && (why === "NS_BINDING_ABORTED" || /\/ws-dead(\?|$)/.test(r.url()))) return;
    problems.push(`requestfailed: ${r.url()} ${why}`);
  });
  page.on("response", (r) => {
    if (r.url().startsWith(base) && r.status() >= 400 && !noise(r.url()) && !(refusalsExpected && r.status() === 401) && !statusExpected.includes(r.status())) problems.push(`HTTP ${r.status()}: ${r.url()}`);
  });
  page.on("dialog", (d) => { dialogs.push(`${d.type()}: ${d.message()}`); d.dismiss(); });
  // Chromium only (CDP): where downloads go. Firefox saves to its own default folder; no step reads a download from outDir.
  const client = firefox ? null : await page.createCDPSession();
  await client?.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: path.resolve(outDir) });

  const bare = variant === "bare"; // a run against a config with no devices: no device cards, the rest works
  const stats = { steps: [], failed: [], assertions: 0 };
  const report = { perf: null }; // a step may leave a note for the final output

  // ---- waiting and reading ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = (fn, ...a) => page.waitForFunction(fn, { timeout: 15000 }, ...a).catch((e) => {
    throw new Error(`${e.message} while waiting for ${String(fn).slice(0, 160)} with ${JSON.stringify(a)}`);
  });
  // n animation frames: a scroll or resize event the page was sent has been delivered (they fire at the start of a frame)
  const frames = (n = 3) => page.evaluate((k) => new Promise((r) => { const tick = (i) => (i ? requestAnimationFrame(() => tick(i - 1)) : r()); tick(k); }), n);
  // Waits until the layout has stopped moving after a resize, a tab switch or a reload: the page's size and the charts'
  // canvases have been the same for 8 frames in a row (at most 4 s, then the step carries on and its own checks decide).
  const layoutSettled = () => page.evaluate(() => new Promise((r) => {
    const sig = () => `${innerWidth}x${innerHeight} ${document.documentElement.scrollWidth}x${document.documentElement.scrollHeight} ${[...document.querySelectorAll("canvas")].map((c) => c.width).join(",")}`;
    const t0 = performance.now();
    let last = sig();
    let same = 0;
    const tick = () => {
      const now = sig();
      same = now === last ? same + 1 : 0;
      last = now;
      if (same >= 8 || performance.now() - t0 > 4000) r();
      else requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }));
  // Waits for `n` more live messages from the server (about one a second); the page's own handler has run by then.
  const nextMessages = (n = 2) => page.evaluate((k) => new Promise((resolve) => {
    const socket = window.__smokeSockets.at(-1);
    let seen = 0;
    socket.addEventListener("message", function counted() {
      if (++seen < k) return;
      socket.removeEventListener("message", counted);
      resolve();
    });
  }), n);
  // Waits until an element has stopped moving (a smooth scroll to it has ended): its top is the same for 8 frames in a row.
  const positionSettled = (sel) => page.evaluate((s) => new Promise((r) => {
    const t0 = performance.now();
    let last = null;
    let same = 0;
    const tick = () => {
      const top = document.querySelector(s)?.getBoundingClientRect().top;
      same = top === last ? same + 1 : 0;
      last = top;
      if (same >= 8 || performance.now() - t0 > 4000) r();
      else requestAnimationFrame(tick);
    };
    requestAnimationFrame(() => requestAnimationFrame(tick));
  }), sel);
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

  // Tab (and Shift+Tab) never reaches the page behind an open dialog.
  const tabStaysIn = async (modal) => {
    for (const shift of [false, true]) {
      if (shift) await page.keyboard.down("Shift");
      let outside = 0;
      for (let i = 0; i < 40; i++) {
        await page.keyboard.press("Tab");
        if (!(await page.evaluate((m) => document.getElementById(m).contains(document.activeElement), modal))) outside++;
      }
      if (shift) await page.keyboard.up("Shift");
      eq(`${modal}: ${shift ? "Shift+Tab" : "Tab"} stops outside the dialog`, outside, 0);
    }
  };

  // A dialog's keyboard: it opens with focus inside it, Tab stays in it, Escape closes it even after a click on plain
  // text (focus is then on the body), and focus goes back to the control that opened it.
  const dialogKeys = async (opener, modal) => {
    await page.$eval(opener, (e) => e.focus());
    await click(opener);
    await until((m) => document.getElementById(m).classList.contains("active"), modal);
    await until((m) => document.getElementById(m).contains(document.activeElement), modal);
    await tabStaysIn(modal);
    await page.click(`#${modal} .modal-t`);
    ok(`${modal}: a click on plain text leaves focus on the page body`, await page.evaluate(() => document.activeElement === document.body));
    await page.keyboard.press("Escape");
    await until((m) => !document.getElementById(m).classList.contains("active"), modal);
    eq(`${modal}: focus is back on its opener`, await page.evaluate((o) => document.activeElement === document.querySelector(o), opener), true);
  };

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
  // the page answers these calls itself (see smokeFetch above) from a fresh load on; null: no stubs (and a fresh load)
  const stubFetch = async (stubs) => {
    await page.evaluate((s) => { if (s) sessionStorage.setItem("smokeFetch", JSON.stringify(s)); else sessionStorage.removeItem("smokeFetch"); }, stubs);
    await page.reload({ waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    await layoutMounted();
  };
  const noSideScroll = (modal) => page.evaluate((id) => { const b = document.querySelector(`#${id} .modal-b`); return b.scrollWidth <= b.clientWidth + 1; }, modal);

  // ---- light or dark, through the header's theme menu (so the charts re-theme as they do for a person) ----
  // pickMode("light" | "dark" | ""): "" is "Like the system"; flipMode: the other one of what shows now.
  const darkNow = () => page.evaluate(() => (document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches));
  const pickInMenu = async (action, arg) => {
    // a window resize closes open menus (chrome.js): one a step caused just before (setViewport) can land right after
    // this press and close the menu again, so a menu that is not open after a moment gets one more press
    await frames(2); // a resize the page was sent has been delivered
    for (let tries = 0; ; tries++) {
      await click("#btnTheme");
      const open = await page.waitForFunction(() => !document.getElementById("themeMenu").hidden, { timeout: 3000 })
        .then(() => true, () => false);
      if (open) break;
      if (tries) { await until(() => !document.getElementById("themeMenu").hidden); break; } // the second press: the usual wait and error
    }
    await click(`#themeMenu [data-action=${action}][data-arg="${arg}"]`);
    await until(() => document.getElementById("themeMenu").hidden);
  };
  const pickMode = (mode) => pickInMenu("pickMode", mode);
  // pickLook(id), "" for the default: done once the theme is on the page (after its stylesheet has loaded)
  const pickLook = async (id) => {
    await pickInMenu("pickLook", id);
    await until((want) => document.documentElement.dataset.look === (want || document.documentElement.dataset.lookDefault), id);
  };
  const flipMode = async () => pickMode((await darkNow()) ? "light" : "dark");
  // null: the system theme
  const systemTheme = await page.evaluate(() => (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  const setTheme = async (t) => {
    const want = t || systemTheme;
    const now = await page.evaluate(() => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
    if (now !== want) await pickMode(t || "");
    await until((w) => (document.documentElement.dataset.theme || w) === w, want);
  };

  // ---- screenshots ----
  const shot = (name, opts = {}) => page.screenshot({ path: path.join(outDir, `ps-${name}.png`), ...opts }); // the phone-and-states set (PENGUMON_SMOKE_SHOTS=1)
  const dialogShot = (name) => page.screenshot({ path: path.join(outDir, `ui-${name}.png`) });
  const snap = (file, opts = {}) => page.screenshot({ path: path.join(outDir, file), ...opts }); // a named file in outDir
  // a dialog photographed in the current theme, the other one, and a phone width (<name>-dark.png, -light.png, -phone-*.png)
  async function shots(name) {
    const dark = await page.evaluate(() => document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches));
    const flip = async () => { await flipMode(); await sleep(400); };
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

  // Hop details is not drawn while it is off screen (js/cards.js, offscreen: 'defer'): a step that waits for what it
  // shows brings it on screen first, as a person scrolls to it.
  const showCard = async (panel) => {
    await page.$eval(`[data-panel=${panel}] .panel-b`, (b) => b.scrollIntoView({ block: "center" }));
    await frames(2);
  };

  // ---- help popovers ----
  const popoverText = () => page.$eval("#helpPopover", (e) => (e.hidden ? null : e.textContent.trim()));
  // The page closes a popover when its button is scrolled off screen, and Escape's focus() on the previous button scrolls
  // (the event comes a frame later). So the button is scrolled into view first and the events are let through; a person
  // clicks a button they can see. (Without this the step "the help texts quote the live values" failed now and then.)
  const openHelp = async (key) => {
    await page.$eval(`.info[data-help="${key}"]`, (b) => b.scrollIntoView({ block: "center" }));
    await frames(3);
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
    if (await p.evaluate(() => window.isSecureContext)) throw new Error("the page is a secure context: the run must open the app by BROWSER_HOST");
  };

  // ---- the page between step files: a known state (desktop, system theme, nothing stored, a fresh socket) ----
  async function resetPage() {
    if (!firefox) await page.emulateMediaFeatures([]); // CDP only
    await page.setViewport(DESKTOP);
    const clearState = () => page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); window.__smokeMutate = null; });
    try {
      await clearState();
    } catch (e) {
      // The page has no origin that allows storage right now (localStorage "Access is denied": an error page, a blank
      // document, one being replaced). Go to the app first, then clear; the text is printed, not swallowed.
      console.log(`resetPage: storage was not available (${String(e.message ?? e).split("\n")[0]}); going back to the app first`);
      await page.goto(base + "/", { waitUntil: "load" });
      await clearState();
    }
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
    browser, firefox, page, client, base, outDir, variant, bare, problems, dialogs, stats, report,
    LINE, ROUTER, USUAL_PANELS, PHONE, DESKTOP, SHOTS, systemTheme, fs, path,
    sleep, frames, layoutSettled, nextMessages, positionSettled, until, text, count, shown, visible, click, clickAt, waitText, waitCount, chartLabels, hasChart, modalOpen, eq, ok, dialogKeys, tabStaysIn,
    panelOrder, layoutMounted, openLayout, pageReady, phone, noSideScroll, setTheme, pickMode, pickLook, flipMode, darkNow, stubFetch,
    shot, dialogShot, snap, shots, inspect, inspectNamed, canvasSig, inkedPixels, moduleUrl,
    popoverText, openHelp, showCard, signIn, signInWithToken, expectRefusals, expectStatus, resetPage, step,
  };
  return ctx;
}
