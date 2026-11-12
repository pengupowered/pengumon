// The Settings page (header > Settings, or #settings): sections, rows with their default, badge and Reset, nothing applied until
// Save, 409, versions, export and import, the preview (the real replay over the seeded samples), secrets that are never read
// back, the list editors (the devices editor's own steps mount it in a scratch container with the page's schema), view-only
// mode and the phone. Everything runs against the real settings API of the seeded app.
// The `full` variant has PENGUMON_LOG_LEVEL set (test_ui_smoke.py) for the "From environment" row.

import { loopback } from "../lib.mjs";

export const subject = "dialog:settings";
export const variants = ["full"];

const SECTIONS = ["outage", "alerts", "line", "speedtest", "devices", "probing", "retention", "access", "privacy", "discovery", "display", "advanced", "about"];
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
// the speed test's interval: a whole number in its box and a unit (hours, days, weeks)
const settingsInterval = async (page, count, unit) => {
  await settingsType(page, "speedtest.interval_hours", count);
  await page.select(`${settingsRow("speedtest.interval_hours")} select[data-unit]`, unit);
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
    ctx.ok("quiet hours show the time in their zone", /It is \d{1,2}:\d\d(\s?[AP]M)? there now/.test(await page.$eval(`${settingsRow("alerts.quiet_hours.start")} [data-meta]`, (e) => e.textContent)));
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

  ["Outages: the group is under Outage rules with a minutes field and two time fields; a change of the gap is saved, read back, and regroups the incidents card (the next step puts it back)", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const GAP = "incidents.group_gap_minutes";
    await settingsOpen(ctx);
    ok("the group's title", /Outages/.test(await ctx.text("#set-sec-outage")));
    const row = settingsRow(GAP);
    eq("a number field", await page.$eval(`${row} input[data-key]`, (e) => `${e.type}|${e.value}`), "text|10"); // (the page's numeric fields are text boxes)
    ok("in minutes, and 0 means no grouping", /min/.test(await ctx.text(row)) && /0: every drop is its own outage/.test(await ctx.text(row)));
    eq("two time fields", await page.$$eval(`${settingsRow("incidents.maintenance_start")} input[data-key], ${settingsRow("incidents.maintenance_end")} input[data-key]`,
      (els) => els.map((e) => e.value).join()), "00:00,06:00");
    await settingsType(page, GAP, "0");
    eq("one change", await settingsSaveLabel(page), "Save 1 change");
    await settingsSave(ctx);
    eq("read back", (await settingsValuesNow(page)).values[GAP], 0);
    eq("the API follows at once", await page.evaluate(async () => (await (await fetch("/api/outages")).json()).gap_minutes), 0);
    await ctx.click("#btnSettingsCancel");
    // grouping off: the three drops of the night are rows of their own again
    await until(() => /^17 recorded events$/.test(document.getElementById("incidentCountBadge").textContent) && !document.querySelector("#incidentsBody [data-v=summary]"));
  }],

  ["search: rows are filtered by label, help text or key across sections; Escape clears it first and then closes; a phone and the keyboard can use it", async (ctx) => {
    const { eq, ok, page, phone, until } = ctx;
    await settingsOpen(ctx);
    const visible = () => page.$$eval("#settingsSections .set-row[data-key]:not([hidden])", (rows) => rows.filter((r) => r.offsetParent !== null).map((r) => r.dataset.key));
    const total = (await visible()).length;
    await page.focus("#settingsSearch");
    await page.keyboard.type("time zone");
    await until(() => document.querySelectorAll("#settingsSections .set-row[data-key]:not([hidden])").length < 10);
    const zone = await visible();
    ok(`the time zone row is found by its label (${zone.join()})`, zone.includes("general.timezone"));
    ok("rows of other sections are gone", !(await visible()).includes(LOSS));
    ok("a section with no match is hidden", await page.$eval("#set-sec-retention", (e) => e.hidden || e.offsetParent === null));
    await page.$eval("#settingsSearch", (e) => { e.value = ""; });
    await page.keyboard.type(LOSS.split(".")[0]);
    ok("the key matches too", (await visible()).includes(LOSS));
    await page.$eval("#settingsSearch", (e) => { e.select(); });
    await page.keyboard.type("quiet hours");
    ok("help text and label match (quiet hours sit under Alerts)", (await visible()).some((k) => k.startsWith("alerts.quiet_hours")));
    await page.$eval("#settingsSearch", (e) => { e.select(); });
    await page.keyboard.type("zzzz-no-such-setting");
    await until(() => /No setting matches/.test(document.getElementById("settingsSearchStatus").textContent));
    eq("nothing shown", (await visible()).length, 0);
    await page.keyboard.press("Escape");
    eq("Escape clears the box", await page.$eval("#settingsSearch", (e) => e.value), "");
    eq("all rows are back", (await visible()).length, total);
    ok("and the dialog is still open", await page.$eval("#settingsModal", (m) => m.classList.contains("active")));
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
    await phone(async () => {
      await settingsOpen(ctx);
      await page.focus("#settingsSearch");
      await page.keyboard.type("timezone");
      await until(() => document.querySelectorAll("#settingsSections .set-row[data-key]:not([hidden])").length < 10);
      ok("the phone filters too and does not scroll sideways", (await visible()).length >= 1 && await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await ctx.dialogShot("settings-search-phone");
      await ctx.click("#btnSettingsCancel");
      await until(() => !document.getElementById("settingsModal").classList.contains("active"));
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
    eq("the standard value is shown", await page.$eval(`${settingsRow(LOSS)} .set-def`, (e) => e.textContent.trim()), "Standard: 3 s");
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

  ["closing Settings leaves no dead Back press, by any way out, with or without a draft", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const closed = () => until(() => !document.getElementById("settingsModal").classList.contains("active"));
    const fresh = () => page.evaluate(() => { history.replaceState({ e0: 1 }, "", "/"); history.pushState({ e1: 1 }, "", "/"); }); // the page the user came from, and this one
    // one Back from the closed dialog goes to the page the user came from, not to a copy of this one
    const oneBack = async (how) => {
      await page.evaluate(() => history.back());
      await until(() => history.state && history.state.e0 === 1);
      ok(`${how}: one Back reached the page before`, await page.evaluate(() => history.state.e0 === 1 && location.hash === ""));
    };
    const draft = () => settingsType(page, LOSS, "5");
    // no draft: the dialog's own entry only (a draft adds none); one Back goes past it
    await fresh();
    const length = await page.evaluate(() => history.length);
    await settingsOpen(ctx);
    await ctx.click("#btnSettingsCancel");
    await closed();
    eq("no draft: the dialog's own entry (spent now) is the only one added", await page.evaluate(() => history.length), length + 1);
    await oneBack("no draft");
    // a draft, Cancel > Discard
    await fresh();
    await settingsOpen(ctx);
    await draft();
    await settingsDiscard(ctx);
    await oneBack("Cancel and Discard");
    // a draft, Escape > Discard
    await fresh();
    await settingsOpen(ctx);
    await draft();
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("settingsConfirm").hidden);
    await ctx.click("#btnConfirmYes");
    await closed();
    await oneBack("Escape and Discard");
    // a draft taken back by Reset (the entry is still there), then Cancel: nothing to discard
    await fresh();
    await settingsOpen(ctx);
    await draft();
    await ctx.click(`${settingsRow(LOSS)} [data-action=settingsReset]`);
    eq("no change left", await settingsSaveLabel(page), "Save");
    await ctx.click("#btnSettingsCancel");
    await closed();
    await oneBack("a draft undone, then Cancel");
    // a draft, the Back button itself > Discard
    await fresh();
    await settingsOpen(ctx);
    await draft();
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("settingsConfirm").hidden);
    await ctx.click("#btnConfirmYes");
    await closed();
    await oneBack("Back and Discard");
    await page.evaluate(() => history.forward());
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

  ["Speed test section: choices made of numbers stay numbers in the draft the server validates, and the window's two ends go together", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const row = (name) => settingsRow(`speedtest.${name}`);
    const set = (name, value) => page.$eval(`${row(name)} input[data-key]`, (e, v) => { e.value = v; e.dispatchEvent(new Event("input", { bubbles: true })); e.dispatchEvent(new Event("change", { bubbles: true })); }, value);
    // what the page asked the server to validate, and the answer: seen from inside the page, so it is the same in every browser
    await page.evaluate(() => {
      window.__validations = [];
      window.__fetch = window.fetch;
      window.fetch = async (input, init) => {
        const res = await window.__fetch(input, init);
        if (String(input).endsWith("/api/settings/validate")) {
          const body = JSON.parse(init.body);
          res.clone().json().then((answer) => window.__validations.push({ body, answer }));
        }
        return res;
      };
    });
    const validation = async (key) => {
      await until((k) => window.__validations.some((v) => k in v.body.values), key);
      return page.evaluate((k) => window.__validations.filter((v) => k in v.body.values).pop(), key);
    };
    await settingsOpen(ctx);
    ok("the section follows Your line", await page.$eval("#set-sec-speedtest", (e) => e.previousElementSibling.id === "set-sec-line"));
    ok("its title and the owner's words for the switch", /Speed test/.test(await ctx.text("#set-sec-speedtest-t")) && /Each test uses the data below\./.test(await ctx.text(`${row("enabled")} .set-effect`)));
    eq("the interval's standard value is in words", await page.$eval(`${row("interval_hours")} .set-def`, (e) => e.textContent.trim()), "Standard: 1 day");
    // (the seeded app has it on, for the Speed card's steps: the standard value is what says it is off for a new install)
    eq("off by default", await page.$eval(`${row("enabled")} .set-def`, (e) => e.textContent.trim()), "Standard: off");
    ok("M-Lab's choice says that it publishes the address", await page.$eval(`${row("provider")} button[data-arg=mlab]`, (e) => /publishes your IP address/.test(e.textContent)));
    // the window's start alone first: the server says the two ends go together
    await set("window_start", "22:00");
    const lone = await validation("speedtest.window_start");
    ok("a window with one end is refused with the reason", lone.answer.errors.length >= 1 && /go together/.test(JSON.stringify(lone.answer.errors)));
    // the rest, then the other end; the last validation holds the whole draft
    await settingsInterval(page, "6", "hours");
    await page.select(`${row("download_mb")} select`, "50");
    await ctx.click(`${row("provider")} button[data-arg=mlab]`);
    await set("window_end", "02:00");
    const whole = await validation("speedtest.window_end");
    const sent = whole.body.values;
    eq("the hours are sent as a number", sent["speedtest.interval_hours"], 6);
    eq("the megabytes too", sent["speedtest.download_mb"], 50);
    eq("the provider as its name", sent["speedtest.provider"], "mlab");
    eq("the window as times", [sent["speedtest.window_start"], sent["speedtest.window_end"]].join(), "22:00,02:00");
    eq("the server finds nothing wrong", whole.answer.errors.length, 0);
    ok("the version line names the changes", /Run every/.test(whole.answer.summary) && /Test against/.test(whole.answer.summary));
    eq("five changes", await settingsSaveLabel(page), "Save 5 changes");
    eq("the interval shows as typed", await page.$eval(`${row("interval_hours")} input[data-key]`, (e) => e.value) + " " + await page.$eval(`${row("interval_hours")} select[data-unit]`, (e) => e.value), "6 hours");
    eq("an unchanged choice has no badge", await ctx.count(`${row("upload_mb")} [data-badge=unsaved]`), 0);
    await page.evaluate(() => { window.fetch = window.__fetch; });
    await settingsDiscard(ctx);   // not saved: Saves are rate limited (20 a minute) and the steps around it use them
  }],

  ["Speed test section: the line under the data fields says what a month of tests uses, follows the fields, and says when the limit stops them; M-Lab's privacy note shows with it", async (ctx) => {
    const { eq, ok, page } = ctx;
    const row = (name) => settingsRow(`speedtest.${name}`);
    const estimate = () => page.$eval("#set-sec-speedtest [data-extra=speed-estimate] [data-estimate]", (e) => e.textContent);
    const note = () => page.$eval("#set-sec-speedtest [data-extra=speed-mlab]", (e) => getComputedStyle(e).display !== "none");
    await settingsOpen(ctx);
    eq("at the standard sizes, daily: 30 tests of 35 MB", await estimate(), "About 1.1 GB a month (daily, 25 MB down + 10 MB up).");
    eq("the line sits under the last data field", await page.$eval("#set-sec-speedtest [data-extra=speed-estimate]", (e) => e.previousElementSibling.dataset.key), "speedtest.monthly_cap_mb");
    await settingsInterval(page, "6", "hours");
    eq("every 6 hours over the 2,000 MB limit set in the seeded app", await estimate(), "About 4.2 GB a month (every 6 hours, 25 MB down + 10 MB up); the 2,000 MB limit stops tests after about day 15.");
    await page.select(`${row("download_mb")} select`, "100");
    eq("a bigger download", await estimate(), "About 13.2 GB a month (every 6 hours, 100 MB down + 10 MB up); the 2,000 MB limit stops tests after about day 5.");
    await settingsType(page, "speedtest.monthly_cap_mb", "0");
    eq("no limit: no clause", await estimate(), "About 13.2 GB a month (every 6 hours, 100 MB down + 10 MB up).");
    await settingsInterval(page, "1", "hours");
    await page.select(`${row("download_mb")} select`, "10");
    await page.select(`${row("upload_mb")} select`, "5");
    eq("hourly at the smallest sizes: 720 tests of 15 MB", await estimate(), "About 10.8 GB a month (hourly, 10 MB down + 5 MB up).");
    await settingsInterval(page, "1", "days");
    eq("daily at the smallest sizes is in MB", await estimate(), "About 450 MB a month (daily, 10 MB down + 5 MB up).");
    await settingsInterval(page, "1", "weeks");
    eq("weekly", await estimate(), "About 64 MB a month (weekly, 10 MB down + 5 MB up).");
    await settingsInterval(page, "30", "days");
    eq("about monthly", await estimate(), "About 15 MB a month (every 30 days, 10 MB down + 5 MB up).");
    eq("no M-Lab note while Cloudflare is the choice", await note(), false);
    await ctx.click(`${row("provider")} button[data-arg=mlab]`);
    eq("M-Lab: the note shows", await note(), true);
    ok("in words", /M-Lab publishes every result, with your public IP address, as open data\./.test(await ctx.text("#set-sec-speedtest [data-extra=speed-mlab]")));
    await ctx.click(`${row("provider")} button[data-arg=cloudflare]`);
    eq("and goes with the choice", await note(), false);
    await settingsDiscard(ctx);
  }],

  ["Speed test section: the interval is any whole number of hours, days or weeks, shown in the largest unit that fits, with the range in words", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const row = settingsRow("speedtest.interval_hours");
    const shown = async () => `${await page.$eval(`${row} input[data-key]`, (e) => e.value)} ${await page.$eval(`${row} select[data-unit]`, (e) => e.value)}`;
    const message = () => page.$eval(`${row} [data-msg]`, (e) => e.textContent.trim());
    await page.evaluate(() => {
      window.__validations = [];
      window.__fetch = window.__fetch || window.fetch;
      window.fetch = async (input, init) => {
        const res = await window.__fetch(input, init);
        if (String(input).endsWith("/api/settings/validate")) {
          const body = JSON.parse(init.body);
          res.clone().json().then((answer) => window.__validations.push({ body, answer }));
        }
        return res;
      };
    });
    const sent = async (hours) => {
      await until((h) => window.__validations.some((v) => v.body.values["speedtest.interval_hours"] === h), hours);
      return page.evaluate((h) => window.__validations.filter((v) => v.body.values["speedtest.interval_hours"] === h).pop(), hours);
    };
    await settingsOpen(ctx);
    eq("24 hours shows as 1 day", await shown(), "1 days");
    await settingsInterval(page, "2", "weeks");
    const weeks = await sent(336);
    eq("2 weeks is sent as 336 hours, which the server takes", weeks.answer.errors.length, 0);
    ok("the version line says it in words", /Run every 1 day → 2 weeks/.test(weeks.answer.summary));
    await settingsInterval(page, "14", "days");
    await page.focus(`${settingsRow("speedtest.download_mb")} select`);
    eq("14 days stays as typed (it is the same 2 weeks)", await shown(), "14 days");
    await settingsInterval(page, "36", "hours");
    await sent(36);
    eq("36 hours is not a whole number of days", await shown(), "36 hours");
    await settingsInterval(page, "0", "hours");
    eq("0 is refused with the range", await message(), "Enter from 1 hour to 365 days.");
    await settingsInterval(page, "400", "days");
    eq("past a year too", await message(), "Enter from 1 hour to 365 days.");
    await settingsInterval(page, "1.5", "days");
    eq("a fraction is refused", await message(), "Enter a whole number.");
    await settingsInterval(page, "30", "days");
    await sent(720);
    eq("30 days clears the message", await message(), "");
    await page.evaluate(() => { window.fetch = window.__fetch; });
    await settingsDiscard(ctx);
  }],

  ["Speed test section at 390 px: the provider choices fit, the M-Lab label and note are whole, nothing scrolls sideways", async (ctx) => {
    const { eq, page, phone } = ctx;
    await phone(async () => {
      await settingsOpen(ctx);
      await page.select("#settingsJump", "speedtest");
      await ctx.click(`${settingsRow("speedtest.provider")} button[data-arg=mlab]`);
      await ctx.until(() => document.querySelector("#set-sec-speedtest [data-extra=speed-mlab]").getBoundingClientRect().width > 0);
      const fit = await page.evaluate(() => {
        const b = document.querySelector("#settingsModal .modal-b");
        const mlab = document.querySelector('#set-sec-speedtest button[data-arg=mlab]');
        const r = mlab.getBoundingClientRect();
        const note = document.querySelector("#set-sec-speedtest [data-extra=speed-mlab]").getBoundingClientRect();
        return { body: b.scrollWidth <= b.clientWidth + 1, page: document.documentElement.scrollWidth <= innerWidth,
          labelWhole: mlab.scrollWidth <= mlab.clientWidth + 1 && r.right <= innerWidth, noteInside: note.right <= innerWidth };
      });
      await settingsDiscard(ctx); // (before the check: a draft left open holds the reload back with its leave-the-page question)
      eq("fits, the label and the note are whole", JSON.stringify(fit), JSON.stringify({ body: true, page: true, labelWhole: true, noteInside: true }));
    });
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

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
    // a running preview can be canceled: the server drops it and the panel says so
    await ctx.click("#settingsPreview [data-action=settingsPreviewRun]");
    await until(() => !!document.querySelector("#settingsPreview [data-action=settingsPreviewCancel]"));
    await ctx.click("#settingsPreview [data-action=settingsPreviewCancel]");
    await until(() => /Preview canceled/.test(document.getElementById("settingsPreview").textContent));
    ok("and can be run again", await page.$eval("#settingsPreview [data-action=settingsPreviewRun]", (b) => !b.disabled));
    // what is typed in the days box comes back as text, never as markup (CodeQL: DOM text reinterpreted as HTML)
    const typed = '7"><b id="pvInjected">x</b>';
    await page.$eval("#settingsPreviewDays", (e, v) => { e.value = v; }, typed);
    await ctx.click("#settingsPreview [data-action=settingsPreviewRun]");
    await until(() => /Enter a whole number of days/.test(document.getElementById("settingsPreview").textContent));
    eq("no element made from the typed text", await page.evaluate(() => !!document.getElementById("pvInjected")), false);
    eq("the box shows what was typed", await page.$eval("#settingsPreviewDays", (e) => e.value), typed);
    await settingsDiscard(ctx);
    eq("the panel is gone with the draft", await page.$eval("#settingsPreview", (e) => e.hidden), true);
  }],

  ["the Preview's \"would open differently\" line says what differs; an ongoing outage a few seconds apart says so", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await settingsType(page, LOSS, "4");
    await until(() => !document.getElementById("btnSettingsPreview").hidden);
    // the server's answer, made up: one outage still going on that opens 8 s later, one that also ends later
    await page.evaluate(() => {
      const real = window.fetch;
      window.__realFetch = real;
      const now = Math.floor(Date.now() / 60000) * 60 - 7200 + 10; // a minute's tenth second, so +8 s stays in the minute
      const job = {
        state: "done", progress: 1, recorded: 2, notes: [],
        current: { incidents: 2, alerts: null }, draft: { incidents: 2, alerts: null },
        changes: [
          { in: "changed", kind: "local_down", severity: "critical", start: now + 8, end: null, was_start: now, was_end: null, was_severity: "critical" },
          { in: "changed", kind: "gateway_slow", severity: "warning", start: now + 3600 + 5, end: now + 3600 + 400, was_start: now + 3600, was_end: now + 3600 + 300, was_severity: "warning" }
        ]
      };
      window.fetch = (url, opts) => {
        const u = String(url);
        if (!u.startsWith("/api/settings/preview")) return real(url, opts);
        const body = u === "/api/settings/preview" ? { id: "fake" } : job;
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
      };
    });
    await ctx.click("#btnSettingsPreview");
    await until(() => document.querySelectorAll("#settingsPreview .set-changes li").length === 2);
    const items = await page.$$eval("#settingsPreview .set-changes li", (els) => els.map((e) => e.textContent));
    eq("an ongoing outage: only the offset, not the same text twice", items[0], "local down: opens 8 s later with the draft; still going on.");
    eq("another: a start a few seconds off and an end that moved", items[1], "gateway slow: opens 5 s later with the draft; 5 min now, 7 min with the draft.");
    ok("no note ends without a full stop", items.every((s) => s.endsWith(".")));
    await settingsDiscard(ctx);
    await page.evaluate(() => { window.fetch = window.__realFetch; });
  }],

  ["a DNS lookup is added and saved; it appears in Path health's matrix", async (ctx) => {
    const { eq, ok, page, until, waitCount } = ctx;
    await ctx.showCard("matrix"); // not drawn while it is off screen
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
    // a collapsed card says what is wrong in words, and Save's disabled state says why, with a way to the first problem
    await page.$eval(card, (c) => { c.open = false; });
    const inSummary = await page.$eval(`${card} > summary .set-card-e`, (e) => e.textContent);
    ok(`the collapsed card shows the problem (${inSummary})`, inSummary.length > 5 && await page.$eval(`${card} > summary .set-card-e`, (e) => e.offsetParent !== null));
    ok("the summary bar is showing", await page.$eval("#settingsProblems", (e) => !e.hidden && e.offsetParent !== null));
    const why = await ctx.text("#settingsProblemText");
    ok(`the summary names the item and the reason (${why})`, /^1 problem: Host app checks: Extra app: .+\.$/.test(why) && why.includes(inSummary.replace(/[.\s]+$/, "")));
    eq("Save's tooltip says the same", await page.$eval("#btnSettingsSave", (b) => b.title), why);
    await settingsType(page, LOSS, "abc");
    const two = await ctx.text("#settingsProblemText");
    ok(`two problems: the setting by its label first (${two})`, /^2 problems: .*Loss outage starts after: .+; Host app checks: Extra app: /.test(two));
    // the server's answer to this edit redraws the messages and the notes: let it land before the page is scrolled, or
    // on a slow machine it arrives after "Go to the first" and moves what was just brought into view
    const validated = page.waitForResponse((r) => r.url().includes("/api/settings/validate"), { timeout: 10000 }).catch(() => null);
    await settingsType(page, LOSS, "3"); // the standard value: no change left
    await until(() => /^1 problem:/.test(document.getElementById("settingsProblemText").textContent));
    await validated;
    await new Promise((r) => setTimeout(r, 100));
    await page.$eval(card, (c) => { c.open = false; c.scrollIntoView(); });
    await ctx.click("#settingsProblems button");
    ok("Go to the first opens the card", await page.$eval(card, (c) => c.open));
    // what is wrong is in view: the card's message for the field (the card itself may be taller than the dialog)
    const msg = `${card} [data-lmsg$='timeout_seconds']`;
    let seen = false; // (the scroll may be smooth: Firefox animates it, so give it a moment to land)
    for (let i = 0; i < 30 && !(seen = await inView(page, msg)); i++) await new Promise((r) => setTimeout(r, 100));
    const where = seen ? "" : await page.$eval(msg, (e) => {
      const r = e.getBoundingClientRect(), b = document.getElementById("settingsBody").getBoundingClientRect();
      return ` (message top ${Math.round(r.top)}, height ${Math.round(r.height)}; dialog body ${Math.round(b.top)}-${Math.round(b.bottom)}; card open ${e.closest("details")?.open})`;
    }).catch((e) => ` (${e.message})`);
    ok(`and brings the problem into view${where}`, seen);
    await page.$eval(`${card} [data-f=timeout_seconds]`, (e) => { e.focus(); e.select(); });
    await page.type(`${card} [data-f=timeout_seconds]`, "4");
    await until(() => !document.getElementById("btnSettingsSave").disabled);
    ok("no summary once nothing is wrong", await page.$eval("#settingsProblems", (e) => e.hidden));
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

  ["webhooks: header slots keep their label, take any name but one that cannot be a key, never share a stored secret", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=alerts]");
    const addWebhook = async (name) => {
      await ctx.click("[data-list=webhooks] [data-la=add]");
      const card = "[data-list=webhooks] .set-card:last-of-type";
      await page.$eval(`${card} [data-f=name]`, (e) => { e.focus(); e.select(); });
      await page.type(`${card} [data-f=name]`, name);
      await page.$eval(`${card} [data-f=name]`, (e) => e.dispatchEvent(new Event("change", { bubbles: true })));
      const id = await page.$eval(card, (e) => e.dataset.id);
      return `[data-list=webhooks] .set-card[data-id="${id}"]`;
    };
    const addHeader = async (card, name) => {
      await page.$eval(`${card} [data-hdr-name]`, (e) => { e.value = ""; });
      await page.type(`${card} [data-hdr-name]`, name);
      await ctx.click(`${card} [data-la=header-add]`);
    };
    const first = await addWebhook("a b");
    await addHeader(first, "Authorization");
    const label = () => page.$$eval(`${first} .set-hdr .set-label`, (els) => els.map((e) => e.textContent));
    eq("the header row is labeled as a header", (await label()).join(), "Header Authorization");
    await page.$eval(`${first} [data-f=timeout_seconds]`, (e) => { e.value = "7"; e.dispatchEvent(new Event("input", { bubbles: true })); }); // any edit redraws the slots
    eq("and still is after the draft changed", (await label()).join(), "Header Authorization");
    await addHeader(first, "constructor");
    eq("a name such as constructor is a header like any other", (await label()).join(), "Header Authorization,Header constructor");
    await addHeader(first, "__proto__");
    ok("a name that cannot be a key is refused with a message", /cannot be used/.test(await page.$eval(`${first} [data-hdr-msg]`, (e) => e.textContent)));
    await addHeader(first, "constructor");
    ok("a header that is there already says so", /already/.test(await page.$eval(`${first} [data-hdr-msg]`, (e) => e.textContent)));
    // webhook "a" with header "b c" and webhook "a b" with header "c" would both be webhook_header.a_b_c
    await addHeader(first, "c");
    const second = await addWebhook("a");
    await addHeader(second, "b c");
    const slots = await page.$$eval(`${first} .set-hdr .set-slot, ${second} .set-hdr .set-slot`, (els) => els.map((e) => e.dataset.slot));
    eq("every header has a stored secret of its own", new Set(slots).size, slots.length);
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

  ["About this install offers a database backup to an editor as a plain download link, and the download is a SQLite file named for the version", async (ctx) => {
    const { count, eq, ok, page, until, outDir, fs, path } = ctx;
    const files = () => fs.readdirSync(outDir).filter((f) => /^pengumon-.*\.db$/.test(f));
    files().forEach((f) => fs.rmSync(path.join(outDir, f), { force: true }));
    await settingsOpen(ctx);
    eq("one backup link", await count("#set-sec-about a[data-backup]"), 1);
    eq("it goes to the API, by navigation (not a fetch into memory)", await page.$eval("#set-sec-about a[data-backup]", (e) => [e.getAttribute("href"), e.hasAttribute("download")].join()), "/api/backup,true");
    ok("the sentence says what is in it and where to restore it", /history and the settings, with no passwords/.test(await ctx.text("#set-sec-about")) && /Backups in docs\/storage\.md/.test(await ctx.text("#set-sec-about")));
    await ctx.click("#set-sec-about a[data-backup]");
    for (let i = 0; i < 100 && !(files().length && !fs.readdirSync(outDir).some((f) => f.endsWith(".crdownload"))); i++) await new Promise((r) => setTimeout(r, 100));
    eq("one file arrived", files().length, 1);
    ok("named pengumon-<version>-<UTC stamp>.db", /^pengumon-\d+\.\d+\.\d+\S*-\d{8}T\d{6}Z\.db$/.test(files()[0]));
    const head = fs.readFileSync(path.join(outDir, files()[0])).subarray(0, 16).toString("latin1");
    eq("it is a SQLite database", head, "SQLite format 3\0");
    await ctx.click("#btnSettingsCancel");
  }, { chromiumOnly: "the download folder is set over CDP (Firefox saves to its own default folder)" }],

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
    await settingsType(page, "classifier.busy_load_pct", "58");
    await settingsType(page, key, "40");
    await settingsType(page, other, "44");
    eq("three changes", await settingsSaveLabel(page), "Save 3 changes");
    // someone else saves the same setting meanwhile (another tab): the page's base revision is now behind
    await page.evaluate(async (k) => {
      const state = await (await fetch("/api/settings")).json();
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, values: { [k]: 31, "classifier.busy_load_pct": 57 }, lists: {}, secrets: {} }) });
      if (!res.ok) throw new Error(`PUT ${res.status}`);
    }, key);
    ctx.expectStatus(409); // the real conflict is a real 409
    await scrollBottom(page);
    await ctx.click("#btnSettingsSave");
    await until(() => /Changed elsewhere/.test(document.getElementById("settingsNotes").textContent));
    ctx.expectStatus();
    ok("the note is in view, wherever the page was scrolled", await inView(page, "#settingsNotes"));
    const note = await ctx.text("#settingsNotes");
    ok(`the note names a setting by its group and label, with the value kept (${note})`, /Latency spike levels: Router \(now 31 ms\)/.test(note));
    ok(`and one whose label is a sentence stem in full, not cut ("Busy line from.")`, /Busy line from \(now 57 %\)/.test(note));
    eq("theirs is shown", await page.$eval(`${settingsRow(key)} input[data-key]`, (e) => e.value), "31");
    eq("the rest stays", await settingsSaveLabel(page), "Save 1 change");
    eq("the other input", await page.$eval(`${settingsRow(other)} input[data-key]`, (e) => e.value), "44");
    await settingsSave(ctx);
    const v = (await settingsValuesNow(page)).values;
    eq("theirs", v[key], 31);
    eq("ours", v[other], 44);
    await ctx.click("#btnSettingsCancel");
  }],

  ["the help texts quote the live values: hop colors, busy level, router and DOCSIS levels", async (ctx) => {
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
    ok(`hop colors: ${hop}`, /33 ms for local/.test(hop) && !/20 ms for local/.test(hop));
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

  ["the time zone reads \"UTC\", lists zones by place, and offers this browser's zone in one click", async (ctx) => {
    const { eq, ok, page } = ctx;
    const row = settingsRow("general.timezone");
    await page.emulateTimezone("America/New_York"); // the test image runs in UTC: this browser is elsewhere
    await settingsOpen(ctx);
    const shown = () => page.$eval(`${row} select`, (s) => s.selectedOptions[0].textContent);
    ok(`the server's UTC is "UTC" (${await shown()})`, /^UTC\b/.test(await shown()) && !/Etc\//.test(await shown()));
    ok("zones are named by place", await page.$eval(`${row} select`, (s) => [...s.options].some((o) => o.textContent === "Chicago (America/Chicago)")));
    ok("the browser's zone is suggested by city, whole in the box", await page.$eval(`${row} select optgroup[label=Suggested]`, (g) => [...g.children].some((o) => o.textContent === "New York · your browser")));
    eq("the browser's zone is offered", await page.$eval(`${row} [data-action=settingsUseZone]`, (b) => b.textContent), "Use this browser's zone (America/New_York)");
    await ctx.click(`${row} [data-action=settingsUseZone]`);
    eq("it is in the draft", await settingsSaveLabel(page), "Save 1 change");
    eq("the box shows it", await page.$eval(`${row} select`, (s) => s.value), "America/New_York");
    eq("the offer is gone", await ctx.count(`${row} [data-action=settingsUseZone]`), 0);
    eq("focus is on the zone's box", await page.evaluate((r) => document.activeElement === document.querySelector(`${r} select`), row), true);
    ok("quiet hours' clock names the zone", /there now \(New York\)/.test(await ctx.text(`${settingsRow("alerts.quiet_hours.start")} [data-meta]`)));
    await settingsDiscard(ctx);
    await page.emulateTimezone();
  }, { chromiumOnly: "page.emulateTimezone is CDP only" }],

  ["the browser's layout can become the one new browsers start with, in one row with its Reset; the Layout dialog links to it", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const row = "#settingsSections .set-row[data-extra=layout]";
    await ctx.openLayout();
    await ctx.click("#layoutList [data-action=movePanel][data-arg=report][data-arg2=up]");
    await ctx.click("#layoutModal [data-action=openSettingsAt]");
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelectorAll("#settingsSections .set-row").length > 10);
    ok("the layout dialog closed", !(await ctx.modalOpen("layoutModal")));
    eq("one row for the two settings", await ctx.count(`${settingsRow("dashboard.order")}, ${settingsRow("dashboard.hidden")}`), 0);
    eq("the standard layout in words", await ctx.text(`${row} [data-layout]`), "All panels, standard order");
    eq("nothing to reset yet", await ctx.count(`${row} [data-action=settingsResetLayout]`), 0);
    await ctx.click(`${row} [data-action=settingsUseLayout]`);
    eq("two settings in the draft", await settingsSaveLabel(page), "Save 1 change"); // hidden is [] = the default: only the order differs
    ok(`the order shown by the panels' titles (${await ctx.text(`${row} [data-layout]`)})`, /^Order: .*Reliability/.test(await ctx.text(`${row} [data-layout]`)));
    await ctx.click(`${row} [data-action=settingsResetLayout]`);
    eq("Reset to standard empties the draft", await settingsSaveLabel(page), "Save");
    eq("and the row says so", await ctx.text(`${row} [data-layout]`), "All panels, standard order");
    await ctx.click("#btnSettingsCancel");
    await page.evaluate(() => localStorage.clear());
  }],

  ["Display: the theme every browser starts with, and your own themes: duplicate one, change a color, Save", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=display]");
    await until(() => document.querySelector("[data-list=themes] .th-row"));
    ok("the built-in themes are listed, Pengumon first", (await ctx.count("[data-list=themes] .th-row")) >= 3 && /^Pengumon/.test(await ctx.text("[data-list=themes] .th-row b")));
    await ctx.click("[data-list=themes] [data-ta=duplicate-builtin][data-arg=nord]");
    await until(() => document.querySelector("[data-list=themes] .th-card"));
    eq("a draft theme", await ctx.text("[data-list=themes] .th-card .set-card-t"), "Nord copy");
    await page.$eval('[data-list=themes] .th-card input[data-tc="dark --bg"]', (e) => { e.value = "#101820"; e.dispatchEvent(new Event("change", { bubbles: true })); });
    await until(() => [...document.querySelectorAll(`#settingsSections .set-row[data-key="display.theme"] option`)].some((o) => o.value === "custom-nord-copy"));
    ok("Theme offers the new one at once", true);
    await page.select(`${settingsRow("display.theme")} select`, "custom-nord-copy");
    eq("two changes", await settingsSaveLabel(page), "Save 2 changes");
    await settingsSave(ctx);
    const api = await page.evaluate(async () => (await fetch("/api/themes")).json());
    eq("the default for every browser", api.default, "custom-nord-copy");
    eq("the color is saved", api.themes.find((t) => t.id === "custom-nord-copy").theme.tokens.dark["--bg"], "#101820");
    await page.reload({ waitUntil: "load" });
    await ctx.layoutMounted();
    eq("a browser with no choice of its own starts with it", await page.evaluate(() => document.documentElement.dataset.look), "custom-nord-copy");
    await ctx.pickMode("dark");
    eq("in its colors", await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()), "#101820");
    // the standard again for the steps after this one
    const status = await page.evaluate(async () => {
      const { revision } = await (await fetch("/api/settings", { cache: "no-store" })).json();
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: revision, values: { "display.theme": null }, lists: { themes: null } }) });
      return res.status;
    });
    eq("put back", status, 200);
    await page.evaluate(() => localStorage.clear());
  }, { variants: ["bare"] }], // the full variant's steps save 18 times a minute; Save allows 20 (api/settings.py)

  ["Themes: a custom theme whose text matches its background gets a warning at the themes list, and it goes with the cause (not an error: Save stays possible)", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=display]");
    await until(() => document.querySelector("[data-list=themes] .th-row"));
    await ctx.click("[data-list=themes] [data-ta=duplicate-builtin][data-arg=nord]");
    await until(() => document.querySelector("[data-list=themes] .th-card"));
    const setDark = (name, value) => page.$eval(`[data-list=themes] .th-card input[data-tc="dark ${name}"]`, (e, v) => { e.value = v; e.dispatchEvent(new Event("change", { bubbles: true })); }, value);
    const bg = await page.$eval('[data-list=themes] .th-card input[data-tc="dark --bg"]', (e) => e.value);
    await setDark("--ink-1", bg);
    await until(() => document.querySelector("[data-list=themes] [data-th-warn]"));
    const warning = await ctx.text("[data-list=themes] [data-th-warn]");
    ok(`the warning names the theme and the colors (${warning})`, /^Custom theme custom-nord-copy dark: --ink-1 on/.test(warning));
    eq("it sits in the themes list, above the cards", await page.evaluate(() => document.querySelector("[data-list=themes] [data-th-warn]").closest("[data-list=themes]") !== null), true);
    eq("it is not an error: no card needs a fix", await ctx.count("[data-list=themes] .th-card .st-crit"), 0);
    ok("Save is still possible", await page.$eval("#btnSettingsSave", (b) => !b.disabled));
    await setDark("--ink-1", "#eceff4");
    await until(() => !document.querySelector("[data-list=themes] [data-th-warn]"));
    ok("the warning went with its cause", true);
    await settingsDiscard(ctx);
  }, { variants: ["bare"] }],

  ["Themes: Export gives any theme as a file; Import reads one as a draft and says what it filled in and left out", async (ctx) => {
    const { eq, fs, ok, outDir, page, path, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=display]");
    await until(() => document.querySelector("[data-list=themes] .th-row"));
    await ctx.click("[data-list=themes] [data-ta=export-builtin][data-arg=digital-rain]");
    await until(() => document.getElementById("themeExportText"));
    const exported = JSON.parse(await page.$eval("#themeExportText", (t) => t.value));
    ok("the file is the theme: every color, no effect, no fonts", exported.id === "digital-rain" && !("effect" in exported) && exported.tokens.dark["--bg"] && !exported.tokens.dark["--font"]);
    const file = path.join(outDir, "imported.theme.json");
    fs.writeFileSync(file, JSON.stringify({ id: "paper", name: "Paper", modes: ["light"], tokens: { light: { "--bg": "#fbf8f1", "--font": "Georgia, serif" } } }));
    await (await page.$("#themeImportFile")).uploadFile(file);
    await until(() => [...document.querySelectorAll("[data-list=themes] .th-card .set-card-t")].some((t) => t.textContent === "Paper"));
    const note = await ctx.text("[data-list=themes] .result");
    ok(`the note says what came from where (${note})`, /missing colors come from the standard theme/.test(note) && /left out/.test(note));
    eq("light only", await ctx.text("[data-list=themes] .th-card .set-card-s"), "light only");
    eq("one change, nothing saved", await settingsSaveLabel(page), "Save 1 change");
    // a short hex or rgb() color shows as the color it is in the picker, not as black
    const pale = path.join(outDir, "pale.theme.json");
    fs.writeFileSync(pale, JSON.stringify({ name: "Pale", modes: ["light"], tokens: { light: { "--bg": "#abc", "--ink-1": "rgb(250, 250, 250)" } } }));
    await (await page.$("#themeImportFile")).uploadFile(pale);
    await until(() => [...document.querySelectorAll("[data-list=themes] .th-card .set-card-t")].some((t) => t.textContent === "Pale"));
    const swatch = (name) => page.$eval(`[data-list=themes] .th-card[data-id="custom-pale"] [data-tc="light ${name}"]`, (e) => e.value);
    eq("#abc is #aabbcc", await swatch("--bg"), "#aabbcc");
    eq("rgb(250, 250, 250) is #fafafa", await swatch("--ink-1"), "#fafafa");
    // a file that is no theme by its size is not read at all
    const big = path.join(outDir, "big.theme.json");
    fs.writeFileSync(big, JSON.stringify({ name: "Big", modes: ["light"], pad: "x".repeat(300 * 1024) }));
    await (await page.$("#themeImportFile")).uploadFile(big);
    await until(() => /is not a theme file/.test(document.querySelector("[data-list=themes]").textContent));
    ok("no card was made of it", !(await page.$$eval("[data-list=themes] .th-card .set-card-t", (ts) => ts.some((t) => t.textContent === "Big"))));
    await settingsDiscard(ctx);
  }, { chromiumOnly: "uploadFile is CDP only" }],

  ["Settings: closed before its state arrives, it stays closed and empty", async (ctx) => {
    const { eq, page, until } = ctx;
    await page.evaluate(() => { // the state takes a second
      window.__stateFetch = window.fetch;
      window.fetch = async (input, init) => {
        const res = await window.__stateFetch(input, init);
        if (String(input) === "/api/settings" && !(init && init.method)) await new Promise((r) => setTimeout(r, 1200));
        return res;
      };
    });
    await ctx.click("#btnSettings");
    await until(() => document.getElementById("settingsModal").classList.contains("active"));
    await ctx.click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
    await new Promise((r) => setTimeout(r, 2000));
    eq("nothing was drawn into the closed dialog", await page.$$eval("#settingsSections .set-row", (r) => r.length), 0);
    await page.evaluate(() => { window.fetch = window.__stateFetch; });
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
      ok("the footer is one row: every button on the same line, inside the screen", await page.$$eval("#settingsModal .modal-f .btn:not([hidden])", (bs) => {
        const tops = bs.map((b) => Math.round(b.getBoundingClientRect().top));
        return bs.length >= 4 && Math.max(...tops) - Math.min(...tops) <= 2 && bs.every((b) => b.getBoundingClientRect().right <= innerWidth);
      }));
      ok("the icon-only buttons keep their names", await page.$$eval("#settingsModal .modal-f .left .btn", (bs) => bs.every((b) => b.getAttribute("aria-label"))));
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

  ["devices editor in Settings: a validation answer does not take the focus or the caret from the field being typed in", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsOpen(ctx);
    await ctx.click("#settingsRail [data-arg=devices]");
    await until(() => document.querySelectorAll("[data-box=devices] .dv-card").length > 0);
    await page.evaluate(() => document.querySelector("[data-box=devices] [data-act=toggle]").click());
    const url = "[data-box=devices] .dv-card.open [data-f=url]";
    await page.evaluate(() => { // count the validation answers the page has taken
      window.__validated = 0;
      const was = window.fetch;
      window.fetch = async (input, init) => {
        const res = await was(input, init);
        if (String(input).endsWith("/api/settings/validate")) setTimeout(() => { window.__validated += 1; }, 150);
        return res;
      };
    });
    for (const [what, text, errorShown] of [["a valid address", "http://192.0.2.77", false], ["an address the server refuses", "ftp://192.0.2.77", true]]) {
      await page.focus(url);
      await page.$eval(url, (e) => { e.select(); window.__field = e; });
      await page.keyboard.press("Backspace");
      await page.type(url, text);
      await page.$eval(url, (e) => e.setSelectionRange(4, 4)); // the caret in the middle: a redraw would put it at the end or lose it
      const seen = await page.evaluate(() => window.__validated);
      await until((n) => window.__validated > n, seen);
      // with nothing new to say the card is not drawn again; with a message to show it is, and the focus and caret come back
      if (!errorShown) eq(`${what}: the field is the same element`, await page.evaluate(() => document.querySelector("[data-box=devices] .dv-card.open [data-f=url]") === window.__field), true);
      eq(`${what}: it still has the focus`, await page.evaluate(() => document.activeElement === document.querySelector("[data-box=devices] .dv-card.open [data-f=url]")), true);
      eq(`${what}: the caret stays`, await page.evaluate(() => { const f = document.activeElement; return [f.selectionStart, f.selectionEnd].join(); }), "4,4");
      if (errorShown) ok("the server's message is shown under the field", await page.$eval("[data-box=devices] .dv-card.open [data-err=url]", (e) => e.textContent.trim().length > 0));
      await page.keyboard.type("x"); // the next keystroke lands in the field
      await page.$eval(url, (e) => { window.__field = e; });
      ok(`${what}: the next keystroke lands in the field`, (await page.$eval(url, (e) => e.value)).charAt(4) === "x");
    }
    await settingsDiscard(ctx);
  }],

  ["Settings: nothing typed while a Save is in flight is lost without a word", async (ctx) => {
    const { eq, page, until } = ctx;
    await settingsOpen(ctx);
    await page.evaluate(() => { // the PUT's answer takes 1.5 s
      window.__putFetch = window.fetch;
      window.fetch = async (input, init) => {
        const res = await window.__putFetch(input, init);
        if (String(input).startsWith("/api/settings") && init && init.method === "PUT") await new Promise((r) => setTimeout(r, 1500));
        return res;
      };
    });
    await settingsType(page, LOSS, "7");
    await settingsType(page, "classifier.busy_load_pct", "61");
    await page.evaluate(() => document.querySelector("#btnSettingsSave").click());
    await until(() => document.querySelector("#btnSettingsSave").disabled);
    const sel = `${settingsRow("classifier.busy_load_pct")} input[data-key]`;
    eq("the settings cannot be edited while the Save is in flight", await page.evaluate(() => document.getElementById("settingsFields").disabled), true);
    await page.focus(sel).catch(() => {}); // a person tries to type in another box
    await page.keyboard.type("9");
    await until(() => document.getElementById("toast")?.classList.contains("on") && document.querySelector("#btnSettingsSave").textContent.trim() === "Save");
    eq("editable again after the answer", await page.evaluate(() => document.getElementById("settingsFields").disabled), false);
    eq("the box holds what was saved, no digit was typed into the void", await page.$eval(sel, (e) => e.value), "61");
    eq("saved", String((await settingsValuesNow(page)).values["classifier.busy_load_pct"]), "61");
    await page.evaluate(() => { window.fetch = window.__putFetch; });
    await ctx.click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
    // put the two settings back to their defaults for the steps after this one
    eq("put back", await page.evaluate(async (k) => {
      const state = await (await fetch("/api/settings")).json();
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, values: { [k]: null, "classifier.busy_load_pct": null }, lists: {}, secrets: {} }) });
      return res.status;
    }, LOSS), 200);
  }, { variants: ["bare"] }], // a Save: the full variant's steps are at the limit of 20 a minute (api/settings.py)

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
    ok("the standard poll interval is the placeholder", /Standard: 10/.test(await page.$eval("#dvScratch [data-f=poll_interval_seconds]", (i) => i.placeholder)));
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
    // the row a person deletes is the row that goes, also when its key sorts after a number
    for (const key of ["b", "10"]) {
      await page.evaluate(() => document.querySelector("#dvScratch [data-act=map-add]").click());
      await page.$$eval("#dvScratch [data-map-k]", (ins, k) => { const i = ins[ins.length - 1]; i.value = k; i.dispatchEvent(new Event("input", { bubbles: true })); }, key);
    }
    eq("rows in typing order", await page.$$eval("#dvScratch [data-map-k]", (ins) => ins.map((i) => i.value).join()), "02:00:00:00:00:02,b,10");
    await page.evaluate(() => document.querySelectorAll("#dvScratch [data-act=map-del]")[1].click()); // the "b" row
    eq("b is gone, 10 stays", Object.keys((await collect())[1].options.mesh_node_labels).sort().join(), "02:00:00:00:00:02,10");
    await page.evaluate(() => document.querySelectorAll("#dvScratch [data-act=map-del]")[1].click()); // the "10" row
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
    });
    // a viewer's card is open at once (its toggle would be a button in the disabled fieldset, which takes no click)
    ok("view only: the card is open and every field is disabled", await page.$$eval("#dvScratch input, #dvScratch select", (els) => els.length > 0 && els.every((e) => e.disabled)));
    eq("view only: no edit button is offered at all (Add, Remove, Move, Set up)", await page.evaluate(() => document.querySelectorAll("#dvScratch [data-needs-edit], #dvScratch [data-act=add]").length), 0);
    await page.evaluate(() => document.querySelectorAll("#dvScratch").forEach((e) => e.remove()));  // the page's own editor is not this one
  }],

  ["devices list editor: an snmp device shows and saves its SNMPv3 options only with version 3; phone width, both themes", async ({ eq, ok, page, phone, shots }) => {
    const mount = () => page.evaluate(async () => {
      const base = document.querySelector("link[rel=stylesheet][href*='/static/v/']").href.replace(/[^/]*$/, "");
      const mod = (await import(`${base}dialogs/settings/lists/devices.js`)).default;
      const schema = await (await fetch("/api/settings/schema")).json();
      document.querySelectorAll("#dvScratch").forEach((e) => e.remove());
      const host = document.createElement("div");
      host.id = "dvScratch";
      host.style.cssText = "position:fixed;inset:0;overflow:auto;background:var(--surface-1);z-index:9999;padding:16px";
      document.body.appendChild(host);
      window.__dv = { mod, host, dirty: 0 };
      mod.render(host, [{ id: "core-sw", driver: "snmp", url: "snmp://192.0.2.2", enabled: true }],
        { schema, markDirty: () => { window.__dv.dirty += 1; }, canEdit: () => true, openDeviceSetup() {}, itemErrors: {} });
      host.querySelector("[data-act=toggle]").click();
    });
    const opts = () => page.$$eval("#dvScratch [data-opt]", (els) => els.map((e) => e.dataset.opt));
    const v3 = ["security_level", "auth_protocol", "priv_protocol", "context_name"];
    const collected = () => page.evaluate(() => window.__dv.mod.collect()[0].options || {});
    await mount();
    eq("SNMPv2c (the default): the version and no SNMPv3 option", await page.$eval("#dvScratch [data-opt=version]", (s) => s.value), "2c");
    ok("no SNMPv3 option shown", !(await opts()).some((o) => v3.includes(o)));
    ok("the user name says what it is for snmp", /SNMPv3: the user name/.test(await page.$eval("#dvScratch [data-f=username]", (i) => i.closest(".field").textContent)));
    await page.select("#dvScratch [data-opt=version]", "3");
    const shownNow = await opts();
    ok(`version 3 shows the SNMPv3 options (${shownNow})`, v3.every((o) => shownNow.includes(o)));
    eq("the version select keeps the focus", await page.evaluate(() => document.activeElement?.dataset.opt), "version");
    eq("the standard level and protocols", await page.$$eval("#dvScratch [data-opt=security_level], #dvScratch [data-opt=auth_protocol], #dvScratch [data-opt=priv_protocol]", (els) => els.map((e) => e.value).join()), "authPriv,SHA,AES");
    ok("no MD5 and no DES", !(await page.$$eval("#dvScratch option", (els) => els.map((e) => e.value))).some((v) => /MD5|DES/.test(v)));
    await page.select("#dvScratch [data-opt=auth_protocol]", "SHA-256");
    await page.$eval("#dvScratch [data-opt=context_name]", (i) => { i.value = "lab"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    eq("collected with version 3", JSON.stringify(await collected()), JSON.stringify({ version: "3", auth_protocol: "SHA-256", context_name: "lab" }));
    ok("the draft is dirty", await page.evaluate(() => window.__dv.dirty) > 0);
    await shots("settings-devices-snmpv3");
    await page.select("#dvScratch [data-opt=version]", "2c");
    ok("back to 2c: hidden again", !(await opts()).some((o) => v3.includes(o)));
    eq("and not saved", JSON.stringify(await collected()), JSON.stringify({ version: "2c" }));
    await page.select("#dvScratch [data-opt=version]", "3");
    eq("the draft kept them meanwhile", JSON.stringify(await collected()), JSON.stringify({ version: "3", auth_protocol: "SHA-256", context_name: "lab" }));
    await phone(async () => {
      await mount();
      await page.select("#dvScratch [data-opt=version]", "3");
      ok("phone width: no horizontal scroll", await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth && document.getElementById("dvScratch").scrollWidth <= document.getElementById("dvScratch").clientWidth));
      ok("phone width: every control fits", await page.$$eval("#dvScratch .dv-card input, #dvScratch .dv-card select", (els) => els.every((e) => e.getBoundingClientRect().right <= window.innerWidth)));
    });
    await page.evaluate(() => document.querySelectorAll("#dvScratch").forEach((e) => e.remove()));
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
    eq("a viewer's backup", await refused("GET", "/api/backup"), 401);
    eq("no backup button for a viewer", await ctx.count("#set-sec-about [data-backup]"), 0);
    eq("reading is allowed", await refused("GET", "/api/settings"), 200);
    await ctx.shots("settings-viewonly");
    // signing in raises editaccess (the page navigates in a real sign-in; this is the event itself)
    await page.evaluate(async (url) => { (await import(url)).setCanEdit(true); }, await moduleUrl("view-only.js"));
    eq("the fieldset is on", await page.$eval("#settingsFields", (e) => e.disabled), false);
    eq("the bar is gone", await page.$eval("#settingsReadOnly", (e) => e.hidden), true);
    eq("Export is shown", await page.$eval("#settingsExport", (e) => e.hidden), false);
    eq("Import is on", await page.$eval("#settingsModal [data-action=settingsImport]", (e) => e.getAttribute("aria-disabled")), null);
    await page.evaluate(async (url) => { (await import(url)).setCanEdit(false); }, await moduleUrl("view-only.js"));
    eq("off again", await page.$eval("#settingsFields", (e) => e.disabled), true);
    await ctx.click("#btnSettingsCancel");
  }, { variants: ["viewonly"] }],

  ["view only: the device, host-app and webhook cards read sensibly (what the server blanks is not shown as an empty value, and nothing that changes them is offered)", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await until(() => document.documentElement.hasAttribute("data-view-only"));
    // a webhook, as the viewer shaping of GET /api/settings sends it (no headers, the address only as set)
    await page.evaluate(() => {
      const real = window.fetch;
      window.__realFetch = real;
      window.fetch = async (url, ...rest) => {
        const res = await real(url, ...rest);
        if (String(url).endsWith("/api/settings") && !rest[0]?.method) {
          const body = await res.json();
          body.lists.webhooks.items = [{ id: "hook", name: "Phone alerts", format: "slack", headers: {}, timeout_seconds: 5 }];
          body.secrets["webhook.hook.url"] = { set: true, source: "stored", host: null };
          return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
        }
        return res;
      };
    });
    try {
      await settingsOpen(ctx);
      await until(() => document.querySelector("[data-list=webhooks] .set-card") !== null);
      await page.evaluate(() => document.querySelectorAll("[data-list] details.set-card").forEach((d) => { d.open = true; })); // (a viewer opens the host app and webhook cards with the mouse)
      eq("the device cards are all open for a viewer (their toggle would be a dead button)", await ctx.count(".dv-card.open"), await ctx.count(".dv-card"));
      eq("and nothing that looks like a toggle", await ctx.count("button.dv-toggle"), 0);
      const all = await ctx.text("#settingsSections");
      ok("no undefined, null or [object] anywhere in the sections", !/undefined|\bnull\b|\[object/.test(all));
      // devices
      ok("a device card is there", (await ctx.count(".dv-card")) >= 2);
      eq("no user name, CA file or device option field (the server blanks them)", await ctx.count('.dv-card [data-f=username], .dv-card [data-f="tls.ca_file"], .dv-card [data-opt]'), 0);
      ok("the card says they are for the owner", /shown to the signed-in owner only/.test(await ctx.text(".dv-card .dv-body")));
      eq("no option fieldset named after a driver", await page.$$eval(".dv-card .dv-opts legend", (l) => l.map((x) => x.textContent).filter((t) => / options$/.test(t)).length), 0);
      // host apps and webhooks
      ok("a host app check is there", (await ctx.count("[data-list=services] .set-card")) >= 1);
      eq("no CA file field on a host app check", await ctx.count("[data-list=services] [data-f=ca_file]"), 0);
      ok("its note says the path is not shown", /without their path/.test(await ctx.text("[data-list=services] .fine")));
      const hook = "[data-list=webhooks] .set-card";
      ok("the webhook is there with its format", /Phone alerts/.test(await ctx.text(hook)) && /Slack/.test(await ctx.text(hook)));
      ok("its address is a secret that is set", /Address/.test(await ctx.text(hook)) && !/URL needed/.test(await ctx.text(hook)));
      ok("headers are not claimed to be absent", !/No headers/.test(await ctx.text(hook)) && /owner only/.test(await ctx.text(hook)));
      eq("no field to add a header", await ctx.count("[data-list=webhooks] [data-hdr-name]"), 0);
      // nothing that changes a list is there, and nothing in them takes input
      eq("no Add, Remove, Move or Set up control", await ctx.count('.dv [data-act]:not([data-act=toggle]), .set-list [data-la]'), 0);
      eq("no 'Add' or 'Remove all' button text", await page.$$eval(".dv button, .set-list button, .set-lnk", (b) => b.filter((x) => /^(Add |Remove all|Back to the)/.test(x.textContent.trim())).length), 0);
      ok("every input, select and button in the lists is off (but the card toggles)", await page.$$eval(".dv input, .dv select, .set-list input, .set-list select, .set-list .btn, .dv .btn", (els) => els.every((e) => e.matches(":disabled"))));
    } finally {
      await page.evaluate(() => { if (window.__realFetch) window.fetch = window.__realFetch; });
      await ctx.click("#btnSettingsCancel");
    }
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

  ["Outages: by the end of the file the gap is back at its default (an Import or a Save of values puts every other key back), and the incidents card groups the night event again", async (ctx) => {
    const { eq, until, page } = ctx;
    eq("the gap", (await settingsValuesNow(page)).values["incidents.group_gap_minutes"], 10);
    await page.reload({ waitUntil: "load" });
    await until(() => /^15 events · 17 incidents$/.test(document.getElementById("incidentCountBadge")?.textContent ?? ""));
  }],

];

