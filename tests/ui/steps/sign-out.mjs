// The sign-out dialog (homeguard's POST /logout). The `token` variant starts the app with an access token, and smoke.mjs
// signs in through the sign-in page before the first step. The header shows Sign out only for a browser signed in that
// way; "Sign out" ends this browser's sign-in and the server refuses the old cookie from then on; "Sign out everywhere"
// asks first, then ends a second browser's sign-in too (its requests are refused, its live connection is closed).
// The `viewonly` variant adds PENGUMON_VIEW_WITHOUT_TOKEN: the page opens without signing in, view only (the header says
// so, every edit control is off with a tooltip, the server refuses a viewer's changes), and "Sign in to edit" in the
// header signs in; signing out makes the page view only again. The run fails on any refused request the page itself
// makes (lib.mjs), so a view-only page that still tried to change something would fail here too.

import { loopback } from "../lib.mjs";
export const subject = "dialog:sign-out";
export const variants = ["token"];
const VIEW_TIP = "View only · Sign in to edit";
// the edit controls of the page as it opens (the path editor's own are the editor's)
const EDIT_CONTROLS = ["#headerTools [data-arg=addTargetModal]", "#headerTools [data-arg=tracerouteModal]", "#btnEditPath",
  "[data-panel=line] [data-action=openDeviceSetup]", "[data-panel=router] [data-action=openDeviceSetup]"];
const editState = (p) => p.evaluate((sels) => sels.map((s) => {
  const e = document.querySelector(s);
  return e ? `${s}: ${e.getAttribute("aria-disabled") === "true" ? "off" : "on"} ${e.title}` : `${s}: missing`;
}), EDIT_CONTROLS);
// What the server answers a request from outside the page without the token (Node's fetch: no cookie).
// These two run in node, which cannot resolve the browser's host name: they ask the app on 127.0.0.1 (its own origin).
const asViewer = async (base, method, path, body) => (await fetch(`${loopback(base)}${path}`, {
  method, headers: { Origin: loopback(base), "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) })).status;

// The page's session cookie. It is HttpOnly, so it is read through the browser, the way someone copying it out of a
// browser profile would get it.
const sessionCookieOf = async (p) => (await p.browserContext().cookies()).find((c) => c.name === "pg_session");
// What the server answers a request that carries only this cookie (sent from Node, not the page: a 401 is expected).
const statusWithCookie = async (base, value) => (await fetch(`${loopback(base)}/api/status`, { headers: { Cookie: `pg_session=${value}` } })).status;
const atSignInPage = (p) => new URL(p.url()).pathname === "/login";
const openSignOut = async ({ click, until }) => {
  await click("#btnSignOut");
  await until(() => document.getElementById("signOutModal").classList.contains("active"));
};

export const steps = [
  ["without the token: the dashboard, view only, says so in the header and on every edit control", async (ctx) => {
    const { DESKTOP, eq, ok, page, phone, until } = ctx;
    ok(`on the dashboard, not the sign-in page (${page.url()})`, !atSignInPage(page));
    await until(() => /^Live/.test(document.getElementById("wsText")?.textContent ?? "")); // the live socket is open to a viewer
    const info = await page.evaluate(async () => (await (await fetch("/api/info")).json()));
    eq("can_edit", info.can_edit, false);
    eq("nothing to sign out of", JSON.stringify(info.session), JSON.stringify({ sign_out: false, sign_out_everywhere: false }));
    await until(() => !document.getElementById("viewOnly").hidden);
    eq("the header says so", await page.$eval("#viewOnly", (e) => e.textContent.trim()), VIEW_TIP);
    eq("and links to the sign-in page", await page.$eval("#viewOnly", (e) => new URL(e.href).pathname), "/login");
    eq("no Sign out button", await page.$eval("#btnSignOut", (b) => b.hidden), true);
    eq("every edit control is off, with the tooltip", (await editState(page)).join("\n"),
      EDIT_CONTROLS.map((s) => `${s}: off ${VIEW_TIP}`).join("\n"));
    ok("the dossier and the layout stay usable", await page.evaluate(() =>
      ["openDossierModal", "openLayoutDialog", "toggleNotifications"].every((a) => document.querySelector(`#headerTools [data-action=${a}]`).getAttribute("aria-disabled") !== "true")));
    for (const width of [1400, 1290, 1100, 900, 721]) {
      await page.setViewport({ width, height: 900 });
      await ctx.sleep(100); // the header's own layout; the charts below follow a new width later, and are not checked here
      const fit = await page.evaluate(() => {
        const r = document.getElementById("viewOnly").getBoundingClientRect();
        const bar = document.querySelector("#topbar .tb-inner");
        return { shown: r.width > 0, right: Math.round(r.right), client: document.documentElement.clientWidth, bar: bar.scrollWidth - bar.clientWidth };
      });
      ok(`the header fits at ${width} px (${JSON.stringify(fit)})`, fit.shown && fit.right <= fit.client && fit.bar <= 0);
    }
    await page.setViewport(DESKTOP);
    await phone(async () => {
      await until(() => !document.getElementById("viewOnly").hidden);
      ok("on a phone the header says View only", await page.$eval("#viewOnly", (e) => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.right <= document.documentElement.clientWidth && /View only/.test(e.innerText);
      }));
      ok("the phone page does not scroll sideways", await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
      await ctx.click(".tb-more");
      await until(() => !document.getElementById("headerMenu").hidden);
      eq("the phone menu's Add target is off", await page.$eval("#headerMenu [data-arg=addTargetModal]", (e) => `${e.getAttribute("aria-disabled")} ${e.title}`), `true ${VIEW_TIP}`);
      await ctx.click(".tb-more");
    });
  }, { variants: ["viewonly"] }],

  ["an edit control does nothing for a viewer but say why", async (ctx) => {
    const { clickAt, eq, page, sleep, until } = ctx;
    await until(() => document.documentElement.hasAttribute("data-view-only"));
    await clickAt("#headerTools [data-arg=addTargetModal]"); // a real pointer click
    await until(() => document.getElementById("toast")?.classList.contains("on"));
    eq("the toast says why", await page.$eval("#toast", (e) => e.textContent), VIEW_TIP);
    for (const [control, modal] of [["#headerTools [data-arg=addTargetModal]", "addTargetModal"], ["#headerTools [data-arg=tracerouteModal]", "tracerouteModal"],
      ["#btnEditPath", "pathEditorModal"], ["[data-panel=router] [data-action=openDeviceSetup]", "deviceSetupModal"]]) {
      await ctx.click(control);
      await sleep(200);
      eq(`${control} opens nothing`, await page.evaluate((id) => !!document.getElementById(id)?.classList.contains("active"), modal), false);
    }
    // a control drawn later is marked too: the device card's "Set up" button, drawn by the page
    const later = await page.evaluate(() => {
      const b = document.createElement("button");
      b.dataset.needsEdit = "";
      b.title = "Something";
      document.body.appendChild(b);
      return new Promise((r) => setTimeout(() => { r(`${b.getAttribute("aria-disabled")} ${b.title}`); b.remove(); }, 50));
    });
    eq("a control added later", later, `true ${VIEW_TIP}`);
  }, { variants: ["viewonly"] }],

  ["the server refuses what a viewer would change, and the export and credentials", async ({ base, eq }) => {
    eq("reading the status", await asViewer(base, "GET", "/api/status"), 200);
    eq("adding a target", await asViewer(base, "POST", "/api/targets", { id: "x", name: "x", address: "192.0.2.9" }), 401);
    eq("deleting a target", await asViewer(base, "DELETE", "/api/targets/router"), 401);
    eq("applying a path", await asViewer(base, "PUT", "/api/path", { base_revision: 0, hops: [] }), 401);
    eq("setting device credentials", await asViewer(base, "POST", "/api/devices/router/credentials", { password: "x" }), 401);
    eq("a server alert test", await asViewer(base, "POST", "/api/notifications/test"), 401);
    eq("a discovery", await asViewer(base, "POST", "/api/discovery", {}), 401);
    eq("the path export", await asViewer(base, "GET", "/api/path/export"), 401);
  }, { variants: ["viewonly"] }],

  ["Sign in to edit: the sign-in page, then the edit controls work; Sign out: view only again", async (ctx) => {
    const { base, click, eq, layoutMounted, ok, page, until } = ctx;
    await until(() => !document.getElementById("viewOnly").hidden);
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), ctx.clickAt("#viewOnly")]);
    ok(`at the sign-in page (${page.url()})`, atSignInPage(page));
    await page.type("input[name=token]", process.env.PENGUMON_SMOKE_TOKEN ?? "");
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), page.click("button[type=submit]")]);
    eq("back on the dashboard", new URL(page.url()).pathname, "/");
    await layoutMounted();
    await until(() => !document.getElementById("btnSignOut").hidden);
    eq("can_edit", await page.evaluate(async () => (await (await fetch("/api/info")).json()).can_edit), true);
    eq("no View only", await page.$eval("#viewOnly", (e) => e.hidden), true);
    eq("the edit controls are on, with their own tooltips", (await editState(page)).map((l) => l.split(": ")[1].split(" ")[0]).join(" "),
      EDIT_CONTROLS.map(() => "on").join(" "));
    eq("Add target keeps its title", await page.$eval("#headerTools [data-arg=addTargetModal]", (e) => e.title), "Add target");
    await click("#headerTools [data-arg=addTargetModal]");
    await until(() => document.getElementById("addTargetModal").classList.contains("active"));
    await click("[data-action=cancelAddTarget]");
    await ctx.dialogKeys("#btnSignOut", "signOutModal");
    await openSignOut(ctx);
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnSignOutHere")]);
    await page.goto(base + "/", { waitUntil: "load" });
    ok(`signed out, the dashboard opens view only (${page.url()})`, !atSignInPage(page));
    await until(() => !document.getElementById("viewOnly").hidden);
    eq("Add target is off again", await page.$eval("#headerTools [data-arg=addTargetModal]", (e) => e.getAttribute("aria-disabled")), "true");
  }, { variants: ["viewonly"] }],

  ["without a token there is nothing to sign out of: no Sign out button", async ({ eq, page }) => {
    const session = await page.evaluate(async () => (await (await fetch("/api/info")).json()).session);
    eq("session", JSON.stringify(session), JSON.stringify({ sign_out: false, sign_out_everywhere: false }));
    eq("no Sign out button", await page.$eval("#btnSignOut", (b) => b.hidden), true);
  }, { variants: ["full"] }],

  ["signed in with the token: Sign out in the header and the phone menu, its dialog fits a phone", async (ctx) => {
    const { DESKTOP, click, eq, ok, page, phone, noSideScroll, shots, until } = ctx;
    await until(() => !document.getElementById("btnSignOut").hidden);
    for (const width of [1290, 1280, 900]) { // the header's tightest widths, with and without the button labels
      await page.setViewport({ width, height: 900 });
      // The header alone is measured, not the page: the charts below resize a moment after the window does (Chart.js),
      // and until then the page is still as wide as it was. The page's own width is asserted once it has settled.
      ok(`the header fits at ${width} px`, await page.evaluate(() => {
        const r = document.getElementById("btnSignOut").getBoundingClientRect();
        const bar = document.querySelector(".tb-inner");
        return r.width > 0 && r.right <= document.documentElement.clientWidth && bar.scrollWidth <= bar.clientWidth;
      }));
      await until(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
    }
    await page.setViewport(DESKTOP);
    await openSignOut(ctx);
    ok("everywhere is offered", await page.$eval("[data-sign-out-everywhere]", (e) => !e.hidden));
    eq("its confirmation is not shown yet", await page.$eval("#signOutEverywhereConfirm", (e) => e.hidden), true);
    await shots("sign-out");
    await click("#btnSignOutEverywhere"); // ask, then leave with Escape: the next opening is fresh
    eq("the confirmation is shown", await page.$eval("#signOutEverywhereConfirm", (e) => e.hidden), false);
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("signOutModal").classList.contains("active"));
    await openSignOut(ctx);
    eq("reopened: the confirmation is hidden again", await page.$eval("#signOutEverywhereConfirm", (e) => e.hidden), true);
    eq("reopened: the ask button is enabled", await page.$eval("#btnSignOutEverywhere", (e) => e.disabled), false);
    await click("[data-action=closeModal][data-arg=signOutModal]");
    await phone(async () => {
      await click(".tb-more");
      const item = "#headerMenu [data-action=openModal][data-arg=signOutModal]";
      await until((s) => { const e = document.querySelector(s); return !!e && !e.hidden && e.getBoundingClientRect().width > 0; }, item);
      await click(item);
      await until(() => document.getElementById("signOutModal").classList.contains("active"));
      ok("the dialog does not scroll sideways", await noSideScroll("signOutModal"));
      ok("nor does the page", await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
      await click("[data-action=closeModal][data-arg=signOutModal]");
    });
  }],

  ["the header never overflows, whatever the verdict says: longest title, metrics, LAN address, every button", async ({ DESKTOP, eq, ok, page, until }) => {
    // The title is the classifier's (an ISP or gateway name goes into it) and the LAN chip shows the address /api/info
    // gave; neither may push the header past the page at any width. Content is set directly, so no timing is involved.
    await until(() => document.getElementById("statusPillText").textContent !== "Connecting"); // the first verdict
    eq("the pill's title= is the verdict's full title", await page.$eval("#statusPill", (e) => e.title), await page.$eval("#statusPillText", (e) => e.textContent));
    await page.evaluate(() => {
      const long = "ISP Line / A Very Long Internet Provider Name Communications Outage";
      document.getElementById("statusPillText").textContent = long;
      document.getElementById("statusPillWord").textContent = "Degraded";
      document.getElementById("statusPillMetrics").innerHTML = ["100% loss", "1234.5 ms to internet"]
        .map((t) => `<span class="sep" aria-hidden="true">·</span><span class="num">${t}</span>`).join("");
      document.getElementById("lanUrlLink").textContent = "192.0.2.200:8000";
      document.getElementById("lanPill").hidden = false;
      document.getElementById("btnSignOut").hidden = false;
    });
    // The header alone is measured (every item inside the bar, the bar not scrolling), not the page's scrollWidth: the
    // charts resize a moment after the window does and keep the page wide until then.
    const fits = () => page.evaluate(() => {
      const bar = document.querySelector(".tb-inner");
      const items = [...bar.children].filter((e) => e.getClientRects().length && e.id !== "headerMenu");
      const over = Math.max(...items.map((e) => e.getBoundingClientRect().right)) - (bar.getBoundingClientRect().right - parseFloat(getComputedStyle(bar).paddingRight));
      return { over, scrolls: bar.scrollWidth > bar.clientWidth };
    });
    for (const width of [1600, 1400, 1290, 1281, 1280, 1181, 1180, 1100, 1001, 1000, 900, 800, 721, 720, 600, 390]) {
      await page.setViewport({ width, height: 900 });
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))); // the header fits itself on the next frame
      const f = await fits();
      ok(`the header fits at ${width} px (${f.over.toFixed(1)} px past the bar's padding)`, f.over <= 0.5 && !f.scrolls);
      // the LAN address is never left cut off as "LAN": whole in the header, or the first item of the "more" menu (the phone's menu has it too)
      const lan = await page.evaluate(() => {
        const link = document.getElementById("lanUrlLink");
        const chip = document.getElementById("lanPill").getBoundingClientRect().width > 0;
        const more = document.querySelector(".tb-more").getBoundingClientRect().width > 0;
        return { chip, whole: link.scrollWidth <= link.getBoundingClientRect().width && link.clientWidth > 0, more };
      });
      ok(`the LAN address is whole or in the menu at ${width} px (${JSON.stringify(lan)})`, lan.chip ? lan.whole : lan.more);
    }
    await page.setViewport(DESKTOP);
  }],

  ["a request the server refuses for want of a sign-in sends the page to the sign-in page", async (ctx) => {
    const { base, expectRefusals, ok, page, layoutMounted } = ctx;
    expectRefusals(true);
    try {
      // without the cookie, as if the sign-in had ended: homeguard's 401 (WWW-Authenticate: Bearer)
      await Promise.all([page.waitForNavigation({ waitUntil: "load", timeout: 15000 }),
        page.evaluate(() => { fetch("/api/status", { credentials: "omit" }); })]);
      ok(`at the sign-in page (${page.url()})`, atSignInPage(page));
    } finally {
      expectRefusals(false);
    }
    await page.goto(base + "/", { waitUntil: "load" }); // the sign-in itself is still valid
    await layoutMounted();
    ok("back on the dashboard", !atSignInPage(page));
  }],

  ["Sign out: the sign-in page again, and the old cookie is refused", async (ctx) => {
    const { base, click, eq, ok, page, layoutMounted, signInWithToken } = ctx;
    const old = await sessionCookieOf(page);
    ok("signed in with an HttpOnly session cookie", !!old && old.httpOnly);
    ok("the cookie works before", (await statusWithCookie(base, old.value)) !== 401);
    await openSignOut(ctx);
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnSignOutHere")]);
    ok(`at the sign-in page (${page.url()})`, atSignInPage(page));
    eq("the browser's cookie is gone", await sessionCookieOf(page), undefined);
    await page.goto(base + "/", { waitUntil: "load" });
    ok("the dashboard asks for the token again", atSignInPage(page));
    eq("the old cookie, sent again", await statusWithCookie(base, old.value), 401);
    // a copy of the old cookie put back into the browser: still the sign-in page
    const { name, value, domain, path, httpOnly, sameSite, expires } = old;
    await page.browserContext().setCookie({ name, value, domain, path, httpOnly, sameSite, expires });
    await page.goto(base + "/", { waitUntil: "load" });
    ok("a copy of the old cookie does not sign in", atSignInPage(page));
    await signInWithToken();
    await layoutMounted();
    const fresh = await sessionCookieOf(page);
    ok("signing in again gives a new cookie that works", fresh.value !== old.value && (await statusWithCookie(base, fresh.value)) !== 401);
  }],

  ["Sign out everywhere: asks first, then a second browser is signed out too", async (ctx) => {
    const { base, browser, click, eq, ok, page, layoutMounted, signInWithToken, until } = ctx;
    const other = await browser.createBrowserContext();
    try {
      const second = await other.newPage();
      await signInWithToken(second);
      await second.waitForFunction(() => /^Live/.test(document.getElementById("wsText")?.textContent ?? ""), { timeout: 15000 });
      const theirs = await sessionCookieOf(second);
      const mine = await sessionCookieOf(page);
      ok("the second browser is in", (await second.evaluate(async () => (await fetch("/api/status")).status)) !== 401);

      await openSignOut(ctx);
      await click("#btnSignOutEverywhere");
      await until(() => !document.getElementById("signOutEverywhereConfirm").hidden);
      await click("[data-action=cancelSignOutEverywhere]");
      await until(() => document.getElementById("signOutEverywhereConfirm").hidden);
      ok("Cancel signs nobody out", (await statusWithCookie(base, theirs.value)) !== 401);
      await click("#btnSignOutEverywhere");
      await until(() => !document.getElementById("signOutEverywhereConfirm").hidden);
      await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnSignOutEverywhereConfirm")]);
      ok(`this browser is at the sign-in page (${page.url()})`, atSignInPage(page));
      eq("this browser's old cookie", await statusWithCookie(base, mine.value), 401);

      await second.waitForFunction(() => !/^Live/.test(document.getElementById("wsText")?.textContent ?? ""), { timeout: 15000 });
      eq("the second browser's requests are refused", await second.evaluate(async () => (await fetch("/api/status")).status), 401);
      await second.goto(base + "/", { waitUntil: "load" });
      ok("the second browser is asked for the token again", atSignInPage(second));
    } finally {
      await other.close();
    }
    await signInWithToken(); // signing in again works at once
    await layoutMounted();
  }],

  ["a page whose sign-in ends elsewhere goes to the sign-in page by itself, once", async (ctx) => {
    const { browser, eq, expectRefusals, ok, page, signInWithToken, sleep, until } = ctx;
    await until(() => /^Live/.test(document.getElementById("wsText")?.textContent ?? ""));
    const other = await browser.createBrowserContext();
    expectRefusals(true); // this page's requests are refused from the moment the other browser signs out everywhere
    try {
      const second = await other.newPage();
      await signInWithToken(second);
      // the page's modules (the dialog's own among them) are loaded once the layout is mounted
      await second.waitForFunction(() => document.querySelector("[data-panel=report]")?.dataset.layoutHidden !== undefined, { timeout: 15000 });
      await second.waitForFunction(() => !document.getElementById("btnSignOut")?.hidden, { timeout: 15000 })
        .catch((e) => { throw new Error(`the second browser shows no Sign out (${second.url()}): ${e.message}`); });
      await second.evaluate(() => document.getElementById("btnSignOut").click());
      await second.waitForFunction(() => document.getElementById("signOutModal").classList.contains("active"), { timeout: 15000 });
      await second.evaluate(() => document.getElementById("btnSignOutEverywhere").click());
      await second.waitForFunction(() => !document.getElementById("signOutEverywhereConfirm").hidden, { timeout: 15000 });
      const left = page.waitForNavigation({ waitUntil: "load", timeout: 15000 });
      await Promise.all([second.waitForNavigation({ waitUntil: "load" }), second.evaluate(() => document.getElementById("btnSignOutEverywhereConfirm").click())]);
      await left;
      ok(`this page went to the sign-in page on its own (${page.url()})`, atSignInPage(page));
      let moves = 0;
      const count = (f) => { if (f === page.mainFrame()) moves++; };
      page.on("framenavigated", count);
      await sleep(3000);
      page.off("framenavigated", count);
      eq("and stayed there (no loop)", moves, 0);
      ok("still the sign-in page", atSignInPage(page));
    } finally {
      await other.close();
      expectRefusals(false);
    }
    // The last step: the page is left at the sign-in page (signing in again would be a sixth sign-in this minute,
    // over the sign-in page's limit of five per address).
  }],
];
