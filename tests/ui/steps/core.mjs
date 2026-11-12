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
  ["page loads gray (Connecting), then the websocket connects and the favicon is served", async ({ base, eq, outDir, page, path, waitText }) => {
    await page.goto(base + "/", { waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    // the run opens the app by a non-local name (lib.mjs BROWSER_HOST), as people do: no secure context, so no Clipboard API
    eq("not a secure context, no Clipboard API", await page.evaluate(() => [window.isSecureContext, "clipboard" in navigator].join()), "false,false");
    eq("live indicator", await page.$eval("#wsStatus", (e) => e.className), "live live-live");
    const icon = await page.evaluate(async () => { const r = await fetch("/favicon.ico"); return `${r.status} ${r.headers.get("content-type")}`; });
    eq("favicon", icon, "200 image/x-icon");
    await page.screenshot({ path: path.join(outDir, "dashboard.png") });
  }, { variants: [...ALL_VARIANTS, "longpath"], fatal: true }],

  ["a subscriber that throws does not stop the others: the matrix keeps updating, a late subscriber gets the last value", async ({ moduleUrl, eq, page, showCard, text, until, waitCount }) => {
    await showCard("matrix"); // Hop details is not drawn while it is off screen
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
    await page.evaluate(() => window.scrollTo(0, 0)); // the later steps start at the page's top
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
    for (const sel of ["#diagnosisBanner", "#latencyChart", "#hopMatrixBody", LINE, ROUTER, "#trafficCard", "#servicesCard", "#incidentsBody", "#reportOutput"]) {
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
      const all = { live: ["#diagnosisBanner", "#latencyChart", "#hopMatrixBody"], network: [ROUTER, "#trafficCard", "#servicesCard"], line: [LINE], incidents: ["#incidentsBody", "#reportOutput"] };
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

  ["theme menu: light and dark are picked there, kept, survive a reload and beat the OS setting", async ({ SHOTS, click, eq, ok, page, pickMode, shot, until, waitCount }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
    await page.evaluate(() => localStorage.removeItem("pengumon.theme"));
    await page.reload({ waitUntil: "load" });
    ok("light by the OS setting", lum(await bgOf(page)) > 200);
    await pickMode("dark");
    eq("dark chosen", await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    ok("dark background", lum(await bgOf(page)) < 60);
    eq("stored", await page.evaluate(() => localStorage.getItem("pengumon.theme")), "dark");
    await page.reload({ waitUntil: "load" });
    ok("still dark after reload (OS says light)", lum(await bgOf(page)) < 60);
    await click("#btnTheme");
    await until(() => !document.getElementById("themeMenu").hidden);
    eq("the menu marks Dark", await page.$eval("#themeMenu [data-action=pickMode][data-arg=dark]", (b) => b.getAttribute("aria-checked")), "true");
    eq("and the default theme", await page.$eval('#themeMenu [data-action=pickLook][data-arg=""]', (b) => b.getAttribute("aria-checked")), "true");
    await page.keyboard.press("Escape");
    if (SHOTS) await shot("desktop-dark");
    await pickMode("light");
    eq("light chosen", await page.evaluate(() => localStorage.getItem("pengumon.theme")), "light");
    await page.reload({ waitUntil: "load" });
    ok("light after reload", lum(await bgOf(page)) > 200);
    await waitCount("#hopMatrixBody tr", 5);
    if (SHOTS) await shot("desktop-light", { fullPage: true });
    await page.evaluate(() => localStorage.removeItem("pengumon.theme"));
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["themes: a theme picked in the menu applies at once (charts too), is kept, and is on the page before its first paint; only its stylesheet is loaded", async ({ click, eq, ok, page, pickLook, until }) => {
    // the theme <html> carries when <body> starts, and its colors then: what the first paint shows (js/theme-boot.js
    // runs in <head> and links the theme's stylesheet there)
    const { identifier } = await page.evaluateOnNewDocument(() => {
      new MutationObserver((_, obs) => {
        if (!document.body) return;
        window.__lookAtBody = `${document.documentElement.dataset.look}/${document.documentElement.dataset.theme || ""}`;
        window.__bgAtBody = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
        obs.disconnect();
      }).observe(document, { childList: true, subtree: true });
    });
    const linked = () => page.$$eval("link[data-look-css]", (ls) => ls.map((l) => l.dataset.lookCss).join());
    eq("the default theme links no theme stylesheet", await linked(), "");
    // the colors at the moment the page changes theme: the new theme's stylesheet has loaded first (no flash)
    await page.evaluate(() => new MutationObserver(() => {
      window.__bgAtSwitch = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-look"] }));
    await pickLook("nord");
    eq("Nord on the page", await page.evaluate(() => document.documentElement.dataset.look), "nord");
    const nordBg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg").trim());
    ok(`Nord's background (${nordBg})`, ["#242933", "#e5e9f0"].includes(nordBg));
    eq("Nord's colors were there when the page changed to it", await page.evaluate(() => window.__bgAtSwitch), nordBg);
    eq("only Nord's stylesheet is linked", await linked(), "nord");
    eq("the charts' grid follows", await page.evaluate(() => Chart.defaults.scale.grid.color), await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--line").trim()));
    eq("kept in this browser", await page.evaluate(() => localStorage.getItem("pengumon.look")), "nord");
    await page.reload({ waitUntil: "load" });
    eq("on <html> before the body is drawn", (await page.evaluate(() => window.__lookAtBody)).split("/")[0], "nord");
    eq("with its colors", await page.evaluate(() => window.__bgAtBody), nordBg);
    eq("and only its stylesheet", await linked(), "nord");
    // a theme with one look forces it, and the menu says why light and dark cannot be picked
    await pickLook("digital-rain");
    eq("digital rain is dark", await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    eq("its stylesheet instead of Nord's", await linked(), "digital-rain");
    await click("#btnTheme");
    await until(() => !document.getElementById("themeMenu").hidden);
    ok("light and dark are off", await page.$$eval("#themeMenu [data-action=pickMode]", (bs) => bs.every((b) => b.disabled)));
    ok("and the menu says why", /dark look only/.test(await page.$eval("#themeMenu", (m) => m.textContent)));
    await page.keyboard.press("Escape");
    await page.reload({ waitUntil: "load" });
    eq("forced before the first paint too", await page.evaluate(() => window.__lookAtBody), "digital-rain/dark");
    await pickLook("");
    eq("back to the default", await page.evaluate(() => [document.documentElement.dataset.look, localStorage.getItem("pengumon.look")].join()), "default,");
    eq("with no theme stylesheet", await linked(), "");
    await page.removeScriptToEvaluateOnNewDocument(identifier);
  }],

  ["effects: digital rain falls, stops while the tab is hidden and with reduced motion, and the menu turns it off", async ({ click, eq, ok, page, pickLook, sleep, until }) => {
    const frame = () => page.evaluate(() => document.querySelector("#fxLayer canvas")?.toDataURL() ?? "");
    // the rain draws a few frames a second: wait up to 5 s for a frame other than `from` (a slow runner gets its time)
    const moves = async (from) => { for (let i = 0; i < 25; i++) { if ((await frame()) !== from) return true; await sleep(200); } return false; };
    await pickLook("digital-rain");
    await until(() => document.getElementById("fxLayer").dataset.state === "running" && document.querySelector("#fxLayer canvas"));
    ok("it moves", await moves(await frame()));
    // hidden: the browser reports the tab hidden (as when another tab is in front)
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    eq("paused while hidden", await page.evaluate(() => document.getElementById("fxLayer").dataset.state), "paused");
    await sleep(150); // a frame already on its way
    const b = await frame();
    await sleep(600);
    ok("nothing drawn while hidden", (await frame()) === b);
    await page.evaluate(() => {
      delete document.hidden;
      delete document.visibilityState;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await until(() => document.getElementById("fxLayer").dataset.state === "running");
    ok("and again once visible", await moves(b));
    await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
    await until(() => !document.querySelector("#fxLayer canvas"));
    await page.reload({ waitUntil: "load" });
    await sleep(800);
    eq("never started with reduced motion", await page.evaluate(() => document.getElementById("fxLayer").childElementCount), 0);
    await click("#btnTheme");
    await until(() => !document.getElementById("themeMenu").hidden);
    ok("the menu says why", /reduced motion/.test(await page.$eval("#themeMenu", (m) => m.textContent)));
    await page.keyboard.press("Escape");
    await page.emulateMediaFeatures([]);
    await until(() => document.querySelector("#fxLayer canvas"));
    await click("#btnTheme");
    await until(() => !document.getElementById("themeMenu").hidden);
    await click("#themeMenu [data-action=toggleEffects]");
    await until(() => !document.querySelector("#fxLayer canvas"));
    eq("off for this browser", await page.evaluate(() => localStorage.getItem("pengumon.effects")), "off");
    await pickLook(""); // the steps after this one start from the default theme
    await page.evaluate(() => localStorage.removeItem("pengumon.effects"));
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["an effect whose start() throws is stopped and does not break theme switching", async ({ eq, moduleUrl, ok, page, pickLook, sleep, until }) => {
    // The rain effect may be in the browser's memory cache already (an earlier step ran it), which request interception does
    // not see; so the theme is pointed at an address of its own, and that one is answered with an effect that throws.
    await page.evaluate(async (themes) => { const { lookInfo } = await import(themes); lookInfo("digital-rain").effect += "?smoke-effect-boom"; }, await moduleUrl("themes.js"));
    await page.setRequestInterception(true);
    const handler = (req) => {
      if (/\/themes\/digital-rain\/effect\.js\?smoke-effect-boom$/.test(req.url())) {
        req.respond({ status: 200, contentType: "text/javascript", headers: { "access-control-allow-origin": "*" },
          body: "export default { start(layer, ctx) { ctx.onFrame(() => { window.__fxFrames = (window.__fxFrames || 0) + 1; }); layer.appendChild(document.createElement('canvas')); layer.dataset.touched = '1'; throw new Error('smoke-effect-boom'); } };" });
      } else req.continue();
    };
    page.on("request", handler);
    try {
      await pickLook("digital-rain");
      await until(() => performance.getEntriesByType("resource").some((r) => /smoke-effect-boom/.test(r.name)));
      await until(() => document.getElementById("fxLayer").dataset.touched === "1"); // start() ran (and threw)
      await sleep(700); // a frame callback it left behind would be drawing by now
      eq("what it set going was stopped", await page.evaluate(() => window.__fxFrames ?? 0), 0);
      eq("the layer is clean", await page.evaluate(() => `${document.getElementById("fxLayer").childElementCount}|${document.getElementById("fxLayer").dataset.fx ?? ""}`), "0|");
      await pickLook("amber-crt");
      eq("another theme can be picked", await page.evaluate(() => document.documentElement.dataset.look), "amber-crt");
      await pickLook("");
      ok("and the default", await page.evaluate(() => document.documentElement.dataset.look === document.documentElement.dataset.lookDefault));
    } finally {
      page.off("request", handler);
      await page.setRequestInterception(false);
      await pickLook("");
      await page.evaluate(async (themes) => { const { lookInfo } = await import(themes); const l = lookInfo("digital-rain"); l.effect = l.effect.replace("?smoke-effect-boom", ""); }, await moduleUrl("themes.js"));
    }
  }, { chromiumOnly: "the effect module is swapped through request interception" }],

  ["card views: a switch between two themes that draw cards their own way shows the new theme's views from the moment the page takes the new theme (never the old view without its styles)", async ({ eq, page, pickLook, stubFetch, until }) => {
    // a fresh page, so the second theme's views have not been loaded by anything before the switch
    await page.evaluate(() => localStorage.removeItem("pengumon.look"));
    await stubFetch(null);
    await pickLook("amber-crt");
    await until(() => ["diagnosis", "latency"].every((p) => document.querySelector(`[data-panel=${p}] .panel-b`).dataset.view === "amber-crt"));
    await page.evaluate(() => {
      window.__switched = null;
      document.addEventListener("themechange", () => {
        if (window.__switched || document.documentElement.dataset.look !== "synthwave") return;
        // after the cards' own handling of this change (its promise jobs), before the page is painted again
        queueMicrotask(() => { window.__switched = ["diagnosis", "latency"].map((p) => document.querySelector(`[data-panel=${p}] .panel-b`).dataset.view).join(); });
      });
    });
    await pickLook("synthwave");
    await until(() => window.__switched);
    eq("the cards' views as the page took the new theme", await page.evaluate(() => window.__switched), "synthwave,synthwave");
    await pickLook("");
  }],

  ["card views: every theme's view of a card shows every value, status, action and help of the card's standard view (each card's fixture)", async ({ eq, moduleUrl, ok, page }) => {
    // docs/themes.md, "Drawing a card": the standard view and each theme view drawn apart from the page from the same view
    // model (the card's fixture); a theme view that lacks a mark of the standard one fails here
    const found = await page.evaluate(async (u, t) => {
      const { compareMarks, drawApart, registeredCards } = await import(u);
      const { loadView } = await import(t);
      const cards = registeredCards();
      const out = { checked: [], problems: [], untested: [], standard: {} };
      // a card may add fixture.variants (functions): other made-up view models of the same card, e.g. a device with the
      // fewest capabilities; each is drawn by the standard view and by every theme view like the main one
      const fixtures = (c) => [c.fixture, ...(c.fixture.variants || [])];
      cards.filter((c) => c.fixture).forEach((c) => {
        const ours = drawApart(c.panel, c.standard, c.fixture());
        out.standard[c.panel] = { values: Object.keys(ours.marks.values).length, actions: ours.marks.actions.length, status: Object.keys(ours.marks.status).length };
        ours.remove();
        (c.fixture.variants || []).forEach((variant, i) => {
          const minimal = drawApart(c.panel, c.standard, variant());
          out.standard[`${c.panel}#${i + 1}`] = { values: Object.keys(minimal.marks.values).length, actions: minimal.marks.actions.length, status: Object.keys(minimal.marks.status).length };
          minimal.remove();
        });
      });
      for (const look of JSON.parse(document.documentElement.dataset.looks)) {
        for (const [panel, url] of Object.entries(look.views || {})) {
          const card = cards.find((c) => c.panel === panel && c.fixture);
          if (!card) { out.untested.push(`${look.id}/${panel}`); continue; }
          const view = await loadView(url);
          if (!view) { out.problems.push(`${look.id}/${panel}: could not be loaded`); continue; }
          for (const [n, make] of fixtures(card).entries()) {
            const vm = make();
            const ours = drawApart(panel, card.standard, vm);
            try {
              const theirs = drawApart(panel, view, vm, { look: look.id });
              out.problems.push(...compareMarks(ours.marks, theirs.marks).map((p) => `${look.id}/${panel}${n ? `#${n}` : ''}: ${p}`));
              theirs.remove();
              out.checked.push(`${look.id}/${panel}${n ? `#${n}` : ''}`);
            } catch (e) {
              out.problems.push(`${look.id}/${panel}${n ? `#${n}` : ''}: threw ${e.message}`);
            } finally {
              ours.remove();
            }
          }
        }
      }
      return out;
    }, await moduleUrl("cards.js"), await moduleUrl("themes.js"));
    eq("what the theme views lack", found.problems.join("; "), "");
    eq("theme views of a card without a model fixture", found.untested.join(", "), "");
    ok(`Amber CRT's incidents view is checked (${found.checked})`, found.checked.includes("amber-crt/incidents"));
    // a fixture that draws nothing would let any view pass: each standard view shows values, statuses and actions
    ok(`the reference cards have fixtures (${Object.keys(found.standard)})`, ["diagnosis", "services", "latency"].every((p) => p in found.standard));
    ok(`the device cards have fixtures, each with a minimal-driver variant (${Object.keys(found.standard)})`, ["router", "router#1", "line", "line#1"].every((p) => p in found.standard));
    // (the reliability report has no control in its body: its range buttons and the dossier button are the frame's)
    for (const [panel, n] of Object.entries(found.standard)) {
      if (panel.includes("#")) ok(`${panel}: the minimal fixture draws a mark (${JSON.stringify(n)})`, n.values >= 1);
      else ok(`${panel}: its fixture draws marks (${JSON.stringify(n)}; cards ${Object.keys(found.standard)})`, n.values >= 3 && (n.actions >= 1 || panel === "report"));
    }
  }],

  ["card views: after Settings has read the theme list again (a Save), a theme picked in the header menu still draws its own views and chart style", async ({ moduleUrl, page, pickLook, until }) => {
    await page.evaluate(async (u) => { await (await import(u)).refreshThemes(); }, await moduleUrl("themes.js"));
    await pickLook("amber-crt");
    await until(() => document.querySelector("[data-panel=incidents] .panel-b").dataset.view === "amber-crt");
    await until(() => document.querySelector('link[data-look-css="amber-crt"]')?.sheet); // its stylesheet too
    // the latency card's line chart, found in the card's body (docs/themes.md, "Finding a card's parts"): Amber CRT's steps
    await until(() => [...document.querySelectorAll("[data-panel=latency] .panel-b canvas")].map((c) => Chart.getChart(c)).find((c) => c?.config.type === "line")
      ?.data.datasets.find((x) => x.id === "path_router")?.stepped === "middle");
    await pickLook("");
    await until(() => document.querySelector("[data-panel=incidents] .panel-b").dataset.view === "standard");
  }],

  ["themes: a Save that changes nothing about themes, and a layout change that keeps the cards' colors, redraw nothing", async ({ eq, moduleUrl, page }) => {
    const redraws = await page.evaluate(async (u) => {
      const { refreshThemes, setCardColors } = await import(u);
      let n = 0;
      const count = () => { n++; };
      document.addEventListener("themechange", count);
      const colors = { latency: { custom: { "--accent": "#336699" } } };
      setCardColors(colors); // new colors: the cards are painted and the charts drawn again
      const changed = n;
      setCardColors(JSON.parse(JSON.stringify(colors))); // the same colors (layout.js calls this on every layout change)
      const same = n - changed;
      setCardColors({});
      n = 0;
      await refreshThemes(); // what Settings does after a Save; nothing about themes changed
      const refreshed = n;
      document.removeEventListener("themechange", count);
      return { changed, same, refreshed };
    }, await moduleUrl("themes.js"));
    eq("redraws for new colors, the same colors, a Save without theme changes", `${redraws.changed},${redraws.same},${redraws.refreshed}`, "1,0,0");
  }],

  ["card views: a theme switch swaps a card's view without a reload, keeping focus and the model's state; live data patches it; a restyled card (Starship) keeps the standard view and its live patching; the chart style reshapes the charts; a view that throws gives way to the standard one", async ({ ROUTER, click, eq, moduleUrl, ok, page, phone, until, waitText }) => {
    const card = "[data-panel=incidents]";
    await until((c) => document.querySelectorAll(`${c} #incidentsBody tr.incident-row`).length >= 2, card);
    // the model's state: the latency card's Each target, chosen in the standard view
    await click("[data-panel=latency] .panel-b [data-action=setLatencyView][data-arg=targets]");
    const themes = await moduleUrl("themes.js");
    // focus on an incident's title, then the theme changes (as from another tab or Settings: the header menu would take the focus)
    const first = await page.evaluate(async (c, u) => {
      const button = document.querySelector(`${c} #incidentsBody tr.incident-row button.primary`);
      button.focus();
      (await import(u)).setLook("amber-crt");
      return button.dataset.arg;
    }, card, themes);
    await until((c) => document.querySelector(`${c} .panel-b`).dataset.view === "amber-crt", card);
    eq("the focus stays on the same action", await page.evaluate(() => `${document.activeElement.className}|${document.activeElement.dataset.action}|${document.activeElement.dataset.arg}`), `ac-what|inspectIncident|${first}`);
    await until(() => document.querySelector("[data-panel=latency] .panel-b").dataset.view === "amber-crt");
    eq("the model's line mode stays", await page.$eval("[data-panel=latency] .panel-b", (b) => b.querySelector("[data-action=setLatencyView][aria-pressed=true]")?.dataset.arg), "targets");
    // live data: a redraw keeps an unchanged line as it is (the same element), and draws a changed one again
    const kept = await page.evaluate(async (c, u) => {
      const { updateIncidentsTable } = await import(u);
      const lines = [...document.querySelectorAll(`${c} .ac-ln`)];
      lines[1].__kept = true;
      const list = await (await fetch("/api/incidents?limit=500")).json();
      const changed = list.map((i) => (String(i.id) === lines[0].dataset.item ? { ...i, title: "Renamed incident" } : i));
      updateIncidentsTable(changed);
      const now = [...document.querySelectorAll(`${c} .ac-ln`)];
      const out = { same: now[1].__kept === true, redrawn: now[0].__kept === undefined && now[0].textContent.includes("Renamed incident") };
      updateIncidentsTable(list);
      return out;
    }, card, await moduleUrl("../panels/incidents/model.js"));
    eq("the unchanged line is the same element", kept.same, true);
    eq("the changed line is drawn again", kept.redrawn, true);
    await phone(async () => {
      await click("#mobileTabs [data-arg=incidents]"); // the incidents card's tab
      await until((c) => document.querySelector(`${c} .ac-ln`)?.offsetParent, card);
      eq("no sideways scroll on a phone", await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth <= 1), true);
    });
    // a restyled card: Starship draws no view of its own, it restyles the standard one (docs/themes.md, "Restyling a card")
    await page.evaluate(async (u) => (await import(u)).setLook("starship"), themes);
    await until(() => document.documentElement.dataset.look === "starship" && document.querySelector('link[data-look-css="starship"]')?.sheet);
    await until((r) => document.querySelector(`${r} .panel-b`)?.dataset.view === "standard" && document.querySelector(`${r} .wan-chart .chart`), ROUTER);
    const bar = await page.evaluate(() => {
      const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
      const rgb = (hex) => `rgb(${[1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(", ")})`;
      return { bg: getComputedStyle(document.querySelector("[data-panel=services] .panel-h")).backgroundColor, want: [rgb(css("--accent")), rgb(css("--crit"))] };
    });
    ok(`the header is a bar in the frame's color (${bar.bg})`, bar.want.includes(bar.bg));
    // a new reading patches the restyled router card: the changed figure is written (its tank follows), a part that did not
    // change (the hour's traffic) is kept
    await page.evaluate((r) => {
      document.querySelector(`${r} .wan-chart .chart`).__kept = true;
      document.querySelector(`${r} .rblock.sys [data-f=cpu] .fill`).style.transition = "none"; // measured at once below
    }, ROUTER);
    // the demo network's own readings keep arriving, so the figure and the tank are read in the same moment as the
    // injected reading is drawn (two frames later), and the tank is compared with the figure it shows
    // (a real reading can still land between the two: then the injection is tried again, up to five times)
    const seen = await page.evaluate(async (b, f, r) => {
      let got = null;
      for (let i = 0; i < 5 && (!got || got.cpu !== "77"); i++) {
        const last = (await import(b)).bus.last("device:router");
        (await import(f)).publishDevice("router", { ...last, cpu_usage_pct: 77, timestamp: last.timestamp + 1 });
        await new Promise((ok) => requestAnimationFrame(() => requestAnimationFrame(ok)));
        const t = document.querySelector(`${r} .rblock.sys [data-f=cpu] .track`);
        got = { cpu: document.querySelector(`${r} .rblock.sys [data-v=cpu]`).textContent.trim(),
          tank: Math.round((t.querySelector(".fill").getBoundingClientRect().height / t.getBoundingClientRect().height) * 100) };
      }
      return got;
    }, await moduleUrl("bus.js"), await moduleUrl("device-feed.js"), ROUTER);
    eq("the new reading is drawn", seen.cpu, "77");
    eq("the router's unchanged traffic chart is the same element", await page.$eval(`${ROUTER} .wan-chart .chart`, (b) => b.__kept === true), true);
    // how full the CPU tank is, in % of its height (its transition was turned off before the reading)
    ok(`Starship's CPU tank is 77 % full (${seen.tank} %)`, Math.abs(seen.tank - 77) <= 2);
    // the chart style (theme.json "chart"): Starship's lines are 3 px and straight, the standard ones 2 px and curved
    // the router's line in the latency card's line chart, whichever view draws it: found in the card's body, not by an id
    // (docs/themes.md, "Finding a card's parts")
    const routerLine = () => {
      const chart = [...document.querySelectorAll("[data-panel=latency] .panel-b canvas")].map((c) => Chart.getChart(c)).find((c) => c?.config.type === "line");
      const d = chart?.data.datasets.find((x) => x.id === "path_router");
      return d ? `${d.borderWidth}/${d.tension}` : "";
    };
    await until(() => document.querySelector("[data-panel=latency] .panel-b").dataset.view === "standard");
    await click("[data-panel=latency] .panel-b [data-action=setLatencyView][data-arg=path]");
    await until(routerLine);
    eq("Starship's chart lines", await page.evaluate(routerLine), "3/0");
    await page.evaluate(async (u) => (await import(u)).setLook(""), themes);
    await until((c) => document.querySelector(`${c} .panel-b`).dataset.view === "standard", card);
    await until(routerLine);
    eq("the standard chart lines again", await page.evaluate(routerLine), "2/0.25");
    // a theme view that throws: the standard view stands in for it (logged once as a warning)
    const view = await page.evaluate(() => JSON.parse(document.documentElement.dataset.looks).find((t) => t.id === "amber-crt").views.incidents);
    await page.evaluate(async (u, v) => {
      const mod = (await import(v)).default;
      mod.mount = () => { throw new Error("a broken view"); };
      (await import(u)).setLook("amber-crt");
      await new Promise((r) => setTimeout(r, 300)); // the view is loaded (from the module cache) and fails
    }, themes, view);
    await until((c) => document.documentElement.dataset.look === "amber-crt" && document.querySelector(`${c} .panel-b`).dataset.view === "standard", card);
    ok("the standard rows instead", await page.$$eval(`${card} #incidentsBody tr.incident-row`, (els) => els.length) >= 2);
    await page.evaluate(async (u) => (await import(u)).setLook(""), themes);
    await page.reload({ waitUntil: "load" }); // the broken module is forgotten
  }],

  ["loading: before any data the pill and the verdict are gray 'Connecting', the path is skeletons, nothing green", async ({ layoutMounted, PHONE, SHOTS, count, eq, ok, page, shot, text }) => {
    await page.evaluate(() => sessionStorage.setItem("smokeWs", "dead"));
    await reloadPhone({ page, PHONE, layoutMounted });
    await new Promise((r) => setTimeout(r, 800));
    const p = await pill(page);
    ok(`pill gray: ${JSON.stringify(p)}`, p.cls.includes("st-unknown") && /Connecting/.test(p.text));
    eq("verdict badge", (await text("#statusBadge")).trim(), "Connecting");
    ok("skeleton nodes, no live nodes", (await count("#pathChain .node.st-unknown")) >= 3 && (await count("#pathChain .node.st-good:not(:first-child)")) <= 1);
    eq("no green tab dot or page tint", await page.evaluate(() => document.body.dataset.status), "unknown");
    if (SHOTS) await shot("state-loading", { fullPage: false });
  }],

  ["disconnected: the socket never opens, so a banner says the connection is lost and the pill stays gray", async ({ SHOTS, eq, ok, page, shot, text, until }) => {
    await until(() => !document.getElementById("connBanner").hidden);
    ok("reconnecting text", /Reconnecting/.test(await text("#wsText")));
    eq("body marks it", await page.evaluate(() => document.body.dataset.conn), "lost");
    ok("cards dimmed", await page.$eval(".page .panel-b", (e) => Number(getComputedStyle(e).opacity) < 0.9));
    if (SHOTS) await shot("state-disconnected");
    await page.evaluate(() => sessionStorage.removeItem("smokeWs"));
  }],

  ["connected again: the banner goes and the verdict turns from gray to a status", async ({ layoutMounted, PHONE, page, until }) => {
    await reloadPhone({ page, PHONE, layoutMounted });
    await until(() => document.getElementById("connBanner").hidden && document.body.dataset.conn === "open");
    await until(() => !document.getElementById("statusPill").className.includes("st-unknown"));
  }],

  ["a malformed address hash (a lone %) does not stop the page: it still connects, and a later hashchange to one throws nothing", async ({ base, eq, layoutMounted, page, waitText }) => {
    await page.goto(base + "/#%E0%A4%A", { waitUntil: "load" });
    await layoutMounted();
    await waitText("#wsText", /^Live/);
    eq("connected, no connection banner", await page.evaluate(() => document.getElementById("connBanner").hidden), true);
    await page.evaluate(() => { location.hash = "%"; });
    await page.evaluate(() => new Promise((r) => setTimeout(r, 200))); // (a throw in the hashchange handler is a page error: the run fails on it)
    eq("still live", await page.evaluate(() => document.body.dataset.conn), "open");
    await page.evaluate(() => { history.replaceState(null, "", location.pathname); });
  }],

  ["a socket that dies without a close: the badge says Stale, then the page opens a new socket and is live again", async ({ eq, ok, page, until, waitText }) => {
    await page.goto((await page.evaluate(() => location.origin)) + "/", { waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    const before = await page.evaluate(() => {
      const socket = window.__smokeSockets.at(-1);
      socket.onmessage = null; // the page hears nothing more from it and is never told it closed
      return window.__smokeSockets.length;
    });
    await until(() => /Stale/.test(document.getElementById("wsText").textContent));
    ok("a stale badge is not green", await page.$eval("#wsStatus", (e) => !e.className.includes("live-live")));
    await until((n) => window.__smokeSockets.length > n, before);
    await waitText("#wsText", /^Live/);
    eq("the connection banner is gone", await page.evaluate(() => document.getElementById("connBanner").hidden), true);
  }],

  ["close code 1008 while this browser changes its sign-in: the page stays and the socket is opened again (no dead end)", async ({ eq, moduleUrl, page, until, waitText }) => {
    await page.goto((await page.evaluate(() => location.origin)) + "/", { waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    const url = await moduleUrl("session.js");
    const before = await page.evaluate(async (src) => {
      const { holdSignIn } = await import(src);
      window.__holding = holdSignIn(() => new Promise((release) => { window.__release = release; }));
      const socket = window.__smokeSockets.at(-1);
      socket.dispatchEvent(new CloseEvent("close", { code: 1008 })); // what the server sends when the sign-in changed
      return window.__smokeSockets.length;
    }, url);
    await until((n) => window.__smokeSockets.length > n, before);
    await waitText("#wsText", /^Live/);
    eq("still on the dashboard", await page.evaluate(() => location.pathname), "/");
    await page.evaluate(() => { window.__release(); return window.__holding; });
  }],

  ["the live message's unchanged topics are not published again: the verdict, a device's snapshot and the scalars reach their subscribers when they change", async ({ moduleUrl, nextMessages, ok, page }) => {
    const src = await moduleUrl("bus.js");
    await page.evaluate(async (u) => {
      const { bus } = await import(u);
      window.__pub = { diagnosis: 0, "device:router": 0, incidents_version: 0, message: 0 };
      window.__seen = [];
      Object.keys(window.__pub).forEach((topic) => bus.subscribe(topic, (v) => { window.__pub[topic]++; if (topic === "diagnosis") window.__seen.push(v); }));
    }, src);
    await nextMessages(6);
    const n = await page.evaluate(() => window.__pub);
    const seen = await page.evaluate(() => window.__seen.map((v) => JSON.stringify({ ...v, timestamp: 0 })));
    ok(`the verdict was published ${n.diagnosis} times for 6 messages (its own replay included): ${seen.length > 1 ? seen.slice(0, 2).join(" <> ").slice(0, 600) : ""}`, n.diagnosis <= 3);
    ok(`the router's snapshot ${n["device:router"]} times`, n["device:router"] <= 3);
    ok(`incidents_version ${n.incidents_version} times`, n.incidents_version <= 2);
    ok(`the whole message still came every time (${n.message})`, n.message >= 6);
  }],

  ["a tab-dot observer is not woken by a card that writes its status again: nothing in the cards changes when nothing changed", async ({ nextMessages, eq, page }) => {
    await page.evaluate(() => {
      window.__statusWrites = 0;
      window.__statusWatch = new MutationObserver((records) => { window.__statusWrites += records.length; window.__statusWho = records.map((r) => `${r.target.id || r.target.className} ${r.attributeName}=${r.target.getAttribute(r.attributeName)} was ${r.oldValue}`); });
      window.__statusWatch.observe(document.getElementById("panels"), { subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ["data-status"] });
    });
    await nextMessages(6);
    const writes = await page.evaluate(() => { window.__statusWatch.disconnect(); return window.__statusWrites; });
    eq(`status writes in 6 messages (${await page.evaluate(() => window.__statusWho)})`, writes, 0);
  }],

  ["a card on a hidden tab is not redrawn: the latency chart keeps its last drawing off screen, and catches up when its tab shows", async ({ click, layoutMounted, nextMessages, ok, page, until, PHONE }) => {
    await page.setViewport(PHONE);
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await until(() => document.querySelector("[data-panel=latency] canvas") && window.Chart && window.Chart.getChart(document.querySelector("[data-panel=latency] canvas")));
    const lastLabel = () => page.evaluate(() => window.Chart.getChart(document.querySelector("[data-panel=latency] canvas")).data.labels.at(-1));
    await click("#mobileTabs [data-arg=incidents]");
    await until(() => document.querySelector("[data-panel=latency]").hidden);
    await new Promise((r) => setTimeout(r, 400));
    const before = await lastLabel();
    await nextMessages(4);
    ok("the hidden chart was not redrawn", (await lastLabel()) === before);
    await click("#mobileTabs [data-arg=live]");
    await until((b) => window.Chart.getChart(document.querySelector("[data-panel=latency] canvas")).data.labels.at(-1) !== b, before);
  }],

  ["keyed lists, action names and the other helpers hold up against odd input", async ({ eq, moduleUrl, page }) => {
    const out = await page.evaluate(async ([cards, dispatch, escape, path]) => {
      const { kit } = await import(cards);
      const { registerActions } = await import(dispatch);
      const { numText } = await import(escape);
      const { shortLabels } = await import(path);
      const box = document.createElement("div");
      const render = (item) => { const li = document.createElement("li"); li.textContent = item.t; return li; };
      kit.list(box, [{ k: "a", t: "one" }, { k: "a", t: "two" }], { key: (i) => i.k, render });
      const twice = box.children.length;
      kit.list(box, [{ k: "a", t: "one" }], { key: (i) => i.k, render });
      let registered = "no";
      try { registerActions("click", { constructor: () => {}, toString: () => {} }); registered = "yes"; } catch (e) { registered = e.message; }
      const labels = shortLabels([{ id: "x", category: "__proto__", name: "Odd", address: "192.0.2.1", enabled: true }]);
      const inherited = shortLabels([{ id: "y", category: "constructor", name: "Odd two", address: "192.0.2.2", enabled: true }]);
      return { twice, after: box.children.length, registered, numbers: [numText("<img>"), numText(" "), numText(null), numText("12.50"), numText(3)].join("|"), labelled: Object.hasOwn(labels, "x"), inherited: inherited.y };
    }, [await moduleUrl("cards.js"), await moduleUrl("dispatch.js"), await moduleUrl("escape.js"), await moduleUrl("path.js")]);
    eq("two rows with one key are two rows", out.twice, 2);
    eq("and one row is left when one is wanted (no orphan)", out.after, 1);
    eq("action names that Object.prototype also has can be registered", out.registered, "yes");
    eq("numText", out.numbers, "|||12.5|3");
    eq("a category named __proto__ does not break the labels", out.labelled, true);
    eq("a category named constructor is not the role table's inherited function", out.inherited, "Odd two");
  }],

  ["the settings values rendered into the page carry their revision: a first live message with another revision reads them again, with the same one it does not", async ({ eq, layoutMounted, nextMessages, ok, page, waitText }) => {
    const reads = () => page.evaluate(() => performance.getEntriesByType("resource").filter((r) => /\/api\/settings\/values/.test(r.name)).length);
    const live = await page.evaluate(async () => (await (await fetch("/api/settings/values", { cache: "no-store" })).json()).revision);
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    eq("the page carries the revision it was rendered at", await page.evaluate(() => document.body.dataset.settingsRevision), String(live));
    await waitText("#wsText", /^Live/);
    await nextMessages(2);
    eq("nothing changed since the render: the first message needed no read", await reads(), 0);
    // a Save between the render and the connection: the page claims another revision than the server's
    const hook = await page.evaluateOnNewDocument((fake) => {
      new MutationObserver((_, obs) => { if (document.body) { document.body.dataset.settingsRevision = String(fake); obs.disconnect(); } }).observe(document, { childList: true, subtree: true });
    }, live + 1000);
    try {
      await page.reload({ waitUntil: "load" });
      await layoutMounted();
      eq("(the page claims a revision the server is not at)", await page.evaluate(() => document.body.dataset.settingsRevision), String(live + 1000));
      await waitText("#wsText", /^Live/);
      await nextMessages(2);
      ok("the first message's revision differed: the values were read again", (await reads()) >= 1);
    } finally {
      await page.removeScriptToEvaluateOnNewDocument(hook.identifier);
      await page.reload({ waitUntil: "load" });
      await layoutMounted();
    }
  }],

  ["the zone changes: Uptime, the report and the loss grid read their data again; the settings values are read one at a time", async ({ eq, ok, moduleUrl, page, sleep }) => {
    const calls = await page.evaluate(async (settings) => {
      const real = window.fetch;
      window.__calls = [];
      window.fetch = (url, ...rest) => { window.__calls.push(String(url)); return real(url, ...rest); };
      document.dispatchEvent(new CustomEvent("zonechange"));
      const { refreshSettingsValues } = await import(settings);
      await Promise.all([refreshSettingsValues(), refreshSettingsValues(), refreshSettingsValues()]);
      window.fetch = real;
      return window.__calls;
    }, await moduleUrl("settings-values.js"));
    await sleep(500);
    const seen = (re) => calls.filter((u) => re.test(u)).length;
    ok(`Uptime asked again (${seen(/\/api\/uptime/)})`, seen(/\/api\/uptime/) >= 1);
    ok("the report asked again", seen(/\/api\/report\?/) >= 1);
    ok("the loss grid asked again", seen(/loss-grid/) >= 1);
    ok(`three requests for the settings values were one or two reads (${seen(/settings\/values/)})`, seen(/settings\/values/) <= 2);
    eq("reads", true, true);
  }],

  ["a discovery that never ends is given up after its limit; reloads of the incident list asked together are one read", async ({ eq, moduleUrl, page }) => {
    const out = await page.evaluate(async ([api, list]) => {
      const real = window.fetch;
      window.fetch = (url, ...rest) => {
        if (String(url).includes("/api/discovery/")) return Promise.resolve(new Response(JSON.stringify({ state: "running", progress: "x" }), { status: 200 }));
        return real(url, ...rest);
      };
      const { waitDiscovery } = await import(api);
      let gaveUp = "never";
      try { await waitDiscovery("stuck", () => {}, 5, 60); } catch (e) { gaveUp = e.message; }
      window.fetch = real;
      const { reload } = await import(list);
      const counted = [];
      window.fetch = (url, ...rest) => { counted.push(String(url)); return real(url, ...rest); };
      await Promise.all([reload(), reload(), reload(), reload(), reload()]);
      window.fetch = real;
      return { gaveUp, reads: counted.filter((u) => /\/api\/incidents\?/.test(u)).length };
    }, [await moduleUrl("path-api.js"), (await moduleUrl("main.js")).replace("js/main.js", "panels/incidents/list.js")]);
    eq("gave up", /too long/.test(out.gaveUp), true);
    eq("the incident list was read once for five reloads", out.reads, 1);
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

  ["colors: a target's color and the design tokens are read once (not by getComputedStyle on every use) and follow both themes live", async ({ eq, ok, page, setTheme, until, waitCount }) => {
    await waitCount("#pathChain .node[data-node]", 3);
    const probe = () => page.evaluate(async () => {
      const find = (f) => performance.getEntriesByType("resource").map((r) => r.name).find((n) => n.endsWith(f));
      const targets = await import(find("/js/targets.js"));
      const theme = await import(find("/js/chart-theme.js"));
      const id = targets.canonicalTargetOrder[0];
      const real = window.getComputedStyle;
      let calls = 0;
      window.getComputedStyle = (...a) => { calls++; return real.apply(window, a); };
      let color; let crit;
      try { for (let i = 0; i < 30; i++) { color = targets.getTargetInfo(id).color; crit = theme.token("--crit"); } } finally { window.getComputedStyle = real; }
      const root = real(document.documentElement);
      return { calls, color, crit, wantCrit: root.getPropertyValue("--crit").trim(), want: [1, 2, 3, 4, 5, 6, 7, 8].map((k) => root.getPropertyValue(`--s${k}`).trim()) };
    });
    const seen = {};
    for (const theme of ["dark", "light", "dark"]) {
      await setTheme(theme);
      const p = await probe();
      ok(`${theme}: 60 reads cost at most 2 style lookups (got ${p.calls})`, p.calls <= 2);
      ok(`${theme}: the target color is one of the theme's series colors (${p.color})`, p.want.includes(p.color));
      eq(`${theme}: the crit token follows the theme`, p.crit, p.wantCrit);
      if (seen[theme]) eq(`${theme}: the same on the way back`, p.color + p.crit, seen[theme]);
      seen[theme] = p.color + p.crit;
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

  ["Path health off screen is not drawn: live messages change nothing in its chain, and it shows the newest values once scrolled into view", async ({ eq, nextMessages, ok, page, showCard, until, waitCount }) => {
    const chain = "#pathChain";
    await page.evaluate(() => window.scrollTo(0, 0));
    await waitCount(`${chain} .node[data-node]`, 3);
    await showCard("matrix"); // the chain is far above the window now
    ok("the chain is out of sight, well above the window", await page.$eval(chain, (c) => c.getBoundingClientRect().bottom < -300));
    await page.evaluate((sel) => {
      window.__writes = 0;
      window.__offObs = new MutationObserver((records) => { window.__writes += records.length; });
      window.__offObs.observe(document.querySelector(sel), { subtree: true, childList: true, attributes: true, characterData: true });
    }, chain);
    await setMutate(page, "d.targets.forEach((t) => { t.last_ms = 77.7; t.recent_latencies = Array(30).fill(77.7); });");
    await nextMessages(3);
    eq("no DOM writes in the off-screen chain", await page.evaluate(() => window.__writes), 0);
    ok("and it still shows the old figures", !/77\.7/.test(await page.$eval(chain, (c) => c.textContent)));
    await page.$eval(chain, (c) => c.scrollIntoView({ block: "center" }));
    await until((sel) => /77\.7/.test(document.querySelector(sel).textContent), chain);
    await setMutate(page, null);
    await page.evaluate(() => { window.__offObs.disconnect(); window.scrollTo(0, 0); });
  }],

  ["Hop details off screen is not drawn: live messages change nothing in its body, and it shows the newest values once scrolled into view", async ({ eq, moduleUrl, ok, page, until, waitCount }) => {
    const body = "[data-panel=matrix] .panel-b"; // the last card: off screen while the page is at its top
    await waitCount(`${body} [data-item] [data-v=jitter]`, 5);
    await page.evaluate(() => window.scrollTo(0, 0));
    ok("the matrix is well below the window", await page.$eval(body, (b) => b.getBoundingClientRect().top > innerHeight + 400));
    await page.evaluate(async (b, u) => {
      window.__writes = 0;
      window.__offObs = new MutationObserver((records) => { window.__writes += records.length; });
      window.__offObs.observe(document.querySelector(b), { subtree: true, childList: true, attributes: true, characterData: true });
      window.__targets = 0;
      (await import(u)).bus.subscribe("targets", () => { window.__targets++; });
      window.__targets = 0; // subscribing replays the last value
    }, body, await moduleUrl("bus.js"));
    await setMutate(page, "d.targets.forEach((t) => { t.jitter_ms = 54321.5; });");
    await until(() => window.__targets >= 3); // three messages published, each with the new jitter
    eq("no DOM writes in the off-screen card", await page.evaluate(() => window.__writes), 0);
    // no further message may draw it: the tab is hidden (live.js then holds the targets), so what shows is the kept one
    const held = await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
      return window.__targets;
    });
    await page.$eval(body, (b) => b.scrollIntoView({ block: "center" }));
    await until((b) => [...document.querySelectorAll(`${b} [data-item] [data-v=jitter]`)].every((e) => /54321/.test(e.textContent)), body);
    eq("drawn from the view model it kept, with no new message", await page.evaluate(() => window.__targets), held);
    await setMutate(page, null);
    await page.evaluate(() => {
      delete document.visibilityState; delete document.hidden;
      document.dispatchEvent(new Event("visibilitychange"));
      window.__offObs.disconnect();
      window.scrollTo(0, 0);
    });
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
    await until(() => !document.getElementById("tracerouteModal").classList.contains("active") && !history.state?.pgModal);
    // a link to #settings opens the page's Settings on load
    await page.evaluate(() => { location.hash = "settings"; });
    await page.reload({ waitUntil: "load" });
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
    await click("[data-action=settingsClose]");
    await until(() => !document.getElementById("settingsModal").classList.contains("active") && location.hash === "");
  }],

  ["Back closes the top dialog and stays on the page; closing by its button or Escape leaves no history entry behind (Dossier, Layout, Trace route)", async ({ click, eq, layoutMounted, ok, page, sleep, until }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    // the page's own entry carries a mark: a press of Back that is "dead" (an entry left behind by a closed dialog) would
    // show as the dialog's entry still being the current one after the dialog has closed
    await page.evaluate(() => history.replaceState({ mark: "base" }, ""));
    const here = () => page.evaluate(() => ({ mark: history.state?.mark ?? (history.state?.pgModal ? "dialog" : null), open: [...document.querySelectorAll(".modal.active")].map((m) => m.id).join(), url: location.pathname + location.search + location.hash }));
    const base = (await here()).url;
    const dialogs = [["Dossier", "[data-action=openDossierModal]", "dossierModal", "[data-action=closeModal][data-arg=dossierModal]"],
      ["Layout", "#headerTools [data-action=openLayoutDialog]", "layoutModal", "[data-action=closeModal][data-arg=layoutModal]"],
      ["Trace route", "[data-action=openModal][data-arg=tracerouteModal]", "tracerouteModal", "[data-action=closeModal][data-arg=tracerouteModal]"]];
    for (const [name, opener, id, closer] of dialogs) {
      await click(opener);
      await until((i) => document.getElementById(i).classList.contains("active"), id);
      eq(`${name}: opening gives it a history entry`, (await here()).mark, "dialog");
      await page.evaluate(() => history.back());
      await until((i) => !document.getElementById(i).classList.contains("active"), id);
      let now = await here();
      eq(`${name}: Back closed it and stayed on the page`, `${now.url}|${now.mark}|${now.open}`, `${base}|base|`);
      for (const how of ["button", "Escape"]) {
        await click(opener);
        await until((i) => document.getElementById(i).classList.contains("active"), id);
        if (how === "button") await click(closer); else await page.keyboard.press("Escape");
        await until((i) => !document.getElementById(i).classList.contains("active"), id);
        await until(() => history.state?.mark === "base"); // its entry is the page's own again (spent)
        await sleep(200);
        now = await here();
        eq(`${name}: closed by ${how}, no entry left behind`, `${now.url}|${now.mark}|${now.open}`, `${base}|base|`);
      }
    }
    // one dialog over another: Back closes the top one only
    await click("[data-action=openDossierModal]");
    await until(() => document.getElementById("dossierModal").classList.contains("active"));
    await click("[data-action=openAiPackModal]");
    await until(() => document.getElementById("aiPackModal").classList.contains("active"));
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("aiPackModal").classList.contains("active"));
    eq("Back closed Ask AI only", (await here()).open, "dossierModal");
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("dossierModal").classList.contains("active"));
    eq("a second Back closed the Dossier too, still on the page", `${(await here()).url}|${(await here()).mark}`, `${base}|base`);
    ok("the page was never left", await page.evaluate(() => location.href.startsWith("http")));
  }],

  ["Back after closing two dialogs in a row leaves the page and opens neither; the second reuses the first's spent entry", async ({ click, eq, layoutMounted, ok, page, until }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await page.evaluate(() => { history.replaceState({ mark: "prev" }, ""); history.pushState({ mark: "page" }, ""); });
    const length = await page.evaluate(() => history.length);
    const open = () => page.evaluate(() => [...document.querySelectorAll(".modal.active")].map((m) => m.id).join());
    for (const [opener, id, closer] of [["[data-action=openDossierModal]", "dossierModal", "[data-action=closeModal][data-arg=dossierModal]"],
      ["#headerTools [data-action=openLayoutDialog]", "layoutModal", "[data-action=closeModal][data-arg=layoutModal]"]]) {
      await click(opener);
      await until((i) => document.getElementById(i).classList.contains("active"), id);
      await click(closer);
      await until((i) => !document.getElementById(i).classList.contains("active"), id);
    }
    eq("both dialogs together added one entry", await page.evaluate(() => history.length), length + 1);
    await page.evaluate(() => history.back());
    await until(() => history.state?.mark === "prev");
    eq("one Back reached the page before, with no dialog open", await open(), "");
    await page.evaluate(() => history.forward());
    await until(() => history.state?.mark === "page");
    eq("Forward is the page again, still no dialog", await open(), "");
    await page.evaluate(() => history.forward());
    await until(() => history.state?.pgSpent !== undefined);
    ok("and onto the spent entry: nothing opens", (await open()) === "" && (await page.evaluate(() => history.state.mark)) === "page");
  }],

  ["Forward after a dialog closed does not open it again (Dossier by Back; Settings by Back, address #settings)", async ({ click, eq, layoutMounted, ok, page, sleep, until }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    const open = () => page.evaluate(() => [...document.querySelectorAll(".modal.active")].map((m) => m.id).join());
    await click("[data-action=openDossierModal]");
    await until(() => document.getElementById("dossierModal").classList.contains("active"));
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("dossierModal").classList.contains("active"));
    await page.evaluate(() => history.forward());
    await until(() => history.state?.pgModal !== undefined); // on the Dossier's old entry
    await sleep(300);
    eq("the Dossier stayed closed", await open(), "");
    // Back from the Dossier's dead entry passes the page entry it stands for and goes one on (js/modal.js: a Back that
    // starts from a spent entry), so the page ends on an earlier entry than the one it started on, whose address may differ. The first
    // landing already has no dialog state, so the step waits for the moves to end, and compares addresses with the one
    // the page has then.
    await page.evaluate(() => history.back());
    await until(() => history.state?.pgModal === undefined);
    await sleep(500);
    const below = await page.evaluate(() => location.hash);
    await click("#btnSettings");
    await until(() => document.getElementById("settingsModal").classList.contains("active") && location.hash === "#settings");
    await page.evaluate(() => history.back());
    await until((h) => !document.getElementById("settingsModal").classList.contains("active") && location.hash === h, below);
    await page.evaluate(() => history.forward());
    await until(() => location.hash === "#settings"); // the address of the Settings entry
    await sleep(500);
    ok("Settings stayed closed on its old address", (await open()) === "" && await page.evaluate(() => history.state?.pgModal !== undefined));
    await page.evaluate(() => history.back());
    await sleep(500); // (Back from that dead entry goes on past the page it stands for as well)
    ok("Back left the old Settings address and no dialog is open", (await open()) === "" && await page.evaluate(() => location.hash !== "#settings"));
  }],

  ["a hash set in the same task as a dialog's close is kept, and Back from it lands on the page the dialog was opened from", async ({ click, eq, layoutMounted, page, sleep, until }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await page.evaluate(() => { history.replaceState({ mark: "prev" }, ""); history.pushState({ mark: "page" }, ""); });
    const start = await page.evaluate(() => location.hash);
    const next = start === "#network" ? "live" : "network";
    await click("[data-action=openDossierModal]");
    await until(() => document.getElementById("dossierModal").classList.contains("active"));
    await page.evaluate((h) => { document.querySelector("[data-action=closeModal][data-arg=dossierModal]").click(); location.hash = h; }, next);
    await sleep(500);
    eq("the hash stayed", await page.evaluate(() => location.hash), `#${next}`);
    eq("the dialog is closed", await page.evaluate(() => document.getElementById("dossierModal").classList.contains("active")), false);
    await page.evaluate(() => history.back());
    await until((h) => location.hash === h, start);
    eq("Back skipped the spent entry: the page the dialog was opened from", await page.evaluate(() => history.state?.mark), "page");
    await page.evaluate(() => history.back());
    await until(() => history.state?.mark === "prev");
  }],

  ["a reload right after a dialog closed stays on the page, and the closed dialog's entry is still skipped by Back", async ({ click, eq, layoutMounted, page, until }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await page.evaluate(() => { history.replaceState({ mark: "prev" }, ""); history.pushState({ mark: "page" }, ""); });
    await click("[data-action=openDossierModal]");
    await until(() => document.getElementById("dossierModal").classList.contains("active"));
    await click("[data-action=closeModal][data-arg=dossierModal]");
    await page.reload({ waitUntil: "load" }); // at once: nothing of the close is still on its way
    await layoutMounted();
    eq("still the dashboard, no dialog", await page.evaluate(() => `${location.pathname}|${document.querySelectorAll(".modal.active").length}`), "/|0");
    await page.evaluate(() => history.back());
    await until(() => history.state?.mark === "prev");
  }],

  ["a reload in the same task as a dialog closing goes through (a close moves nothing in the history)", async ({ click, layoutMounted, page, until }) => {
    await page.setViewport({ width: 1400, height: 1000 });
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await click("[data-action=openDossierModal]");
    await until(() => document.getElementById("dossierModal").classList.contains("active"));
    await page.evaluate(() => { window.__beforeReload = true; });
    // closing only marks the dialog's entry spent: no traversal follows it that could land on the reload in flight. (Not
    // waitForNavigation: the close's replaceState is a same-document navigation to it and would end that wait at once.)
    await page.evaluate(() => { document.querySelector("[data-action=closeModal][data-arg=dossierModal]").click(); location.reload(); }).catch(() => {}); // (the context may go before the answer)
    await page.waitForFunction(() => window.__beforeReload === undefined && document.readyState === "complete", { timeout: 15000 });
    await layoutMounted();
  }],
];
