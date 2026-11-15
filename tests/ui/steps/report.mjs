// The reliability report.

export const subject = "panel:report";
export const variants = ["full"];

export const steps = [
  ["reliability report renders and switches range", async ({ chooseRange, count, eq, ok, page, rangeState, text, until }) => {
    await until(() => /^Last 7 days:/.test(document.getElementById("reportOutput").dataset.summary ?? ""));
    ok("headline", /\d+\.\d\d%/.test(await text("#reportOutput .rep-hero")));
    ok("hop rows", (await count("#reportOutput .rep-hops tbody tr")) >= 3);
    // the seed's network incidents that are drops of the line: the night event (3 drops, down 6 min, 13 min from the first to the last), the
    // gateway drop and the line loss (one drop each); the label is on the night event, and on the line loss too when this runs at night
    const outages = (await text("#reportOutput [data-v=outages]")).trim();
    ok(`the outages next to the incidents (${outages})`, /^3 outages, down 6 min in all, longest 13 min, [12] likely maintenance$/.test(outages));
    const hops = await page.$$eval("#reportOutput .rep-hops tbody td.lead .primary", (els) => els.slice(0, 3).map((e) => `${e.textContent}|${e.title}`).join(","));
    eq("hops as the rest of the page names them, the configured name on hover", hops,
      "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    ok("a bar per day with data", (await count("#reportOutput .days .day")) >= 1);
    ok("today is cut by the range (it ends now): its bar says how much of the day the range holds", /only .+ of this day are in the range/.test(await page.$eval("#reportOutput .days .day:last-child", (e) => e.dataset.tip)));
    ok("the bars are what was lost per day, with the worst day named", /Lost per day/i.test(await text("#reportOutput .rep-days")) && /Worst: |No probe lost/.test(await text("#reportOutput .rep-days .rep-note")));
    const causes = await page.$$eval("#reportOutput .causes li span:first-child", (els) => els.map((e) => e.textContent));
    ok(`causes in words, not classifier codes: ${causes}`, causes.length > 0 && causes.every((c) => !/_/.test(c) && !/^unknown outage$/.test(c)));
    eq("the control offers 7, 30, 90 days and All", (await rangeState("#reportRangeButtons")).words.join(","), "Last 7 days,Last 30 days,Last 90 days,All · 180 d");
    await chooseRange("#reportRangeButtons", "30d");
    await until(() => /^Last 30 days:/.test(document.getElementById("reportOutput").dataset.summary ?? ""));
    eq("active", (await rangeState("#reportRangeButtons")).value, "30d");
  }],

  ["a hop that ignores ping is listed as such in Every hop, not with the availability of its unanswered pings", async ({ eq, page, until }) => {
    await until(() => /^Last \d+ days:/.test(document.getElementById("reportOutput").dataset.summary ?? ""));
    await page.evaluate(() => { document.querySelector("#reportOutput .rep-hops-box").open = true; });
    await page.evaluate(() => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => {
        if (t.target_id === "local_router") Object.assign(t, { status: "down", last_ms: null, recent_latencies: Array(30).fill(null), loss_pct: 100, loss_recent_pct: 100, ignores_ping: true });
      });
    });
    await until(() => [...document.querySelectorAll("#reportOutput .rep-hops tbody tr")].some((r) => /Ignores ping/.test(r.textContent)));
    eq("the open table stays open when its rows change", await page.$eval("#reportOutput .rep-hops-box", (d) => d.open), true);
    const rows = await page.$$eval("#reportOutput .rep-hops tbody tr", (rs) => rs.map((r) => [...r.cells].map((c) => c.textContent.trim()).join("|")));
    eq("the router's row", rows.filter((r) => /Ignores ping/.test(r)).join(), "Router|Ignores ping|–");
    await page.evaluate(() => { window.__smokeMutate = null; });
    await until(() => ![...document.querySelectorAll("#reportOutput .rep-hops tbody tr")].some((r) => /Ignores ping/.test(r.textContent)));
  }],

  ["every range draws the report (a bar per day to 30 days, a bar per week past it, bars wide enough to see), on a desktop, a phone and in every theme", async ({ atEveryRange, chooseRange, eq, everyLook, noSideways, ok, openPanel, page, phone, rangeState, settled, showCard }) => {
    // (Synthwave draws the card with its own view: columns and a title of its own)
    const BARS = "#reportOutput .days .day, #reportOutput .sw-col";
    const TITLE = "#reportOutput .rep-days h3, #reportOutput .sw-days .sw-t";
    const WANT = { "7d": [7, 8, "day"], "30d": [30, 31, "day"], "90d": [13, 15, "week"], all: [26, 28, "week"] };
    const check = (where) => async (id) => {
      const what = `${where} ${id}`;
      const [lo, hi, unit] = WANT[id];
      try {
        await page.waitForFunction((n, sel) => document.querySelectorAll(sel).length >= n, { timeout: 15000 }, lo, BARS);
      } catch (e) {
        const seen = await page.evaluate((b, t) => ({ bars: document.querySelectorAll(b).length, title: document.querySelector(t)?.textContent, summary: document.getElementById("reportOutput").dataset.summary, chosen: document.querySelector("#reportRangeButtons select")?.value }), BARS, TITLE);
        throw new Error(`${what}: waited for ${lo} bars, the card shows ${JSON.stringify(seen)}`);
      }
      const bars = await page.$$eval(BARS, (b) => b.map((x) => ({ w: x.getBoundingClientRect().width, tip: x.dataset.tip || x.title })));
      ok(`${what}: ${bars.length} bars (${lo}-${hi})`, bars.length >= lo && bars.length <= hi);
      // (the first and last bar hold only the hours the window reaches: narrower by design, a sliver an hour or two in)
      const whole = Math.min(...bars.slice(1, -1).map((b) => b.w));
      ok(`${what}: the whole days' or weeks' bars are at least 2 px wide (${whole.toFixed(1)})`, whole >= 2);
      ok(`${what}: the title says ${unit}s (${await page.$eval(TITLE, (e) => e.textContent)})`, new RegExp(`Lost per ${unit}`).test(await page.$eval(TITLE, (e) => e.textContent)));
      if (unit === "week") ok(`${what}: a bar's tip names its week (${bars[1].tip})`, bars.every((b) => /^week of [A-Z][a-z]{2} \d{1,2} · /.test(b.tip)));
      ok(`${what}: nothing sticks out sideways`, await noSideways());
    };
    await showCard("report");
    eq("7 d to All", (await rangeState("#reportRangeButtons")).ids.join(), "7d,30d,90d,all");
    await atEveryRange("#reportRangeButtons", "#reportOutput", async (id) => { await check("desktop")(id); });
    await everyLook(async (look) => {
      await showCard("report");
      for (const id of ["30d", "all"]) {
        await chooseRange("#reportRangeButtons", id);
        await settled();
        await check(look)(id);
      }
    });
    await phone(async () => {
      await openPanel("report", "incidents");
      await atEveryRange("#reportRangeButtons", "#reportOutput", async (id) => { await check("phone")(id); });
    });
  }, { variants: ["longhistory"] }],
];
