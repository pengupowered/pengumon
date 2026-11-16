// The incident log and its Inspector dialog. The inspector steps open incidents from the table, so they keep the page size the table step chose.

export const subject = "panel:incidents";
export const variants = ["full"];

export const steps = [
  ["incident table: paging, search, page size, expected-events toggle", async ({ click, eq, ok, page, shown, text, waitCount, waitText }) => {
    await waitCount("#incidentsBody tr.incident-row", 10);
    // 17 incidents, three of them one outage (the seed's night event): 15 rows
    eq("count", await text("#incidentCountBadge"), "15 events · 17 incidents");
    eq("info", await text("#incidentPaginationInfo"), "Showing 1–10 of 15 events");
    await click("#btnPageNext");
    await waitText("#incidentPageNum", /^Page 2 of 2$/);
    await waitCount("#incidentsBody tr.incident-row", 5);
    await click("#btnPageFirst");
    await waitText("#incidentPageNum", /^Page 1 of 2$/);
    await page.type("#incidentSearchInput", "blip 1");
    await waitCount("#incidentsBody tr.incident-row", 4); // blips 1, 10, 11, 12
    await waitText("#incidentPaginationInfo", /\(filtered\)/);
    await page.$eval("#incidentSearchInput", (i) => { i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); });
    await page.select("#incidentPageSizeSelect", "50");
    await waitCount("#incidentsBody tr.incident-row", 15);
    eq("toggle visible", await shown("#btnToggleExpected"), true);
    ok("toggle label", /Show 1 expected/.test(await text("#btnToggleExpected")));
    await click("#btnToggleExpected");
    await waitCount("#incidentsBody tr.incident-row", 16);
    await waitText("#incidentCountBadge", /^16 events · 18 incidents$/);
    ok("busy badge", /Expected/.test(await text("#incidentsBody")));
    await click("#btnToggleExpected");
    await waitCount("#incidentsBody tr.incident-row", 15);
  }],

  ["incident rows: one line each, the details are the inspector's, and a click anywhere on the row opens it", async ({ click, count, eq, ok, page, until, waitText }) => {
    eq("no description line in the list", await count("#incidentsBody .sub"), 0);
    eq("no load or busy chips in the list", /load|busy line/.test(await page.$eval("#incidentsBody", (e) => e.textContent)), false);
    const heights = await page.$$eval("#incidentsBody tr.incident-row:not(.outage-row)", (els) => els.map((e) => e.getBoundingClientRect().height));
    ok(`rows stay small (${Math.max(...heights).toFixed(0)} px)`, Math.max(...heights) <= 44);
    await page.evaluate(() => [...document.querySelectorAll("#incidentsBody tr")].find((tr) => tr.textContent.includes("Short gateway drop")).querySelector(".when").click());
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    ok("the description is in the inspector", /home-side address/.test(await page.$eval("#inspCauseDesc", (e) => e.textContent)));
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await until(() => !document.getElementById("incidentInspectorModal").classList.contains("active"));
  }],

  // The seed's night event: three drops of the line the night before, two minutes apart ("Night event drop 1" to "3", the first
  // at 03:12 UTC, 240 s + 180 s + 120 s, the first and the third critical): one outage, down 6 min, likely maintenance.
  ["an outage of several drops is one row with its summary and the maintenance label; its drops are listed when it is opened, and a drop opens the inspector", async ({ click, count, eq, ok, page, text, until, waitText }) => {
    const only = "#incidentsBody tr.outage-row";
    eq("one outage row", await count(only), 1);
    eq("and the three drops are not rows of their own", await page.$$eval("#incidentsBody tr.incident-row", (rows) => rows.filter((r) => /Night event drop [23]/.test(r.textContent)).length), 0);
    const row = await page.$eval(only, (tr) => ({
      title: tr.querySelector("[data-v=title]").textContent.trim(), status: tr.querySelector("[data-v=status]").textContent.trim(),
      summary: tr.querySelector("[data-v=summary]").textContent.trim(), chip: tr.querySelector("[data-v=maintenance]").textContent.trim(),
      chipTitle: tr.querySelector("[data-v=maintenance]").title, duration: tr.querySelector("[data-v=duration]").textContent.trim(),
      cause: tr.querySelector("[data-v=cause]").textContent.trim(), icon: !!tr.querySelector("[data-v=status] svg"),
      expanded: tr.querySelector("button[data-action=toggleOutage]").getAttribute("aria-expanded"), state: !!tr.querySelector("[data-v=state]") }));
    eq("its title is the longest critical drop's", row.title, "Night event drop 1");
    eq("critical: the status word, with an icon", `${row.status}|${row.icon}`, "Outage|true");
    eq("the summary: drops and the time the line was down", row.summary, "3 drops · down 6 min");
    eq("the label", row.chip, "likely maintenance");
    ok(`the label says what it is (${row.chipTitle})`, /Started between 00:00 and 06:00 at the provider: often planned work\. Only a label: the downtime counts in full\./.test(row.chipTitle));
    eq("how long: the span from the first drop to the last", row.duration, "13m 00s");
    eq("where: the places of the drops", row.cause, "Line · ISP");
    eq("closed", row.expanded, "false");
    eq("over: no ongoing note", row.state, false);
    // the button is reached by the keyboard
    await page.focus(`${only} button[data-action=toggleOutage]`);
    await page.keyboard.press("Enter");
    await until(() => document.querySelectorAll("#incidentsBody tr.drop-row").length === 3);
    eq("open", await page.$eval(`${only} button[data-action=toggleOutage]`, (b) => b.getAttribute("aria-expanded")), "true");
    eq("the drops, in the order they happened", await page.$$eval("#incidentsBody tr.drop-row [data-v=title]", (els) => els.map((e) => e.textContent.trim()).join()),
      "Night event drop 1,Night event drop 2,Night event drop 3");
    eq("the pager still counts rows", await text("#incidentPaginationInfo"), "Showing 1–15 of 15 events");
    ok("the keyboard still has the button", await page.evaluate(() => document.activeElement.dataset.action === "toggleOutage"));
    await page.evaluate(() => [...document.querySelectorAll("#incidentsBody tr.drop-row")].find((tr) => /drop 2/.test(tr.textContent)).querySelector(".when").click());
    await waitText("#inspCauseTitle", /^Night event drop 2$/);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await until(() => !document.getElementById("incidentInspectorModal").classList.contains("active"));
    await click(`${only} button[data-action=toggleOutage]`);
    await until(() => document.querySelectorAll("#incidentsBody tr.drop-row").length === 0);
  }],

  ["a search finds a drop inside an outage and shows the outage; opened, it lists every drop, not only the match", async ({ click, count, eq, page, until, waitText }) => {
    await page.type("#incidentSearchInput", "drop 2");
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 1);
    eq("the outage is the row", await count("#incidentsBody tr.outage-row"), 1);
    await waitText("#incidentPaginationInfo", /^Showing 1–1 of 1 events \(filtered\)$/);
    await click("#incidentsBody tr.outage-row button[data-action=toggleOutage]");
    await until(() => document.querySelectorAll("#incidentsBody tr.drop-row").length === 3);
    await click("#incidentsBody tr.outage-row button[data-action=toggleOutage]");
    await until(() => document.querySelectorAll("#incidentsBody tr.drop-row").length === 0);
    await page.$eval("#incidentSearchInput", (i) => { i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 15);
  }],

  ["a time another panel picked keeps an outage when any drop overlaps it", async ({ click, count, eq, page, text, until }) => {
    // the window is the third drop's two minutes: only that drop overlaps it, the outage shows with all of its drops
    await page.evaluate(async () => {
      const third = (await (await fetch("/api/incidents?limit=500")).json()).find((i) => i.title === "Night event drop 3");
      const b = document.createElement("button");
      Object.assign(b.dataset, { action: "showIncidentWindow", start: String(third.start_time + 10), end: String(third.start_time + 20), label: "Night hour", lost: "5", sent: "10" });
      document.body.append(b);
      b.click();
      b.remove();
    });
    await until(() => !document.getElementById("incidentWindow").hidden);
    eq("one row, the outage", await count("#incidentsBody tr.incident-row"), 1);
    eq("it is the outage", await count("#incidentsBody tr.outage-row"), 1);
    eq("and the bar counts the incidents in the time", (await text("#incidentWindow [data-f=found]")).trim(), "1 incident");
    await click("[data-action=clearIncidentWindow]");
    await until(() => document.getElementById("incidentWindow").hidden && document.querySelectorAll("#incidentsBody tr.incident-row").length === 15);
  }],

  ["a group is never shown with drops missing: held back while there is more to load, which is loaded until it is whole", async ({ eq, moduleUrl, page }) => {
    const found = await page.evaluate(async (u) => {
      const { present } = await import(u);
      const now = Date.now() / 1000;
      const inc = (id, extra = {}) => ({ id, title: `Example drop ${id}`, severity: "critical", cause: "line", expected: false, description: "", cause_code: "access_line_drop",
        start_time: now - 3600 * id, end_time: now - 3600 * id + 60, duration_seconds: 60, loss_seconds: 60, ...extra });
      const outage = { id: 3, start_time: now - 3 * 3600, end_time: now - 1 * 3600 + 60, ongoing: false, settling: false, duration_seconds: 7260, down_seconds: 180, affected_seconds: 180, drops: 3,
        critical_drops: 3, severity: "critical", places: ["line"], title: "Example drop 3", cause_code: "access_line_drop", likely_maintenance: false, incident_ids: [3, 2, 1] };
      const state = (all, more) => ({ all, query: "", pageSize: 10, page: 1, showExpected: false, window: null, totals: null, more, loading: false, oldest: null, outages: [outage], maintenance: { start: "00:00", end: "06:00" } });
      const rows = (vm) => vm.list.rows.map((r) => `${r.kind || "incident"}:${r.id}`).join();
      return {
        // the oldest drop is not loaded yet and there is more: the group is held back, and its two loaded drops are not shown alone
        held: rows(present(state([inc(1), inc(2)], true), now * 1000)),
        heldCount: present(state([inc(1), inc(2)], true), now * 1000).total,
        whole: rows(present(state([inc(1), inc(2), inc(3)], true), now * 1000)),
        // at the end of the list (nothing more to load) it shows what there is, and says so
        end: present(state([inc(1), inc(2)], false), now * 1000).list.rows[0].summary,
        none: rows(present({ ...state([inc(1), inc(2), inc(3)], false), outages: null }, now * 1000)) };
    }, await moduleUrl("../panels/incidents/model.js"));
    eq("held back while drops are missing", found.held, "");
    eq("and not counted as rows", found.heldCount, 0);
    eq("whole: one outage row", found.whole, "outage:o3");
    eq("at the end of the list, with what is there", found.end, "3 drops · down 3 min (2 loaded)");
    eq("no outages read: every incident is a row", found.none, "incident:1,incident:2,incident:3");
  }],

  ["older incidents are loaded until an outage's drops are all there (the pages are cut through an outage)", async ({ eq, ok, page, text, until, waitText }) => {
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length > 0);
    await page.evaluate(() => {
      const now = Date.now() / 1000;
      // 120 incidents, one every 3 days (the week holds the first 3); the outage is 9049, 9050 and 9051:
      // the week ends before it
      const all = Array.from({ length: 120 }, (_, i) => ({ id: 9000 + i, kind: "isp_loss", title: `Synthetic ${i}`, description: "", severity: "critical", cause_code: "access_line_drop", cause: "line",
        start_time: now - 600 - i * 259200, end_time: now - 540 - i * 259200, duration_seconds: 60, expected: false, affected_targets: [] }));
      const outage = { id: 9049, start_time: all[51].start_time, end_time: all[49].end_time, ongoing: false, settling: false, duration_seconds: 2 * 259200 + 60, down_seconds: 180, affected_seconds: 180,
        drops: 3, critical_drops: 3, severity: "critical", places: ["line"], title: "Synthetic 49", cause_code: "access_line_drop", likely_maintenance: false, incident_ids: [9049, 9050, 9051] };
      const real = window.fetch;
      window.__asked = [];
      window.__realFetch = real;
      const json = (body) => Promise.resolve(new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }));
      window.fetch = (url, opts) => {
        const u = new URL(String(url), location.href);
        if (u.pathname === "/api/incidents/count") return json({ total: all.length, expected: 0 });
        if (u.pathname === "/api/outages") return json({ gap_minutes: 10, outages: [outage] });
        if (u.pathname !== "/api/incidents") return real(url, opts);
        const limit = Number(u.searchParams.get("limit")), offset = Number(u.searchParams.get("offset") || 0), since = Number(u.searchParams.get("since") || 0);
        window.__asked.push(`${since > 1e9 ? "range" : limit}@${offset}`);
        return json(all.filter((i) => i.end_time >= since).slice(offset, offset + limit));
      };
    });
    await new Promise((r) => setTimeout(r, 2500)); // the first message has set the version the panel compares with
    await page.evaluate(() => { window.__smokeMutate = new Function("d", "d.incidents_version = (d.incidents_version || 0) + 1000"); });
    await until(() => window.__asked.length >= 2);
    await page.evaluate(() => { window.__smokeMutate = null; });
    eq("the week (3 incidents), then the next page because the outage reaches into it", await page.evaluate(() => window.__asked.slice(0, 2).join(" ")), "range@0 50@3");
    await page.select("#incidentPageSizeSelect", "100");
    await until(() => document.querySelectorAll("#incidentsBody tr.outage-row").length === 1);
    ok("the outage is a row", /3 drops/.test(await text("#incidentsBody tr.outage-row")));
    ok("its drops are not rows of their own", !/Synthetic 50|Synthetic 51/.test(await text("#incidentsBody")));
    await waitText("#incidentCountBadge", /^\d+\+? events · 120 incidents$/);
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 10);
    await page.select("#incidentPageSizeSelect", "50"); // (as the steps before this one left it)
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 15);
  }],

  ["Amber CRT draws the outage: its line, summary and label, and its drops as lines that open the inspector", async ({ click, count, eq, ok, page, pickLook, text, until, waitText }) => {
    await page.select("#incidentPageSizeSelect", "50"); // (the outage is older than the first ten rows)
    await pickLook("amber-crt");
    await until(() => document.querySelector("[data-panel=incidents] .panel-b").dataset.view === "amber-crt");
    await until(() => document.querySelectorAll("[data-panel=incidents] .ac-outage").length === 1);
    const line = await page.$eval("[data-panel=incidents] .ac-outage", (l) => ({ text: l.textContent, chip: l.querySelector("[data-v=maintenance]")?.textContent, summary: l.querySelector("[data-v=summary]")?.textContent,
      expanded: l.querySelector("button[data-action=toggleOutage]")?.getAttribute("aria-expanded"), status: l.querySelector("[data-v=status]")?.textContent.trim() }));
    eq("summary", line.summary, "3 drops · down 6 min");
    eq("label", line.chip, "likely maintenance");
    eq("status", line.status, "Outage");
    eq("closed", line.expanded, "false");
    eq("the drops are not lines yet", await count("[data-panel=incidents] .ac-drop"), 0);
    await click("[data-panel=incidents] .ac-outage button[data-action=toggleOutage]");
    await until(() => document.querySelectorAll("[data-panel=incidents] .ac-drop").length === 3);
    await page.evaluate(() => [...document.querySelectorAll("[data-panel=incidents] .ac-drop")].find((l) => /drop 3/.test(l.textContent)).querySelector(".ac-what").click());
    await waitText("#inspCauseTitle", /^Night event drop 3$/);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await until(() => !document.getElementById("incidentInspectorModal").classList.contains("active"));
    ok("the header counts the same", /^15 events · 17 incidents$/.test((await text("#incidentCountBadge")).trim()));
    await click("[data-panel=incidents] .ac-outage button[data-action=toggleOutage]");
    await until(() => document.querySelectorAll("[data-panel=incidents] .ac-drop").length === 0);
    await pickLook("");
  }],

  ["every theme draws the outage inside the card at 1400 and at 390 px: nothing sticks out sideways, a drop's row is reachable", async ({ eq, page, phone, pickLook, until }) => {
    // as the screenshot reviews measure it: an element past the window's right edge, or cut off by a box that hides its overflow
    const sticksOut = (width) => page.evaluate((w) => {
      const out = [];
      const name = (e) => `${e.tagName.toLowerCase()}${typeof e.className === "string" && e.className ? "." + e.className.split(" ")[0] : ""}`;
      document.querySelectorAll("[data-panel=incidents] .panel *").forEach((e) => {
        if (!e.offsetParent && getComputedStyle(e).position !== "fixed") return;
        const r = e.getBoundingClientRect();
        if (!r.width || !r.height) return;
        let edge = w, scrolls = false;
        for (let p = e.parentElement; p && p !== document.documentElement; p = p.parentElement) {
          const o = getComputedStyle(p).overflowX;
          if (o === "auto" || o === "scroll") { scrolls = true; break; }
          if (o === "hidden" || o === "clip") edge = Math.min(edge, p.getBoundingClientRect().right);
        }
        if (!scrolls && r.right > edge + 1 && getComputedStyle(e).textOverflow !== "ellipsis") out.push(`${name(e)} +${Math.round(r.right - edge)}px`);
      });
      return `${out.slice(0, 4).join(", ")}|${document.documentElement.scrollWidth <= w}`;
    }, width);
    const open = async () => {
      await page.select("#incidentPageSizeSelect", "50"); // (the outage is older than the first ten rows)
      await until(() => document.querySelector("[data-panel=incidents] [data-action=toggleOutage]") !== null);
      await until(() => { const b = document.querySelector("[data-panel=incidents] [data-action=toggleOutage]"); if (b.getAttribute("aria-expanded") !== "true") b.click(); return document.querySelectorAll("[data-panel=incidents] [data-action=inspectIncident][data-arg]").length >= 3; });
    };
    const looks = (await page.evaluate(() => JSON.parse(document.documentElement.dataset.looks).map((l) => l.id)));
    const seen = [];
    // (a theme is picked in the header's menu: a page load per theme would run into the rate limit of the history)
    for (const id of ["", ...looks]) {
      await pickLook(id);
      await open();
      eq(`${id || "default"} at 1400 px`, await sticksOut(1400), "|true");
      seen.push(id || "default");
    }
    await phone(async () => {
      await page.evaluate(() => { location.hash = "incidents"; });
      await until(() => !document.querySelector("[data-panel=incidents]").hidden);
      for (const id of ["", ...looks]) {
        await pickLook(id);
        await open();
        eq(`${id || "default"} at 390 px`, await sticksOut(390), "|true");
      }
    });
    await pickLook("");
    await page.select("#incidentPageSizeSelect", "50"); // (the phone reloaded the page: back to what the steps after this one expect)
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 15);
    eq("the default theme and every built-in one were looked at", seen.length >= 5, true);
  }],

  ["incident timeline: bars, lanes by cause, a day line at every midnight in the window", async ({ count, ok, page, until }) => {
    await until(() => document.querySelectorAll("#incidentTimeline .tl-bar").length >= 10);
    ok("lanes by cause", (await count("#incidentTimeline .tl-lane")) >= 1);
    // 7 x 24 h ending now holds 7 local midnights (6 or 8 around a clock change is not this test's week): each lane draws
    // a line at each, as background layers of its track (a declaration the browser refuses would draw none)
    const lines = await page.$eval("#incidentTimeline .tl-track", (t) => (getComputedStyle(t).backgroundImage.match(/linear-gradient/g) || []).length);
    ok(`a day line per midnight (${lines})`, lines >= 6 && lines <= 8);
  }],

  ["incident timeline: 7 d and 30 d; 30 d keeps a day line per midnight and labels the Mondays only", async ({ chooseRange, count, eq, ok, page, rangeState, until }) => {
    eq("the card offers 7 d to All (the incidents are kept 180 days)", (await rangeState("#incidentRanges")).words.join(","), "Last 7 days,Last 30 days,Last 90 days,All · 180 d");
    eq("7 d is chosen", (await rangeState("#incidentRanges")).value, "7d");
    const week = await count("#incidentTimeline .tl-bar");
    await chooseRange("#incidentRanges", "30d");
    await until(() => document.querySelector("#incidentRanges select").value === "30d");
    await until((n) => document.querySelectorAll("#incidentTimeline .tl-bar").length >= n && document.querySelectorAll("#incidentTimeline [data-v=days] span").length <= 6, week);
    ok(`the 30 d timeline holds the week's bars (${week} or more)`, (await count("#incidentTimeline .tl-bar")) >= week);
    const lines = await page.$eval("#incidentTimeline .tl-track", (t) => (getComputedStyle(t).backgroundImage.match(/linear-gradient/g) || []).length);
    ok(`a day line per midnight (${lines})`, lines >= 29 && lines <= 31);
    const labels = await page.$$eval("#incidentTimeline [data-v=days] span", (s) => s.map((x) => x.textContent));
    ok(`at most one label a week (${labels.join(", ")})`, labels.length >= 3 && labels.length <= 5);
    ok("each is a date", labels.every((l) => /^[A-Z][a-z]{2} \d{1,2}$/.test(l)));
    ok("and a Monday", labels.every((l) => new Date(`${l} ${new Date().getFullYear()} 12:00`).getDay() === 1 || new Date(`${l} ${new Date().getFullYear() - 1} 12:00`).getDay() === 1));
    await chooseRange("#incidentRanges", "7d");
    await until(() => document.querySelectorAll("#incidentTimeline [data-v=days] span").length >= 6);
    const again = await page.$eval("#incidentTimeline .tl-track", (t) => (getComputedStyle(t).backgroundImage.match(/linear-gradient/g) || []).length);
    ok(`back on 7 d: a line per midnight (${again})`, again >= 6 && again <= 8);
  }],

  ["lanes use Path health's words: Router, Gateway, Line, ISP, Internet, DNS, Load, Service; the cause help explains them", async ({ eq, ok, page, until }) => {
    const r = await page.evaluate(async () => {
      const main = [...document.scripts].find((s) => s.type === "module").src;
      const m = await import(main.replace(/js\/main\.js$/, "panels/incidents/incidents.js"));
      const api = await import(main.replace(/js\/main\.js$/, "js/roles.js"));
      return { lanes: api.CAUSES.map(([k, w]) => `${k}=${w}`).join(","), words: ["router", "gateway", "line", "isp"].map((c) => m.causeOf({ cause: c })).join(">") };
    });
    eq("lane keys and words", r.lanes, "router=Router,gateway=Gateway,line=Line,isp=ISP,internet=Internet,dns=DNS,load=Load,service=Service,unknown=Other");
    eq("the server's router, gateway, line and isp categories", r.words, "Router>Gateway>Line>ISP");
    await until(() => document.querySelectorAll("#incidentTimeline .tl-l").length >= 1);
    ok("no lane is still called Home", !/Home/.test(await page.$eval("#incidentTimeline", (e) => e.textContent)));
  }],

  ["an incident lasts as long as its probes were lost: the list shows the loss, the detected span on hover; the inspector both", async ({ click, eq, inspectNamed, ok, page, text, until, waitText }) => {
    // seeded: detected 3 s after the gateway's probes start failing, closed 8 s after they answer again (98 s), the
    // probes lost for about 90 s; the blips have no affected targets, so only their recorded duration is known
    const rows = await page.$$eval("#incidentsBody tr.incident-row", (trs) => Object.fromEntries(trs.map((tr) => {
      const d = tr.querySelector(".dur");
      return [tr.querySelector("td.lead").textContent.trim(), { text: d.textContent, title: d.title }];
    })));
    const drop = rows["Short gateway drop"];
    ok(`the loss, not the detected span: ${JSON.stringify(drop)}`, /^1m (29|30|31)s lost$/.test(drop.text) && /open 1m 38s/.test(drop.title) && /first to last lost probe/.test(drop.title));
    eq("an incident without a loss span keeps its duration", rows["Seeded blip 1"].text, "20s");
    ok("and says why on hover", /no stored probes/.test(rows["Seeded blip 1"].title));
    await inspectNamed("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    ok(`the inspector gives both: ${await text("#inspDuration")}`, /^Loss 1m (29|30|31)s · incident 1m 38s$/.test((await text("#inspDuration")).trim()));
    ok("so does its header", /Loss 1m \d\ds · incident 1m 38s$/.test(await text("#inspTimeWindowLabel")));
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["inspector: hop table, path trace, DOCSIS card and four charts", async ({ chartLabels, count, eq, hasChart, inspect, ok, outDir, page, path, shown, text, waitText }) => {
    await inspect("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    eq("modal", await page.$eval("#incidentInspectorModal", (m) => m.classList.contains("active")), true);
    eq("hop rows", await count("#inspHopTableBody tr"), 3);
    const names = await page.$$eval("#inspHopTableBody tr td:first-child .primary", (els) => els.map((e) => `${e.textContent.trim()}|${e.title}`));
    eq("canonical hop order, the page's short names, the configured name on hover", names.join(","),
      "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    eq("path trace rows", await count("#inspPathTraceBody tr"), 2);
    eq("path trace shown", await shown("#inspPathTraceContainer"), true);
    eq("docsis card shown", await shown("#inspDocsisCard"), true);
    ok("docsis badge", (await text("#inspDocsisBadge")).trim().length > 0);
    for (const id of ["incidentZoomChart", "incidentZoomLossChart", "incidentZoomDocsisChart", "incidentZoomRouterChart"]) await hasChart(id);
    ok("zoom chart has data", (await chartLabels("incidentZoomChart")) > 100);
    eq("docsis track shown", await shown("#incidentZoomDocsisContainer"), true);
    eq("router track shown", await shown("#incidentZoomRouterContainer"), true);
    await page.screenshot({ path: path.join(outDir, "inspector.png") });
  }],

  ["inspector: 'view in main history chart' loads the window and closes the modal", async ({ click, eq, page, until, waitText }) => {
    await click("#btnSnapDashboard");
    eq("closed", await page.$eval("#incidentInspectorModal", (m) => m.classList.contains("active")), false);
    await waitText("#chartModeLabel", /^incident window /);
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.labels.length > 50);
  }],

  ["inspector: a two hour incident comes back as buckets and renders", async ({ chartLabels, click, hasChart, inspect, ok, waitText }) => {
    await inspect("Long evening outage");
    await waitText("#inspCauseTitle", /^Long evening outage$/);
    await hasChart("incidentZoomChart");
    const n = await chartLabels("incidentZoomChart");
    ok(`bucketed labels ${n}`, n > 100 && n <= 1900);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["inspector: a gateway verdict is not blamed on the line; a line verdict reads the line's errors (desktop and phone)", async ({ click, eq, inspectNamed, noSideScroll, ok, page, phone, snap, text, waitText }) => {
    // the seed: the gateway drop has uncorrectable errors in its window, the line incident none
    const look = async (title, file) => {
      await inspectNamed(title);
      await waitText("#inspCauseTitle", new RegExp(`^${title}$`));
      await waitText("#inspDocsisDesc", /\S/);
      const seen = { desc: await text("#inspCauseDesc"), line: await text("#inspDocsisDesc"), evidence: await text("#inspEvidence") };
      ok(`${title}: no sideways scroll`, await noSideScroll("incidentInspectorModal"));
      await snap(file);
      await page.$eval("#inspDocsisCard", (e) => e.scrollIntoView({ block: "center" }));
      await snap(file.replace(".png", "-line-card.png"));
      await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
      return seen;
    };
    for (const [suffix, run] of [["desktop", (fn) => fn()], ["phone", phone]]) {
      await run(async () => {
        const gw = await look("Short gateway drop", `verdict-gateway-${suffix}.png`);
        ok(`gateway: the verdict says its probes do not use the line (${gw.desc})`, /do not use the provider's line/.test(gw.desc));
        ok(`gateway: the line's errors are not said to explain it (${gw.line})`, /does not use the line, so these errors do not explain it/.test(gw.line) && !/fits this packet loss/.test(gw.line));
        ok(`gateway: the evidence names the gateway (${gw.evidence})`, /Gateway/.test(gw.evidence));
        const line = await look("Line or ISP Hub Packet Loss", `verdict-line-${suffix}.png`);
        ok(`line: the clean line places it past the modem (${line.line})`, /The line was clean/.test(line.line) && /provider's first router/.test(line.line));
        eq("line: the verdict keeps its words", /the local router and the gateway answer normally/.test(line.desc), true);
      });
    }
  }],

  ["inspector: verdict, lanes per hop, what happened, synced tracks, evidence, hop figures", async ({ count, dialogShot, eq, hasChart, inspectNamed, modalOpen, ok, page, setTheme, tabStaysIn, text, until, waitText }) => {
    await setTheme("dark");
    await inspectNamed("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    await until(() => document.querySelectorAll("#inspLanes .strip").length >= 3);
    ok("verdict chips name the cause", /Cause: /.test(await text("#inspChips")));
    eq("one lane per hop", await count("#inspLanes .strip"), 3);
    ok("the gateway lane shows lost probes", (await count("#inspLanes .strip:nth-of-type(2) i.w, #inspLanes .strip:nth-of-type(2) i.c")) > 3);
    ok("the router lane has none", (await count("#inspLanes .strip:nth-of-type(1) i.w, #inspLanes .strip:nth-of-type(1) i.c")) === 0);
    ok("events: window, first loss, end", (await count("#inspEvents li")) >= 3 && /First lost probes at/.test(await text("#inspEvents")));
    ok("evidence: line, load and first hop with loss", (await count("#inspEvidence li")) >= 4 && /Line load/.test(await text("#inspEvidence")));
    ok("an incident without a load figure says so", /unknown/.test(await text("#inspEvidence")));
    for (const id of ["incidentZoomChart", "incidentZoomLossChart", "incidentZoomDocsisChart", "incidentZoomRouterChart"]) await hasChart(id);
    // one gutter for every track: the plot areas line up
    const areas = await page.evaluate(() => ["incidentZoomChart", "incidentZoomLossChart", "incidentZoomDocsisChart", "incidentZoomRouterChart"].map((i) => { const a = Chart.getChart(document.getElementById(i)).chartArea; return `${Math.round(a.left)}-${Math.round(a.right)}`; }));
    ok(`tracks share one plot width (${areas})`, new Set(areas).size === 1);
    // one reading per hop in every probe round (the seeded probes straddle the half second), losses where they happened,
    // a lost probe breaking the latency line, and tooltips that describe values, never "null"
    const tracks = await page.evaluate(() => {
      const lat = Chart.getChart(document.getElementById("incidentZoomChart"));
      const loss = Chart.getChart(document.getElementById("incidentZoomLossChart"));
      // the router and Cloudflare never lose a probe in the seed: a hole in their lines is a round split in two
      const none = [0, 2].map((k) => lat.data.datasets[k].data.slice(1, -1).filter((v) => v === null).length);
      const i = loss.data.datasets[1].data.findIndex((v) => v !== null);
      const t = loss.options.plugins.tooltip;
      const lines = loss.data.datasets.map((d, k) => ({ datasetIndex: k, dataIndex: i, raw: d.data[i], parsed: { y: d.data[i] }, dataset: d }))
        .filter(t.filter || (() => true)).map(t.callbacks.label);
      const latLines = lat.data.datasets.map((d) => lat.options.plugins.tooltip.callbacks.label({ dataset: d, dataIndex: i, parsed: { y: d.data[i] } }));
      return { none: none.join(","), lines: lines.join(" | "), latLines: latLines.join(" | "), gaps: lat.data.datasets.every((d) => d.spanGaps === false) };
    });
    eq("every probe round has a reading from the hops that never lost one", tracks.none, "0,0");
    ok(`the loss tooltip names only the hop that lost, by its short name (${tracks.lines})`, /^Gateway lost 100%$/.test(tracks.lines));
    ok(`the latency tooltip: router answered, gateway lost, internet answered (${tracks.latLines})`,
      /^Router: \d+\.\d+ ms \| Gateway: lost \| Internet: \d+\.\d+ ms$/.test(tracks.latLines));
    // every label in the inspector names a hop as the rest of the page does, the configured name only on hover
    const labels = await page.evaluate(() => ({
      lanes: [...document.querySelectorAll("#inspLanes .lane-n")].map((e) => `${e.textContent}|${e.title}`).join(","),
      tip: document.querySelector("#inspLanes .strip:nth-of-type(2) i").dataset.tip,
      legend: [...document.querySelectorAll("#inspLatLegend > span")].map((e) => `${e.textContent}|${e.title}`).join(","),
      text: ["#inspChips", "#inspEvents", "#inspEvidence"].map((s) => document.querySelector(s).textContent).join(" ")
    }));
    eq("lanes", labels.lanes, "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    ok(`a lane's tooltip (${labels.tip})`, /^Gateway · /.test(labels.tip));
    eq("chart legend", labels.legend, "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    ok(`no configured name in the chips, events or evidence: ${labels.text}`, !/Hop \d:|Public:/.test(labels.text) && /First lost probes at Gateway/.test(labels.text));
    eq("a lost probe breaks the latency line", tracks.gaps, true);
    eq("keyboard focus starts on close", await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Close");
    await dialogShot("inspector-dark");
    const box = await page.$eval("#incidentZoomChart", (c) => { const r = c.getBoundingClientRect(); return { x: r.left + r.width * 0.5, y: r.top + r.height * 0.5 }; });
    await page.mouse.move(box.x, box.y);
    await dialogShot("inspector-hover-dark");
    await page.mouse.move(2, 2);
    await setTheme("light");
    await dialogShot("inspector-light");
    await setTheme("dark");
    await tabStaysIn("incidentInspectorModal");
    await page.click("#incidentInspectorModal .modal-t"); // plain text: focus goes to the body, Escape still closes
    await page.keyboard.press("Escape");
    eq("escape closes it", await modalOpen("incidentInspectorModal"), false);
  }],

  ["inspector: a busy-line incident and one with an unusable load figure", async ({ click, count, dialogShot, eq, inspectNamed, ok, text, until, waitText }) => {
    await inspectNamed("Busy line slowdown");
    await waitText("#inspCauseTitle", /^Busy line slowdown$/);
    await until(() => document.querySelectorAll("#inspLanes .strip").length >= 3);
    ok("load shown", /96% load/.test(await text("#inspChips")) && /96% of capacity/.test(await text("#inspEvidence")));
    ok("lanes hatch the busy period", (await count("#inspLanes .strip i.b")) > 3);
    await dialogShot("inspector-busy-dark");
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await inspectNamed("Short gateway drop", { suspectLoad: true });
    await until(() => /not a valid figure/.test(document.getElementById("inspChips").textContent));
    ok("evidence says the same", /load: not a valid figure/.test(await text("#inspEvidence")));
    eq("no figure shown", /912/.test(await text("#incidentInspectorModal")), false);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["inspector: the dossier opens on top and Escape closes only the dossier", async ({ click, eq, inspectNamed, modalOpen, page, until, waitText }) => {
    await inspectNamed("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    await click("#incidentInspectorModal [data-action=openDossierModal]");
    await until(() => document.getElementById("dossierModal").classList.contains("active"));
    await page.click("#dossierModal .modal-t"); // plain text: focus goes to the body, Escape still closes the topmost dialog
    await page.keyboard.press("Escape");
    eq("dossier closed", await modalOpen("dossierModal"), false);
    eq("inspector still open", await modalOpen("incidentInspectorModal"), true);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["the inspector as a bottom sheet on a phone: it fits the width and scrolls inside", async ({ click, dialogShot, eq, hasChart, inspectNamed, noSideScroll, page, phone, setTheme, until }) => {
    await phone(async () => {
      await setTheme("dark");
      await page.evaluate(() => { location.hash = "incidents"; }); // the chart is on the Live tab, out of sight
      await until(() => document.querySelector("[data-panel=latency]").hidden);
      await inspectNamed("Short gateway drop");
      await until(() => document.querySelectorAll("#inspLanes .strip").length >= 3);
      await hasChart("incidentZoomChart");
      eq("inspector fits", await noSideScroll("incidentInspectorModal"), true);
      await dialogShot("inspector-phone");
      // "Show in latency chart" leaves the Incidents tab for the Live tab, where the chart is
      await click("#btnSnapDashboard");
      await until(() => !document.querySelector("[data-panel=latency]").hidden && /^incident window /.test(document.getElementById("chartModeLabel").textContent));
      // the inspector gave its history entry back before the tab changed: one Back returns to the Incidents tab (no dead press)
      await until(() => location.hash === "#live" && history.state?.pgModal === undefined);
      await page.evaluate(() => history.back());
      await until(() => location.hash === "#incidents");
      eq("Back from the Live tab lands on the Incidents tab, not on the inspector's leftover entry", await page.evaluate(() => `${location.hash}|${history.state?.pgModal === undefined}`), "#incidents|true");
      await setTheme(null);
    });
  }],

  ["the list loads by range: one request for the week at start (and one for its outages), older ones in pages for a search and the last page", async ({ click, eq, ok, page, text, until, waitText }) => {
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length > 0);
    const first = await page.evaluate(() => performance.getEntriesByType("resource").map((r) => r.name).filter((n) => /\/api\/incidents\?/.test(n)).map((n) => n.replace(/^.*\/api/, "/api")));
    eq("the first fetch is one request for the 7-day timeline (every incident that overlaps it, the light fields)", first.length, 1);
    ok(`it asks by time (${first[0]})`, /^\/api\/incidents\?since=\d+&limit=5000&fields=list$/.test(first[0]));
    const week = Number(first[0].match(/since=(\d+)/)[1]);
    ok("the week it asks for starts 7 days back", Math.abs(Date.now() / 1000 - 7 * 86400 - week) < 120);
    eq("and one request for the outages", await page.evaluate(() => performance.getEntriesByType("resource").filter((r) => /\/api\/outages/.test(r.name)).length), 1);
    // a server with 130 incidents, one every 2 h (84 of them in the 7 days), newest first, honoring limit and offset
    await page.evaluate(() => {
      const now = Date.now() / 1000;
      const all = Array.from({ length: 130 }, (_, i) => ({ id: 9000 + i, kind: "gateway_down", title: i === 120 ? "Old needle" : `Synthetic ${i}`, description: "", severity: "warning", cause_code: "unknown_outage", cause: "isp",
        start_time: now - 600 - i * 7200, end_time: now - 560 - i * 7200, duration_seconds: 40, expected: i === 3 || i === 7, affected_targets: [] })); // two busy-line ones, so 98 of the first 100 show
      const real = window.fetch;
      window.__asked = [];
      window.__realFetch = real;
      window.fetch = (url, opts) => {
        const u = new URL(String(url), location.href);
        if (u.pathname === "/api/incidents/count") return Promise.resolve(new Response(JSON.stringify({ total: all.length, expected: 2 }), { headers: { "content-type": "application/json" } }));
        if (u.pathname === "/api/outages") return Promise.resolve(new Response(JSON.stringify({ gap_minutes: 10, outages: [] }), { headers: { "content-type": "application/json" } }));
        if (u.pathname !== "/api/incidents") return real(url, opts);
        const limit = Number(u.searchParams.get("limit")), offset = Number(u.searchParams.get("offset") || 0), since = Number(u.searchParams.get("since") || 0);
        const until = u.searchParams.get("until") === null ? Infinity : Number(u.searchParams.get("until"));
        window.__asked.push(`${since > 1e9 ? "range" : limit}@${offset}`);
        const rows = all.filter((i) => i.end_time >= since && i.start_time <= until);
        return Promise.resolve(new Response(JSON.stringify(rows.slice(offset, offset + limit)), { headers: { "content-type": "application/json" } }));
      };
    });
    const asked = () => page.evaluate(() => window.__asked.join(" "));
    await new Promise((r) => setTimeout(r, 2500)); // the first message has set the version the panel compares with
    // the server says the list changed: the week is read again, in one request
    await page.evaluate(() => { window.__smokeMutate = new Function("d", "d.incidents_version = (d.incidents_version || 0) + 1000"); });
    await until(() => window.__asked.length >= 1);
    await page.evaluate(() => { window.__smokeMutate = null; });
    eq("one request for the 7 days of the timeline (no pages of 50)", (await asked()).split(" ").slice(0, 1).join(" "), "range@0");
    await waitText("#incidentCountBadge", /^128 recorded events$/); // the server's count (130 less the 2 hidden), not the ones loaded
    const inWeek = await page.evaluate(() => {
      const now = Date.now() / 1000;
      return Array.from({ length: 130 }, (_, i) => now - 560 - i * 7200).filter((t) => t >= now - 7 * 86400).length;
    });
    const rows = inWeek - 2; // (the two busy-line ones are hidden)
    const pages = Math.ceil(rows / 10);
    eq("the footer shows the same total as the badge (not the week's rows+)", await text("#incidentPaginationInfo"), "Showing 1–10 of 128 events");
    for (let n = 2; n <= pages; n++) {
      await click("#btnPageNext");
      await waitText("#incidentPageNum", new RegExp(`^Page ${n} of ${pages}$`));
    }
    eq("a short last page", await page.$$eval("#incidentsBody tr.incident-row", (r) => r.length), rows - (pages - 1) * 10);
    // Next from the last loaded page loads the next 50 (older than the week), the page fills up, no row is skipped
    await click("#btnPageNext");
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 10);
    await waitText("#incidentPaginationInfo", new RegExp(`^Showing ${(pages - 1) * 10 + 1}–${pages * 10} of `));
    eq("the older ones came in one page of 50 at the offset of what was loaded", (await asked()).split(" ").slice(1, 2).join(" "), `50@${inWeek}`);
    // the timeline is the last 7 x 24 hours ending now: count the synthetic incidents in it the same way (the two
    // busy-line ones may be hidden)
    const inWindow = await page.evaluate(() => {
      const now = Date.now() / 1000, start = now - 7 * 86400;
      return Array.from({ length: 130 }, (_, i) => now - 560 - i * 7200).filter((t) => t >= start).length;
    });
    ok(`the timeline has the week's incidents (${inWindow - 2} or more)`, await page.$$eval("#incidentTimeline .tl-bar", (b) => b.length) >= inWindow - 2);
    // a search looks through everything: 130 loaded, the old one found
    await page.type("#incidentSearchInput", "needle");
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 1);
    ok("the old incident is found", /Old needle/.test(await page.$eval("#incidentsBody", (e) => e.textContent)));
    await waitText("#incidentCountBadge", /^128 recorded events$/);
    await page.$eval("#incidentSearchInput", (i) => { i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); });
    // Next on the last loaded page loads more; with everything loaded the pager ends
    await page.select("#incidentPageSizeSelect", "50");
    await waitText("#incidentPageNum", /^Page 1 of 3$/);
    await click("#btnPageLast");
    await waitText("#incidentPageNum", /^Page 3 of 3$/);
    eq("the last page", await page.$$eval("#incidentsBody tr.incident-row", (r) => r.length), 28);
    // (the page count shows before the card has finished loading the last pieces: wait for the pager to settle)
    await until(() => document.getElementById("btnPageNext").disabled).catch(() => {});
    eq("no Next past the end", await page.$eval("#btnPageNext", (b) => b.disabled), true);
    // put the real server back
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 10);
  }],

  ["one zone for every time: Settings names Tokyo, this browser is in New York, the incident times are Tokyo's", async ({ eq, page, stubFetch, until }) => {
    await page.emulateTimezone("America/New_York");
    const info = await page.evaluate(async () => (await (await fetch("/api/info")).json()));
    await stubFetch({ "GET /api/info": { body: { ...info, timezone: "Asia/Tokyo" } } });
    const row = await page.evaluate(async () => {
      const first = (await (await fetch("/api/incidents?limit=1")).json())[0];
      const at = (timeZone) => new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", timeZone }).format(first.start_time * 1000);
      return { id: first.id, tokyo: at("Asia/Tokyo"), newYork: at("America/New_York") };
    });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length > 0);
    const shown = await page.$eval("#incidentsBody tr.incident-row .when", (e) => e.textContent);
    eq(`the first incident's time is Tokyo's (${shown})`, shown.endsWith(row.tokyo), true);
    eq("and not the browser's", shown.endsWith(row.newYork), false);
    await stubFetch(null);
    await page.emulateTimezone();
  }, { chromiumOnly: "page.emulateTimezone is CDP only" }],

  ["a change of the page's time zone (a Save in Settings) redraws the times already shown: the incidents and a device card's read time", async ({ LINE, eq, moduleUrl, ok, page, until }) => {
    await until((l) => document.querySelectorAll("#incidentsBody tr.incident-row").length > 0 && document.querySelector(`${l}`)?.dataset.readAt, LINE);
    const shown = await page.evaluate(async (u, l) => {
      const { setPageZone } = await import(u);
      const first = (await (await fetch("/api/incidents?limit=1")).json())[0];
      const zone = (await (await fetch("/api/info")).json()).timezone || undefined; // the page's zone now (Settings)
      const card = document.querySelector(l);
      const at = (ms, timeZone) => new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", timeZone }).format(ms);
      const read = Number(card.dataset.readAt) * 1000;
      const now = () => ({ when: document.querySelector("#incidentsBody tr.incident-row .when").textContent, read: card.querySelector("[data-f=read]").textContent });
      setPageZone("Asia/Tokyo"); // as fetchNetworkInfo does with the zone Settings names; read at once, before any new data
      const tokyo = now();
      setPageZone(zone);
      const back = now();
      return { tokyo, back, want: { when: at(first.start_time * 1000, "Asia/Tokyo"), read: at(read, "Asia/Tokyo") },
        before: { when: at(first.start_time * 1000, zone), read: at(read, zone) } };
    }, await moduleUrl("clock.js"), LINE);
    ok(`Tokyo's clock differs from the page's here (${shown.want.when} / ${shown.before.when})`, shown.want.when !== shown.before.when);
    ok(`the first incident at once on Tokyo's clock (${shown.tokyo.when}, want ${shown.want.when})`, shown.tokyo.when.endsWith(shown.want.when));
    ok(`the line card's read time at once on Tokyo's clock (${shown.tokyo.read})`, shown.tokyo.read.startsWith(`read ${shown.want.read}`));
    eq("and back on the page's own clock", [shown.back.when.endsWith(shown.before.when), shown.back.read.startsWith(`read ${shown.before.read}`)].join(), "true,true");
  }],

  ["card model and view: the five cards of this group have fixtures that draw marks; a refresh of the list keeps unchanged rows and the focus on a row", async ({ eq, moduleUrl, ok, page, until }) => {
    // docs/themes.md, "Drawing a card": incidents, heat, matrix, strips and report are drawn from a view model
    const cards = await page.evaluate(async (u) => {
      const { drawApart, registeredCards } = await import(u);
      const out = {};
      for (const c of registeredCards().filter((r) => ["incidents", "heat", "matrix", "strips", "report"].includes(r.panel))) {
        const drawn = drawApart(c.panel, c.standard, c.fixture());
        out[c.panel] = { values: Object.keys(drawn.marks.values).length, status: Object.keys(drawn.marks.status).length, actions: drawn.marks.actions.length };
        drawn.remove();
      }
      return out;
    }, await moduleUrl("cards.js"));
    eq("every card has a fixture", Object.keys(cards).sort().join(), "heat,incidents,matrix,report,strips");
    for (const [panel, n] of Object.entries(cards)) ok(`${panel}: ${JSON.stringify(n)}`, n.values >= 5 && (n.actions >= 1 || panel === "report"));
    ok("incidents and the hop rows mark a status", cards.incidents.status >= 3 && cards.matrix.status >= 3);
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length >= 3);
    const kept = await page.evaluate(async (u) => {
      const single = () => [...document.querySelectorAll("#incidentsBody tr.incident-row:not(.outage-row)")]; // (an outage row is keyed by the outage)
      const { updateIncidentsTable } = await import(u);
      const list = await (await fetch("/api/incidents?limit=500")).json();
      const rows = single();
      rows.forEach((tr, i) => { tr.__kept = i; });
      const first = rows[0].querySelector("button.primary");
      first.focus();
      updateIncidentsTable(JSON.parse(JSON.stringify(list))); // the same list again: nothing is drawn again
      const same = single().every((tr, i) => tr.__kept === i) && document.activeElement === first;
      const last = rows[rows.length - 1];
      const changed = JSON.parse(JSON.stringify(list));
      changed.find((i) => String(i.id) === last.dataset.arg).title = "Renamed incident";
      updateIncidentsTable(changed); // one incident changed: its row is drawn again, the others stay
      const now = single();
      const result = { same, kept: now.slice(0, -1).every((tr, i) => tr.__kept === i), redrawn: now[now.length - 1].__kept === undefined, title: now[now.length - 1].textContent.includes("Renamed incident"),
        focus: document.activeElement === first };
      updateIncidentsTable(list);
      return result;
    }, await moduleUrl("../panels/incidents/model.js"));
    ok("the same list leaves every row and the focus alone", kept.same);
    ok("a changed incident redraws only its row", kept.kept && kept.redrawn && kept.title && kept.focus);
  }],

  ["more than 5000 incidents: the card says only the newest 5000 are listed and where the rest is, and its count is the server's", async ({ eq, ok, page, stubFetch, text, until, waitText }) => {
    const now = Math.floor(Date.now() / 1000);
    const all = Array.from({ length: 5000 }, (_, i) => ({ id: 20000 + i, kind: "gateway_down", title: `Synthetic ${i}`, severity: "warning", cause_code: "unknown_outage", cause: "isp",
      start_time: now - 600 - i * 600, end_time: now - 560 - i * 600, duration_seconds: 40, expected: false }));
    const stubs = (total) => ({ "GET /api/incidents": { body: all }, "GET /api/incidents/count": { body: { total, expected: 0 } }, "GET /api/outages": { body: { gap_minutes: 10, outages: [] } } });
    await stubFetch(stubs(6500));
    await waitText("#incidentCountBadge", /^6,500 recorded events/);
    eq("the badge keeps the server's count and says what is listed", await text("#incidentCountBadge"), "6,500 recorded events · newest 5000 listed");
    ok("the note names the cap, the real total and where the rest is", /^Only the newest 5000 incidents are listed \(of 6,500 recorded\)\. Older ones are in the ISP dossier .*the reliability report .*the API \(\/api\/incidents with since, limit and offset\)\.$/.test(await text("#incidentCapNote")));
    await until(() => !document.getElementById("incidentCapNote").hidden);
    eq("the footer does not claim more than is listed", await text("#incidentPaginationInfo"), "Showing 1–10 of 5000 events, from the newest 5000 incidents");
    // exactly 5000 recorded: nothing is left out, so nothing is said
    await stubFetch(stubs(5000));
    await waitText("#incidentCountBadge", /^5000 recorded events$/);
    ok("no note when the list holds everything", await page.$eval("#incidentCapNote", (e) => e.hidden));
    await stubFetch(null);
  }],

  ["90 d and All draw every incident of the range with 700 stored (no 500 limit): one request per range, only for what is older than the list holds", async ({ chooseRange, eq, ok, page, rangeState, settled, showCard, until }) => {
    await showCard("incidents");
    // a server with 700 incidents, one every 6 hours (175 days), newest first, answering ranges (since, until, limit, offset) as the real one does
    await page.evaluate(() => {
      const now = Date.now() / 1000;
      const all = Array.from({ length: 700 }, (_, i) => ({ id: 30000 + i, kind: "gateway_down", title: `Range ${i}`, description: "", severity: "warning", cause_code: "unknown_outage", cause: "isp",
        start_time: now - 600 - i * 21600, end_time: now - 560 - i * 21600, duration_seconds: 40, expected: false, affected_targets: [] }));
      const real = window.fetch;
      window.__realFetch = real;
      window.__asked = [];
      window.__outagesAsked = [];
      const json = (body) => Promise.resolve(new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }));
      window.fetch = (url, opts) => {
        const u = new URL(String(url), location.href);
        if (u.pathname === "/api/incidents/count") return json({ total: all.length, expected: 0 });
        if (u.pathname === "/api/outages") { window.__outagesAsked.push(u.search); return json({ gap_minutes: 10, outages: [] }); }
        if (u.pathname !== "/api/incidents") return real(url, opts);
        window.__asked.push(u.search);
        const since = Number(u.searchParams.get("since") || 0), limit = Number(u.searchParams.get("limit")), offset = Number(u.searchParams.get("offset") || 0);
        const until = u.searchParams.get("until") === null ? Infinity : Number(u.searchParams.get("until"));
        return json(all.filter((i) => i.end_time >= since && i.start_time <= until).slice(offset, offset + limit));
      };
    });
    await new Promise((r) => setTimeout(r, 2500)); // the first message has set the version the panel compares with
    await page.evaluate(() => { window.__smokeMutate = new Function("d", "d.incidents_version = (d.incidents_version || 0) + 1000"); });
    await until(() => window.__asked.length >= 1);
    await page.evaluate(() => { window.__smokeMutate = null; });
    await new Promise((r) => setTimeout(r, 2500)); // (a message already on its way may still ask for one more read)
    await chooseRange("#incidentRanges", "7d");
    await settled();
    const bars = () => page.$$eval("[data-panel=incidents] .tl .tl-bar", (b) => b.length);
    const inRange = (days) => page.evaluate((d) => { const now = Date.now() / 1000; return Array.from({ length: 700 }, (_, i) => now - 560 - i * 21600).filter((t) => t >= now - d * 86400).length; }, days);
    eq("7 d: the week's incidents", await bars(), await inRange(7));
    const before = await page.evaluate(() => window.__asked.length);
    for (const id of ["90d", "all"]) {
      await chooseRange("#incidentRanges", id);
      await settled();
      const days = id === "all" ? 180 : 90;
      eq(`${id}: every incident of the range is drawn (${await inRange(days)})`, await bars(), await inRange(days));
    }
    const asked = await page.evaluate((n) => window.__asked.slice(n), before);
    eq(`90 d and All: one request each (${asked.join(" ")})`, asked.length, 2);
    ok(`each asks for what is older than the list holds (${asked.join(" ")})`, asked.every((q) => /until=/.test(q) && /limit=5000/.test(q)));
    ok("and their outages are not asked again for each page", (await page.evaluate(() => window.__outagesAsked.length)) <= 4);
    ok("more than 500 are stored and all of them are drawn", (await inRange(180)) > 500);
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 10);
    eq("(the real page again)", (await rangeState("#incidentRanges")).value, "7d");
  }],

  ["a Save made between the page render and the live connection re-reads the incident list: the panel starts from the revision the page was rendered at", async ({ eq, layoutMounted, nextMessages, ok, page, sleep, waitText }) => {
    const reads = () => page.evaluate(() => performance.getEntriesByType("resource").filter((r) => /\/api\/incidents\?/.test(r.name)).length);
    const live = await page.evaluate(async () => (await (await fetch("/api/settings/values", { cache: "no-store" })).json()).revision);
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    await waitText("#wsText", /^Live/);
    await nextMessages(3);
    await sleep(500);
    const plain = await reads();
    ok(`the list was read at the start (${plain})`, plain >= 1);
    // the page claims another revision than the server's, as after a Save between the render and the connection
    const hook = await page.evaluateOnNewDocument((fake) => {
      new MutationObserver((_, obs) => { if (document.body) { document.body.dataset.settingsRevision = String(fake); obs.disconnect(); } }).observe(document, { childList: true, subtree: true });
    }, live + 1000);
    try {
      await page.reload({ waitUntil: "load" });
      await layoutMounted();
      eq("(the page claims a revision the server is not at)", await page.evaluate(() => document.body.dataset.settingsRevision), String(live + 1000));
      await waitText("#wsText", /^Live/);
      await nextMessages(3);
      await sleep(500);
      const after = await reads();
      ok(`the first message's revision differed: the list was read again (${after} reads, ${plain} without a Save)`, after > plain);
    } finally {
      await page.removeScriptToEvaluateOnNewDocument(hook.identifier);
      await page.reload({ waitUntil: "load" });
      await layoutMounted();
    }
  }],

  ["every range draws the incident timeline (bars, a line per midnight to 30 days, per week past it, month labels past 60 days that do not overlap), on a desktop, a phone and in every theme", async ({ atEveryRange, chooseRange, eq, everyLook, noSideways, ok, openPanel, page, phone, rangeState, settled, showCard }) => {
    const TL = "[data-panel=incidents] .tl"; // (a theme's own view, Amber CRT's, keeps the standard timeline without its id)
    const WANT = { "7d": { bars: 10, lines: [6, 8], label: /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$/ }, "30d": { bars: 10, lines: [29, 31], label: /^[A-Z][a-z]{2} \d{1,2}$/ },
      "90d": { bars: 30, lines: [12, 14], label: /^[A-Z][a-z]{2}( \d{4})?$/ }, all: { bars: 40, lines: [22, 25], /* All reaches the oldest stored incident, not the retention: the seed's oldest is 166 days back (seed_long_history: 44 blips 3.7 days apart from 3 days ago), 24 weeks */ label: /^[A-Z][a-z]{2}( \d{4})?$/ } };
    const check = (where) => async (id) => {
      const what = `${where} ${id}`;
      const w = WANT[id];
      const bars = await page.$$eval(TL + " .tl-bar", (b) => b.length);
      ok(`${what}: ${bars} bars (at least ${w.bars})`, bars >= w.bars);
      const lines = await page.$eval(TL + " .tl-track", (t) => (getComputedStyle(t).backgroundImage.match(/linear-gradient/g) || []).length);
      ok(`${what}: ${lines} day or week lines (${w.lines.join("-")})`, lines >= w.lines[0] && lines <= w.lines[1]);
      const labels = await page.$$eval(TL + " [data-v=days] span", (s) => s.map((x) => ({ t: x.textContent, l: x.getBoundingClientRect().left, r: x.getBoundingClientRect().right })));
      ok(`${what}: ${labels.length} labels, each of the right kind (${labels.map((l) => l.t).join(" | ")})`, labels.length >= 2 && labels.length <= 9 && labels.every((l) => w.label.test(l.t)));
      ok(`${what}: no label runs into the next`, labels.every((l, i) => i === 0 || l.l >= labels[i - 1].r - 1));
      ok(`${what}: nothing sticks out sideways`, await noSideways());
    };
    await showCard("incidents");
    eq("7 d to All (kept 180 days)", (await rangeState("#incidentRanges")).ids.join(), "7d,30d,90d,all");
    await atEveryRange("#incidentRanges", "#incidentTimeline", async (id) => { await check("desktop")(id); }, { pause: 600 });
    await everyLook(async (look) => {
      await showCard("incidents");
      for (const id of ["30d", "all"]) {
        await chooseRange("#incidentRanges", id);
        await settled();
        await check(look)(id);
      }
    });
    await phone(async () => {
      await openPanel("incidents", "incidents");
      await atEveryRange("#incidentRanges", "#incidentTimeline", async (id) => { await check("phone")(id); }, { pause: 600 });
    });
  }, { variants: ["longhistory"] }],

  // ---- latency spikes: the block between the timeline and the list (panels/incidents/spikes.js) ----
  ["latency spikes: the block counts the range's brief latency spells (the seed has 5 counted, 1 busy-line one left out), says where most were, draws a bar per day (cut days narrower), follows the range with one request, and opens nothing", async ({ chooseRange, count, eq, forgetRequests, ok, page, requestsTo, settled, showCard, text, until }) => {
    await showCard("incidents");
    await until(() => !document.getElementById("incidentSpikes").hidden);
    eq("the title says what a spike is, with the setting's wait", await text("#incidentSpikes [data-v=spikesTitle]"), "Latency spikes (no loss, < 2 min):");
    const sums = async () => page.$$eval("#incidentSpikes .sp-day", (bars) => bars.map((b) => Number(/ · (\d+) spikes?/.exec(b.dataset.tip)[1])));
    const week = await sums();
    eq("a bar per local day of the 7 days (the first and last cut by the window)", week.length >= 7 && week.length <= 8, true);
    eq("the bars add up to the count (the busy-line spell is left out)", week.reduce((a, b) => a + b, 0), 5);
    eq("the line: the count over the range and today's, the last bar's", await text("#incidentSpikes [data-v=spikes]"), `5 in 7 days · ${week[week.length - 1]} today`);
    eq("where most were", await text("#incidentSpikes [data-v=spikesWhere]"), "mostly at the ISP hops");
    const widths = await page.$$eval("#incidentSpikes .sp-day", (bars) => bars.map((b) => b.getBoundingClientRect().width));
    ok(`the cut last day is narrower than a whole one (${widths.map((w) => w.toFixed(1))})`, widths[widths.length - 1] < widths[widths.length - 2] - 0.5 || widths[widths.length - 1] < widths[1] - 0.5);
    ok("the cut days say so in their tooltip", (await page.$eval("#incidentSpikes .sp-day:last-child", (b) => b.dataset.tip)).includes("of this day are in the range"));
    ok("a tooltip names the day and the count (Tue Nov 11 · 6 spikes)", /^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} · \d+ spikes?/.test(await page.$eval("#incidentSpikes .sp-day:nth-child(3)", (b) => b.dataset.tip)));
    eq("the block is not a button and opens nothing", await count("#incidentSpikes button, #incidentSpikes [data-action], #incidentSpikes a"), 0);
    // one request per range change
    forgetRequests();
    await chooseRange("#incidentRanges", "30d");
    await settled();
    await until(() => /in 30 days/.test(document.querySelector("#incidentSpikes [data-v=spikes]")?.textContent ?? ""));
    eq("30 d asks once", requestsTo(/\/api\/latency-spikes/).map((u) => u.replace(/&tz.*$/, "")).join(), "/api/latency-spikes?hours=720");
    const month = await sums();
    eq("30 days: a bar per day (31 or 30 cells), adding up to the count", `${month.length >= 30 && month.length <= 31}|${month.reduce((a, b) => a + b, 0)}`, "true|5");
    ok("a longer range contains the shorter", month.reduce((a, b) => a + b, 0) >= week.reduce((a, b) => a + b, 0));
    forgetRequests();
    await chooseRange("#incidentRanges", "7d");
    await settled();
    await until(() => /in 7 days/.test(document.querySelector("#incidentSpikes [data-v=spikes]")?.textContent ?? ""));
    eq("and back to 7 d asks once", requestsTo(/\/api\/latency-spikes/).length, 1);
  }],

  ["latency spikes: the '< N min' follows the setting, a range without spikes has one quiet line, and an incident at once (0) has no block", async ({ chooseRange, eq, ok, page, settled, showCard, text, until }) => {
    await showCard("incidents");
    await until(() => !document.getElementById("incidentSpikes").hidden);
    // the page's own copy of the answer, changed: another wait, none, then the rule 0
    await page.evaluate(() => {
      window.__realFetch = window.__realFetch || window.fetch.bind(window);
      window.__spikeAnswer = (body) => body;
      window.fetch = async (u, ...a) => {
        const res = await window.__realFetch(u, ...a);
        if (new URL(String(u), location.href).pathname !== "/api/latency-spikes" || !res.ok) return res;
        return new Response(JSON.stringify(window.__spikeAnswer(await res.json())), { status: 200, headers: { "content-type": "application/json" } });
      };
    });
    const again = async () => { await chooseRange("#incidentRanges", "30d"); await settled(); await chooseRange("#incidentRanges", "7d"); await settled(); };
    await page.evaluate(() => { window.__spikeAnswer = (b) => ({ ...b, incident_after_seconds: 90 }); });
    await again();
    await until(() => /< 90 s\)/.test(document.querySelector("#incidentSpikes [data-v=spikesTitle]")?.textContent ?? ""));
    await page.evaluate(() => { window.__spikeAnswer = (b) => ({ ...b, incident_after_seconds: 300 }); });
    await again();
    await until(() => /< 5 min\)/.test(document.querySelector("#incidentSpikes [data-v=spikesTitle]")?.textContent ?? ""));
    await page.evaluate(() => { window.__spikeAnswer = (b) => ({ ...b, count: 0, expected: 0, by_stage: [], days: b.days.map((d) => ({ ...d, count: 0 })) }); });
    await again();
    await until(() => document.querySelector("#incidentSpikes [data-v=spikes]")?.textContent === "No latency spikes in this range");
    eq("a range without spikes: one quiet line, no bars", await page.$$eval("#incidentSpikes .sp-day", (b) => b.length), 0);
    ok("the quiet line is shown", await page.$eval("#incidentSpikes", (e) => !e.hidden && e.classList.contains("quiet")));
    await page.evaluate(() => { window.__spikeAnswer = (b) => ({ ...b, incident_after_seconds: 0, count: 0, by_stage: [] }); });
    await again();
    await until(() => document.getElementById("incidentSpikes").hidden);
    ok("with the wait at 0 (an incident at once) there is no block", await page.$eval("#incidentSpikes", (e) => e.hidden));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await again();
    await until(() => !document.getElementById("incidentSpikes").hidden && /^5 in 7 days/.test(document.querySelector("#incidentSpikes [data-v=spikes]")?.textContent ?? ""));
  }],

  ["latency spikes: the block is in every theme at 1400 and at 390 px, with the same bars, and nothing sticks out sideways", async ({ eq, everyLook, noSideways, ok, openPanel, page, phone, settled, showCard }) => {
    const check = (where) => async () => {
      await settled();
      const sel = "[data-panel=incidents] .inc-spikes";
      const box = await page.$eval(sel, (e) => { const r = e.getBoundingClientRect(); const card = e.closest(".panel").getBoundingClientRect(); return { hidden: e.hidden || r.width === 0, inside: r.left >= card.left - 1 && r.right <= card.right + 1, bars: e.querySelectorAll(".sp-day").length, tall: e.querySelectorAll(".sp-day i").length }; });
      ok(`${where}: the block is shown, with a bar per day (${JSON.stringify(box)})`, !box.hidden && box.bars >= 7);
      ok(`${where}: inside its card`, box.inside);
      ok(`${where}: nothing sticks out sideways`, await noSideways());
      eq(`${where}: the line is there`, await page.$eval(sel + " [data-v=spikes]", (e) => /^\d+ in 7 days · \d+ today$/.test(e.textContent)), true);
    };
    await showCard("incidents");
    await check("desktop")();
    await everyLook(async (look) => { await showCard("incidents"); await check(look || "default")(); });
    await phone(async () => {
      await openPanel("incidents", "incidents");
      await check("phone")();
      await everyLook(async (look) => { await openPanel("incidents", "incidents"); await check(`phone ${look || "default"}`)(); });
    });
  }],

  ["latency spikes: 7 d, 30 d, 90 d and All hold each other, bars add up to the count at every range, one request per range", async ({ chooseRange, eq, forgetRequests, ok, page, requestsTo, settled, showCard, until }) => {
    await showCard("incidents");
    const read = () => page.evaluate(() => {
      const m = /^(\d+) in (\d+) days/.exec(document.querySelector("#incidentSpikes [data-v=spikes]")?.textContent ?? "");
      return { count: m ? Number(m[1]) : null, days: m ? Number(m[2]) : null,
        sum: [...document.querySelectorAll("#incidentSpikes .sp-day")].reduce((a, b) => a + Number(/ · (\d+) spikes?/.exec(b.dataset.tip)[1]), 0), bars: document.querySelectorAll("#incidentSpikes .sp-day").length };
    });
    const seen = {};
    for (const [id, days] of [["7d", 7], ["30d", 30], ["90d", 90], ["all", 180]]) {
      forgetRequests();
      await chooseRange("#incidentRanges", id);
      await settled();
      await until((d) => new RegExp(`in ${d} days`).test(document.querySelector("#incidentSpikes [data-v=spikes]")?.textContent ?? ""), days);
      seen[id] = await read();
      if (id !== "7d") eq(`${id}: one request`, requestsTo(/\/api\/latency-spikes/).length, 1); // (7 d is the range it starts on: choosing it again changes nothing)
      eq(`${id}: the bars add up to the count (${JSON.stringify(seen[id])})`, seen[id].sum, seen[id].count);
      ok(`${id}: a bar per day (${seen[id].bars} for ${days} days)`, seen[id].bars >= days && seen[id].bars <= days + 1);
    }
    ok(`a longer range holds the shorter (${["7d", "30d", "90d", "all"].map((i) => seen[i].count)})`, seen["7d"].count <= seen["30d"].count && seen["30d"].count <= seen["90d"].count && seen["90d"].count <= seen.all.count);
    ok("the long history has more than the week", seen.all.count > seen["7d"].count + 10);
    await chooseRange("#incidentRanges", "7d");
  }, { variants: ["longhistory"] }],
];
