// The reliability report.

export const subject = "panel:report";
export const variants = ["full"];

export const steps = [
  ["reliability report renders and switches range", async ({ click, count, eq, ok, page, text, until }) => {
    await until(() => /^Last 7 days:/.test(document.getElementById("reportOutput").dataset.summary ?? ""));
    ok("headline", /\d+\.\d\d%/.test(await text("#reportOutput .rep-hero")));
    ok("hop rows", (await count("#reportOutput .rep-hops tbody tr")) >= 3);
    const hops = await page.$$eval("#reportOutput .rep-hops tbody td.lead .primary", (els) => els.slice(0, 3).map((e) => `${e.textContent}|${e.title}`).join(","));
    eq("hops as the rest of the page names them, the configured name on hover", hops,
      "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    ok("a bar per day with data", (await count("#reportOutput .days .day")) >= 1);
    ok("the bars are what was lost per day, with the worst day named", /Lost per day/i.test(await text("#reportOutput .rep-days")) && /Worst: |No probe lost/.test(await text("#reportOutput .rep-days .rep-note")));
    const causes = await page.$$eval("#reportOutput .causes li span:first-child", (els) => els.map((e) => e.textContent));
    ok(`causes in words, not classifier codes: ${causes}`, causes.length > 0 && causes.every((c) => !/_/.test(c) && !/^unknown outage$/.test(c)));
    await click('[data-action=loadReport][data-arg="30"]');
    await until(() => /^Last 30 days:/.test(document.getElementById("reportOutput").dataset.summary ?? ""));
    eq("active", await page.$eval("#reportRangeButtons button.active", (b) => b.dataset.arg), "30");
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
];
