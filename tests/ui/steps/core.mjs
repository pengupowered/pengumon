// Page-level steps: the page loads, the bus, the header (LAN pill), the desktop and phone layouts, the tab bar, the theme, the loading/
// reconnecting/stale/error/outage states, keyboard focus. Everything about one panel is in that panel's file.
import { ALL_VARIANTS } from "../lib.mjs";

export const subject = "core";
export const variants = ["full"];

// Helpers only the core steps use (each takes the page; anything two files need is in lib.mjs).
const bgOf = (page) => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
const lum = (rgb) => { const [r, g, b] = rgb.match(/\d+/g).map(Number); return (r + g + b) / 3; };
const pill = (page) => page.$eval("#statusPill", (e) => ({ cls: e.className, text: e.textContent.replace(/\s+/g, " ").trim() }));
// the page's messages pass through window.__smokeMutate (lib.mjs installs the stand-in socket); null: untouched
const setMutate = (page, src) => page.evaluate((code) => { window.__smokeMutate = code ? new Function("d", code) : null; }, src);
const reloadPhone = async ({ page, PHONE, layoutMounted }) => { await page.setViewport(PHONE); await page.reload({ waitUntil: "load" }); await layoutMounted(); };
// A page wider than its window scrolls sideways; so does any card whose content spills out of it.
const overflowing = (page) => page.evaluate(() => {
  const w = document.documentElement.clientWidth;
  const out = [];
  if (document.documentElement.scrollWidth > w) out.push(`page scrollWidth ${document.documentElement.scrollWidth} > ${w}`);
  document.querySelectorAll(".panel:not([hidden]) *, .topbar *, .tabbar *").forEach((e) => {
    if (e.closest("[hidden], .menu, canvas, .tip")) return;
    const r = e.getBoundingClientRect();
    if (r.width && (r.right > w + 1 || r.left < -1) && getComputedStyle(e).position !== "fixed") out.push(`${e.tagName.toLowerCase()}.${String(e.className).split(" ")[0]} ${Math.round(r.left)}..${Math.round(r.right)}`);
  });
  return out.slice(0, 6);
});

// Text a card cuts off without saying so: an element whose box hides overflowing text without an ellipsis, or text that
// runs out of its card. An ellipsis is a choice (the whole text is in a tooltip), so it is allowed; the header pill's
// figures must not be cut on a wide window either.
const clippedText = (page) => page.evaluate(() => {
  const out = [];
  document.querySelectorAll(".panel *").forEach((e) => {
    if (!e.offsetParent || e.closest("svg, canvas, .sr-only, [hidden], .tip, .menu, .skel")) return;
    const own = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own) return;
    const st = getComputedStyle(e);
    const name = `${e.closest("[data-panel]")?.dataset.panel}: ${e.tagName.toLowerCase()}.${String(e.className).split(" ")[0]} "${e.textContent.trim().slice(0, 30)}"`;
    if (/hidden|clip/.test(st.overflowX) && st.textOverflow !== "ellipsis" && e.scrollWidth > e.clientWidth + 1) out.push(`cut ${name}`);
    const card = e.closest(".panel").getBoundingClientRect();
    const r = e.getBoundingClientRect();
    if (r.width && (r.right > card.right + 1 || r.left < card.left - 1)) out.push(`outside its card ${name}`);
  });
  const metrics = document.querySelector("#statusPillMetrics");
  if (window.innerWidth > 1180 && metrics && metrics.scrollWidth > metrics.clientWidth + 1) out.push(`header pill figures cut: "${metrics.textContent}"`);
  return out.slice(0, 8);
});

export const steps = [
  ["page loads grey (Connecting), then the websocket connects and the favicon is served", async ({ base, eq, outDir, page, path, waitText }) => {
    await page.goto(base + "/", { waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    // the run opens the app by a non-local name (lib.mjs BROWSER_HOST), as people do: no secure context, so no Clipboard API
    eq("not a secure context, no Clipboard API", await page.evaluate(() => [window.isSecureContext, "clipboard" in navigator].join()), "false,false");
    eq("live indicator", await page.$eval("#wsStatus", (e) => e.className), "live live-live");
    const icon = await page.evaluate(async () => { const r = await fetch("/favicon.ico"); return `${r.status} ${r.headers.get("content-type")}`; });
    eq("favicon", icon, "200 image/x-icon");
    await page.screenshot({ path: path.join(outDir, "dashboard.png") });
  }, { variants: [...ALL_VARIANTS, "longpath"], fatal: true }],

  ["a subscriber that throws does not stop the others: the matrix keeps updating, a late subscriber gets the last value", async ({ moduleUrl, eq, page, text, until, waitCount }) => {
    await waitCount("#hopMatrixBody tr", 5); // the first targets message has arrived (the bus keeps it for a late subscriber)
    const seen = await page.evaluate(async (src) => {
      const { bus } = await import(src);
      window.__busCalls = 0;
      bus.subscribe("targets", () => { throw new Error("smoke-boom"); });
      bus.subscribe("targets", () => { window.__busCalls++; });
      return window.__busCalls; // the last value arrives at once, before any new message
    }, await moduleUrl("bus.js"));
    eq("sticky last value reaches a late subscriber", seen, 1);
    const before = await text("#hopMatrixBody tr:first-child [data-f=total]");
    await until((b) => document.querySelector("#hopMatrixBody tr:first-child [data-f=total]").textContent !== b, before);
    await until(() => window.__busCalls >= 3); // a subscriber after the thrower still runs on every message
  }, { variants: ALL_VARIANTS }],

  ["lan pill shows the access URL and copies it", async ({ click, eq, ok, page, text, until }) => {
    // wide enough for the chip in any browser's fonts (where it fits or moves to the menu is sign-out.mjs's header-fit step)
    const viewport = page.viewport();
    await page.setViewport({ ...viewport, width: 1700 });
    await until(() => getComputedStyle(document.getElementById("lanPill")).display !== "none");
    ok("lan url (the address, without the scheme; the link has it whole)", /^[^/]+:\d+$/.test(await text("#lanUrlLink")) && /^http:\/\/.+:\d+$/.test(await page.$eval("#lanUrlLink", (a) => a.href.replace(/\/$/, ""))));
    await click("#btnCopyLan");
    await until(() => document.getElementById("btnCopyLan").dataset.copied === "1");
    eq("clipboard", await page.evaluate(() => window.__clip()), await page.$eval("#lanUrlLink", (a) => a.title));
    await page.setViewport(viewport);
  }],

  ["desktop layout: no tab bar and every card shown", async ({ LINE, ROUTER, eq, ok, visible }) => {
    eq("bar hidden", await visible("#mobileTabs"), false);
    for (const sel of ["#diagnosisBanner", "#latencyChart", "#hopMatrixBody", LINE, ROUTER, "#servicesCard", "#incidentsBody", "#reportOutput"]) {
      ok(`${sel} shown on desktop`, await visible(sel));
    }
  }],

  ["mobile viewport (390x844): tab bar, tab switching, hash, back button, reload, tooltip stays on screen", async ({ LINE, ROUTER, click, eq, frames, ok, outDir, page, path, until, visible, waitCount }) => {
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true }); // puppeteer reloads the page for isMobile
    await waitCount("#hopMatrixBody tr", 5); // the 5 default targets (the add-target dialog's step, which adds a sixth, runs last)
    await until(() => getComputedStyle(document.getElementById("mobileTabs")).display !== "none");
    eq("bar at the bottom", await page.$eval("#mobileTabs", (e) => Math.round(e.getBoundingClientRect().bottom) === window.innerHeight), true);
    eq("four tabs", await page.$$eval("#mobileTabs [role=tab]", (b) => b.filter((x) => !x.hidden).map((x) => x.textContent).join()), "Live,Network,Line,Incidents");
    const selected = () => page.$eval("#mobileTabs [aria-selected=true]", (b) => b.dataset.arg);
    const shownCards = async (want) => {
      const all = { live: ["#diagnosisBanner", "#latencyChart", "#hopMatrixBody"], network: [ROUTER, "#servicesCard"], line: [LINE], incidents: ["#incidentsBody", "#reportOutput"] };
      for (const [tab, sels] of Object.entries(all)) {
        for (const sel of sels) eq(`${sel} on tab ${want}`, await visible(sel), tab === want);
      }
    };
    eq("default tab", await selected(), "live");
    await shownCards("live");
    for (const tab of ["network", "line", "incidents"]) {
      await click(`#mobileTabs [data-arg=${tab}]`);
      await until((t) => location.hash === `#${t}`, tab);
      eq("selected", await selected(), tab);
      await shownCards(tab);
    }
    await page.screenshot({ path: path.join(outDir, "mobile-incidents.png") });
    await page.goBack();
    await until(() => location.hash === "#line");
    eq("back selects line", await selected(), "line");
    await shownCards("line");
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelector("#mobileTabs [aria-selected=true]")?.dataset.arg === "line");
    await shownCards("line");
    await click("#mobileTabs [data-arg=live]");
    await until(() => location.hash === "#live");
    await page.evaluate(() => document.querySelector('[data-panel=matrix] .info[data-help="hop.latency"]').scrollIntoView({ block: "center" }));
    await frames(3); // let the scroll events pass before opening
    await click('[data-panel=matrix] .info[data-help="hop.latency"]');
    await until(() => !document.getElementById("helpPopover").hidden);
    ok("tooltip inside the viewport", await page.$eval("#helpPopover", (e) => { const r = e.getBoundingClientRect(); return r.left >= 0 && r.right <= window.innerWidth && r.top >= 0 && r.bottom <= window.innerHeight; }));
    await page.keyboard.press("Escape");
    await page.setViewport({ width: 1400, height: 1000 });
    await until(() => getComputedStyle(document.getElementById("mobileTabs")).display === "none");
    for (const sel of ["#diagnosisBanner", LINE, ROUTER, "#servicesCard", "#incidentsBody"]) ok(`${sel} shown again on desktop`, await visible(sel));
  }],

  ["phone (390 px): no sideways scrolling on any tab, dark and light", async ({ bare, PHONE, SHOTS, click, eq, layoutSettled, page, shot, until, waitCount }) => {
    await page.setViewport(PHONE);
    await waitCount("#hopMatrixBody tr", 5);
    await until(() => getComputedStyle(document.getElementById("mobileTabs")).display !== "none");
    const TABS = bare ? ["live", "network", "incidents"] : ["live", "network", "line", "incidents"];
    const wide = [];
    for (const scheme of ["dark", "light"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      for (const tab of TABS) {
        await click(`#mobileTabs [data-arg=${tab}]`);
        await until((t) => location.hash === `#${t}`, tab);
        await layoutSettled(); // charts and tables settle
        const over = (await overflowing(page)).join("; ");
        if (over) wide.push(`${tab} (${scheme}): ${over}`);
        if (SHOTS) await shot(`phone-${tab}-${scheme}`, { fullPage: true });
      }
    }
    eq("sideways overflow", wide.join(" | "), "");
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
    await click("#mobileTabs [data-arg=live]");
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["theme: toggle switches light and dark, is stored, survives a reload and beats the OS setting", async ({ SHOTS, click, eq, ok, page, shot, waitCount }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
    await page.evaluate(() => localStorage.removeItem("pengumon.theme"));
    await page.reload({ waitUntil: "load" });
    ok("light by the OS setting", lum(await bgOf(page)) > 200);
    await click("#btnTheme");
    eq("dark chosen", await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    ok("dark background", lum(await bgOf(page)) < 60);
    eq("stored", await page.evaluate(() => localStorage.getItem("pengumon.theme")), "dark");
    await page.reload({ waitUntil: "load" });
    ok("still dark after reload (OS says light)", lum(await bgOf(page)) < 60);
    ok("the button names the next theme", /light/i.test(await page.$eval("#btnTheme", (b) => b.getAttribute("aria-label"))));
    if (SHOTS) await shot("desktop-dark");
    await click("#btnTheme");
    eq("light chosen", await page.evaluate(() => localStorage.getItem("pengumon.theme")), "light");
    await page.reload({ waitUntil: "load" });
    ok("light after reload", lum(await bgOf(page)) > 200);
    await waitCount("#hopMatrixBody tr", 5);
    if (SHOTS) await shot("desktop-light", { fullPage: true });
    await page.evaluate(() => localStorage.removeItem("pengumon.theme"));
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["loading: before any data the pill and the verdict are grey 'Connecting', the path is skeletons, nothing green", async ({ layoutMounted, PHONE, SHOTS, count, eq, ok, page, shot, text }) => {
    await page.evaluate(() => sessionStorage.setItem("smokeWs", "dead"));
    await reloadPhone({ page, PHONE, layoutMounted });
    await new Promise((r) => setTimeout(r, 800));
    const p = await pill(page);
    ok(`pill grey: ${JSON.stringify(p)}`, p.cls.includes("st-unknown") && /Connecting/.test(p.text));
    eq("verdict badge", (await text("#statusBadge")).trim(), "Connecting");
    ok("skeleton nodes, no live nodes", (await count("#pathChain .node.st-unknown")) >= 3 && (await count("#pathChain .node.st-good:not(:first-child)")) <= 1);
    eq("no green tab dot or page tint", await page.evaluate(() => document.body.dataset.status), "unknown");
    if (SHOTS) await shot("state-loading", { fullPage: false });
  }],

  ["disconnected: the socket never opens, so a banner says the connection is lost and the pill stays grey", async ({ SHOTS, eq, ok, page, shot, text, until }) => {
    await until(() => !document.getElementById("connBanner").hidden);
    ok("reconnecting text", /Reconnecting/.test(await text("#wsText")));
    eq("body marks it", await page.evaluate(() => document.body.dataset.conn), "lost");
    ok("cards dimmed", await page.$eval(".page .panel-b", (e) => Number(getComputedStyle(e).opacity) < 0.9));
    if (SHOTS) await shot("state-disconnected");
    await page.evaluate(() => sessionStorage.removeItem("smokeWs"));
  }],

  ["connected again: the banner goes and the verdict turns from grey to a status", async ({ layoutMounted, PHONE, page, until }) => {
    await reloadPhone({ page, PHONE, layoutMounted });
    await until(() => document.getElementById("connBanner").hidden && document.body.dataset.conn === "open");
    await until(() => !document.getElementById("statusPill").className.includes("st-unknown"));
  }],

  ["stale reading: a line read 2 h ago (poll every 30 min) is stale; one read 10 min ago is not; a router 5 min old is", async ({ LINE, ROUTER, SHOTS, click, eq, layoutMounted, ok, page, shot, text, until }) => {
    // the keys of the real message, for the next steps
    const keys = await page.evaluate(() => new Promise((res) => { window.__smokeMutate = (d) => { res(Object.keys(d)); window.__smokeMutate = null; }; }));
    ok("message has devices and diagnosis", keys.includes("devices") && keys.includes("diagnosis"));
    const modem = (age) => `if (d.devices && d.devices.modem) d.devices.modem.timestamp = Date.now() / 1000 - ${age};`;
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await setMutate(page, modem(600));
    await until((s) => document.querySelector(s + " [data-f=read]")?.textContent.startsWith("read "), `${LINE}`);
    eq("10 min old at a 30 min poll: fresh", await page.$eval(LINE, (c) => c.classList.contains("is-stale")), false);
    await setMutate(page, modem(7200));
    await until((s) => document.querySelector(s)?.classList.contains("is-stale"), LINE);
    ok("reading N old chip", /reading 2\.0 h old/.test(await text(`${LINE} [data-f=stale]`)));
    ok("card dimmed", await page.$eval(`${LINE} .panel-b`, (e) => Number(getComputedStyle(e).opacity) < 0.9));
    await setMutate(page, `if (d.devices && d.devices.router) d.devices.router.timestamp = Date.now() / 1000 - 300; ${modem(7200)}`);
    await until((s) => document.querySelector(s)?.classList.contains("is-stale"), ROUTER);
    if (SHOTS) { await click("#mobileTabs [data-arg=line]"); await new Promise((r) => setTimeout(r, 400)); await shot("state-stale"); }
  }],

  ["device error: unreachable and sign-in refused say what happened and offer the fix", async ({ LINE, SHOTS, click, ok, page, shot, text, until }) => {
    await setMutate(page, `if (d.devices && d.devices.modem) { Object.assign(d.devices.modem, { status: "error", error_message: "The modem did not answer on 192.168.99.1. Check that it is on." }); }`);
    await until((s) => !document.querySelector(s + " [data-f=error]")?.hidden, LINE);
    const err = await text(`${LINE} [data-f=error]`);
    ok(`error text: ${err}`, /No reading from/.test(err) && /did not answer/.test(err));
    ok("fix button", /Check the sign-in/.test(err));
    ok("badge says No reading", /No reading/.test(await text(`${LINE} [data-f=badge]`)));
    ok("last body dimmed", await page.$eval(`${LINE} [data-f=body]`, (e) => e.classList.contains("dim")));
    if (SHOTS) { await click("#mobileTabs [data-arg=line]"); await new Promise((r) => setTimeout(r, 400)); await shot("state-deverror"); }
    await setMutate(page, `if (d.devices && d.devices.modem) Object.assign(d.devices.modem, { status: "unconfigured", error_message: null });`);
    await until((s) => /Not set up yet/.test(document.querySelector(s + " [data-f=error]")?.textContent ?? ""), LINE);
  }],

  ["outage and busy: header pill, page tint, hero and the tab dot follow the verdict, each with an icon and a word", async ({ SHOTS, click, count, eq, ok, page, shot, text, until }) => {
    await setMutate(page, `d.diagnosis = Object.assign({}, d.diagnosis, { status: "down", title: "Internet is down", description: "No probe gets past the ISP gateway.", load_pct: null });`);
    await until(() => document.body.dataset.status === "crit");
    ok("pill red with the title", /Internet is down/.test((await pill(page)).text) && (await pill(page)).cls.includes("st-crit"));
    eq("hero badge word", (await text("#statusBadge")).trim(), "Down");
    ok("hero badge has an icon", (await count("#statusBadge svg")) === 1);
    await until(() => !!document.querySelector("#mobileTabs [data-arg=live] .tab-dot.st-crit"));
    ok("tab has an accessible name", /Live: problem/.test(await page.$eval("#mobileTabs [data-arg=live]", (b) => b.getAttribute("aria-label"))));
    if (SHOTS) { await click("#mobileTabs [data-arg=live]"); await new Promise((r) => setTimeout(r, 400)); await shot("state-outage"); }
    await setMutate(page, `d.diagnosis = Object.assign({}, d.diagnosis, { status: "busy", title: "Line busy", description: "Heavy use.", load_pct: 96 });`);
    await until(() => document.body.dataset.status === "busy");
    ok("pill violet", (await pill(page)).cls.includes("st-busy"));
    eq("hero badge word", (await text("#statusBadge")).trim(), "Busy");
    if (SHOTS) await shot("state-busy");
    await setMutate(page, null);
    await until(() => document.body.dataset.status === "good");
  }],

  ["keyboard focus is visible and reduced motion stops the animations", async ({ eq, ok, page }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    const ring = await page.evaluate(() => { const s = getComputedStyle(document.activeElement); return `${s.outlineStyle} ${s.outlineWidth}`; });
    ok(`focus ring: ${ring}`, /solid [1-9]/.test(ring));
    await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
    const anim = await page.$eval("#statusPill .dot", (e) => getComputedStyle(e).animationName);
    eq("pill dot not animated", anim, "none");
    await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }, { name: "prefers-color-scheme", value: "light" }]);
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["info buttons are marked up all over the page (the help popovers themselves are tested in each panel)", async ({ ok, page }) => {
    ok("info buttons are all over the page", await page.$$eval(".info[data-help]", (bs) => bs.length > 15));
  }],

  ["without devices: no device cards, sparklines and the history modal still work", async ({ click, count, eq, until }) => {
    eq("no line or router cards", await count("[data-panel=line] .panel, [data-panel=router] .panel"), 0);
    eq("no nodes built", await count("#topologyBody .tn"), 0);
    await click("#hopMatrixBody tr:first-child");
    await until(() => (Chart.getChart(document.getElementById("targetHistLatency"))?.data.datasets[0]?.data.length ?? 0) > 5);
    await click("[data-action=closeModal][data-arg=targetHistModal]");
  }, { variants: ["bare"] }],

  ["no text is cut or runs out of its card at 1440, 1024 and phone width (every tab)", async ({ PHONE, eq, layoutMounted, layoutSettled, page, until }) => {
    await until(() => /ms/.test(document.getElementById("statusPillMetrics")?.textContent ?? ""));
    for (const w of [1440, 1024]) {
      await page.setViewport({ width: w, height: 900 });
      await layoutSettled();
      eq(`${w}: cut text`, (await clippedText(page)).join(" | "), "");
    }
    await page.setViewport(PHONE);
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await layoutSettled();
    const tabs = await page.$$eval(".tabbar button:not([hidden])", (b) => b.map((x) => x.dataset.arg ?? x.textContent.trim()));
    for (let i = 0; i < tabs.length; i++) {
      await page.evaluate((k) => document.querySelectorAll(".tabbar button:not([hidden])")[k].click(), i);
      await layoutSettled();
      eq(`phone, tab ${tabs[i]}: cut text`, (await clippedText(page)).join(" | "), "");
    }
    await page.evaluate(() => document.querySelector(".tabbar button:not([hidden])").click()); // back to the first tab (the hash outlives a reload)
    await page.setViewport({ width: 1400, height: 1000 });
  }, { variants: ["full", "longpath"] }],

  ["colours: a target's colour and the design tokens are read once (not by getComputedStyle on every use) and follow both themes live", async ({ eq, ok, page, setTheme, until, waitCount }) => {
    await waitCount("#pathChain .node[data-node]", 3);
    const probe = () => page.evaluate(async () => {
      const find = (f) => performance.getEntriesByType("resource").map((r) => r.name).find((n) => n.endsWith(f));
      const util = await import(find("/js/util.js"));
      const theme = await import(find("/js/chart-theme.js"));
      const id = util.canonicalTargetOrder[0];
      const real = window.getComputedStyle;
      let calls = 0;
      window.getComputedStyle = (...a) => { calls++; return real.apply(window, a); };
      let colour; let crit;
      try { for (let i = 0; i < 30; i++) { colour = util.getTargetInfo(id).color; crit = theme.token("--crit"); } } finally { window.getComputedStyle = real; }
      const root = real(document.documentElement);
      return { calls, colour, crit, wantCrit: root.getPropertyValue("--crit").trim(), want: [1, 2, 3, 4, 5, 6, 7, 8].map((k) => root.getPropertyValue(`--s${k}`).trim()) };
    });
    const seen = {};
    for (const theme of ["dark", "light", "dark"]) {
      await setTheme(theme);
      const p = await probe();
      ok(`${theme}: 60 reads cost at most 2 style lookups (got ${p.calls})`, p.calls <= 2);
      ok(`${theme}: the target colour is one of the theme's series colours (${p.colour})`, p.want.includes(p.colour));
      eq(`${theme}: the crit token follows the theme`, p.crit, p.wantCrit);
      if (seen[theme]) eq(`${theme}: the same on the way back`, p.colour + p.crit, seen[theme]);
      seen[theme] = p.colour + p.crit;
    }
    ok("the two themes really differ", seen.dark !== seen.light);
    await setTheme(null);
  }],

  ["a hidden tab publishes only the diagnosis; the newest message comes once when it shows, and the live chart refills", async ({ eq, ok, page, until, waitCount }) => {
    await waitCount("#pathChain .node[data-node]", 3);
    await page.evaluate(async () => {
      const bus = (await import(performance.getEntriesByType("resource").map((r) => r.name).find((n) => n.endsWith("/js/bus.js")))).bus;
      window.__seen = { message: 0, targets: 0, diagnosis: 0 };
      Object.keys(window.__seen).forEach((t) => bus.subscribe(t, () => { window.__seen[t]++; }));
      Object.keys(window.__seen).forEach((t) => { window.__seen[t] = 0; }); // subscribing replays the last value
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await setMutate(page, `d.diagnosis.title = "Hidden " + Math.floor(d.timestamp)`); // the diagnosis changes every message; targets always do
    await new Promise((r) => setTimeout(r, 4500));
    const hidden = await page.evaluate(() => ({ ...window.__seen, history: performance.getEntriesByType("resource").filter((r) => /\/api\/history\?/.test(r.name)).length }));
    eq("hidden: no message published", hidden.message, 0);
    eq("hidden: no targets published", hidden.targets, 0);
    ok(`hidden: the diagnosis still is (${hidden.diagnosis}), as alerts need it`, hidden.diagnosis >= 3);
    await setMutate(page, null);
    const shown = await page.evaluate(() => {
      delete document.visibilityState; delete document.hidden;
      document.dispatchEvent(new Event("visibilitychange"));
      return { ...window.__seen };
    });
    eq("shown: the newest message is published once", shown.message, 1);
    eq("shown: and its targets", shown.targets, 1);
    await until((h) => performance.getEntriesByType("resource").filter((r) => /\/api\/history\?/.test(r.name)).length > h, hidden.history);
    await until(() => window.__seen.message >= 3); // and the feed goes on
  }],

  ["dialogs load when first opened: only the alerts dialog at start, a control the pointer reaches loads its dialog ahead of the click, and #settings opens on load", async ({ click, eq, ok, page, layoutMounted, until }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    const loaded = () => page.evaluate(() => performance.getEntriesByType("resource").map((r) => r.name.match(/\/dialogs\/([\w-]+)\/dialog\.js$/)?.[1]).filter(Boolean).sort().join());
    eq("at start", await loaded(), "alerts");
    await page.hover("#btnSettings");
    await until(() => performance.getEntriesByType("resource").some((r) => /\/dialogs\/settings\/dialog\.js$/.test(r.name)));
    ok("the pointer loaded only that dialog", (await loaded()) === "alerts,settings");
    await click("#btnSettings");
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
    await click("[data-action=settingsClose]");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
    // a click with no hover before it (a keyboard or touch user): the dialog loads, then opens and takes the focus
    await click('[data-action=openModal][data-arg=tracerouteModal]');
    await until(() => document.getElementById("tracerouteModal").classList.contains("active") && document.activeElement?.id === "traceTargetInput");
    ok("the trace dialog was loaded by that click", (await loaded()).split(",").includes("trace"));
    await click("[data-action=closeModal][data-arg=tracerouteModal]");
    // a link to #settings opens the page's Settings on load
    await page.evaluate(() => { location.hash = "settings"; });
    await page.reload({ waitUntil: "load" });
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
    await click("[data-action=settingsClose]");
    await until(() => !document.getElementById("settingsModal").classList.contains("active") && location.hash === "");
  }],
];
