// The target history dialog (opened from a hop row or a path node).
import { ALL_VARIANTS } from "../lib.mjs";

export const subject = "dialog:target-history";
export const variants = ["full"];

export const steps = [
  ["clicking a target row opens its history modal with data; the range control refetches", async ({ chooseRange, click, eq, ok, outDir, page, path, text, until }) => {
    await click("#hopMatrixBody tr:first-child");
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    ok("title", /Local Router/.test(await text("#targetHistTitle")));
    ok("a single target has no switcher", await page.$eval("#targetHistSwitch", (e) => e.hidden));
    const points = (id) => page.evaluate((i) => Chart.getChart(document.getElementById(i))?.data.datasets[0]?.data.length ?? 0, id);
    await until(() => (Chart.getChart(document.getElementById("targetHistLatency"))?.data.datasets[0]?.data.length ?? 0) > 20);
    ok("loss chart has the same points", (await points("targetHistLoss")) > 20);
    eq("1 h is chosen", await page.$eval("#targetHistRanges select", (s) => s.value), "1h");
    await chooseRange("#targetHistRanges", "24h");
    await until(() => document.querySelector("#targetHistRanges select").value === "24h");
    await until(() => /points, one per 10 min/.test(document.getElementById("targetHistStatus").textContent));
    await page.screenshot({ path: path.join(outDir, "target-history.png") });
    await click("[data-action=closeModal][data-arg=targetHistModal]");
    eq("closed", await page.$eval("#targetHistModal", (m) => m.classList.contains("active")), false);
  }, { variants: ALL_VARIANTS }],

  ["target history: where the hop's address was replaced, a marker on the charts and the strip says so (escaped)", async ({ click, eq, ok, page, text, until }) => {
    // the path's events for the router, answered by the page (the real ones come from the path editor's steps)
    await page.evaluate(() => {
      window.__realFetch = window.__realFetch || window.fetch;
      const at = Date.now() / 1000 - 1800;
      window.fetch = (url, opts) => (String(url).includes("/api/path/events")
        ? Promise.resolve(new Response(JSON.stringify([{ at, kind: "address_changed", target_id: "local_router", old: "<b>192.0.2.1</b>", new: "192.0.2.9", revision: 4 }]), { status: 200 }))
        : window.__realFetch(url, opts));
    });
    await click("#hopMatrixBody tr:first-child");
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active") && !document.getElementById("targetHistEvents").hidden);
    ok("listed under the charts, escaped", /^Address changed: <b>192\.0\.2\.1<\/b> → 192\.0\.2\.9/.test(await text("#targetHistEvents li")));
    eq("no markup let through", await page.$$eval("#targetHistEvents b", (b) => b.length), 0);
    eq("marked on both charts", await page.evaluate(() => ["targetHistLatency", "targetHistLoss"].map((i) => Chart.getChart(document.getElementById(i)).pgMarks.length).join()), "1,1");
    await until(() => document.querySelector("#targetHistStrip .tgt-mark"));
    ok("and on the 24 h strip, at its time", await page.$eval("#targetHistStrip .tgt-mark", (m) => /Address changed: <b>192/.test(m.title) && parseFloat(m.style.left) > 90));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await click("[data-action=closeModal][data-arg=targetHistModal]");
  }],

  ["target detail dialog: stats, latency chart with band, loss, 24 h strip; eight ranges; Live refreshes; Escape closes", async ({ chooseRange, click, count, eq, ok, page, rangeState, shots, showCard, until }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await click('#pathChain .node[data-node^="Gateway"]');
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    eq("eight ranges (180 days of probes are kept)", (await rangeState("#targetHistRanges")).ids.length, 8);
    eq("ranges", (await rangeState("#targetHistRanges")).words.join(","), "Live,Last 15 minutes,Last hour,Last 24 hours,Last 7 days,Last 30 days,Last 90 days,All · 180 d");
    await until(() => document.querySelectorAll("#targetHistStrip .strip i").length === 96);
    await until(() => /ms/.test(document.querySelectorAll("#targetHistStats .val")[1].textContent));
    eq("five figures", await count("#targetHistStats .stat"), 5);
    ok("a status badge", /Healthy|Degraded|Down|Busy|No data/.test(await page.$eval("#targetHistStats .val", (e) => e.textContent)));
    ok("the strip has an accessible name", /available over 24 hours/.test(await page.$eval("#targetHistStrip .strip", (e) => e.getAttribute("aria-label"))));
    eq("line first", await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets[0].id), "line");
    const bars = await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLoss")).data.datasets[0].data.map((p) => p.y));
    ok(`a bar only where probes were lost (${bars.filter((y) => y > 0).length} of ${bars.length})`, bars.length > 20 && !bars.includes(0));
    for (const [id, word] of [["live", /one per 2 s/], ["15m", /one per 10 s/], ["7d", /one per 15 min/], ["30d", /one per 1 h/], ["90d", /one per 3 h/], ["all", /one per (?!3 h)\d/]]) { // (All starts at the oldest stored probe: this seed holds a few days, so its bucket is not the 90 d one)
      await chooseRange("#targetHistRanges", id);
      await until((w) => new RegExp(w).test(document.getElementById("targetHistStatus").textContent), word.source);
      eq(`chosen ${id}`, await page.$eval("#targetHistRanges select", (s) => s.value), id);
    }
    await chooseRange("#targetHistRanges", "30d");
    await until(() => /one per 1 h/.test(document.getElementById("targetHistStatus").textContent));
    // 30 d asked for the last 30 days, ending now, in 1-hour buckets, and drew them
    const asked = await page.evaluate(() => {
      const url = new URL(performance.getEntriesByType("resource").filter((e) => e.name.includes("/api/history?")).pop().name);
      const q = url.searchParams;
      return { span: Number(q.get("end_time")) - Number(q.get("start_time")), bucket: q.get("bucket_seconds"), lag: Date.now() / 1000 - Number(q.get("end_time")) };
    });
    eq("the 30 d request spans 30 days", asked.span, 30 * 86400);
    eq("in 1 h buckets", asked.bucket, "3600");
    ok(`and ends now (${asked.lag.toFixed(0)} s ago)`, asked.lag >= 0 && asked.lag < 60);
    await chooseRange("#targetHistRanges", "live");
    await shots("target-detail");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("targetHistModal").classList.contains("active"));
  }],

  ["the Internet node opens its three servers together: All draws a line each and the median, the switcher shows one alone as a single target; keyboard and phone width", async ({ click, eq, layoutSettled, noSideScroll, ok, page, shots, showCard, text, until, waitCount }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 6);
    await click('#pathChain .node[data-node^="Internet"]');
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    eq("the title says what the node says", await text("#targetHistTitle"), "Internet · median of 3 public servers");
    // the number on the node is in the dialog: opened on Live, the node's own figure shown as it is now, the window said
    eq("a path node opens on Live", await page.$eval("#targetHistRanges select", (s) => s.value), "live");
    await until(() => {
      const v = document.querySelector('#pathChain .node[data-node^="Internet"] .node-val').textContent.replace(/ms$/, "");
      const now = document.getElementById("targetHistNow");
      return !now.hidden && now.textContent.startsWith(`On the path now: ${v} ms (the median of the servers' last 60 probes)`);
    });
    ok("the figures say their window", /last 5 min\.$/.test(await text("#targetHistNow")) && /Median, last 5 min/.test(await text("#targetHistStats")));
    const node = await page.$eval('#pathChain .node[data-node^="Internet"] .node-name', (e) => e.textContent);
    const names = await page.$$eval("#targetHistSwitch:not([hidden]) button", (bs) => bs.map((b) => b.textContent));
    eq("All, then every member as the node names it", names.join(", "), `All, ${node}`);
    await until(() => (Chart.getChart(document.getElementById("targetHistLatency"))?.data.datasets.length ?? 0) === 4);
    const sets = await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets.map((d) => ({ id: d.id, label: d.label, color: d.borderColor, width: d.borderWidth })));
    eq("a line per member, then the median", sets.map((d) => d.label).join(", "), `${node}, Median`);
    ok("the median is drawn distinctly", sets[3].id === "group" && sets[3].width > sets[0].width);
    const keys = await page.$$eval("#targetHistSwitch button .key", (ks) => ks.map((k) => k.style.getPropertyValue("--c")));
    eq("each member in its own color, the switcher's key the same", keys.join(), sets.slice(0, 3).map((d) => d.color).join());
    ok("three distinct colors", new Set(keys).size === 3);
    eq("loss per member", await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLoss")).data.datasets.length), 3);
    await until(() => document.querySelectorAll("#targetHistStrip .tgt-strips .strip").length === 3 || document.querySelectorAll("#targetHistStrip .tgt-strips > .tgt-sn").length === 3);
    ok("the figures say the group's rules", /\(median\)/.test(await text("#targetHistStats")));
    eq("All is pressed", await page.$eval('#targetHistSwitch button[data-arg="all"]', (b) => b.getAttribute("aria-pressed")), "true");
    await shots("internet-history");
    await page.setViewport({ width: 390, height: 844 });
    await layoutSettled();
    ok("no sideways scroll at 390 px", await noSideScroll("targetHistModal"));
    await page.setViewport({ width: 1400, height: 1000 });
    // a member alone, chosen with the keyboard: exactly the single-target view
    await page.focus('#targetHistSwitch button[data-arg="lp_public2"]');
    await page.keyboard.press("Enter");
    await until(() => /Second Example Resolver/.test(document.getElementById("targetHistTitle").textContent));
    await until(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets[0]?.id === "line");
    eq("its pressed button", await page.$eval('#targetHistSwitch button[data-arg="lp_public2"]', (b) => b.getAttribute("aria-pressed")), "true");
    await until(() => document.querySelectorAll("#targetHistStats .lbl")[1].textContent === "Median, last 5 min" && /^This target now: /.test(document.getElementById("targetHistNow").textContent));
    eq("the single view's figures, with their window", await page.$$eval("#targetHistStats .lbl", (ls) => ls.map((l) => l.textContent).join("|")),
      "Status, latest 15 min|Median, last 5 min|p90 peak, last 5 min|Loss, last 5 min|Available, 24 h");
    await until(() => document.querySelectorAll("#targetHistStrip > .strip").length === 1 || /No uptime record/.test(document.getElementById("targetHistStrip").textContent));
    await click('#targetHistSwitch button[data-arg="all"]');
    await until(() => document.getElementById("targetHistTitle").textContent === "Internet · median of 3 public servers");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("targetHistModal").classList.contains("active"));
    // ten targets and eight series colors: the two DNS lookups (ninth and tenth) still differ from each other, not gray
    const colors = [];
    for (const row of [9, 10]) {
      const name = await page.$eval(`#hopMatrixBody tr:nth-child(${row}) [data-f=name]`, (e) => e.textContent);
      ok(`row ${row} is a DNS lookup (${name})`, /DNS/i.test(name));
      await click(`#hopMatrixBody tr:nth-child(${row})`);
      await until((n) => document.getElementById("targetHistModal")?.classList.contains("active") && document.getElementById("targetHistTitle").textContent === n, name);
      // the color the dialog draws this target in is the one the page gives it (js/targets.js getTargetInfo)
      await until(() => /points|No samples/.test(document.getElementById("targetHistStatus").textContent));
      colors.push(await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets[0].borderColor));
      await click("[data-action=closeModal][data-arg=targetHistModal]");
      await until(() => !document.getElementById("targetHistModal").classList.contains("active"));
    }
    const gray = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--ink-3").trim());
    ok(`the DNS lookups have distinct series colors (${colors}, gray ${gray})`, colors[0] !== colors[1] && !colors.includes(gray));
  }, { variants: ["longpath"] }],

  ["a folded run of ISP hops opens its hops together; a single public server still opens alone", async ({ click, eq, ok, page, showCard, text, until }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await click('#pathChain .node[data-node^="Internet"]');
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    ok("one public server: its own history, no switcher", /Cloudflare/.test(await text("#targetHistTitle")) && await page.$eval("#targetHistSwitch", (e) => e.hidden));
    await click("[data-action=closeModal][data-arg=targetHistModal]");
    await page.setViewport({ width: 1440, height: 1000 });
    await page.evaluate(() => {
      window.__smokeMutate = (d) => {
        const gw = d.targets.find((t) => t.category === "gateway");
        const hops = Array.from({ length: 9 }, (_, k) => ({ ...gw, target_id: `x_isp${k}`, name: `Hop ${k + 3}: Example provider router ${k + 1}`, category: "isp",
          address: `198.51.100.${k + 1}`, last_ms: 5 + k, recent_latencies: Array(30).fill(5 + k), loss_pct: 0, loss_recent_pct: 0, status: "up" }));
        d.targets.splice(d.targets.indexOf(gw) + 1, 0, ...hops);
      };
    });
    await until(() => document.querySelector('#pathChain .node[data-node="ISP:run"]'));
    await click('#pathChain .node[data-node="ISP:run"]');
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    eq("the title says what the node says", await text("#targetHistTitle"), "ISP · 9 hops");
    const hops = Array.from({ length: 9 }, (_, k) => `ISP hop ${k + 1}`);
    eq("every hop in the switcher", await page.$$eval("#targetHistSwitch:not([hidden]) button", (bs) => bs.map((b) => b.textContent).join("|")), ["All", ...hops].join("|"));
    await until(() => (Chart.getChart(document.getElementById("targetHistLatency"))?.data.datasets.length ?? 0) === 10);
    const labels = await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets.map((d) => d.label));
    eq("a line per hop, then the node's figure", labels.join("|"), [...hops, "Last hop that answers"].join("|"));
    ok("the figures say the run's rules", /worst hop/.test(await text("#targetHistStats")));
    await click('#targetHistSwitch button[data-arg="x_isp4"]');
    await until(() => document.getElementById("targetHistTitle").textContent === "Hop 7: Example provider router 5");
    await until(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets[0]?.id === "line");
    await click("[data-action=closeModal][data-arg=targetHistModal]");
    await page.evaluate(() => { window.__smokeMutate = null; });
    await page.setViewport({ width: 1400, height: 1000 });
  }],

  ["every range draws the target's latency and loss (400-800 points from 7 d on, axis labels of the range's kind), on a desktop, a phone and in every theme", async ({ atEveryRange, axisOk, chartAxis, chooseRange, click, eq, everyLook, noSideScroll, ok, openPanel, phone, rangeState, settled, until }) => {
    const open = async (onPhone = false) => {
      if (onPhone) await openPanel("matrix", "live"); // (the hop table is on the Live tab, and a tap needs it on screen)
      await until(() => document.querySelectorAll("#hopMatrixBody tr [data-f=last]").length >= 3); // (the rows are drawn from the first message on)
      await click("#hopMatrixBody tr:first-child");
      await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    };
    const check = (where) => async (id, s) => {
      const what = `${where} ${id}`;
      ok(`${what}: the chart is drawn`, !!s);
      const long = ["7d", "30d", "90d", "all"].includes(id);
      ok(`${what}: ${s.sets[0].n} points (${long ? "300-820" : "at least 20"})`, long ? s.sets[0].n >= 300 && s.sets[0].n <= 820 : s.sets[0].n >= 20);
      axisOk(what, id, s);
      ok(`${what}: nothing sticks out sideways`, await noSideScroll("targetHistModal"));
    };
    await open();
    eq("eight ranges", (await rangeState("#targetHistRanges")).ids.join(), "live,15m,1h,24h,7d,30d,90d,all");
    await atEveryRange("#targetHistRanges", "#targetHistLatency", check("desktop"));
    await everyLook(async (look) => {
      for (const id of ["24h", "all"]) {
        await chooseRange("#targetHistRanges", id);
        await settled();
        await check(look)(id, await chartAxis("#targetHistLatency"));
      }
    });
    await click("[data-action=closeModal][data-arg=targetHistModal]");
    await phone(async () => {
      await open(true);
      await atEveryRange("#targetHistRanges", "#targetHistLatency", check("phone"));
      await click("[data-action=closeModal][data-arg=targetHistModal]");
    });
  }, { variants: ["longhistory"] }],

  ["All starts at the oldest stored probe when that is younger than the retention: the target history (42 days of data, then 400)", async ({ chartAxis, chooseRange, click, eq, forgetRequests, ok, page, rangeState, requestsTo, settled, until, youngData }) => {
    for (const { days, kind, age } of [{ days: 42, kind: "date", age: 42 }, { days: 400, kind: "month", age: 180 }]) { // (the probes' rollups are kept 180 days: the server answers no more)
      await youngData(days);
      await until(() => document.querySelectorAll("#hopMatrixBody tr [data-f=last]").length >= 3);
      await click("#hopMatrixBody tr:first-child");
      await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
      eq(`${days}: the retention is a year`, (await rangeState("#targetHistRanges")).words.at(-1), "All · 1 y");
      forgetRequests();
      await chooseRange("#targetHistRanges", "all");
      await settled();
      eq(`${days}: All asks the history once (${requestsTo(/^\/api\/history\?/).join(" ")})`, requestsTo(/^\/api\/history\?/).length, 1);
      const s = await chartAxis("#targetHistLatency");
      const x = await page.evaluate(() => { const c = Chart.getChart(document.getElementById("targetHistLatency")); return { min: c.scales.x.min / 1000, first: c.data.datasets[0].data[0].x / 1000, now: Date.now() / 1000 }; });
      const back = (x.now - x.first) / 86400;
      ok(`${days}: the first point is ${back.toFixed(1)} days back (about ${age}, not 365)`, Math.abs(back - age) < 1.5);
      ok(`${days}: the axis starts there (${((x.now - x.min) / 86400).toFixed(1)} days back)`, Math.abs((x.now - x.min) / 86400 - age) < 1.5);
      ok(`${days}: ${s.sets[0].n} points (400-800)`, s.sets[0].n >= 400 && s.sets[0].n <= 800);
      ok(`${days}: the labels are ${kind}s (${s.labels.join(" | ")})`, s.labels.length >= 2 && [s.labels[0], s.labels.at(-1)].every((l) => (kind === "date" ? /^[A-Z][a-z]{2} \d{1,2}$/ : /^[A-Z][a-z]{2} \d{4}$/).test(l)));
      ok(`${days}: no label twice in a row`, s.labels.every((l, i) => i === 0 || l !== s.labels[i - 1]));
      await click("[data-action=closeModal][data-arg=targetHistModal]");
    }
    await youngData(null);
  }, { variants: ["longhistory"] }],
];
