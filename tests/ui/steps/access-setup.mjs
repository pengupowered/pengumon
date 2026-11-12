// The first-run question (dialogs/access-setup) and Settings > Sign-in. The `firstrun` variant starts a fresh install:
// no password from anywhere and the question unanswered, so the dialog opens by itself. Both choices are offered with
// "Anyone on my home network" preselected; "Anyone" is answered here against a stand-in for the server (the page itself
// answers POST /api/access/setup, so the server's question stays open: tests/test_access.py answers it for real), then
// "Only people with a password" for real: this browser stays signed in, the password is shown once with Copy, and another
// browser gets the sign-in page. Settings > Sign-in then never shows it again, asks for the current password to change or
// remove it (a wrong one is refused, and what was typed stays), makes a read token with the current password (shown
// once; it reads, and changes nothing), turns "Others may look" on only with the current password (Save sends it), and
// removes the password, which removes the read token too: open to everyone again.

import { loopback } from "../lib.mjs";
export const subject = "dialog:access-setup";
export const variants = ["firstrun"];
let FIRST_PASSWORD = "first-run-smoke-pw"; // made up for the test; the password in force (the Settings steps change it)
const MODAL = "accessSetupModal";
let readToken = ""; // made before the password is removed: removing it removes the read token too
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

  ["a group of radio buttons is one stop for the dialog's keyboard trap: Shift+Tab from the checked choice (not the first button) wraps to the last control, Tab from the last returns to it", async (ctx) => {
    const { click, eq, page, until } = ctx;
    await setupOpen(until);
    await page.$eval("#accessChoiceOpen", (e) => e.focus());
    await page.keyboard.press("ArrowDown"); // the second choice is checked and focused; the first one is not the group's stop any more
    await until(() => document.getElementById("accessChoicePassword").checked && document.activeElement.id === "accessChoicePassword");
    await page.keyboard.down("Shift");
    await page.keyboard.press("Tab");
    await page.keyboard.up("Shift");
    eq("Shift+Tab from the first stop wraps to the last control", await page.evaluate(() => document.activeElement.id), "btnAccessSetup");
    await page.keyboard.press("Tab");
    eq("and Tab from the last comes back to the checked choice", await page.evaluate(() => document.activeElement.id), "accessChoicePassword");
    await click("#accessChoiceOpen");
    await until(() => document.getElementById("accessSetupPassword").hidden);
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
    eq("the generated password is not left in the closed dialog", await page.evaluate(() => document.getElementById("accessSetupShown").textContent + document.getElementById("accessSetupPw").value), "");
    await until(() => !document.getElementById("btnSignOut").hidden); // signed in: Sign out in the header
    eq("another browser now gets the sign-in page", await strangerSees(base), "/login");
    await until(() => /^Live/.test(document.getElementById("wsText")?.textContent ?? "")); // the live view came back
    await page.reload({ waitUntil: "load" });
    await ctx.layoutMounted();
    ok(`still on the dashboard after a reload (${page.url()})`, new URL(page.url()).pathname === "/");
    eq("and not asked again", await ctx.modalOpen(MODAL), false);
  }],

  ["Settings > Sign-in never shows the password again, asks for the current one to change it, keeps what was typed after a refused Save", async (ctx) => {
    const { click, eq, ok, page, until } = ctx;
    await click("#btnSettings");
    await until(() => document.getElementById("setAccess") !== null);
    await page.$eval("#set-sec-access", (e) => e.scrollIntoView());
    ok("it says a password is needed", /Only people with the password/.test(await ctx.text("#setAccess [data-state]")));
    ok("the viewers switch is beside it", await page.$eval('#set-sec-access .set-row[data-key="access.allow_viewers"]', (e) => /Others may look/.test(e.textContent)));
    eq("Change and Remove, no Show", await page.$$eval("#setAccess .set-secret-state button", (els) => els.map((e) => e.textContent.trim()).join(" ")), "Change Remove");
    await click("#setAccess [data-action=accessEdit]");
    await until(() => document.activeElement?.id === "setAccessCurrent"); // the current password is asked first
    eq("it is a password field", await page.$eval("#setAccessCurrent", (e) => `${e.type} ${e.autocomplete}`), "password current-password");
    await page.$eval("#setAccessPw", (e) => { e.value = ""; });
    await page.type("#setAccessPw", "abc123");
    await click("#setAccess [data-action=accessSave]");
    await ctx.waitText("#setAccess .set-msg", /Enter the current password/);
    await page.type("#setAccessCurrent", FIRST_PASSWORD);
    await click("#setAccess [data-action=accessSave]");
    await ctx.waitText("#setAccess .set-msg", /at least 8 characters/);
    eq("the typed password is still there", await page.$eval("#setAccessPw", (e) => e.value), "abc123");
    // a wrong current password: the server refuses it (403, counted as a failed sign-in) and nothing changes
    await page.$eval("#setAccessCurrent", (e) => { e.value = ""; });
    await page.type("#setAccessCurrent", "not-the-password");
    await page.$eval("#setAccessPw", (e) => { e.value = ""; });
    await page.type("#setAccessPw", "a-new-password-1");
    // a 422 whose detail is a list (a request model failing validation) reads as one sentence, not "[object Object]"
    await page.evaluate(() => {
      const real = window.fetch;
      window.__realFetch = real;
      window.fetch = (url, opts) => (String(url).endsWith("/api/access/password") && opts?.method === "PUT"
        ? Promise.resolve(new Response(JSON.stringify({ detail: [{ msg: "Value error, Too easy to guess" }, { msg: "Not long enough" }] }), { status: 422, headers: { "content-type": "application/json" } }))
        : real(url, opts));
    });
    await click("#setAccess [data-action=accessSave]");
    await ctx.waitText("#setAccess .set-msg", /Too easy to guess · Not long enough/);
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    ctx.expectStatus(403);
    await click("#setAccess [data-action=accessSave]");
    await ctx.waitText("#setAccess .set-msg", /current password is not right/);
    ctx.expectStatus();
    eq("the new password is still typed", await page.$eval("#setAccessPw", (e) => e.value), "a-new-password-1");
    eq("focus is back on the current password", await page.evaluate(() => document.activeElement?.id), "setAccessCurrent");
    await click("#setAccess [data-action=accessCancel]");
    await until(() => document.getElementById("setAccessPw") === null);
    ok("still a password", /Only people with the password/.test(await ctx.text("#setAccess [data-state]")));
  }],

  ["closing Settings drops the password and the read token it has just shown: neither is in the document or kept by the module", async (ctx) => {
    const { click, eq, ok, page, until } = ctx;
    const SECOND = "second-smoke-pw-2"; // made up
    const modal = "settingsModal";
    const accessModule = await ctx.moduleUrl("../dialogs/settings/access.js");
    // Where a secret could still be: the document (markup, text, every field's value) and what the module would draw
    // again (accessHtml() renders from the module's variables, so a value it still holds shows up in it).
    const trace = (secrets) => page.evaluate(async (url, list) => {
      const { accessHtml } = await import(url);
      const where = [document.documentElement.outerHTML, document.documentElement.textContent, accessHtml(),
        ...[...document.querySelectorAll("input, textarea")].map((e) => e.value)].join("\n");
      return list.map((v) => where.includes(v)).join(); // "true,false": eq compares with ===
    }, accessModule, secrets);
    const closeSettings = async () => {
      await click("[data-action=settingsClose]");
      await until((m) => !document.getElementById(m).classList.contains("active"), modal);
    };
    const openSettings = async () => {
      await click("#btnSettings");
      await until(() => document.getElementById("setAccess") !== null);
      await page.$eval("#set-sec-access", (e) => e.scrollIntoView());
    };
    const changePassword = async (current, next) => {
      await click("#setAccess [data-action=accessEdit]");
      await until(() => document.activeElement?.id === "setAccessCurrent");
      await page.type("#setAccessCurrent", current);
      await page.$eval("#setAccessPw", (e) => { e.value = ""; });
      await page.type("#setAccessPw", next);
      await click("#setAccess [data-action=accessSave]");
      await until(() => document.getElementById("setAccessShown") !== null);
      eq("the new password is shown once", (await ctx.text("#setAccessShown")).trim(), next);
    };
    // the read token first: made, then Settings is closed while it is shown, and the document and the module are searched
    await click("#setReadToken [data-action=readTokenAskNew]");
    await until(() => document.activeElement?.id === "setReadTokenCurrent");
    await page.type("#setReadTokenCurrent", FIRST_PASSWORD);
    await click("#setReadToken [data-action=readTokenNew]");
    await until(() => document.getElementById("setReadTokenShown") !== null);
    const token = (await ctx.text("#setReadTokenShown")).trim();
    ok(`a read token (${token.slice(0, 4)}…)`, /^pgr_[A-Za-z0-9_-]{40,}$/.test(token));
    eq("while it is shown the module would draw it (so the check below can see it)", await trace([token]), "true");
    await closeSettings();
    eq("closed: the read token is nowhere in the document or the module", await trace([token]), "false");
    await openSettings();
    eq("it exists, and is not shown again", (await ctx.text("#setReadToken [data-state]")).trim(), "Made");
    ok("no token in the row", await page.$("#setReadTokenShown") === null);
    // the password: a new one removes the read token (the next step makes its own), and every later step uses the new one
    await changePassword(FIRST_PASSWORD, SECOND);
    FIRST_PASSWORD = SECOND;
    eq("while it is shown the module would draw it", await trace([SECOND]), "true");
    await closeSettings();
    eq("closed: the password is nowhere in the document or the module", await trace([SECOND]), "false");
    await openSettings();
    eq("the read token went with the old password", (await ctx.text("#setReadToken [data-state]")).trim(), "None");
  }],

  ["Settings > Sign-in makes a read token with the current password: shown once with Copy, it reads and changes nothing, Replace makes the old one stop working", async (ctx) => {
    const { base, click, eq, ok, page, until } = ctx;
    await until(() => document.getElementById("setReadToken") !== null);
    eq("none yet", (await ctx.text("#setReadToken [data-state]")).trim(), "None");
    await click("#setReadToken [data-action=readTokenAskNew]");
    await until(() => document.activeElement?.id === "setReadTokenCurrent"); // the current password is asked first
    eq("it is a password field, empty, its value in no attribute", await page.$eval("#setReadTokenCurrent", (e) => `${e.type} ${e.value} ${e.hasAttribute("value")}`), "password  false");
    await click("#setReadToken [data-action=readTokenNew]");
    await ctx.waitText("#setReadToken .set-msg", /Enter the current password/);
    await page.type("#setReadTokenCurrent", "not-the-password");
    ctx.expectStatus(403);
    await click("#setReadToken [data-action=readTokenNew]");
    await ctx.waitText("#setReadToken .set-msg", /current password is not right/);
    ctx.expectStatus();
    eq("what was typed stays, and is in no attribute", await page.$eval("#setReadTokenCurrent", (e) => `${e.value} ${e.hasAttribute("value")}`), "not-the-password false");
    await page.$eval("#setReadTokenCurrent", (e) => { e.value = ""; });
    await page.type("#setReadTokenCurrent", FIRST_PASSWORD);
    await click("#setReadToken [data-action=readTokenNew]");
    await until(() => document.getElementById("setReadTokenShown") !== null);
    const token = (await ctx.text("#setReadTokenShown")).trim();
    ok(`a read token (${token.slice(0, 4)}…)`, /^pgr_[A-Za-z0-9_-]{40,}$/.test(token));
    await click("#setReadToken [data-action=readTokenCopy]");
    await ctx.waitText("#setReadToken [data-action=readTokenCopy]", /Copied/);
    eq("Copy put it on the clipboard", await page.evaluate(() => window.__clip()), token);
    const asReader = async (method, path) => (await fetch(`${loopback(base)}${path}`, { method,
      headers: { Authorization: `Bearer ${token}`, Origin: loopback(base), "Content-Type": "application/json" }, body: method === "GET" ? undefined : "{}" })).status;
    eq("it reads", await asReader("GET", "/api/status"), 200);
    eq("it changes nothing", await asReader("POST", "/api/access/read-token"), 403);
    await click("#setReadToken [data-action=readTokenHide]");
    await until(() => document.getElementById("setReadTokenShown") === null);
    eq("never shown again: Replace and Remove", await page.$$eval("#setReadToken .set-secret-state button", (els) => els.map((e) => e.textContent.trim()).join(" ")), "Replace Remove");
    eq("it says one exists", (await ctx.text("#setReadToken [data-state]")).trim(), "Made");
    await click("#setReadToken [data-action=readTokenAskNew]");
    await until(() => document.activeElement?.id === "setReadTokenCurrent");
    eq("nothing typed before is left in it", await page.$eval("#setReadTokenCurrent", (e) => e.value), "");
    await page.type("#setReadTokenCurrent", FIRST_PASSWORD);
    await click("#setReadToken [data-action=readTokenNew]");
    await until(() => document.getElementById("setReadTokenShown") !== null);
    ok("a new one", (await ctx.text("#setReadTokenShown")).trim() !== token);
    eq("the old one no longer works", await asReader("GET", "/api/status"), 401);
    await click("#setReadToken [data-action=readTokenHide]");
    await click("#setReadToken [data-action=readTokenAskRemove]");
    await until(() => document.activeElement?.id === "setReadTokenCurrent"); // the current password is asked first
    ok("Keep it is offered", await page.$("#setReadTokenKeep") !== null);
    await page.type("#setReadTokenCurrent", FIRST_PASSWORD);
    await click("#setReadToken [data-action=readTokenRemove]");
    await until(() => /None/.test(document.querySelector("#setReadToken [data-state]")?.textContent ?? ""));
    // made once more, for the removal of the password below: it goes with it
    await click("#setReadToken [data-action=readTokenAskNew]");
    await until(() => document.activeElement?.id === "setReadTokenCurrent");
    await page.type("#setReadTokenCurrent", FIRST_PASSWORD);
    await click("#setReadToken [data-action=readTokenNew]");
    await until(() => document.getElementById("setReadTokenShown") !== null);
    readToken = (await ctx.text("#setReadTokenShown")).trim();
    await click("#setReadToken [data-action=readTokenHide]");
  }],

  ["Settings > Sign-in: Others may look is saved only with the current password, which a box under the switch asks for", async (ctx) => {
    const { base, click, eq, ok, page, until } = ctx;
    const row = '#set-sec-access .set-row[data-key="access.allow_viewers"]';
    const box = "#setViewersConfirm";
    ok("no box while the switch is off", await page.$eval(box, (e) => e.hasAttribute("data-inactive")));
    await click(`${row} input[type=checkbox]`);
    await until((sel) => !document.querySelector(sel).hasAttribute("data-inactive"), box);
    await click("#btnSettingsSave");
    await until(() => /Enter the current password under/.test(document.getElementById("settingsNotes").textContent));
    eq("nothing was sent: Save still has the change", (await ctx.text("#btnSettingsSave")).trim(), "Save 1 change");
    await page.type(`${box} input`, "not-the-password");
    ctx.expectStatus(403);
    await click("#btnSettingsSave");
    await until(() => /current password is not right/.test(document.getElementById("settingsNotes").textContent));
    ctx.expectStatus();
    const viewers = async () => (await fetch(`${loopback(base)}/api/status`, { redirect: "manual" })).status;
    eq("still nobody gets in without the password", await viewers(), 401);
    await page.$eval(`${box} input`, (e) => { e.value = ""; });
    await page.type(`${box} input`, FIRST_PASSWORD);
    await click("#btnSettingsSave");
    await until(() => document.querySelector("#btnSettingsSave").textContent.trim() === "Save" && !document.getElementById("settingsFields").disabled);
    eq("saved: others may look now", await viewers(), 200);
    await until((sel) => document.querySelector(sel)?.hasAttribute("data-inactive"), box);
    eq("the box is empty again", await page.$eval(`${box} input`, (e) => e.value), "");
    await click(`${row} input[type=checkbox]`); // off again: no password asked
    await until(() => document.querySelector("#btnSettingsSave").textContent.trim() === "Save 1 change");
    ok("turning it off asks nothing", await page.$eval(box, (e) => e.hasAttribute("data-inactive")));
    await click("#btnSettingsSave");
    await until(() => document.querySelector("#btnSettingsSave").textContent.trim() === "Save" && !document.getElementById("settingsFields").disabled);
    eq("off: the sign-in again", await viewers(), 401);
  }],

  ["Settings > Sign-in: Remove asks for the current password, then opens the dashboard to everyone again", async (ctx) => {
    const { base, click, eq, ok, page, until } = ctx;
    await click("#setAccess [data-action=accessAskRemove]");
    await until(() => document.activeElement?.id === "setAccessRemoveCurrent"); // the current password is asked first
    await click("#setAccess [data-action=accessRemove]");
    await ctx.waitText("#setAccess .set-msg", /Enter the current password/);
    await page.type("#setAccessRemoveCurrent", FIRST_PASSWORD);
    await click("#setAccess [data-action=accessRemove]");
    await until(() => /Anyone on your network/.test(document.querySelector("#setAccess [data-state]")?.textContent ?? ""));
    ok(`the read token went with it (${readToken.slice(0, 4)}…)`, readToken !== "" && /None/.test(await ctx.text("#setReadToken [data-state]")));
    await until(() => /read token was removed/.test(document.getElementById("toast")?.textContent ?? ""));
    await until(() => document.getElementById("btnSignOut").hidden);
    eq("another browser gets the dashboard again", await strangerSees(base), "200");
    eq("Set a password is offered", await page.$eval("#setAccess [data-action=accessEdit]", (e) => e.textContent.trim()), "Set a password");
    eq("nothing to save", (await ctx.text("#btnSettingsSave")).trim(), "Save");
  }]
];
