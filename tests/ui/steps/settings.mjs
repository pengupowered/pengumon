// The Settings page (header > Settings, or #settings): sections, rows with their default, badge and Reset, nothing applied until
// Save, 409, versions, export and import, the preview (the real replay over the seeded samples), secrets that are never read
// back, the list editors (the devices editor's own steps mount it in a scratch container with the page's schema), view-only
// mode and the phone. Everything runs against the real settings API of the seeded app.
// The `full` variant has PENGUMON_LOG_LEVEL set (test_ui_smoke.py) for the "From environment" row.

import { loopback } from "../lib.mjs";

export const subject = "dialog:settings";
export const variants = ["full"];

const SECTIONS = ["outage", "alerts", "line", "devices", "probing", "retention", "privacy", "discovery", "display", "advanced", "about"];
const LOSS = "classifier.consecutive_failures_for_incident";
const SECRET_URL = "https://hooks.example.test/T000/very-secret-path-4471";

const settingsOpen = async ({ click, until }) => {
  await click("#btnSettings");
  await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
};
const settingsValuesNow = (page) => page.evaluate(async () => (await (await fetch("/api/settings/values", { cache: "no-store" })).json()));
const settingsRow = (key) => `#settingsSections .set-row[data-key="${key}"]`;
// types like a person: select what is there, then type
const settingsType = async (page, key, text) => {
  const sel = `${settingsRow(key)} input[data-key]`;
  await page.focus(sel);
  await page.$eval(sel, (e) => e.select());
  await page.keyboard.press("Backspace");
  if (text !== "") await page.type(sel, text);
};
const settingsSaveLabel = (page) => page.$eval("#btnSettingsSave", (b) => b.textContent.trim());
// Cancel with a draft asks; Discard throws it away
const settingsDiscard = async ({ click, until }) => {
  await click("#btnSettingsCancel");
  await until(() => !document.getElementById("settingsConfirm").hidden);
  await click("#btnConfirmYes");
  await until(() => !document.getElementById("settingsModal").classList.contains("active"));
};
const settingsSave = async ({ click, until }) => {
  await click("#btnSettingsSave");
  await until(() => document.getElementById("toast")?.classList.contains("on") && document.querySelector("#btnSettingsSave").textContent.trim() === "Save");
};
// the page answers the way the dispatcher does for a hidden button (an action name with no visible control)
const settingsAct = (page, action, arg) => page.evaluate((a, g) => { const b = document.createElement("button"); b.dataset.action = a; if (g) b.dataset.arg = g; document.body.appendChild(b); b.click(); b.remove(); }, action, arg);

// the element's top is inside the dialog body's visible part (a note at the top of a body scrolled to the bottom is not)
const inView = (page, sel) => page.$eval(sel, (e) => {
  const r = e.getBoundingClientRect(), b = document.getElementById("settingsBody").getBoundingClientRect();
  return r.top >= b.top - 1 && r.top < b.bottom;
});
const scrollBottom = (page) => page.$eval("#settingsBody", (e) => { e.scrollTop = e.scrollHeight; });

export const steps = [
  ["Settings opens from the header button and from #settings; the sections are in order, About last", async (ctx) => {
    const { eq, page, until, text } = ctx;
    await settingsOpen(ctx);
    eq("sections", await page.$$eval("#settingsSections [data-section]", (els) => els.map((e) => e.dataset.section).join()), SECTIONS.join());
    eq("the hash", await page.evaluate(() => location.hash), "#settings");
    ctx.ok("quiet hours show the time in their zone", /It is \d\d:\d\d there now/.test(await page.$eval(`${settingsRow("alerts.quiet_hours.start")} [data-meta]`, (e) => e.textContent)));
    ctx.ok("a title", /Outage rules/.test(await text("#set-sec-outage-t")));
    await ctx.click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
    eq("the hash is gone again", await page.evaluate(() => location.hash), "");
    await page.evaluate(() => { history.replaceState(null, "", "/#settings"); });
    await page.reload({ waitUntil: "load" });
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
    await ctx.click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
    // the phone menu has it too
    await ctx.phone(async () => {
      await ctx.click(".tb-more");
      await until(() => !document.getElementById("headerMenu").hidden);
      ctx.ok("the header menu lists Settings", await page.$eval("#headerMenu [data-action=openSettings]", (e) => /Settings/.test(e.textContent)));
      await ctx.click(".tb-more");
    });
  }],

  ["a value set by an environment variable says so and cannot be edited", async (ctx) => {
    const { eq, ok, page } = ctx;
    await settingsOpen(ctx);
    const row = settingsRow("server.log_level");
    ok("Advanced is collapsed", await page.$eval("#set-sec-advanced", (e) => !e.open));
    await ctx.click("#settingsRail [data-arg=advanced]");
    eq("opened by the rail", await page.$eval("#set-sec-advanced", (e) => e.open), true);
    eq("badge", await page.$eval(`${row} [data-badge=env]`, (e) => e.textContent), "From environment · PENGUMON_LOG_LEVEL");
    ok("the choices are disabled", await page.$$eval(`${row} button[data-action=settingsChoice], ${row} select`, (els) => els.length > 0 && els.every((e) => e.disabled)));
    eq("no Reset", await ctx.count(`${row} [data-action=settingsReset]`), 0);
    eq("Advanced says why it is collapsed", await page.$eval("#set-sec-advanced > summary", (e) => /Change these only if you know why/.test(e.textContent)), true);
    await ctx.click("#btnSettingsCancel");
  }],

  ["a changed value counts as one change; Cancel asks and discards it; the live value does not move", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const before = await settingsValuesNow(page);
    await page.evaluate(() => { history.replaceState({ e0: 1 }, ""); history.pushState({ e1: 1 }, ""); }); // the page the user came from, and this one
    await settingsOpen(ctx);
    ok("Save is off with no change", await page.$eval("#btnSettingsSave", (b) => b.disabled));
    eq("the default is shown", await page.$eval(`${settingsRow(LOSS)} .set-def`, (e) => e.textContent.trim()), "Default 3 s");
    await settingsType(page, LOSS, "5");
    eq("one change", await settingsSaveLabel(page), "Save 1 change");
    eq("badge Unsaved", await page.$eval(`${settingsRow(LOSS)} [data-badge=unsaved]`, (e) => e.textContent), "Unsaved");
    ok("the header meta counts it", /1 unsaved change/.test(await ctx.text("#settingsMeta")));
    // Tab out of the box: focus lands on the next control (Reset to default), never on <body>
    await page.keyboard.press("Tab");
    eq("Tab goes on to Reset to default", await page.evaluate(() => document.activeElement.dataset.action), "settingsReset");
    // Back with a draft asks instead of losing it; Keep editing returns the focus
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("settingsConfirm").hidden);
    ok("still open, draft kept", await ctx.modalOpen("settingsModal") && (await settingsSaveLabel(page)) === "Save 1 change");
    await ctx.click("#btnConfirmNo");
    eq("Keep editing returns to the control that had focus", await page.evaluate(() => document.activeElement.dataset.action), "settingsReset");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("settingsConfirm").hidden);
    await page.keyboard.press("Escape");
    await until(() => document.getElementById("settingsConfirm").hidden);
    await settingsDiscard(ctx);
    // the draft's history entry is gone with it: one Back goes where the user came from, not to a copy of this page
    await page.evaluate(() => history.back());
    await until(() => history.state && history.state.e0 === 1);
    const after = await settingsValuesNow(page);
    eq("the value did not move", after.values[LOSS], before.values[LOSS]);
    eq("nor the revision", after.revision, before.revision);
    await settingsOpen(ctx);
    eq("a fresh draft is empty", await settingsSaveLabel(page), "Save");
    await ctx.click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
  }],

  ["Save applies it: the value is live, the revision goes up, Changed shows; Reset to default then Save puts it back", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const before = await settingsValuesNow(page);
    await settingsOpen(ctx);
    await settingsType(page, LOSS, "5");
    await settingsSave(ctx);
    const now = await settingsValuesNow(page);
    eq("live value", now.values[LOSS], 5);
    ok("revision went up", now.revision > before.revision);
    eq("badge Changed", await page.$eval(`${settingsRow(LOSS)} [data-badge=changed]`, (e) => e.textContent), "Changed");
    eq("no Unsaved", await ctx.count(`${settingsRow(LOSS)} [data-badge=unsaved]`), 0);
    ok("the header meta has the revision", new RegExp(`revision ${now.revision}`).test(await ctx.text("#settingsMeta")));
    await ctx.click(`${settingsRow(LOSS)} [data-action=settingsReset]`);
    eq("input shows the default", await page.$eval(`${settingsRow(LOSS)} input[data-key]`, (e) => e.value), "3");
    eq("one change", await settingsSaveLabel(page), "Save 1 change");
    await settingsSave(ctx);
    eq("back to the default", (await settingsValuesNow(page)).values[LOSS], 3);
    eq("no Changed badge", await ctx.count(`${settingsRow(LOSS)} [data-badge=changed]`), 0);
    // the Grafana address shows its header links at once, and drops them again, without a reload
    const GRAFANA = "display.grafana_url";
    eq("no Grafana links yet", await page.$eval("#tbGrafana", (e) => e.hidden), true);
    await settingsType(page, GRAFANA, "http://grafana.example.test:3000");
    await settingsSave(ctx);
    await until(() => !document.getElementById("tbGrafana").hidden);
    ok("the links use the new address", await page.$$eval("#grafanaMenu a", (as) => as.length === 3 && as.every((a) => a.href.startsWith("http://grafana.example.test:3000/"))));
    await ctx.click("#btnSettingsCancel");  // the address is reset by the Export / Import step (Saves are rate limited: 20 a minute)
  }],

  ["a value kept at its default (as the import keeps what config.yaml set) shows no Changed, no Saved and no Reset", async (ctx) => {
    const { eq, page } = ctx;
    const put = (values) => page.evaluate(async (v) => {
      const state = await (await fetch("/api/settings")).json();
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, values: v, lists: {}, secrets: {} }) });
      if (!res.ok) throw new Error(`PUT ${res.status}`);
    }, values);
    await put({ [LOSS]: 3 });
    await settingsOpen(ctx);
    eq("no Changed badge", await ctx.count(`${settingsRow(LOSS)} [data-badge=changed]`), 0);
    eq("no Saved badge", await ctx.count(`${settingsRow(LOSS)} [data-badge=saved]`), 0);
    eq("no Reset", await ctx.count(`${settingsRow(LOSS)} [data-action=settingsReset]`), 0);
    await ctx.click("#btnSettingsCancel");
    await put({ [LOSS]: null });
  }],

  ["an out-of-range value gets its message in the browser and Save is off; a server warning shows as a note", async (ctx) => {
    const { eq, ok, page, until, text } = ctx;
    await settingsOpen(ctx);
    const key = "database.retention_days";
    await settingsType(page, key, "0");
    ok("the message", /at least 1|from 1 to 90/i.test(await text(`${settingsRow(key)} [data-msg]`)));
    ok("Save is disabled", await page.$eval("#btnSettingsSave", (b) => b.disabled));
    eq("nothing is in the draft", await settingsSaveLabel(page), "Save");
    await settingsType(page, key, "abc");
    ok("not a number", /number/i.test(await text(`${settingsRow(key)} [data-msg]`)));
    await settingsType(page, key, "5");
    eq("fixed", await text(`${settingsRow(key)} [data-msg]`), "");
    await until(() => !document.getElementById("btnSettingsSave").disabled);
    await until(() => /Under 7 days|7 days/.test(document.querySelector(`#settingsSections .set-row[data-key="database.retention_days"] .set-warn`)?.textContent ?? ""));
    await settingsDiscard(ctx);
  }],

  ["Preview over the seeded samples shows incidents instead of incidents; a further edit marks it out of date", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    ok("no Preview without a rule in the draft", await page.$eval("#btnSettingsPreview", (b) => b.hidden));
    await settingsType(page, "database.retention_days", "9");
    ok("none for a retention change", await page.$eval("#btnSettingsPreview", (b) => b.hidden));
    // the seeded gateway drop lost about 90 s of probes: with 120 s before a loss outage starts it would not have opened
    await settingsType(page, LOSS, "120");
    await until(() => !document.getElementById("btnSettingsPreview").hidden);
    await ctx.click("#btnSettingsPreview");
    await until(() => /instead of/.test(document.getElementById("settingsPreviewResult")?.textContent ?? ""));
    ok("counts", /\d+ incidents? instead of \d+/.test(await ctx.text("#settingsPreviewResult")));
    ok("the recorded count is explained in the page, not in a tooltip", /Pengumon recorded \d+ incidents? in that time\. That number is not comparable/.test(await ctx.text("#settingsPreview [data-recorded]")));
    ok("a change list", /Would no longer open|Would newly open|Would open differently/.test(await ctx.text("#settingsPreview")));
    ok("not stale", !(await ctx.text("#settingsPreviewResult")).includes("Out of date"));
    await settingsType(page, LOSS, "130");
    await until(() => /Out of date/.test(document.getElementById("settingsPreviewResult")?.textContent ?? ""));
    await ctx.shots("settings-preview");
    // a running preview can be cancelled: the server drops it and the panel says so
    await ctx.click("#settingsPreview [data-action=settingsPreviewRun]");
    await until(() => !!document.querySelector("#settingsPreview [data-action=settingsPreviewCancel]"));
    await ctx.click("#settingsPreview [data-action=settingsPreviewCancel]");
    await until(() => /Preview cancelled/.test(document.getElementById("settingsPreview").textContent));
    ok("and can be run again", await page.$eval("#settingsPreview [data-action=settingsPreviewRun]", (b) => !b.disabled));
    await settingsDiscard(ctx);
    eq("the panel is gone with the draft", await page.$eval("#settingsPreview", (e) => e.hidden), true);
  }],

  ["a DNS lookup is added and saved; it appears in Path health's matrix", async (ctx) => {
    const { eq, ok, page, until, waitCount } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=probing]");
    const before = await ctx.count("#hopMatrixBody tr");
    await ctx.click('[data-list=dns_probes] [data-la=add]');
    const card = '[data-list=dns_probes] .set-card:last-of-type';
    ok("the new card is open", await page.$eval(card, (e) => e.open));
    await page.type(`${card} [data-f=domain]`, "example.org");
    await page.$eval(`${card} [data-f=domain]`, (e) => e.dispatchEvent(new Event("change", { bubbles: true })));
    await page.$eval(`${card} [data-f=name]`, (e) => { e.focus(); e.select(); });
    await page.type(`${card} [data-f=name]`, "DNS Lookup: example.org");
    ok("no nameserver says what that means", await page.$eval(`${card} [data-hint=nameserver]`, (e) => !e.hidden && /system resolver/.test(e.textContent)));
    await page.type(`${card} [data-f=nameserver]`, "198.51.100.53");
    ok("the note goes", await page.$eval(`${card} [data-hint=nameserver]`, (e) => e.hidden));
    eq("one change", await settingsSaveLabel(page), "Save 1 change");
    await settingsSave(ctx);
    await waitCount("#hopMatrixBody tr", before + 1);
    ok("the row is in the matrix", /example\.org/.test(await page.$eval("#hopMatrixBody", (e) => e.textContent)));
    ok("the id of a saved lookup is read-only", await page.$eval(`${card} [data-f=id]`, (e) => e.readOnly));
    await ctx.click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
  }],

  ["a service check: a bad timeout is refused with the server's words under the field, then fixed and saved", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=devices]");
    await ctx.click("[data-list=services] [data-la=add]");
    const card = "[data-list=services] .set-card:last-of-type";
    await page.$eval(`${card} [data-f=name]`, (e) => { e.focus(); e.select(); });
    await page.type(`${card} [data-f=name]`, "Extra app");
    await page.$eval(`${card} [data-f=name]`, (e) => e.dispatchEvent(new Event("change", { bubbles: true })));
    await page.type(`${card} [data-f=url]`, "http://192.0.2.44:8080/health");
    await page.$eval(`${card} [data-f=timeout_seconds]`, (e) => { e.focus(); e.select(); });
    await page.type(`${card} [data-f=timeout_seconds]`, "0.1");
    await until(() => (document.querySelector("[data-list=services] .set-card:last-of-type [data-lmsg$='timeout_seconds']")?.textContent ?? "") !== "");
    ok("the card is marked", await page.$eval(`${card} summary`, (e) => /Needs a fix/.test(e.textContent)));
    ok("Save is off", await page.$eval("#btnSettingsSave", (b) => b.disabled));
    await page.$eval(`${card} [data-f=timeout_seconds]`, (e) => { e.focus(); e.select(); });
    await page.type(`${card} [data-f=timeout_seconds]`, "4");
    await until(() => !document.getElementById("btnSettingsSave").disabled);
    eq("the id follows the name", await page.$eval(`${card} [data-f=id]`, (e) => e.value), "extra_app");
    await settingsSave(ctx);
    const state = await page.evaluate(async () => (await (await fetch("/api/settings")).json()).lists.services.items.map((i) => i.id));
    ok(`saved (${state})`, state.includes("extra_app"));
    // move it up, remove it: structure edits count as one list change
    await ctx.click("[data-list=services] .set-card:last-of-type [data-la=up]");
    eq("a list is one change", await settingsSaveLabel(page), "Save 1 change");
    await ctx.click("[data-list=services] .set-card:nth-of-type(2) [data-la=remove]");
    await ctx.click("[data-list=services] .set-card:last-of-type [data-la=remove]");
    await settingsDiscard(ctx);
  }],

  ["a webhook URL is write-only: entered, saved, shown as Set · host, and nowhere in the page or the API", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=alerts]");
    await ctx.click("[data-list=webhooks] [data-la=add]");
    const card = "[data-list=webhooks] .set-card:last-of-type";
    await page.$eval(`${card} [data-f=name]`, (e) => { e.focus(); e.select(); });
    await page.type(`${card} [data-f=name]`, "Home chat");
    await page.$eval(`${card} [data-f=name]`, (e) => e.dispatchEvent(new Event("change", { bubbles: true })));
    ok("URL needed, and Save is off", await page.$eval(`${card} summary`, (e) => /URL needed/.test(e.textContent)) && await page.$eval("#btnSettingsSave", (b) => b.disabled));
    await ctx.click(`${card} [data-action=settingsSecretReplace]`);
    await page.type(`${card} input[type=password]`, SECRET_URL);
    await until(() => !document.getElementById("btnSettingsSave").disabled);
    // the server's check (400 ms debounce) must count the typed URL too: wait it out, Save stays on
    await page.evaluate(() => new Promise((r) => setTimeout(r, 1500)));
    ok("Save is still on after the server's check", await page.$eval("#btnSettingsSave", (b) => !b.disabled) && !(await page.$eval(`${card}`, (e) => /a webhook needs its URL/.test(e.textContent))));
    ok("the URL needed mark is gone", await page.$eval(`${card} summary`, (e) => !/URL needed/.test(e.textContent)));
    await settingsSave(ctx);
    const row = "[data-list=webhooks] .set-card:last-of-type .set-slot[data-slot^='webhook.']";
    eq("Set · host", await page.$eval(`${row} [data-state]`, (e) => e.textContent), "Set · hooks.example.test");
    const leak = await page.evaluate((secret) => {
      const inInputs = [...document.querySelectorAll("input, textarea")].some((i) => i.value.includes(secret));
      return { dom: document.documentElement.outerHTML.includes(secret) || inInputs };
    }, SECRET_URL);
    eq("the page does not hold the URL", leak.dom, false);
    const api = await page.evaluate(async () => [await (await fetch("/api/settings")).text(), await (await fetch("/api/settings/values")).text(), await (await fetch("/api/settings/schema")).text()].join("\n"));
    ok("neither does the API", !api.includes("very-secret-path"));
    // a header is a secret slot too
    await page.type(`${card} [data-hdr-name]`, "Authorization");
    await ctx.click(`${card} [data-la=header-add]`);
    ok("a header row, not set", await page.$eval(`${card} .set-hdr .set-slot [data-state]`, (e) => e.textContent === "Not set"));
    await settingsDiscard(ctx);
  }],

  ["Versions: an earlier one loads as a draft and Save restores it", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await settingsType(page, LOSS, "8");
    await settingsSave(ctx);
    eq("live", (await settingsValuesNow(page)).values[LOSS], 8);
    await page.$eval("#settingsBody", (e) => { e.scrollTop = e.scrollHeight; });  // opened while scrolled down, it comes into view
    await ctx.click("#btnSettingsVersions");
    await until(() => document.querySelectorAll("#settingsVersions .set-vers li").length >= 2);
    ok("the versions are in view", await page.$eval("#settingsVersions", (e) => {
      const r = e.getBoundingClientRect(), b = document.getElementById("settingsBody").getBoundingClientRect();
      return r.top >= b.top - 1 && r.top < b.bottom;
    }));
    ok("the newest is current", await page.$eval("#settingsVersions .set-vers li", (e) => /current/.test(e.textContent)));
    await scrollBottom(page);  // the loaded values are far below the list: the note must still come into view
    await page.$eval("#settingsVersions .set-vers li:last-child [data-action=settingsLoadVersion]", (b) => b.click());
    await until(() => /^Save \d+ change/.test(document.querySelector("#btnSettingsSave").textContent.trim()));
    ok("a note says it is a draft", /loaded as a draft/.test(await ctx.text("#settingsNotes")));
    ok("and it is in view", await inView(page, "#settingsNotes"));
    eq("the input shows the old value", await page.$eval(`${settingsRow(LOSS)} input[data-key]`, (e) => e.value), "3");
    await settingsSave(ctx);
    eq("restored", (await settingsValuesNow(page)).values[LOSS], 3);
    await ctx.click("#btnSettingsCancel");
  }],

  ["Export downloads YAML without the webhook URL; Import makes a draft with its notes and applies nothing", async (ctx) => {
    const { eq, ok, page, until, outDir, fs, path } = ctx;
    await settingsOpen(ctx);
    ok("Export is shown to an editor", await page.$eval("#settingsExport", (e) => !e.hidden));
    // Export shows the YAML in the dialog, with Copy and a download link: a file downloaded over plain HTTP from
    // another machine is blocked or warned about by Chrome, so Copy is the way that always works. Opened from the
    // footer with the body scrolled down, it comes into view.
    await page.$eval("#settingsBody", (e) => { e.scrollTop = e.scrollHeight; });
    await ctx.click("#settingsExport");
    await until(() => document.getElementById("settingsExportText")?.value.startsWith("# Pengumon settings"));
    ok("the export is in view", await page.$eval("#settingsExportView", (e) => {
      const r = e.getBoundingClientRect(), b = document.getElementById("settingsBody").getBoundingClientRect();
      return r.top >= b.top - 1 && r.top < b.bottom;
    }));
    eq("the download link", await page.$eval("#settingsExportView a[download]", (e) => e.getAttribute("href")), "/api/settings/export");
    // over plain HTTP (the run's origin is not a secure context, so no navigator.clipboard) Copy still copies, and the page says why to prefer it
    ok("the plain-HTTP note", /served over plain HTTP/.test(await ctx.text("#settingsExportView")));
    await ctx.click("#settingsExportView [data-action=settingsExportCopy]");
    await until(() => document.getElementById("settingsExportNote").textContent !== "");
    eq("Copy without the Clipboard API", await ctx.text("#settingsExportNote"), "Copied.");
    eq("what was copied", await page.evaluate(() => window.__clip()), await page.$eval("#settingsExportText", (e) => e.value));
    await ctx.click("#settingsExport");
    ok("Export closes again", await page.$eval("#settingsExportView", (e) => e.hidden));
    const exported = await page.evaluate(async () => { const r = await fetch("/api/settings/export"); return { status: r.status, body: await r.text() }; });
    eq("export status", exported.status, 200);
    ok("a header line", exported.body.startsWith("# Pengumon settings"));
    ok("no URL in it", !exported.body.includes("very-secret-path") && !exported.body.includes("hooks.example.test"));
    const file = path.join(outDir, "import-settings.yaml");
    fs.writeFileSync(file, "classifier:\n  degraded_loss_pct: 21\nserver:\n  port: 9999\n");
    const input = await page.$("#settingsImportFile");
    await input.uploadFile(file);
    await until(() => /Read import-settings\.yaml/.test(document.getElementById("settingsNotes").textContent));
    ok("the file's note is shown", /left in config\.yaml: server\.port/.test(await ctx.text("#settingsNotes")));
    // the file is the whole document: what it does not name goes back to its default, so there are more changes than the one line
    ok("the draft", /^Save \d+ changes?$/.test(await settingsSaveLabel(page)));
    eq("the input", await page.$eval(`${settingsRow("classifier.degraded_loss_pct")} input[data-key]`, (e) => e.value), "21");
    eq("nothing applied", (await settingsValuesNow(page)).values["classifier.degraded_loss_pct"], 15);
    // a second import over a draft asks first
    await input.uploadFile(file);
    await until(() => !document.getElementById("settingsConfirm").hidden);
    await ctx.click("#btnConfirmNo");
    ok("Keep editing puts the focus back on Cancel", await page.evaluate(() => document.activeElement.id === "btnSettingsCancel"));
    await settingsDiscard(ctx);
    // a failed Import from the bottom of the page shows its note where the person is looking
    await settingsOpen(ctx);
    await scrollBottom(page);
    const bad = path.join(outDir, "bad-settings.yaml");
    fs.writeFileSync(bad, "- not\n- a mapping\n");
    ctx.expectStatus(422);  // the server refuses a list as settings
    await (await page.$("#settingsImportFile")).uploadFile(bad);
    await until(() => /was not imported/.test(document.getElementById("settingsNotes").textContent));
    ctx.expectStatus();
    ok("the failed import's note is in view", await inView(page, "#settingsNotes"));
    // a value the checks refuse keeps the current one (55 here, not the default 60); an unknown key and a secret get a note
    await ctx.click("#btnSettingsCancel");
    const putValues = (values) => page.evaluate(async (v) => {
      const state = await (await fetch("/api/settings")).json();
      await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, values: v, lists: {}, secrets: {} }) });
    }, values);
    await putValues({ "classifier.busy_load_pct": 55, [LOSS]: 3 });  // 3 is the default: stored at its default, as the first import leaves values
    await settingsOpen(ctx);
    const odd = path.join(outDir, "odd-settings.yaml");
    fs.writeFileSync(odd, "classifier:\n  busy_load_pct: 500\nntfy:\n  url: https://ntfy.example.test/topic\nbogus:\n  thing: 1\n");
    await (await page.$("#settingsImportFile")).uploadFile(odd);
    await until(() => /Read odd-settings\.yaml/.test(document.getElementById("settingsNotes").textContent));
    const notes = await ctx.text("#settingsNotes");
    ok(`the refused value is named in words (${notes})`, /Busy line from: .*Left as it is now/.test(notes) && !/Input should/.test(notes));
    ok("the secret and the unknown key have notes", /ntfy\.url: secrets are entered on the page/.test(notes) && /bogus\.thing: not a setting/.test(notes));
    eq("the refused setting is not reset", await page.$eval(`${settingsRow("classifier.busy_load_pct")} input[data-key]`, (e) => e.value), "55");
    await settingsDiscard(ctx);
    // Export, then Import of the same file changes nothing, even with a value stored at its default
    const round = path.join(outDir, "round-trip.yaml");
    fs.writeFileSync(round, await page.evaluate(async () => (await fetch("/api/settings/export")).text()));
    await settingsOpen(ctx);
    await (await page.$("#settingsImportFile")).uploadFile(round);
    await until(() => /Read round-trip\.yaml/.test(document.getElementById("settingsNotes").textContent));
    eq("an exported file imported again is no change", await settingsSaveLabel(page), "Save");
    await ctx.click("#btnSettingsCancel");
    await putValues({ "classifier.busy_load_pct": null, [LOSS]: null, "display.grafana_url": null });
  }],

  ["a part changed elsewhere is a 409: theirs is kept, the rest of the draft stays and saves", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const key = "classifier.local_latency_spike_threshold_ms";
    const other = "classifier.gateway_latency_spike_threshold_ms";
    await settingsOpen(ctx);
    await settingsType(page, key, "40");
    await settingsType(page, other, "44");
    eq("two changes", await settingsSaveLabel(page), "Save 2 changes");
    // someone else saves the same setting meanwhile (another tab): the page's base revision is now behind
    await page.evaluate(async (k) => {
      const state = await (await fetch("/api/settings")).json();
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, values: { [k]: 31 }, lists: {}, secrets: {} }) });
      if (!res.ok) throw new Error(`PUT ${res.status}`);
    }, key);
    ctx.expectStatus(409); // the real conflict is a real 409
    await scrollBottom(page);
    await ctx.click("#btnSettingsSave");
    await until(() => /Changed elsewhere/.test(document.getElementById("settingsNotes").textContent));
    ctx.expectStatus();
    ok("the note is in view, wherever the page was scrolled", await inView(page, "#settingsNotes"));
    ok("the note names it", /Latency spike levels: Router/.test(await ctx.text("#settingsNotes")));
    eq("theirs is shown", await page.$eval(`${settingsRow(key)} input[data-key]`, (e) => e.value), "31");
    eq("the rest stays", await settingsSaveLabel(page), "Save 1 change");
    eq("the other input", await page.$eval(`${settingsRow(other)} input[data-key]`, (e) => e.value), "44");
    await settingsSave(ctx);
    const v = (await settingsValuesNow(page)).values;
    eq("theirs", v[key], 31);
    eq("ours", v[other], 44);
    await ctx.click("#btnSettingsCancel");
  }],

  ["the help texts quote the live values: hop colours, busy level, router and DOCSIS levels", async (ctx) => {
    const { eq, ok, openHelp, page, until } = ctx;
    await page.evaluate(async () => {
      const state = await (await fetch("/api/settings")).json();
      const body = { base_revision: state.revision, lists: {}, secrets: {}, values: {
        "prober.hop_status.local_latency_ms": 33, "classifier.busy_load_pct": 71, "router_levels.cpu_warning_pct": 77, "line_signal.ds_snr_warning_db": 36 } };
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) throw new Error(`PUT ${res.status}`);
    });
    // the page re-reads when told the settings changed (the live message's settings_revision); here it is asked directly
    await page.evaluate(async (url) => { await (await import(url)).refreshSettingsValues(); }, await ctx.moduleUrl("settings-values.js"));
    const hop = await openHelp("hop.latency");
    ok(`hop colours: ${hop}`, /33 ms for local/.test(hop) && !/20 ms for local/.test(hop));
    await page.keyboard.press("Escape");
    const load = await openHelp("line.load");
    ok(`busy level: ${load}`, /Above 71%/.test(load));
    await page.keyboard.press("Escape");
    const cpu = await openHelp("router.cpu");
    ok(`router level: ${cpu}`, /Above 77%/.test(cpu));
    await page.keyboard.press("Escape");
    const snr = await openHelp("docsis.snr");
    ok(`DOCSIS level: ${snr}`, /36 dB and above is good/.test(snr));
    await page.keyboard.press("Escape");
    await until(() => document.getElementById("helpPopover").hidden);
    eq("defaults still quoted where unchanged", /Above 85%/.test(await openHelp("router.mem")), true);
    await page.keyboard.press("Escape");
    // put them back
    await page.evaluate(async () => {
      const state = await (await fetch("/api/settings")).json();
      const values = Object.fromEntries(["prober.hop_status.local_latency_ms", "classifier.busy_load_pct", "router_levels.cpu_warning_pct", "line_signal.ds_snr_warning_db"].map((k) => [k, null]));
      await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, lists: {}, secrets: {}, values }) });
    });
  }],

  ["the latency chart opens on the default range from Settings", async (ctx) => {
    const { eq, page, until } = ctx;
    await page.evaluate(async () => {
      const state = await (await fetch("/api/settings")).json();
      await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, lists: {}, secrets: {}, values: { "display.default_latency_range": "1h" } }) });
    });
    await page.reload({ waitUntil: "load" });
    await ctx.layoutMounted();
    await until(() => /last 1 h/.test(document.getElementById("chartModeLabel").textContent));
    eq("the 1 h button is pressed", await page.$eval('#timeRangeButtons [data-arg="3600"]', (b) => b.getAttribute("aria-pressed")), "true");
    await page.evaluate(async () => {
      const state = await (await fetch("/api/settings")).json();
      await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, lists: {}, secrets: {}, values: { "display.default_latency_range": null } }) });
    });
  }],

  ["the browser's layout can become everyone's default; the Layout dialog links to it", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await ctx.openLayout();
    await ctx.click("#layoutList [data-action=movePanel][data-arg=report][data-arg2=up]");
    await ctx.click("#layoutModal [data-action=openSettingsAt]");
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
    ok("the layout dialog closed", !(await ctx.modalOpen("layoutModal")));
    await ctx.click('[data-action=settingsUseLayout]');
    eq("two settings in the draft", await settingsSaveLabel(page), "Save 1 change"); // hidden is [] = the default: only the order differs
    ok("the order shown", /report/.test(await ctx.text(`${settingsRow("dashboard.order")} .set-layout`)));
    await ctx.click(`${settingsRow("dashboard.order")} [data-action=settingsReset]`);
    eq("reset", await settingsSaveLabel(page), "Save");
    await ctx.click("#btnSettingsCancel");
    await page.evaluate(() => localStorage.clear());
  }],

  ["at 390 px: one column, a jump list, no sideways scroll, with a list card expanded; both themes look right", async (ctx) => {
    const { eq, ok, page, phone, setTheme } = ctx;
    await setTheme("dark");
    await phone(async () => {
      await ctx.click("#btnSettings");
      await ctx.until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
      ok("the rail is replaced by a jump list", await page.$eval("#settingsJump", (e) => getComputedStyle(e.closest("label")).display !== "none") && await page.$eval("#settingsRail", (e) => getComputedStyle(e).display === "none"));
      const fits = () => page.evaluate(() => {
        const b = document.querySelector("#settingsModal .modal-b");
        return { page: document.documentElement.scrollWidth <= innerWidth, body: b.scrollWidth <= b.clientWidth + 1, modal: document.querySelector("#settingsModal .modal-content").getBoundingClientRect().width <= innerWidth };
      });
      eq("fits", JSON.stringify(await fits()), JSON.stringify({ page: true, body: true, modal: true }));
      await page.select("#settingsJump", "devices");
      await ctx.until(() => document.querySelector("#set-sec-devices").getBoundingClientRect().top < innerHeight / 2);
      await page.evaluate(() => { document.querySelectorAll(".set-adv").forEach((d) => { d.open = true; }); });
      eq("fits with Advanced open", JSON.stringify(await fits()), JSON.stringify({ page: true, body: true, modal: true }));
      await ctx.click("[data-list=services] [data-la=add]");
      await page.evaluate(() => { const card = document.querySelector("[data-list=services] .set-card:last-of-type"); card.open = true; card.scrollIntoView(); });
      await page.type("[data-list=services] .set-card:last-of-type [data-f=url]", "http://192.0.2.44:8080/a/very/long/path/that/must/wrap/instead/of/scrolling/sideways/ok");
      eq("fits with a card expanded", JSON.stringify(await fits()), JSON.stringify({ page: true, body: true, modal: true }));
      ok("the buttons are reachable: Save is the last, full width", await page.$eval("#btnSettingsSave", (b) => { const r = b.getBoundingClientRect(); return r.right <= innerWidth && r.width > innerWidth * 0.8; }));
      await ctx.dialogShot("settings-phone-dark");
      await setTheme("light");
      await ctx.dialogShot("settings-phone-light");
      await setTheme("dark");
      await settingsDiscard(ctx);
    });
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["both themes at desktop width", async (ctx) => {
    await settingsOpen(ctx);
    for (const id of ["alerts", "outage", "advanced"]) {
      await ctx.click(`#settingsRail [data-arg=${id}]`);
      await ctx.positionSettled(`#set-sec-${id}`); // the smooth scroll has ended
      await ctx.shots(`settings-${id}`);
    }
    await ctx.click("#btnSettingsCancel");
  }],

  ["the page's devices editor shows the imported devices, collapsed, with their ids; opening a card changes nothing", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=devices]");
    const box = '[data-box=devices]';
    await until(() => document.querySelectorAll("[data-box=devices] .dv-card").length > 0);
    const saved = await page.evaluate(async () => (await (await fetch("/api/settings")).json()).lists.devices.items.map((d) => d.id));
    eq("one card per imported device", await page.$$eval(`${box} .dv-card`, (c) => c.length), saved.length);
    ok(`the seeded devices (${saved})`, saved.includes("modem") && saved.includes("router"));
    await page.evaluate(() => document.querySelector("[data-box=devices] [data-act=toggle]").click());
    eq("the id of a saved device is read-only", await page.$eval(`${box} [data-f=id]`, (i) => i.readOnly), true);
    eq("opening a card is not a change", await settingsSaveLabel(page), "Save");
    await ctx.shots("settings-devices-page");
    await ctx.click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
  }],

  // ---------------- the devices list editor (mounted on its own with the page's schema) ----------------
  ["devices list editor: cards from the saved devices, a failing device shown in red, form generated from the driver, id read-only once saved", async ({ eq, ok, page, shots, until }) => {
    await page.evaluate(async () => {
      const base = document.querySelector("link[rel=stylesheet][href*='/static/v/']").href.replace(/[^/]*$/, "");
      const mod = (await import(`${base}dialogs/settings/lists/devices.js`)).default;
      const schema = await (await fetch("/api/settings/schema")).json();
      const host = document.createElement("div");
      host.id = "dvScratch";
      host.style.cssText = "position:fixed;inset:0;overflow:auto;background:var(--surface-1);z-index:9999;padding:16px";
      document.body.appendChild(host);
      window.__dv = { mod, host, dirty: 0, opened: [], editable: true };
      mod.render(host, [
        { id: "modem", driver: "xfinity_xb", url: "http://192.0.2.10", enabled: true, poll_interval_seconds: 900, username: "admin" },
        { id: "router", driver: "asuswrt", url: "http://192.0.2.1", enabled: true, options: { mesh_node_labels: { "02:00:00:00:00:01": "Hall" } } },
      ], { schema, markDirty: () => { window.__dv.dirty += 1; }, canEdit: () => window.__dv.editable, openDeviceSetup: (id) => window.__dv.opened.push(id), itemErrors: { router: "tls.ca_file '/nope.pem' cannot be used" } });
    });
    eq("two cards", await page.$$eval("#dvScratch .dv-card", (c) => c.length), 2);
    ok("the failing device is shown", /ca_file/.test(await page.$eval("#dvScratch .dv-card[data-key] ~ .dv-card .dv-fail", (e) => e.textContent)));
    ok("collapsed cards have no form", (await page.$$("#dvScratch .dv-form")).length === 0);
    await page.evaluate(() => document.querySelectorAll("#dvScratch [data-act=toggle]")[1].click());
    eq("id read-only for a saved device", await page.$eval("#dvScratch [data-f=id]", (i) => i.readOnly), true);
    eq("the driver select lists the drivers", await page.$eval("#dvScratch [data-f=driver]", (s) => s.value), "asuswrt");
    ok("the driver's options are generated", (await page.$$("#dvScratch [data-opt]")).length >= 4);
    eq("the map option shows its entry", await page.$eval("#dvScratch [data-map-k]", (i) => i.value), "02:00:00:00:00:01");
    ok("the default poll interval is the placeholder", /Default 10/.test(await page.$eval("#dvScratch [data-f=poll_interval_seconds]", (i) => i.placeholder)));
    await shots("settings-devices");
  }],

  ["devices list editor: edits mark the draft, collect() gives the items, remove/undo, add, move, Set up, server messages", async ({ eq, ok, page, until }) => {
    const collect = () => page.evaluate(() => window.__dv.mod.collect());
    await page.evaluate(() => { window.__dv.dirty = 0; });
    await page.$eval("#dvScratch [data-f=url]", (i) => { i.value = "http://192.0.2.99"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    ok("the draft is dirty", await page.evaluate(() => window.__dv.dirty) > 0);
    let items = await collect();
    eq("url edit collected", items[1].url, "http://192.0.2.99");
    eq("an untouched device comes back as saved", JSON.stringify(items[0]), JSON.stringify({ id: "modem", driver: "xfinity_xb", url: "http://192.0.2.10", enabled: true, poll_interval_seconds: 900, username: "admin" }));
    eq("the map is kept", items[1].options.mesh_node_labels["02:00:00:00:00:01"], "Hall");
    // the map editor: add a row with a key, then remove the first
    await page.evaluate(() => document.querySelector("#dvScratch [data-act=map-add]").click());
    await page.$$eval("#dvScratch [data-map-k]", (ins) => { const i = ins[ins.length - 1]; i.value = "02:00:00:00:00:02"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    items = await collect();
    eq("two map entries", Object.keys(items[1].options.mesh_node_labels).length, 2);
    await page.evaluate(() => document.querySelector("#dvScratch [data-act=map-del]").click());
    eq("one map entry again", Object.keys((await collect())[1].options.mesh_node_labels).length, 1);
    // Set up
    await page.evaluate(() => document.querySelectorAll("#dvScratch [data-act=setup]")[1].click());
    eq("Set up opens the dialog for the saved id", await page.evaluate(() => window.__dv.opened.join()), "router");
    // add: a new device has an editable id and no Set up yet
    await page.evaluate(() => document.querySelector("#dvScratch [data-act=add]").click());
    eq("three cards", await page.$$eval("#dvScratch .dv-card", (c) => c.length), 3);
    eq("the new id is editable", await page.$$eval("#dvScratch [data-f=id]", (i) => i.map((x) => x.readOnly).join()), "true,false");
    ok("a new card says to save first", /Save first/.test(await page.$eval("#dvScratch .dv-card:last-child .dv-actions", (e) => e.textContent)));
    ok("an empty address is flagged at once", /Enter the address/.test(await page.$eval("#dvScratch .dv-card:last-child .dv-fail", (e) => e.textContent)));
    await page.$eval("#dvScratch .dv-card:last-child [data-f=id]", (i) => { i.value = "modem"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    ok("a duplicate id is flagged", /Another device has this id/.test(await page.$eval("#dvScratch .dv-card:last-child [data-err=id]", (e) => e.textContent)));
    await page.$eval("#dvScratch .dv-card:last-child [data-f=id]", (i) => { i.value = "other"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    await page.$eval("#dvScratch .dv-card:last-child [data-f=url]", (i) => { i.value = "http://admin:pw@192.0.2.5"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    ok("credentials in the address are flagged", /must not contain a user name/.test(await page.$eval("#dvScratch .dv-card:last-child [data-err=url]", (e) => e.textContent)));
    await page.$eval("#dvScratch .dv-card:last-child [data-f=url]", (i) => { i.value = "http://192.0.2.5"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    eq("no local problem left", await page.evaluate(() => window.__dv.mod.localProblems().length), 0);
    // move the new device up, remove a saved one and undo it
    await page.evaluate(() => document.querySelectorAll("#dvScratch [data-act=up]")[2].click());
    eq("moved up", (await collect()).map((i) => i.id).join(), "modem,other,router");
    await page.evaluate(() => document.querySelector("#dvScratch .dv-card [data-act=remove]").click());
    ok("the removal is explained", /will be removed when you save/.test(await page.$eval("#dvScratch .dv-removed", (e) => e.textContent)));
    await until(() => /unlinked, and stop showing its readings|No path hop is linked to it/.test(document.querySelector("#dvScratch .dv-removed")?.textContent ?? ""));  // names the hops it would unlink
    eq("the removed device is not collected", (await collect()).map((i) => i.id).join(), "other,router");
    await page.evaluate(() => document.querySelector("#dvScratch [data-act=undo]").click());
    eq("undo brings it back", (await collect()).map((i) => i.id).join(), "other,router,modem");
    // server messages land under their fields and open the card; unknown ones are handed back
    const unplaced = await page.evaluate(() => window.__dv.mod.problems([
      { where: "lists.devices[1].url", message: "url must start with http:// or https:// and name a host" },
      { where: "devices.router.tls.ca_file", message: "tls.ca_file '/nope.pem' cannot be used" },
      { where: "values.prober.timeout_seconds", message: "elsewhere" }]));
    eq("unknown ones are returned", unplaced.length, 1);
    ok("the url message is under the field", (await page.$$eval("#dvScratch [data-err=url]", (els) => els.map((e) => e.textContent))).some((t) => /name a host/.test(t)));
    ok("the ca file message too", (await page.$$eval("#dvScratch [data-err='tls.ca_file']", (els) => els.map((e) => e.textContent))).some((t) => /cannot be used/.test(t)));
  }],

  ["devices list editor: phone width has no horizontal scroll; view only disables every control", async ({ eq, ok, page, phone }) => {
    await phone(async () => {
      await page.evaluate(async () => {
        const base = document.querySelector("link[rel=stylesheet][href*='/static/v/']").href.replace(/[^/]*$/, "");
        const mod = (await import(`${base}dialogs/settings/lists/devices.js`)).default;
        const schema = await (await fetch("/api/settings/schema")).json();
        const host = document.createElement("div");
        host.id = "dvScratch";
        host.style.cssText = "position:fixed;inset:0;overflow:auto;background:var(--surface-1);z-index:9999;padding:16px";
        document.body.appendChild(host);
        window.__dv = { mod, host, editable: true };
        mod.render(host, [{ id: "a_rather_long_device_identifier_that_must_wrap_somewhere_sensible", driver: "asuswrt", url: "http://192.0.2.1/with/a/long/path/that/goes/on/and/on/and/on", enabled: true, options: { mesh_node_labels: { "02:00:00:00:00:01": "A long label for a node" } } }],
          { schema, markDirty() {}, canEdit: () => window.__dv.editable, openDeviceSetup() {}, itemErrors: {} });
        host.querySelectorAll("[data-act=toggle]").forEach((b) => b.click());
      });
      ok("no horizontal scroll", await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth && document.getElementById("dvScratch").scrollWidth <= document.getElementById("dvScratch").clientWidth));
      ok("every input fits its card", await page.$$eval("#dvScratch .dv-card input, #dvScratch .dv-card select", (els) => els.every((e) => e.getBoundingClientRect().right <= window.innerWidth)));
    });
    await page.evaluate(async () => {
      const base = document.querySelector("link[rel=stylesheet][href*='/static/v/']").href.replace(/[^/]*$/, "");
      const mod = (await import(`${base}dialogs/settings/lists/devices.js`)).default;
      const schema = await (await fetch("/api/settings/schema")).json();
      const host = document.createElement("div");
      host.id = "dvScratch";
      document.body.appendChild(host);
      mod.render(host, [{ id: "router", driver: "asuswrt", url: "http://192.0.2.1", enabled: true }], { schema, markDirty() {}, canEdit: () => false, openDeviceSetup() {}, itemErrors: {} });
      host.querySelector("[data-act=toggle]").click();
    });
    ok("view only: every field and edit button is disabled", await page.$$eval("#dvScratch input, #dvScratch select, #dvScratch [data-needs-edit]", (els) => els.length > 0 && els.every((e) => e.disabled)));
    eq("view only: nothing to collect changes", await page.evaluate(() => document.querySelector("#dvScratch [data-act=add]").disabled), true);
    await page.evaluate(() => document.querySelectorAll("#dvScratch").forEach((e) => e.remove()));  // the page's own editor is not this one
  }],

  // ---------------- the `viewonly` variant ----------------
  ["view only: the page opens read-only, every changing control is off, Export is hidden, and sign-in turns them on without a reload", async (ctx) => {
    const { base, eq, ok, page, until, moduleUrl } = ctx;
    await until(() => document.documentElement.hasAttribute("data-view-only"));
    await settingsOpen(ctx);
    ok("the bar says so", await page.$eval("#settingsReadOnly", (e) => !e.hidden && /View only · Sign in to change settings/.test(e.textContent)));
    eq("the fieldset is disabled", await page.$eval("#settingsFields", (e) => e.disabled), true);
    ok("every input is disabled", await page.$$eval("#settingsSections input, #settingsSections select, #settingsSections button[data-action=settingsChoice]", (els) => els.length > 20 && els.every((e) => e.matches(":disabled"))));
    const controls = await page.evaluate(() => ["btnSettingsSave", "btnSettingsPreview"].map((id) => document.getElementById(id).getAttribute("aria-disabled")).concat(
      [...document.querySelectorAll("#settingsModal [data-action=settingsImport]")].map((e) => e.getAttribute("aria-disabled"))));
    eq("Save, Preview and Import are aria-disabled", controls.join(), "true,true,true");
    ok("the Reset, Replace and Load controls carry data-needs-edit", await page.evaluate(() => {
      return [...document.querySelectorAll("#settingsSections [data-action=settingsReset], #settingsSections [data-action=settingsSecretReplace], #settingsSections [data-action=settingsSecretRemove]")].every((e) => e.hasAttribute("data-needs-edit"));
    }));
    eq("Export is hidden", await page.$eval("#settingsExport", (e) => e.hidden), true);
    eq("no install paths were sent", await ctx.count("#set-sec-about .set-about"), 0);
    ok("a click on Import only says why", await (async () => {
      await page.evaluate(() => document.querySelector("#settingsModal [data-action=settingsImport]").click());
      await until(() => document.getElementById("toast")?.classList.contains("on"));
      return /View only/.test(await ctx.text("#toast"));
    })());
    const refused = async (method, path, body) => (await fetch(loopback(base) + path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) })).status;
    eq("the server refuses a viewer's PUT", await refused("PUT", "/api/settings", { base_revision: 0, values: {}, lists: {}, secrets: {} }), 401);
    eq("a viewer's import", await refused("POST", "/api/settings/import", {}), 401);
    eq("a viewer's validate", await refused("POST", "/api/settings/validate", { values: {}, lists: {} }), 401);
    eq("the export", await refused("GET", "/api/settings/export"), 401);
    eq("reading is allowed", await refused("GET", "/api/settings"), 200);
    await ctx.shots("settings-viewonly");
    // signing in raises editaccess (the page navigates in a real sign-in; this is the event itself)
    await page.evaluate(async (url) => { (await import(url)).setCanEdit(true); }, await moduleUrl("util.js"));
    eq("the fieldset is on", await page.$eval("#settingsFields", (e) => e.disabled), false);
    eq("the bar is gone", await page.$eval("#settingsReadOnly", (e) => e.hidden), true);
    eq("Export is shown", await page.$eval("#settingsExport", (e) => e.hidden), false);
    eq("Import is on", await page.$eval("#settingsModal [data-action=settingsImport]", (e) => e.getAttribute("aria-disabled")), null);
    await page.evaluate(async (url) => { (await import(url)).setCanEdit(false); }, await moduleUrl("util.js"));
    eq("off again", await page.$eval("#settingsFields", (e) => e.disabled), true);
    await ctx.click("#btnSettingsCancel");
  }, { variants: ["viewonly"] }],

  ["view only at 390 px: the bar and the page fit", async (ctx) => {
    const { eq, ok, page, phone } = ctx;
    await phone(async () => {
      await ctx.until(() => document.documentElement.hasAttribute("data-view-only"));
      await ctx.click("#btnSettings");
      await ctx.until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
      ok("the bar is in view", await page.$eval("#settingsReadOnly", (e) => { const r = e.getBoundingClientRect(); return !e.hidden && r.right <= innerWidth; }));
      eq("no sideways scroll", await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await ctx.click("#btnSettingsCancel");
    });
  }, { variants: ["viewonly"] }],
  ["closed, the page keeps none of the sections' nodes; a draft still asks first, and keeps them while it does", async (ctx) => {
    const { click, eq, ok, page, until } = ctx;
    const rows = () => page.$$eval("#settingsSections .set-row", (e) => e.length);
    await settingsOpen(ctx);
    ok("open: the rows are there", (await rows()) > 10);
    await click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
    eq("closed with nothing changed: the sections are emptied", await page.$eval("#settingsSections", (e) => e.childElementCount), 0);
    await settingsOpen(ctx);
    ok("opened again: rebuilt", (await rows()) > 10);
    await settingsType(page, LOSS, "7");
    await click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsConfirm").hidden);
    ok("with a draft, closing asks and the modal stays open", await page.$eval("#settingsModal", (m) => m.classList.contains("active")));
    ok("the sections and the typed value are still there while it asks", (await rows()) > 10 && await page.$eval(`${settingsRow(LOSS)} input[data-key]`, (e) => e.value) === "7");
    await click("#btnConfirmNo");
    ok("keep editing: still open with the draft", await page.$eval("#settingsModal", (m) => m.classList.contains("active")) && /1 unsaved change/.test(await ctx.text("#settingsMeta")));
    await settingsDiscard(ctx);
    eq("discarded: emptied", await page.$eval("#settingsSections", (e) => e.childElementCount), 0);
    await settingsOpen(ctx);
    eq("the discarded value is gone on the next open", await page.$eval(`${settingsRow(LOSS)} input[data-key]`, (e) => e.value) !== "7", true);
    await click("#btnSettingsCancel");
  }],

];

