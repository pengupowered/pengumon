// The first-run question (dialogs/access-setup) and Settings > Sign-in. The `firstrun` variant starts a fresh install:
// no password from anywhere and the question unanswered, so the dialog opens by itself. Both choices are offered with
// "Anyone on my home network" preselected; "Anyone" is answered here against a stand-in for the server (the page itself
// answers POST /api/access/setup, so the server's question stays open: tests/test_access.py answers it for real), then
// "Only people with a password" for real: this browser stays signed in, the password is shown once with Copy, and another
// browser gets the sign-in page. Settings > Sign-in then never shows it again, keeps a typed password through a refused
// Save, and removes the password: open to everyone again.

import { loopback } from "../lib.mjs";
export const subject = "dialog:access-setup";
export const variants = ["firstrun"];
const FIRST_PASSWORD = "first-run-smoke-pw"; // made up for the test
const MODAL = "accessSetupModal";
// What another browser (no cookie) gets for the dashboard: the page, or a redirect to the sign-in page (Node's fetch,
// on the app's own origin, without following the redirect).
const strangerSees = async (base) => {
  const res = await fetch(`${loopback(base)}/`, { redirect: "manual", headers: { Accept: "text/html" } });
  return res.status === 303 ? res.headers.get("location") : String(res.status);
};
const setupOpen = (until) => until((m) => document.getElementById(m).classList.contains("active") && document.getElementById(m).contains(document.activeElement), MODAL);

export const steps = [
  ["a fresh install asks who can open the dashboard: Anyone is preselected, a password is offered, it fits a phone and keeps the keyboard", async (ctx) => {
    const { click, eq, ok, page, phone, until } = ctx;
    await setupOpen(until);
    eq("the question", (await ctx.text("#accessSetupTitle")).trim(), "Who can open this dashboard?");
    eq("Anyone is chosen", await page.$eval("#accessChoiceOpen", (e) => e.checked), true);
    eq("focus starts on it", await page.evaluate(() => document.activeElement.id), "accessChoiceOpen");
    eq("no password box for Anyone", await page.$eval("#accessSetupPassword", (e) => e.hidden), true);
    await page.keyboard.press("Escape");
    ok("Escape does not skip the question", await ctx.modalOpen(MODAL));
    await ctx.tabStaysIn(MODAL);
    await page.$eval("#accessChoiceOpen", (e) => e.focus());
    await page.keyboard.press("ArrowDown"); // the other choice, by keyboard
    await until(() => !document.getElementById("accessSetupPassword").hidden);
    const proposed = await page.$eval("#accessSetupPw", (e) => e.value);
    ok(`a proposed password (${proposed})`, /^[a-z2-9]{4}(-[a-z2-9]{4}){3}$/.test(proposed));
    await click("[data-action=accessSetupSuggest]");
    ok("New one proposes another", (await page.$eval("#accessSetupPw", (e) => e.value)) !== proposed);
    eq("Others may look is off", await page.$eval("#accessSetupViewers", (e) => e.checked), false);
    await click("#accessChoiceOpen");
    await until(() => document.getElementById("accessSetupPassword").hidden);
    await phone(async () => {
      await setupOpen(until);
      ok("the dialog does not scroll sideways on a phone", await ctx.noSideScroll(MODAL));
      await click("#accessChoicePassword");
      await until(() => !document.getElementById("accessSetupPassword").hidden);
      ok("the password row and Continue fit a phone", await page.evaluate(() => [...document.querySelectorAll("#accessSetupPw, #btnAccessSetup, [data-action=accessSetupSuggest]")]
        .every((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.left >= 0 && r.right <= document.documentElement.clientWidth; })));
    });
    await setupOpen(until);
  }],

  ["Anyone on my home network is one click: Continue closes the question (the server's answer stood in)", async (ctx) => {
    const { click, eq, ok, page, stubFetch, until } = ctx;
    await stubFetch({ "POST /api/access/setup": { status: 200, body: { mode: "open" } } });
    await setupOpen(until);
    await click("#btnAccessSetup");
    await until((m) => !document.getElementById(m).classList.contains("active"), MODAL);
    await ctx.waitText("#toast", /Anyone on your home network can open the dashboard/);
    eq("what was sent", JSON.stringify((await page.evaluate(() => window.__stubbed)).map(([k, b]) => [k, JSON.parse(b)])),
      JSON.stringify([["POST /api/access/setup", { choice: "open" }]]));
    await stubFetch(null);
    await setupOpen(until); // the stand-in answered, not the server: the question is still open
    ok("still asked", await ctx.modalOpen(MODAL));
  }],

  ["Only people with a password: this browser stays signed in, the password is shown once with Copy, another browser gets the sign-in page", async (ctx) => {
    const { base, click, eq, ok, page, until } = ctx;
    eq("before: another browser gets the dashboard", await strangerSees(base), "200");
    await click("#accessChoicePassword");
    await until(() => !document.getElementById("accessSetupPassword").hidden);
    await page.$eval("#accessSetupPw", (e) => { e.value = ""; });
    await page.type("#accessSetupPw", "short");
    await click("#btnAccessSetup");
    await ctx.waitText("#accessSetupError", /at least 8 characters/);
    await page.$eval("#accessSetupPw", (e) => { e.value = ""; });
    await page.type("#accessSetupPw", FIRST_PASSWORD);
    await page.keyboard.press("Enter");
    await until(() => !document.getElementById("accessSetupDone").hidden);
    eq("shown once", (await ctx.text("#accessSetupShown")).trim(), FIRST_PASSWORD);
    ok("it says where to use it", (await ctx.text("#accessSetupWhere")).includes(base));
    await click("#btnAccessSetupCopy");
    await ctx.waitText("#btnAccessSetupCopy", /Copied/);
    eq("Copy put it on the clipboard", await page.evaluate(() => window.__clip()), FIRST_PASSWORD);
    await click("#btnAccessSetup"); // Done
    await until((m) => !document.getElementById(m).classList.contains("active"), MODAL);
    await until(() => !document.getElementById("btnSignOut").hidden); // signed in: Sign out in the header
    eq("another browser now gets the sign-in page", await strangerSees(base), "/login");
    await until(() => /^Live/.test(document.getElementById("wsText")?.textContent ?? "")); // the live view came back
    await page.reload({ waitUntil: "load" });
    await ctx.layoutMounted();
    ok(`still on the dashboard after a reload (${page.url()})`, new URL(page.url()).pathname === "/");
    eq("and not asked again", await ctx.modalOpen(MODAL), false);
  }],

  ["Settings > Sign-in never shows the password again, keeps what was typed after a refused Save, and Remove opens the dashboard to everyone again", async (ctx) => {
    const { base, click, eq, ok, page, until } = ctx;
    await click("#btnSettings");
    await until(() => document.getElementById("setAccess") !== null);
    await page.$eval("#set-sec-access", (e) => e.scrollIntoView());
    ok("it says a password is needed", /Only people with the password/.test(await ctx.text("#setAccess [data-state]")));
    ok("the viewers switch is beside it", await page.$eval('#set-sec-access .set-row[data-key="access.allow_viewers"]', (e) => /Others may look/.test(e.textContent)));
    eq("Change and Remove, no Show", await page.$$eval("#setAccess .set-secret-state button", (els) => els.map((e) => e.textContent.trim()).join(" ")), "Change Remove");
    await click("#setAccess [data-action=accessEdit]");
    await until(() => document.getElementById("setAccessPw") !== null);
    await page.$eval("#setAccessPw", (e) => { e.value = ""; });
    await page.type("#setAccessPw", "abc123");
    await click("#setAccess [data-action=accessSave]");
    await ctx.waitText("#setAccess .set-msg", /at least 8 characters/);
    eq("the typed password is still there", await page.$eval("#setAccessPw", (e) => e.value), "abc123");
    await click("#setAccess [data-action=accessCancel]");
    await until(() => document.getElementById("setAccessPw") === null);
    await click("#setAccess [data-action=accessAskRemove]");
    await until(() => document.activeElement?.id === "setAccessKeep"); // the safe answer has focus
    await click("#setAccess [data-action=accessRemove]");
    await until(() => /Anyone on your network/.test(document.querySelector("#setAccess [data-state]")?.textContent ?? ""));
    await until(() => document.getElementById("btnSignOut").hidden);
    eq("another browser gets the dashboard again", await strangerSees(base), "200");
    eq("Set a password is offered", await page.$eval("#setAccess [data-action=accessEdit]", (e) => e.textContent.trim()), "Set a password");
    eq("nothing to save", (await ctx.text("#btnSettingsSave")).trim(), "Save");
  }]
];
