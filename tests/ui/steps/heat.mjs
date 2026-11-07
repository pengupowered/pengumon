// The loss heatmap.

export const subject = "panel:heat";
export const variants = ["full"];

export const steps = [
  ["loss heatmap: a cell per hour of the week and an insight", async ({ ok, outDir, page, path, text, waitCount }) => {
    await waitCount("#heatGrid > i, #heatGrid > .cell", 7 * 24);
    ok("insight", (await text("#heatInsight")).length > 20);
    await page.screenshot({ path: path.join(outDir, "strips-heat.png"), fullPage: true });
  }],

  ["loss heatmap: every hour so far is a button, and it lists that hour's incidents", async ({ click, count, eq, ok, page, shown, text, until }) => {
    await until(() => document.querySelectorAll("#heatGrid .cell").length >= 1);
    const cells = await page.$$eval("#heatGrid > i, #heatGrid > .cell", (els) => els.map((e) => ({ button: e.tagName === "BUTTON", start: Number(e.dataset.start || 0) })));
    const plain = await page.$$eval("#heatGrid > i", (els) => els.map((e) => [e.dataset.tip, getComputedStyle(e).cursor]));
    ok("only hours still to come are plain cells", plain.every(([tip, cursor]) => /not yet/.test(tip) && cursor !== "pointer"));
    ok("the buttons are the hours that have begun, with and without loss", cells.filter((c) => c.button).length > 24 * 6
      && cells.filter((c) => c.button).every((c) => c.start <= Date.now() / 1000));
    eq("the hint says so", await shown("#heatHint"), true);
    // the hour (or the first of two) the seeded gateway drop starts in, and what overlaps it, counted from the API here
    const pick = await page.evaluate(async () => {
      const list = await (await fetch("/api/incidents?limit=500")).json();
      const inc = list.find((i) => i.title === "Short gateway drop");
      const cell = [...document.querySelectorAll("#heatGrid .cell")].find((c) => Number(c.dataset.start) <= inc.start_time && inc.start_time < Number(c.dataset.end));
      if (!cell) return null;
      const [start, end] = [Number(cell.dataset.start), Number(cell.dataset.end)];
      cell.click();
      return list.filter((i) => !i.expected && i.start_time < end && (i.end_time ?? Date.now() / 1000) > start).length;
    });
    ok("the hour of the seeded drop is a button", pick !== null);
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok("the window names the hour", /\d\d:00–\d\d:00/.test(await text("#incidentWindow")));
    ok(`the list holds the ${pick} incidents overlapping that hour`, new RegExp(`of ${pick} events`).test(await text("#incidentPaginationInfo")));
    ok("and the list says it is filtered", /\(filtered\)/.test(await text("#incidentPaginationInfo")));
    eq("the timeline marks the hour", await count("#incidentTimeline .tl-win"), 1);
    // at the top of the window, or as near as the page's end allows (then still in the upper part of the window)
    await until(() => {
      const top = document.querySelector("[data-panel=incidents] .panel").getBoundingClientRect().top;
      const atEnd = scrollY + innerHeight >= document.documentElement.scrollHeight - 1;
      return Math.abs(top) < 80 || (atEnd && top >= 0 && top < innerHeight / 3);
    });
    await click("[data-action=clearIncidentWindow]");
    await until(() => document.getElementById("incidentWindow").hidden && !document.querySelector("#incidentTimeline .tl-win"));
  }],

  ["loss heatmap: an hour without probes or without loss says so", async ({ ok, page, text, until }) => {
    // a real hour days ago (the seed covers the last 2 h 20 min only): no probes, no incident
    const picked = await page.evaluate(async () => {
      const list = await (await fetch("/api/incidents?limit=500")).json();
      const cell = [...document.querySelectorAll("#heatGrid .cell")].find((c) => c.dataset.sent === "0"
        && !list.some((i) => i.start_time < Number(c.dataset.end) && (i.end_time ?? Date.now() / 1000) > Number(c.dataset.start)));
      if (cell) cell.click();
      return Boolean(cell);
    });
    ok("an hour without probes is a button", picked);
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok("it says nothing was probed", /No probes in this time/.test(await text("#incidentsBody")));
    // a quiet hour with probes (the seed has none: its long outage covers every probed hour)
    await page.evaluate(() => {
      const b = document.createElement("button");
      Object.assign(b.dataset, { action: "showIncidentWindow", start: "1000", end: "4600", label: "Quiet test", lost: "0", sent: "3600" });
      document.body.append(b);
      b.click();
      b.remove();
    });
    await until(() => /Quiet test/.test(document.getElementById("incidentWindow").textContent));
    ok("all its probes answered", /All 3,600 probes answered/.test(await text("#incidentsBody")));
    await page.evaluate(() => document.querySelector("[data-action=clearIncidentWindow]").click());
    await until(() => document.getElementById("incidentWindow").hidden);
  }],
  ["loss heatmap: an hour opens the server's hour, also on a 25-hour day in another zone than this browser's", async ({ eq, moduleUrl, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#heatGrid .cell").length >= 1);
    // Berlin, 25 October 2026: 03:00 CEST becomes 02:00 CET at 01:00 UTC, so 02:00-03:00 happens twice (00:00-02:00 UTC)
    // and 03:00 is 02:00 UTC; this browser runs in another zone (UTC in the test image), where 03:00 is an hour later
    // the made-up answer goes through the model's present() and is drawn by the card's standard view, apart from the page
    const got = await page.evaluate(async ([model, cards]) => {
      const { present } = await import(model);
      const { drawApart, registeredCards } = await import(cards);
      const at = (d, h) => Date.UTC(2026, 9, d, h) / 1000;
      const sunday = Array.from({ length: 25 }, (_, h) => (h < 3 ? at(24, 22) + h * 3600 : at(25, h - 1)));
      const rows = (v) => Array.from({ length: 3 }, () => Array(24).fill(v));
      const apart = drawApart("heat", registeredCards().find((c) => c.panel === "heat").standard, present({ days: ["2026-10-24", "2026-10-25", "2026-10-26"], weekdays: [5, 6, 0], zone: "Europe/Berlin", tz_offset_minutes: 60,
        day_starts: [at(23, 22), at(24, 22), at(25, 23), at(26, 23)], hour_starts: [null, sunday, null], sent: rows(0), lost: rows(0), loss_pct: rows(null) }, Date.now() / 1000));
      const cells = [...apart.root.querySelectorAll(".cell")];
      const span = (c) => `${Number(c.dataset.start) - at(25, 0)}-${Number(c.dataset.end) - at(25, 0)}`;
      const out = { browser: Intl.DateTimeFormat().resolvedOptions().timeZone, twice: span(cells[24 + 2]), three: span(cells[24 + 3]),
        monday: span(cells[48]), tip: cells[24 + 2].dataset.tip };
      cells[24 + 3].click(); // its action is the page's, wherever the button is
      apart.remove();
      return out;
    }, [await moduleUrl("../panels/heat/model.js"), await moduleUrl("cards.js")]);
    ok(`the browser runs in another zone (${got.browser})`, got.browser !== "Europe/Berlin");
    eq("Sunday 02:00-03:00 is two hours: 00:00-02:00 UTC", got.twice, "0-7200");
    ok(`and says why (${got.tip})`, /twice: the clocks went back/.test(got.tip));
    eq("Sunday 03:00-04:00 is 02:00-03:00 UTC", got.three, "7200-10800");
    eq("Monday 00:00 is 23:00 UTC on Sunday", got.monday, "82800-86400");
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok("the window names the hour as the grid does", /Sun 03:00–04:00/.test(await text("#incidentWindow")));
    await page.evaluate(() => document.querySelector("[data-action=clearIncidentWindow]").click());
    await until(() => document.getElementById("incidentWindow").hidden);
  }],
  ["time cells: one Tab stop per grid, arrow keys move between hours, Enter opens the hour", async ({ eq, ok, page, until }) => {
    eq("one heat cell in the tab order", await page.$$eval("#heatGrid > button", (b) => b.filter((e) => e.tabIndex === 0).length), 1);
    ok("one block per uptime strip in the tab order", await page.$$eval("#stripsBody .strip", (rows) => rows.length > 0 && rows.every((r) => [...r.children].filter((e) => e.tabIndex === 0).length === 1)));
    await page.focus("#heatGrid > button[tabindex='0']");
    const at = () => page.evaluate(() => [...document.querySelectorAll("#heatGrid > button")].indexOf(document.activeElement));
    await page.keyboard.press("ArrowRight");
    eq("right: the next hour", await at(), 1);
    await page.keyboard.press("ArrowDown");
    eq("down: the same hour a day later", await at(), 25);
    eq("the focused cell is the one tab stop", await page.$$eval("#heatGrid > button", (b) => b.filter((e) => e.tabIndex === 0).map((e) => e === document.activeElement).join()), "true");
    await page.keyboard.press("Enter");
    await until(() => !document.getElementById("incidentWindow").hidden);
    await page.evaluate(() => document.querySelector("[data-action=clearIncidentWindow]").click());
    await until(() => document.getElementById("incidentWindow").hidden);
  }],

  ["loss heatmap: the same answer again leaves the cells alone, a different one draws them", async ({ moduleUrl, ok, page, until }) => {
    await until(() => document.querySelectorAll("#heatGrid .cell").length >= 1);
    // the answers go through the model's present() into the standard view's grid part, as the model's refresh does
    const result = await page.evaluate(async ([model, cards]) => {
      const { present } = await import(model);
      const { registeredCards } = await import(cards);
      const box = document.createElement("div");
      const grid = registeredCards().find((c) => c.panel === "heat").standard.parts.grid(box);
      const answer = await (await fetch("/api/report/loss-grid?days=7")).json();
      const now = Date.now() / 1000;
      grid.update(present(answer, now));
      box.querySelector(".cell").dataset.mark = "kept";
      grid.update(present(JSON.parse(JSON.stringify(answer)), now));
      const same = box.querySelector(".cell").dataset.mark === "kept";
      const next = JSON.parse(JSON.stringify(answer));
      next.lost[0][0] += 1;
      grid.update(present(next, now));
      return { same, changed: box.querySelector(".cell").dataset.mark === undefined };
    }, [await moduleUrl("../panels/heat/model.js"), await moduleUrl("cards.js")]);
    ok("an identical answer leaves the cells alone", result.same);
    ok("a changed answer draws them", result.changed);
  }],
];
