// The latency panel: the live chart fed by the websocket, and the time-range buttons.

export const subject = "panel:latency";
export const variants = ["full"];

export const steps = [
  ["the theme button recolors charts that are already drawn: grid, axis and tick text follow the page", async ({ click, eq, page, setTheme, until }) => {
    const colors = () => page.evaluate(() => {
      const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
      const o = Chart.getChart(document.getElementById("latencyChart")).scales.y.options;
      const lost = Chart.getChart(document.getElementById("lossChart")).scales.y.options;
      return { grid: o.grid.color, ticks: o.ticks.color, lossGrid: lost.grid.color, line: css("--line"), ink: css("--ink-3") };
    });
    for (const theme of ["light", "dark", "light"]) {
      await setTheme(theme);
      const c = await colors();
      eq(`${theme}: latency grid`, c.grid, c.line);
      eq(`${theme}: loss lane grid`, c.lossGrid, c.line);
      eq(`${theme}: tick text`, c.ticks, c.ink);
    }
    await setTheme();
  }],

  ["the chart says \"1 ms resolution\" only where the backend times in whole milliseconds", async ({ eq, page, until, variant }) => {
    const coarse = variant === "longpath"; // serve.py's stand-in: windows-icmpapi, 1 ms; otherwise kernel timestamps
    if (coarse) await until(() => !document.getElementById("probeResolution").hidden);
    else await until(() => window.document.readyState === "complete"); // hidden from the start; nothing to wait for
    eq("resolution note shown", await page.$eval("#probeResolution", (n) => !n.hidden), coarse);
    if (coarse) eq("resolution note", await page.$eval("#probeResolution", (n) => n.textContent.trim()), "· 1 ms resolution");
    eq("the mode label is unchanged", await page.$eval("#chartModeLabel", (n) => n.textContent), "last 60 s · every probe");
  }, { variants: ["full", "longpath"] }],

  ["stored probes are grouped by probe round, not by whole second: a round whose place drifts across a half second leaves no gap", async ({ eq, moduleUrl, page }) => {
    const r = await page.evaluate(async (u) => {
      const { roundKeys } = await import(u);
      // 3 targets probed 25 ms apart, rounds 1 s apart drifting from .40 past .50 (as the live server's rounds drift),
      // and a round with its last probe late (an answer that took long)
      const stamps = [];
      for (let k = 0; k < 20; k++) [0, 0.025, 0.05].forEach((o, i) => stamps.push({ k, i, ts: 1000 + k + 0.4 + k * 0.01 + o + (k === 7 && i === 2 ? 0.12 : 0) }));
      const keys = roundKeys(stamps.map((s) => s.ts));
      const groups = new Map();
      stamps.forEach((s) => { const g = keys.get(s.ts); if (!groups.has(g)) groups.set(g, new Set()); groups.get(g).add(`${s.k}`); });
      const bySecond = new Map();
      stamps.forEach((s) => { const g = Math.round(s.ts); bySecond.set(g, (bySecond.get(g) || 0) + 1); });
      // a long list of targets (60, 25 ms apart: 1.5 s) still falls into rounds of under a second
      const dense = roundKeys(Array.from({ length: 60 }, (_, i) => 2000 + i * 0.025));
      return {
        rounds: groups.size,
        whole: [...groups.values()].every((g) => g.size === 1),
        each: [...groups.keys()].map((g) => stamps.filter((s) => keys.get(s.ts) === g).length).join(""),
        secondsTorn: [...bySecond.values()].some((n) => n !== 3),
        denseRounds: new Set(dense.values()).size
      };
    }, await moduleUrl("path.js"));
    eq("a group per round", r.rounds, 20);
    eq("no group mixes two rounds", r.whole, true);
    eq("every round has its 3 targets", r.each, "3".repeat(20));
    eq("(grouping by whole seconds would have torn a round: the case this guards)", r.secondsTorn, true);
    eq("60 targets over 1.5 s are two rounds", r.denseRounds, 2);
  }],

  ["back to Live: the rounds that came while the stored window loaded are drawn, so the window meets the feed without a gap", async ({ chooseRange, click, moduleUrl, ok, page, sleep, until }) => {
    // The stored window's answer is held back 3 s, as a slow answer is; the feed's messages of those seconds are noted.
    // 0.28.0 drew them on the view the answer then replaced: a gap in every line where the window met the feed.
    await page.evaluate(() => {
      const real = window.fetch;
      window.__heldMessages = [];
      window.__holding = false;
      window.__smokeMutate = (d) => { if (window.__holding && d && d.timestamp) window.__heldMessages.push(d.timestamp); };
      window.fetch = (u, o) => {
        if (!(String(u).includes("/api/history?") && !String(u).includes("bucket_seconds"))) return real(u, o);
        window.__holding = true;
        return real(u, o).then((res) => new Promise((r) => setTimeout(() => { window.__holding = false; r(res); }, 3000)));
      };
    });
    await chooseRange("#timeRangeButtons", "15m");
    await chooseRange("#timeRangeButtons", "live");
    await sleep(5000);
    const url = await moduleUrl("../panels/latency/charts.js");
    const r = await page.evaluate(async (u) => {
      const { view } = await import(u);
      const held = window.__heldMessages;
      window.__smokeMutate = null;
      const drawn = (t) => view.times.some((x) => Math.abs(x - t) < 0.01);
      return { held: held.length, missing: held.filter((t) => !drawn(t)).length, points: view.times.length };
    }, url);
    ok(`messages came while the window loaded (${r.held})`, r.held >= 2);
    ok(`each of them is drawn after the stored rounds (${r.missing} of ${r.held} missing; ${r.points} points)`, r.missing === 0);
  }],

  ["a stored window's torn edges are left out: a second some path targets are not stored in yet is no gap in their lines", async ({ eq, moduleUrl, page }) => {
    const r = await page.evaluate(async (u) => {
      const { wholeEdges } = await import(u);
      const all = { router: 1, gateway: 1, internet: 1 };
      const rows = (missing) => Object.fromEntries(Object.entries(all).filter(([id]) => !missing.includes(id)));
      // seconds 1-10: the oldest lacks the router (stored after the window began), the two newest the router and the
      // gateway (their round was not stored yet); a dns lookup is never a path target
      const at = { 1: rows(["router"]), 9: rows(["router", "gateway"]), 10: rows(["router", "gateway"]) };
      const rowAt = (ts) => at[ts] || all;
      const times = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
      const ids = ["router", "gateway", "internet"];
      return {
        torn: wholeEdges(times, rowAt, ids).join(),
        whole: wholeEdges(times, () => all, ids) === times,
        middle: wholeEdges(times, (ts) => (ts === 5 ? rows(["router"]) : all), ids).join(),
        capped: wholeEdges(times, (ts) => (ts > 2 ? rows(["gateway"]) : all), ids, 3).join(),
        none: wholeEdges(times, rowAt, []).length
      };
    }, await moduleUrl("path.js"));
    eq("the torn oldest and two newest seconds are left out", r.torn, "2,3,4,5,6,7,8");
    eq("a whole window is kept as it is", r.whole, true);
    eq("a gap in the middle is kept (a real missing row there is said as a gap)", r.middle, "1,2,3,4,5,6,7,8,9,10");
    eq("at most `edge` times go at each end (a target that stopped long ago does not empty the chart)", r.capped, "1,2,3,4,5,6,7");
    eq("without path targets nothing is cut", r.none, 10);
  }],

  ["live chart is fed by the websocket", async ({ chartLabels, eq, hasChart, ok, page, text, until }) => {
    await hasChart("latencyChart");
    await hasChart("lossChart");
    // a new newest point, not a longer chart: the live view keeps at most 60 points, so once a page has been open a
    // minute a new probe pushes the oldest out and the count stays at 60
    const newest = () => page.evaluate((i) => String(Chart.getChart(document.getElementById(i)).data.labels.at(-1) ?? ""), "latencyChart");
    const before = await newest();
    ok("the live chart has points", (await chartLabels("latencyChart")) > 0);
    await until((i, last) => { const l = Chart.getChart(document.getElementById(i)).data.labels; return l.length > 0 && String(l.at(-1)) !== last; }, "latencyChart", before);
    eq("live label", await text("#chartModeLabel"), "last 60 s · every probe");
    const sets = await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.map((d) => d.id).join());
    ok(`three path series and the WAN overlay (${sets})`, /path_router,path_line,path_internet/.test(sets));
    ok("legend", /Internet/.test(await text("#latencyLegend")));
  }],

  ["the page's socket sends deltas: the panels still get whole windows, equal to a full socket's, also across a gap", async ({ eq, moduleUrl, ok, page, until }) => {
    await until(() => window.__smokeSockets.some((s) => s.readyState === 1));
    const busUrl = await moduleUrl("bus.js");
    // the page's own socket (live.js, ?delta=1): the frames it receives, the requests it sends, and the windows the bus gets
    // from it; a second, plain socket of the page's origin gets the same messages in full
    const r = await page.evaluate(async (bu) => {
      const { bus } = await import(bu);
      const own = window.__smokeSockets.filter((s) => s.readyState === 1).at(-1);
      const raw = [];
      const asked = [];
      const sendReal = own.send.bind(own);
      own.send = (t) => { asked.push(t); return sendReal(t); };
      own.addEventListener("message", (e) => raw.push(JSON.parse(e.data)));
      const full = new Map(); // timestamp -> {target id: window} as the plain socket got it
      const plain = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
      plain.addEventListener("message", (e) => { const d = JSON.parse(e.data); full.set(d.timestamp, Object.fromEntries(d.targets.map((t) => [t.target_id, [t.sent, t.recent_latencies]]))); });
      const seen = new Map(); // timestamp -> windows the panels would read (bus "message")
      bus.subscribe("message", (d) => seen.set(d.timestamp, Object.fromEntries(d.targets.map((t) => [t.target_id, [t.sent, t.recent_latencies]]))));
      const wait = (ms) => new Promise((res) => setTimeout(res, ms));
      await wait(5000);
      const calm = raw.length;
      // a gap: one message arrives with the probe counter further on than its tail explains (as if messages were lost)
      let broken = null;
      window.__smokeMutate = (d) => { if (broken === null) { broken = d.timestamp; d.targets.forEach((t) => { t.sent += 7; }); } };
      await wait(6000);
      window.__smokeMutate = null;
      plain.close();
      const compared = [...seen].filter(([ts]) => full.has(ts));
      const same = compared.filter(([ts, w]) => JSON.stringify(w) === JSON.stringify(full.get(ts))).length;
      return {
        calmFrames: raw.slice(0, calm).length,
        deltaFrames: raw.filter((d) => d.targets.every((t) => t.latency_tail && !t.recent_latencies)).length,
        fullAfterAsk: raw.filter((d) => d.targets.every((t) => t.recent_latencies && !t.latency_tail)).length,
        leaked: [...seen.values()].some((w) => Object.values(w).some(([, x]) => !Array.isArray(x))),
        asked, broken, compared: compared.length, same,
        windowLens: [...new Set(Object.values([...seen.values()].at(-1)).map(([, x]) => x.length))]
      };
    }, busUrl);
    ok(`the page's socket sent tails, not windows (${r.deltaFrames} of ${r.calmFrames} frames before the gap)`, r.calmFrames >= 3 && r.deltaFrames >= r.calmFrames);
    ok("the panels never see a delta field and always a whole window", !r.leaked && r.windowLens.every((n) => n >= 1));
    eq("the page asked for a full message once", JSON.stringify(r.asked), '["{\\"resync\\":true}"]');
    ok("and the server answered with whole windows", r.fullAfterAsk >= 1);
    ok("a message that did not follow was met (the counter jumped)", r.broken !== null);
    ok(`every message the panels got (counters and windows) equals the full socket's (${r.same} of ${r.compared} compared)`, r.compared >= 6 && r.same === r.compared);
  }],

  ["a view model's tooltips read the data it was made from, not the live chart's (a fixture's tooltips show no live data)", async ({ eq, moduleUrl, ok, page }) => {
    const said = await page.evaluate(async (u) => {
      const { present } = await import(u);
      const all = [
        { id: "fx_router", name: "Example router", category: "local", address: "192.168.88.1", enabled: true, color: "#3987e5" },
        { id: "fx_a", name: "Example resolver A", category: "public", address: "192.0.2.53", enabled: true, color: "#3ac0a0" },
        { id: "fx_b", name: "Example resolver B", category: "public", address: "192.0.2.54", enabled: true, color: "#c05ac0" }];
      const data = { times: [1767225600, 1767225601], series: { path_router: [1, 1], path_internet: [14, 14.5] },
        byTarget: { fx_router: [1, 1], fx_a: [13.5, 14.5], fx_b: [14.5, null] }, lo: [null, null], hi: [null, null], loss: [0, 50],
        lossBy: [null, "fx_b"], wan: [null, null] };
      const vm = present({ data, mode: "path", all, live: true, quiet: false, windows: [] });
      return { members: vm.memberLines("path_internet", 1).map((l) => l.split(": ").pop()), loss: vm.lossLabel(1, 50) };
    }, await moduleUrl("../panels/latency/charts.js"));
    eq("each member's value at that moment, from the view model's data", said.members.join(" | "), "14.5 ms | no reply");
    ok(`the loss lane names the data's worst target (${said.loss})`, / at Internet 192\.0\.2\.54$/.test(said.loss));
  }],

  ["an empty chart says Not probing while this host cannot send pings, and \"every probe was lost\" otherwise", async ({ layoutMounted, moduleUrl, ok, page, text, until, waitText }) => {
    const url = await moduleUrl("../panels/latency/charts.js");
    const noReplies = (flag) => page.evaluate(async (u, f) => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => { t.probing_unavailable = f; t.last_ms = null; t.recent_latencies = []; });
      const { view } = await import(u);
      Object.assign(view, { series: {}, byTarget: {}, lo: [], hi: [], wan: [] }); // nothing but probes without a reply from here on
    }, url, flag);
    await noReplies(true);
    await until(() => /^Not probing: pings are not allowed on this computer/.test(document.getElementById("latencyNote").textContent));
    ok("it does not suggest an outage", !/lost/.test(await text("#latencyNote")));
    await noReplies(false);
    await until(() => /^No replies in this time: every probe was lost/.test(document.getElementById("latencyNote").textContent));
    await page.evaluate(() => { window.__smokeMutate = null; });
    await page.reload({ waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    await layoutMounted();
  }],

  ["a theme's own latency view fills the card's height too (no blank band under its charts)", async ({ ok, page, pickLook, until }) => {
    const views = await page.evaluate(() => JSON.parse(document.documentElement.dataset.looks || "[]").filter((t) => t.views && t.views.latency).map((t) => t.id));
    ok(`themes with their own latency view: ${views}`, views.length >= 1);
    for (const look of views) {
      await pickLook(look);
      await until((l) => document.querySelector("[data-panel=latency] .panel-b").dataset.view === l && document.querySelector("[data-panel=latency] .lat-loss"), look);
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
      const r = await page.evaluate(() => {
        const body = document.querySelector("[data-panel=latency] .panel-b").getBoundingClientRect();
        const lane = document.querySelector("[data-panel=latency] .lat-loss").getBoundingClientRect();
        const row = Math.max(...[...document.querySelectorAll("[data-panel] .panel")].filter((p) => Math.abs(p.getBoundingClientRect().top - document.querySelector("[data-panel=latency] .panel").getBoundingClientRect().top) < 4).map((p) => p.getBoundingClientRect().height));
        return { gap: Math.round(body.bottom - lane.bottom), card: Math.round(document.querySelector("[data-panel=latency] .panel").getBoundingClientRect().height), row: Math.round(row) };
      });
      ok(`${look}: blank space under the loss lane ${r.gap}px (card ${r.card}px, tallest in its row ${r.row}px)`, r.gap <= 40);
    }
    await pickLook("");
  }],

  ["time ranges switch the main chart: Live, 15 m, 1 h, 24 h, 7 d, 30 d, 90 d, All, each a rolling window ending now", async ({ chooseRange, eq, ok, page, rangeState, until, waitText }) => {
    eq("the card offers the ranges in this order", (await rangeState("#timeRangeButtons")).words.join(","), "Live,Last 15 minutes,Last hour,Last 24 hours,Last 7 days,Last 30 days,Last 90 days,All · 180 d");
    // the range, the words over the chart, the bucket the request names (7 d and 30 d keep about 700 points a line; past 30 d the
    // width is a nice step that gives at most 800)
    for (const [id, arg, label, bucket] of [["1h", "3600", "last 1 h · avg per 10 s", "10"], ["15m", "900", "last 15 min · avg per 2 s", "2"], ["24h", "86400", "last 24 h · avg per 1 min", "60"],
      ["7d", "604800", "last 7 d · avg per 15 min", "900"], ["30d", "2592000", "last 30 d · avg per 1 h", "3600"],
      ["90d", "7776000", "last 90 d · avg per 3 h", "10800"], ["all", "15552000", "all 180 d · avg per 6 h", "21600"]]) {
      await chooseRange("#timeRangeButtons", id);
      await waitText("#chartModeLabel", new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`));
      await until((i) => Chart.getChart(document.getElementById(i)).data.datasets.length >= 3, "latencyChart");
      eq("the range is chosen", (await rangeState("#timeRangeButtons")).value, id);
      const lastAsked = () => {
        const q = new URL(performance.getEntriesByType("resource").filter((e) => e.name.includes("/api/history?") && e.name.includes("bucket_seconds")).pop().name).searchParams;
        return { span: Number(q.get("end_time")) - Number(q.get("start_time")), bucket: q.get("bucket_seconds"), lag: Date.now() / 1000 - Number(q.get("end_time")) };
      };
      await until((s) => { try { const q = new URL(performance.getEntriesByType("resource").filter((e) => e.name.includes("/api/history?") && e.name.includes("bucket_seconds")).pop().name).searchParams; return Number(q.get("end_time")) - Number(q.get("start_time")) === s; } catch { return false; } }, Number(arg));
      const asked = await page.evaluate(lastAsked);
      eq(`${label}: the request spans the range`, asked.span, Number(arg));
      eq(`${label}: in buckets of`, asked.bucket, bucket);
      ok(`${label}: it ends now (${asked.lag.toFixed(0)} s ago)`, asked.lag >= 0 && asked.lag < 60);
    }
    // (All draws what is stored: this seed holds a few days, so its axis is not months; the longhistory variant checks months over 90 days)
    ok("the axis labels follow the span drawn, not the range's name: no months for a few days of data", !/^[A-Z][a-z]{2} \d{4}$/.test(await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.labels[0])));
    await chooseRange("#timeRangeButtons", "30d");
    await waitText("#chartModeLabel", /^last 30 d /);
    await until(() => (Chart.getChart(document.getElementById("latencyChart")).data.labels[0] ?? "") !== ""); // (the answer is drawn)
    // (the axis follows the span drawn, not the range's name: this seed holds a few days, so a 30 d chart has times, not dates
    // alone; the longhistory variant has 30 days of data and checks that dates come then)
    ok("the 30 d axis is of the span drawn (not months)", !/^[A-Z][a-z]{2} \d{4}$/.test(await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.labels[0])));
    await chooseRange("#timeRangeButtons", "live");
    await waitText("#chartModeLabel", /^last 60 s · every probe$/);
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.length >= 3);
  }],

  ["a range whose history cannot be loaded empties the chart and says so (it never draws the previous range under the new one's name)", async ({ chooseRange, eq, page, until, waitText }) => {
    await chooseRange("#timeRangeButtons", "24h");
    await waitText("#chartModeLabel", /^last 24 h /);
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.labels.length > 0);
    // the stored history's next answer fails (in the page, not on the server: the step needs the 24 h chart drawn first)
    await page.evaluate(() => {
      window.__realFetch = window.fetch;
      window.fetch = (url, opts) => (String(url).includes("/api/history?") && String(url).includes("bucket_seconds")
        ? Promise.resolve(new Response("{}", { status: 503 })) : window.__realFetch(url, opts));
    });
    try {
      await chooseRange("#timeRangeButtons", "7d");
      await waitText("#chartModeLabel", /^last 7 d · avg per 15 min · could not load the history$/);
      await until(() => Chart.getChart(document.getElementById("latencyChart")).data.labels.length === 0);
      eq("no line keeps the 24 h points", await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.reduce((n, d) => n + d.data.length, 0)), 0);
    } finally {
      await page.evaluate(() => { window.fetch = window.__realFetch; });
    }
    await chooseRange("#timeRangeButtons", "7d");
    await waitText("#chartModeLabel", /^last 7 d · avg per 15 min$/);
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.labels.length > 0);
    await chooseRange("#timeRangeButtons", "live");
    await waitText("#chartModeLabel", /^last 60 s · every probe$/);
  }],

  ["the latency chart's time labels never run into each other, on a phone in every theme (a monospaced theme's labels are wider)", async ({ chooseRange, everyLook, ok, page, phone, settled, showCard, sleep, until }) => {
    // the gap between neighbouring labels as drawn: each label is centered on its tick, as wide as the theme's font makes it
    const gaps = () => page.evaluate(() => {
      const c = Chart.getChart(document.querySelector("[data-panel=latency] .lat-main canvas")); // (a theme's own view has no #latencyChart)
      const x = c.scales.x;
      const ctx = document.createElement("canvas").getContext("2d");
      ctx.font = Chart.helpers.toFont(x.options.ticks.font).string;
      const boxes = x.ticks.map((t, i) => { const at = x.getPixelForTick(i); const w = ctx.measureText(String(t.label)).width; return [at - w / 2, at + w / 2, t.label]; });
      const out = [];
      for (let i = 1; i < boxes.length; i++) out.push({ gap: Math.round(boxes[i][0] - boxes[i - 1][1]), between: `${boxes[i - 1][2]} | ${boxes[i][2]}` });
      return { n: boxes.length, out };
    });
    await phone(async () => {
      // (2 calls of /api/history a theme, at most one theme every 5 s: well under the route's 60 a minute, which the slice's other steps use too)
      await everyLook(async (look) => {
        const began = Date.now();
        await showCard("latency");
        for (const id of ["live", "1h"]) { // (clock labels with seconds, the widest; the longhistory step checks the dates' count)
          await chooseRange("#timeRangeButtons", id);
          await settled();
          await until((r) => r && Chart.getChart(document.querySelector("[data-panel=latency] .lat-main canvas"))?.scales.x.ticks.length > 1, `${look} ${id}`);
          const g = await gaps();
          const worst = g.out.reduce((m, o) => (o.gap < m.gap ? o : m), { gap: Infinity, between: "" });
          ok(`${look} ${id}: ${g.n} labels, the closest ${worst.gap} px apart (${worst.between})`, worst.gap >= 4);
        }
        await sleep(Math.max(0, 5000 - (Date.now() - began)));
      });
    });
  }],

  ["the legend says what each line is, the chart fills its card, the loss lane says when it is empty", async ({ eq, ok, page, text, until }) => {
    await until(() => /Router/.test(document.getElementById("latencyLegend").textContent));
    const legend = await text("#latencyLegend");
    ok(`Router and Gateway name their target: ${legend}`, /Router\s*Hop 1: Local Router \(LAN\)/.test(legend) && /Gateway\s*Hop 2: ISP Modem \/ Gateway/.test(legend));
    ok(`Internet names its one public target: ${legend}`, /Internet\s*Public: Cloudflare Anycast/.test(legend));
    ok("a note says what the path view leaves out", (await text("#latencyNote")).length > 20);
    // no blank band under the charts: the plot and the loss lane reach the bottom of the card body
    const gap = await page.evaluate(() => {
      const body = document.querySelector("[data-panel=latency] .panel-b").getBoundingClientRect();
      const lane = document.querySelector("[data-panel=latency] .lat-loss").getBoundingClientRect();
      return Math.round(body.bottom - lane.bottom);
    });
    ok(`blank space under the loss lane: ${gap}px`, gap <= 24);
    // the lane's plot area lines up with the chart's
    const [a, b] = await page.evaluate(() => ["latencyChart", "lossChart"].map((id) => { const c = Chart.getChart(document.getElementById(id)); return [Math.round(c.chartArea.left), Math.round(c.chartArea.right)]; }));
    ok(`loss lane under the chart's plot area: ${a} vs ${b}`, Math.abs(a[0] - b[0]) <= 2 && Math.abs(a[1] - b[1]) <= 2);
    const tip = await page.evaluate(() => Chart.getChart(document.getElementById("lossChart")).options.plugins.tooltip.callbacks.label({ dataIndex: 0, parsed: { y: 20 } }));
    eq("the loss tooltip names its window", tip, " Lost 20% of the last 10 probes");
  }],

  ["Each target draws one line per pinged target in its own color and the legend names them; Path brings the three back", async ({ click, eq, ok, page, text, until }) => {
    await click('#latencyViewButtons button[data-arg="targets"]');
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.some((d) => String(d.id).startsWith("t:")));
    const ids = await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.map((d) => d.id).filter((i) => i.startsWith("t:")).join());
    eq("one line per ping target, DNS lookups left out", ids, "t:local_router,t:isp_gateway,t:cloudflare_dns");
    eq("pressed", await page.$eval('#latencyViewButtons button[data-arg="targets"]', (b) => b.getAttribute("aria-pressed")), "true");
    ok("legend uses the short names", /Router.*Gateway.*Internet/.test(await text("#latencyLegend")));
    await click('#latencyViewButtons button[data-arg="path"]');
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.some((d) => d.id === "path_internet"));
  }],

  ["long path: Internet is the median of three named servers, the tooltip gives each, Each target shows the ISP hops", async ({ click, eq, ok, page, text, until }) => {
    await until(() => /median of/.test(document.getElementById("latencyLegend")?.textContent ?? ""));
    const item = await page.$$eval("#latencyLegend .lg", (els) => els.map((e) => ({ text: e.textContent, title: e.title })).find((e) => e.text.startsWith("Internet")));
    ok(`the legend names the three: ${item.text}`, /median of 3|median of .+, .+, .+/.test(item.text));
    ok(`the tooltip of the item has every configured name: ${item.title}`, ["Example Public Resolver", "Second Example Resolver", "Third Example Resolver"].every((n) => item.title.includes(n)));
    ok("the note sends to the ISP hops", /3 ISP hops/.test(await text("#latencyNote")));
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.labels.length > 2);
    const lines = await page.evaluate(() => {
      const c = Chart.getChart(document.getElementById("latencyChart"));
      return c.options.plugins.tooltip.callbacks.afterLabel({ dataset: { id: "path_internet" }, dataIndex: c.data.labels.length - 1 });
    });
    eq(`one tooltip line per member: ${JSON.stringify(lines)}`, lines.length, 3);
    ok(`members in ms: ${lines}`, lines.every((l) => /Internet \d: \d+(\.\d)? ms$|no reply$/.test(l.trim())));
    await click('#latencyViewButtons button[data-arg="targets"]');
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.filter((d) => String(d.id).startsWith("t:")).length === 8);
    ok("ISP hops in the legend", /ISP hop 1.*ISP hop 2.*ISP hop 3/.test(await text("#latencyLegend")));
  }, { variants: ["longpath"] }],

  ["the Internet line covers the IPv4 servers only; the IPv6 group is a line of its own, as on Path health", async ({ click, eq, ok, page, stubFetch, until }) => {
    const live = await page.evaluate(async () => (await (await fetch("/api/targets")).json()));
    const publics = live.filter((t) => t.category === "public");
    const v6 = publics[publics.length - 1].target_id;
    const marked = live.map((t) => (t.target_id === v6 ? { ...t, group: "internet_v6", address: "2001:db8::53" } : { ...t, group: t.group || "" }));
    await stubFetch({ "GET /api/targets": { body: marked } });
    try {
      await click('#latencyViewButtons button[data-arg="path"]');
      await until(() => /Internet \(IPv6\)/.test(document.getElementById("latencyLegend")?.textContent ?? ""));
      const items = await page.$$eval("#latencyLegend .lg", (els) => els.map((e) => e.textContent));
      const v4 = items.find((t) => /^Internet(?! \(IPv6\))/.test(t));
      ok(`Internet is the median of the two IPv4 servers: ${v4}`, v4 && /median of .+, .+/.test(v4) && !/median of .+, .+, .+/.test(v4));
      const sets = await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.map((d) => d.id).join());
      ok(`a separate IPv6 series (${sets})`, /path_internet,/.test(sets) && /path_internet_v6/.test(sets));
      const lines = await page.evaluate(() => {
        const c = Chart.getChart(document.getElementById("latencyChart"));
        const i = c.data.labels.length - 1;
        return ["path_internet", "path_internet_v6"].map((id) => c.options.plugins.tooltip.callbacks.afterLabel({ dataset: { id }, dataIndex: i }).length);
      });
      eq("tooltip members: two IPv4 servers, and the IPv6 one alone", JSON.stringify(lines), JSON.stringify([2, 0]));
      // the IPv6 line is told from the Internet line by its dashes as well as its color (amber beside orange-red is a weak difference)
      const dashes = await page.evaluate(() => Object.fromEntries(Chart.getChart(document.getElementById("latencyChart")).data.datasets.filter((d) => /^path_/.test(d.id)).map((d) => [d.id, JSON.stringify(d.borderDash ?? null)])));
      eq("only the IPv6 line is dashed", JSON.stringify(dashes), JSON.stringify({ path_router: "null", path_line: "null", path_internet: "null", path_internet_v6: "[6,4]" }));
      eq("its legend key is dashed too", await page.$$eval("#latencyLegend .lg .key.line", (ks) => ks.map((k) => k.classList.contains("dash")).join()), "false,false,false,true");
    } finally {
      await stubFetch(null);
    }
  }, { variants: ["longpath"] }],

  ["the chart labels are not rebuilt with toLocaleTimeString for every point on every message", async ({ eq, ok, page, until }) => {
    await until(() => window.document.readyState === "complete");
    const calls = await page.evaluate(() => new Promise((resolve) => {
      const native = Date.prototype.toLocaleTimeString;
      let n = 0;
      Date.prototype.toLocaleTimeString = function (...a) { n++; return native.apply(this, a); };
      setTimeout(() => { Date.prototype.toLocaleTimeString = native; resolve(n); }, 3500); // three or four messages, 60 points each
    }));
    ok(`toLocaleTimeString calls in 3.5 s: ${calls} (one per point per message would be 180 or more)`, calls < 40);
  }],

  // ---- the `longhistory` variant: 200 days of made-up history (tests/test_ui_smoke.py seed_long_history) ----
  ["every range draws the latency chart (400-800 points from 7 d on, axis labels of the range's kind), on a desktop, a phone and in every theme", async ({ atEveryRange, axisOk, chartAxis, chooseRange, eq, everyLook, noSideways, ok, phone, rangeState, settled, showCard }) => {
    const CHART = "[data-panel=latency] .lat-main canvas";
    const check = (where) => async (id, s) => {
      const what = `${where} ${id}`;
      ok(`${what}: the chart is drawn`, !!s);
      const long = ["7d", "30d", "90d", "all"].includes(id);
      ok(`${what}: ${s.count} points (${long ? "300-820" : "at least 40"})`, long ? s.count >= 300 && s.count <= 820 : s.count >= 40);
      ok(`${what}: the first line has its points (${s.sets[0].n} of ${s.count})`, s.sets[0].n >= s.count * 0.5);
      axisOk(what, id, s);
      ok(`${what}: no sideways scroll`, await noSideways());
    };
    eq("the ranges are Live, 15 m, 1 h, 24 h, 7 d, 30 d, 90 d and All", (await rangeState("#timeRangeButtons")).ids.join(), "live,15m,1h,24h,7d,30d,90d,all");
    await atEveryRange("#timeRangeButtons", CHART, check("desktop"), { skip: ["live"] });
    await phone(async () => {
      await showCard("latency");
      await atEveryRange("#timeRangeButtons", CHART, async (id, s) => {
        await check("phone")(id, s);
        ok(`phone ${id}: at most 3 time labels fit (${s.labels.length})`, s.labels.length <= 4);
      }, { skip: ["live"] });
    });
    await everyLook(async (look) => {
      await showCard("latency");
      for (const id of ["24h", "7d", "all"]) {
        await chooseRange("#timeRangeButtons", id);
        await settled();
        await check(look)(id, await chartAxis(CHART));
      }
    });
  }, { variants: ["longhistory"] }],

  ["All starts at the oldest stored probe when that is younger than the retention: the latency chart (42 days of data, then 400), dates then months, a bucket that suits the span", async ({ chartAxis, chooseRange, eq, ok, page, rangeState, settled, showCard, youngData }) => {
    const CHART = "[data-panel=latency] .lat-main canvas";
    await showCard("latency");
    for (const { days, kind } of [{ days: 42, kind: "date" }, { days: 400, kind: "month" }]) {
      await youngData(days);
      eq(`${days}: the retention is a year`, (await rangeState("#timeRangeButtons")).words.at(-1), "All · 1 y");
      await chooseRange("#timeRangeButtons", "all");
      await settled();
      const s = await chartAxis(CHART);
      ok(`${days}: ${s.count} points (400-800; 365 days in 6 h buckets would be 1460, 42 of them only 168)`, s.count >= 400 && s.count <= 800);
      ok(`${days}: the labels are ${kind}s (${s.labels.join(" | ")})`, s.labels.length >= 2 && [s.labels[0], s.labels.at(-1)].every((l) => (kind === "date" ? /^[A-Z][a-z]{2} \d{1,2}$/ : /^[A-Z][a-z]{2} \d{4}$/).test(l)));
      ok(`${days}: no label twice in a row`, s.labels.every((l, i) => i === 0 || l !== s.labels[i - 1]));
      const words = await page.$eval("#chartModeLabel", (e) => e.textContent);
      ok(`${days}: the label says where it starts (${words})`, /^all stored, since [A-Z][a-z]{2} \d{1,2}/.test(words));
    }
    await youngData(null);
  }, { variants: ["longhistory"] }],
];
