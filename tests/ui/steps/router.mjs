// The router card, its History dialog and the Local network dialog (clients, topology). The Local network steps are a chain:
// each relies on the page state the one before left (sort order, range, chart mode), which is why they stay in this order.

export const subject = "panel:router";
export const variants = ["full"];

// The Local network dialog's topology, as the figures the steps compare (an SVG drawing: read from the DOM).
const topo = (page) => page.evaluate(() => {
  const t = (g, c) => g.querySelector(c).textContent;
  const main = [...document.querySelectorAll("#topologyBody .topo-wide .tn")].filter((g) => !g.closest(".tgo"));
  return {
    kinds: main.map((g) => t(g, ".tk")), names: main.map((g) => t(g, ".tname")), subs: main.map((g) => t(g, ".tsub")),
    leaves: [...document.querySelectorAll("#topologyBody .topo-wide .tgo")].map((e) => `${t(e, ".tn .tname")}: ${t(e, ".tc .tname")}`),
    classes: main.map((g) => g.getAttribute("class")),
    summary: document.getElementById("topologySummary").textContent
  };
});

export const steps = [
  ["two routers: one card each, with their own name, readings, history and setup", async ({ ROUTER, click, dialogKeys, eq, ok, openLayout, outDir, page, path, text, until, waitCount, waitText }) => {
    await waitCount("[data-panel=router] .panel", 2);
    await dialogKeys("[data-panel=router] .panel[data-device=router2] [data-action=openRouterHistory]", "routerHistModal");
    const titles = await page.$$eval("[data-panel=router] .panel .panel-t", (els) => els.map((e) => e.textContent).join("|"));
    eq("router cards in config order", titles, "Example Router|Second AP");
    const second = "[data-panel=router] .panel[data-device=router2]";
    await waitText(`${second} [data-f=rx] [data-f=v]`, /^5\.2$/);
    await waitText(`${ROUTER} [data-f=rx] [data-f=v]`, /^123$/);
    await click(`${second} [data-action=openRouterHistory]`);
    await waitText("#routerHistTitle", /^Second AP history$/);
    await until(() => /averages per|No router samples/.test(document.getElementById("routerHistStatus").textContent));
    await click("[data-action=closeModal][data-arg=routerHistModal]");
    await click(`${second} [data-action=openDeviceSetup]`);
    await waitText("#setupTitle", /^Set up Second AP$/);
    eq("its own address", await page.$eval("#setupAddress", (i) => i.value), "http://192.168.97.1");
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
    await openLayout();
    ok("the layout keeps one row for both", /2 cards/.test(await text("#layoutList")));
    await click("[data-action=closeModal][data-arg=layoutModal]");
    await page.screenshot({ path: path.join(outDir, "two-routers.png"), fullPage: true });
  }, { variants: ["tworouters"] }],

  ["router card: title, readings and meters with the server's grades", async ({ ROUTER, eq, page, text, until, waitText }) => {
    eq("router title", await text(`${ROUTER} [data-f=title]`), "Example Router");
    await waitText(`${ROUTER} [data-f=rx] [data-f=v]`, /^123$/);
    eq("memory meter has the warn grade", await page.$eval(`${ROUTER} [data-f=mem]`, (e) => e.style.getPropertyValue("--mc")), "var(--warn)");
    eq("cpu", await text(`${ROUTER} [data-f=cpu] [data-f=v]`), "23");
    await until((s) => document.querySelector(s)?.innerHTML.includes("<svg"), `${ROUTER} [data-f=wanChart]`);
  }],

  ["router card: uptime and its restart, CPU temperature, WAN link and the recent events; a link below the usual or down is said in words", async ({ ROUTER, count, eq, ok, page, text, until, waitText }) => {
    const sys = `${ROUTER} [data-f=sys]`;
    await waitText(`${sys} [data-item=uptime] [data-v=value]`, /^2h 0m$/);
    ok("the restart, from the device's events", /^restarted \d+h \d+m ago$/.test(await text(`${sys} [data-item=uptime] [data-v=sub]`)));
    eq("the CPU temperature", await text(`${sys} [data-item=temp] [data-v=value]`), "64 °C");
    eq("the WAN link, in the router's words", await text(`${sys} [data-item=link] [data-v=value]`), "2.5 Gb/s");
    eq("a link as usual has no status", await count(`${sys} [data-item=link] [data-st]`), 0);
    await until((s) => document.querySelectorAll(`${s} [data-f=eventList] li`).length === 3, sys);
    const events = await page.$$eval(`${sys} [data-f=eventList] li [data-v=text]`, (els) => els.map((e) => e.textContent));
    eq("the events, newest first", events.join(" | "), `WAN link came back at 2.5 Gb/s | WAN link went down (was 2.5 Gb/s) | ${events[2]}`);
    ok("the oldest is the restart", /^Restarted \(up since \d{4}-\d\d-\d\d \d\d:\d\d\)$/.test(events[2]));
    // a link slower than the week's usual one, and one that is down: said as icon and word, never color alone
    const link = (mbps) => page.evaluate((v) => { window.__smokeMutate = (d) => { if (d.devices && d.devices.router) d.devices.router.wan_link_mbps = v; }; }, mbps);
    await link(1000);
    await waitText(`${sys} [data-item=link] [data-v=value]`, /^1 Gb\/s$/);
    eq("below usual", await text(`${sys} [data-item=link] [data-v=status]`), "Below usual");
    eq("with its icon", await page.$eval(`${sys} [data-item=link] [data-v=status]`, (e) => !!e.querySelector("svg")), true);
    ok("and what it usually is", /usually 2\.5 Gb\/s/.test(await text(`${sys} [data-item=link] [data-v=sub]`)));
    await link(0);
    await waitText(`${sys} [data-item=link] [data-v=value]`, /^down$/);
    eq("down", await text(`${sys} [data-item=link] [data-v=status]`), "No link");
    await page.evaluate(() => { window.__smokeMutate = null; });
    await waitText(`${sys} [data-item=link] [data-v=value]`, /^2\.5 Gb\/s$/);
  }],

  ["router History modal: the CPU temperature chart when the router reports one", async ({ ROUTER, click, eq, page, shown, until }) => {
    await click(`${ROUTER} [data-action=openRouterHistory]`);
    await until(() => Number(document.getElementById("routerHistModal").dataset.points) > 20);
    eq("the section is shown", await shown("#routerHistTempBox"), true);
    await until(() => !!Chart.getChart(document.getElementById("routerHistTempChart")));
    eq("temperature average and peak", await page.evaluate(() => Chart.getChart(document.getElementById("routerHistTempChart")).data.datasets.map((d) => d.label).join()), "Temperature avg,Temperature max");
    eq("on the shared time axis", await page.evaluate(() => Chart.getChart(document.getElementById("routerHistTempChart")).options.scales.x.min === Chart.getChart(document.getElementById("routerHistWanChart")).options.scales.x.min), true);
    await click("[data-action=closeModal][data-arg=routerHistModal]");
  }],

  ["router History modal: at 1 h the temperature line joins readings a minute apart (spanGaps), so the line has segments, not lone dots", async ({ ROUTER, click, eq, ok, page, rangeState, until }) => {
    await click(`${ROUTER} [data-action=openRouterHistory]`);
    await until(() => Number(document.getElementById("routerHistModal").dataset.points) > 20);
    eq("the range is 1 h", (await rangeState("#routerHistRanges")).value, "1h");
    await until(() => !!Chart.getChart(document.getElementById("routerHistTempChart")));
    const info = await page.evaluate(() => {
      const c = Chart.getChart(document.getElementById("routerHistTempChart"));
      const meta = c.getDatasetMeta(0);
      const segments = meta.dataset.segments || [];
      return { spanGaps: c.data.datasets[0].spanGaps, readings: c.data.datasets[0].data.length,
        segments: segments.length, longest: Math.max(0, ...segments.map((s) => s.end - s.start + 1)) };
    });
    ok(`the dataset joins readings up to 3 minutes apart (spanGaps ${info.spanGaps})`, info.spanGaps === 180000);
    ok(`the line has segments of several points (${info.readings} readings in ${info.segments} segments, the longest of ${info.longest} points)`, info.readings >= 3 && info.longest >= 3);
    await click("[data-action=closeModal][data-arg=routerHistModal]");
  }],

  ["router History modal: past a day the temperature line is joined too (slices of 15 minutes and more: the join follows the slice)", async ({ ROUTER, chooseRange, click, ok, page, settled, until }) => {
    await click(`${ROUTER} [data-action=openRouterHistory]`);
    await until(() => Number(document.getElementById("routerHistModal").dataset.points) > 20);
    for (const id of ["7d", "30d", "all"]) {
      await chooseRange("#routerHistRanges", id);
      await settled();
      await until(() => !!Chart.getChart(document.getElementById("routerHistTempChart")));
      const info = await page.evaluate(() => {
        const c = Chart.getChart(document.getElementById("routerHistTempChart"));
        const segments = c.getDatasetMeta(0).dataset.segments || [];
        return { readings: c.data.datasets[0].data.length, segments: segments.length, longest: Math.max(0, ...segments.map((g) => g.end - g.start + 1)) };
      });
      ok(`${id}: the line has segments of several points (${info.readings} readings in ${info.segments} segments, the longest of ${info.longest})`, info.readings >= 3 && info.longest >= Math.min(info.readings, 10));
    }
    await click("[data-action=closeModal][data-arg=routerHistModal]");
  }, { variants: ["longhistory"] }],

  ["router History modal: three charts on one time axis, the range control refetches", async ({ ROUTER, chooseRange, click, eq, ok, outDir, page, path, rangeState, shown, until }) => {
    eq("history button shown", await shown(`${ROUTER} [data-action=openRouterHistory]`), true);
    await click(`${ROUTER} [data-action=openRouterHistory]`);
    await until(() => Number(document.getElementById("routerHistModal").dataset.points) > 20);
    const info = () => page.evaluate(() => Object.fromEntries(["routerHistSystemChart", "routerHistWanChart", "routerHistClientsChart"].map((id) => {
      const c = Chart.getChart(document.getElementById(id));
      return [id, { sets: c.data.datasets.length, points: c.data.datasets[0].data.length, min: c.options.scales.x.min, max: c.options.scales.x.max }];
    })));
    const one = await info();
    for (const [id, v] of Object.entries(one)) ok(`${id} has data ${JSON.stringify(v)}`, v.sets >= 2 && v.points > 20);
    eq("system sets (cpu/mem avg+max)", one.routerHistSystemChart.sets, 4);
    eq("shared axis min", new Set(Object.values(one).map((v) => v.min)).size, 1);
    eq("shared axis max", new Set(Object.values(one).map((v) => v.max)).size, 1);
    eq("active range", (await rangeState("#routerHistRanges")).value, "1h");
    eq("the dialog offers 1 h to All (no 6 h; the router keeps 180 days)", (await rangeState("#routerHistRanges")).words.join(","), "Last hour,Last 24 hours,Last 7 days,Last 30 days,Last 90 days,All · 180 d");
    await page.screenshot({ path: path.join(outDir, "router-history.png") });
    const requests = () => page.evaluate(() => performance.getEntriesByType("resource").filter((e) => e.name.includes("/history?")).length);
    const before = await requests();
    await chooseRange("#routerHistRanges", "24h");
    await until((n) => performance.getEntriesByType("resource").filter((e) => e.name.includes("/history?")).length > n, before);
    await until(() => document.getElementById("routerHistStatus").textContent.includes("24 hours"));
    eq("active range after the choice", (await rangeState("#routerHistRanges")).value, "24h");
    await chooseRange("#routerHistRanges", "30d");
    await until(() => document.getElementById("routerHistStatus").textContent.includes("30 days"));
    eq("active range 30 d", (await rangeState("#routerHistRanges")).value, "30d");
    const asked = await page.evaluate(() => {
      const q = new URL(performance.getEntriesByType("resource").filter((e) => e.name.includes("/history?") && e.name.includes("max_points")).pop().name).searchParams;
      return { span: Number(q.get("end_time")) - Number(q.get("start_time")), lag: Date.now() / 1000 - Number(q.get("end_time")) };
    });
    eq("30 d asks for 30 days", asked.span, 30 * 86400);
    ok(`ending now (${asked.lag.toFixed(0)} s ago)`, asked.lag >= 0 && asked.lag < 60);
    await click("[data-action=closeModal][data-arg=routerHistModal]");
  }],

  ["clients modal: summary, table, link speed label, offline toggle, node filter, sorting, chart", async ({ ROUTER, chartLabels, chooseRange, click, count, dialogKeys, eq, ok, outDir, page, path, rangeState, signIn, text, until, waitCount }) => {
      await signIn(ROUTER); // the clients feature needs the router's password stored: do what a person does (this file does not rely on another file having done it)
    // the stored password makes the `clients` feature available; a reload picks that up
    await page.reload({ waitUntil: "load" });
    await until((s) => document.querySelector(s) && !document.querySelector(s).disabled, `${ROUTER} [data-f=count]`);
    // the card's counts come from the live summary: online clients, bands and mesh nodes, with no client-list request
    await until((s) => document.querySelector(`${s} [data-f=countVal]`)?.textContent === "3", ROUTER);
    await until((s) => document.querySelectorAll(`${s} [data-f=nodes] li`).length === 3, ROUTER);
    eq("mesh node counts on the card", await page.$$eval(`${ROUTER} [data-f=nodes] li`, (els) => els.map((e) => `${e.querySelector("b").textContent} ${e.querySelector(".n-c b").textContent}`).join(", ")), "Hall 2, Study 1, Garage 0");
    eq("node status and backhaul on the card", await page.$$eval(`${ROUTER} [data-f=nodes] li`, (els) => els.map((e) => `${e.querySelector(".n-id span").textContent}|${e.querySelector(".n-st").textContent}|${e.classList.contains("st-warn")}`).join(" ; ")), "1 wired||false ; 5 GHz, -54 dBm||false ; mesh node|Offline|true");
    ok("the band bar has the wired and Wi-Fi parts", (await page.$$eval(`${ROUTER} [data-f=bands] i`, (els) => els.length)) >= 2);
    eq("no client-list request for the card", await page.evaluate(() => performance.getEntriesByType("resource").filter((e) => new URL(e.name).pathname.endsWith("/clients")).length), 0);
    await dialogKeys(`${ROUTER} [data-f=count]`, "networkModal");
    await click(`${ROUTER} [data-f=count]`);
    await until(() => document.getElementById("networkModal").classList.contains("active"));
    await waitCount("#clientsBody tr", 3); // three online clients; the offline one is hidden by default
    eq("the topology is on top of the clients", await count("#topologyBody .topo-wide .tgo"), 3);
    // an unrounded figure in the live message (a median or an average comes out like this) reads as one decimal
    await page.evaluate(() => { window.__smokeMutate = (d) => d.targets.forEach((t) => { if (t.category === "public" || t.category === "gateway") { t.recent_latencies = Array(30).fill(17.69766665802256); t.last_ms = 17.69766665802256; } }); });
    await until(() => [...document.querySelectorAll("#topologyBody .topo-wide .tsub")].some((e) => /^17\.7 ms$/.test(e.textContent)));
    await page.evaluate(() => { window.__smokeMutate = null; });
    const chips = await page.$$eval("#clientsChips .clients-chip", (els) => els.map((e) => e.textContent));
    eq("chips", chips.join("|"), "3 online|1 wired|1 on 2.4 GHz|1 on 5 GHz|Hall: 2|Study: 1|Garage: 0");
    const rows = await page.$$eval("#clientsBody tr", (trs) => trs.map((tr) => tr.textContent));
    ok("names and addresses shown under privacy full", rows.some((t) => /Ann's phone/.test(t) && /192\.0\.2\.12/.test(t)));
    ok("the phone is labeled by its device type", rows.some((t) => /Ann's phone/.test(t) && /Phone/.test(t)));
    ok("link speed and signal", rows.some((t) => /866 \/ 780 Mbps/.test(t) && /-58 dBm/.test(t) && /27h 5m/.test(t)));
    ok("link speed is labeled as not traffic", /link speed \(Wi-Fi rate\), not traffic/i.test(await text("#networkModal")));
    ok("no offline device yet", !rows.some((t) => /Old laptop/.test(t)));
    await click("#clientsShowOffline");
    await waitCount("#clientsBody tr", 4);
    await click("#clientsShowOffline");
    await waitCount("#clientsBody tr", 3);
    await click('#clientsChips button[data-arg="0000a2"]');
    await waitCount("#clientsBody tr", 1);
    ok("node filter", /Ann's phone/.test(await text("#clientsBody")));
    await click('#clientsChips button[data-arg="0000a2"]');
    await waitCount("#clientsBody tr", 3);
    await click('#networkModal button[data-action=sortClients][data-arg=label]'); // sorted by name already: now descending
    eq("sorted", await page.$eval("#clientsBody tr:first-child td:first-child", (e) => e.textContent.split("192")[0].trim()), "Plug");
    await until(() => !!Chart.getChart(document.getElementById("clientsChart")));
    await until(() => Chart.getChart(document.getElementById("clientsChart")).data.datasets.length === 2); // 5 GHz, Wired (the seeded history has no 2.4 GHz client)
    ok("chart has points", (await chartLabels("clientsChart")) > 10);
    await click('#clientsModeButtons button[data-arg=node]');
    await until(() => Chart.getChart(document.getElementById("clientsChart")).data.datasets.map((d) => d.label).join() === "Hall,Study");
    eq("the clients chart offers 1 h to All", (await rangeState("#clientsRangeButtons")).words.join(","), "Last hour,Last 24 hours,Last 7 days,Last 30 days,Last 90 days,All · 180 d");
    await chooseRange("#clientsRangeButtons", "30d");
    await until(() => document.querySelector("#clientsRangeButtons select").value === "30d");
    await until(() => { const q = new URL(performance.getEntriesByType("resource").filter((e) => e.name.includes("clients/history")).pop().name).searchParams; return Number(q.get("end_time")) - Number(q.get("start_time")) === 30 * 86400; });
    await until(() => Chart.getChart(document.getElementById("clientsChart")).data.labels.length > 0);
    const asked = await page.evaluate(() => {
      const q = new URL(performance.getEntriesByType("resource").filter((e) => e.name.includes("clients/history")).pop().name).searchParams;
      return { span: Number(q.get("end_time")) - Number(q.get("start_time")), lag: Date.now() / 1000 - Number(q.get("end_time")) };
    });
    eq("30 d asks for 30 days", asked.span, 30 * 86400);
    ok(`ending now (${asked.lag.toFixed(0)} s ago)`, asked.lag >= 0 && asked.lag < 60);
    await chooseRange("#clientsRangeButtons", "1h");
    await until(() => Chart.getChart(document.getElementById("clientsChart")).data.labels.length > 0);
    await page.screenshot({ path: path.join(outDir, "clients.png") });
    await click("[data-action=closeModal][data-arg=networkModal]");
  }],

  ["Local network modal: internet, modem, router, mesh nodes with client counts", async ({ ROUTER, click, count, eq, ok, outDir, page, path, until }) => {
    await click(`${ROUTER} [data-f=lan]`);
    await until(() => document.getElementById("networkModal").classList.contains("active"));
    await until(() => document.querySelector("#topologyBody .topo-wide .tgo .tc .tname")?.textContent);
    await until(() => /ms/.test(document.querySelector("#topologyBody .topo-wide .tn .tsub")?.textContent ?? ""));
    const t = await topo(page);
    eq("chain", t.kinds.join(">"), "INTERNET>GATEWAY>ROUTER");
    ok(`modem is named by its host (${t.names[1]})`, /192\.168\.99\.1/.test(t.names[1]));
    eq("router is named by its model", t.names[2], "Example Router");
    eq("mesh nodes", t.leaves.join(" | "), "Hall: 2 clients | Study: 1 client | Garage: 0 clients");
    eq("summary", t.summary, "3 mesh nodes · 3 clients");
    ok("wan rate on the router", /↓123\.4 ↑2\.3|↓123\.4 ↑12\.3/.test(t.subs[2]));
    ok("all hops healthy", t.classes.every((c) => /\bst-good\b/.test(c)));
    eq("3 mesh nodes", await count("#topologyBody .topo-wide .tgo"), 3);
    const nodes = await page.$$eval("#topologyBody .topo-wide .tgo", (els) => els.map((e) => `${e.querySelector(".tname").textContent}|${e.querySelector(".tsub").textContent}|${/\bst-warn\b/.test(e.querySelector(".tn").getAttribute("class"))}|${e.querySelector("title").textContent}`));
    eq("offline node is a warning in the picture, the others are not", nodes.map((n) => n.split("|").slice(0, 3).join("|")).join(" ; "), "Hall|1 Wi-Fi · 1 wired|false ; Study|1 Wi-Fi|false ; Garage|Offline|true");
    ok("backhaul is in the node's hover text", /Study.*backhaul 5 GHz, -54 dBm/.test(nodes[1]) && /Garage.*offline/.test(nodes[2]));
    const tree = await page.$$eval("#topologyBody .topo-narrow li.d1", (els) => els.map((e) => `${e.querySelector("b").textContent}|${e.querySelector(".tv-s").textContent}|${e.querySelector(".tv-st").textContent}|${/\bst-warn\b/.test(e.className)}`));
    eq("the narrow tree says offline and the backhaul", tree.join(" ; "), "Hall|2 clients · 1 Wi-Fi · 1 wired||false ; Study|1 client · 1 Wi-Fi · 5 GHz, -54 dBm||false ; Garage|0 clients · 0 Wi-Fi|Offline|true");
    await page.screenshot({ path: path.join(outDir, "topology.png") });
  }],

  ["topology: a mesh node filters the client table to it; the online chip clears the filter", async ({ click, clickAt, eq, ok, page, text, waitCount }) => {
    await waitCount("#clientsBody tr", 3);
    await clickAt('#topologyBody .topo-wide .tgo[data-arg="0000a2"]');
    await waitCount("#clientsBody tr", 1);
    ok("filtered to Study", /Ann's phone/.test(await text("#clientsBody")));
    ok("filter note", /Showing only Study/.test(await text("#clientsNodeFilter")));
    eq("node marked pressed", await page.$eval('#topologyBody .topo-wide .tgo[data-arg="0000a2"]', (e) => e.getAttribute("aria-pressed")), "true");
    await clickAt('#topologyBody .topo-wide .tgo[data-arg="0000a2"]'); // a second click must not toggle the filter off
    await waitCount("#clientsBody tr", 1);
    await click("#clientsChips button[data-action=clearClientsFilter]");
    await waitCount("#clientsBody tr", 3);
    await click("[data-action=closeModal][data-arg=networkModal]");
  }],

  ["metric help popovers: link speed in the Local network dialog sits above it; Escape closes", async ({ ROUTER, click, eq, ok, openHelp, page, popoverText, until }) => {
    await click(`${ROUTER} [data-f=count]`);
    await until(() => document.getElementById("networkModal").classList.contains("active"));
    const speed = await openHelp("clients.speed");
    ok("link speed text", /not traffic/.test(speed));
    ok("popover above the modal", await page.$eval("#helpPopover", (e) => Number(getComputedStyle(e).zIndex) > Number(getComputedStyle(document.getElementById("networkModal")).zIndex)));
    await page.keyboard.press("Escape");
    eq("escape closes in the modal", await popoverText(), null);
    await click("[data-action=closeModal][data-arg=networkModal]");
  }],

  ["Local network dialog: keyboard on the topology, band filter chips, sort state, signal words, empty-state element", async ({ ROUTER, click, eq, flipMode, ok, outDir, page, path, rangeState, until, waitCount }) => {
    await click(`${ROUTER} [data-f=lan]`);
    await until(() => document.getElementById("networkModal").classList.contains("active"));
    await waitCount("#clientsBody tr", 3);
    await page.evaluate(() => document.querySelector('#topologyBody .topo-wide .tgo[data-arg="0000a2"]').focus()); // an SVG group: page.focus refuses it
    await page.keyboard.press("Enter");
    await waitCount("#clientsBody tr", 1);
    eq("node pressed by keyboard", await page.$eval('#topologyBody .topo-wide .tgo[data-arg="0000a2"]', (e) => e.getAttribute("aria-pressed")), "true");
    await click("#clientsChips button[data-action=clearClientsFilter]");
    await waitCount("#clientsBody tr", 3);
    await click('#clientsChips button[data-action=filterClientsBand][data-arg="wired"]');
    await waitCount("#clientsBody tr", 1);
    eq("wired chip pressed", await page.$eval('#clientsChips button[data-arg="wired"]', (e) => e.getAttribute("aria-pressed")), "true");
    ok("wired row has no signal", /wired/.test(await page.$eval("#clientsBody tr td:nth-last-child(3)", (e) => e.textContent)));
    await click('#clientsChips button[data-arg="wired"]');
    await waitCount("#clientsBody tr", 3);
    const signals = await page.$$eval("#clientsBody .sig", (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()));
    ok(`signal words with dBm (${signals.join("|")})`, signals.length >= 1 && signals.every((t) => /^(Excellent|Good|Fair|Weak) -?\d+ dBm$/.test(t)));
    eq("sorted column has aria-sort", await page.$$eval("#networkModal th[aria-sort]", (ths) => ths.map((t) => t.getAttribute("aria-sort")).join()), "descending"); // the clients step above clicked Device twice
    eq("empty state is hidden while clients are listed", await page.$eval("#clientsEmpty", (e) => e.hidden), true);
    eq("the range control shows the choice", (await rangeState("#clientsRangeButtons")).value, "1h"); // the clients step chose 1 h
    eq("by node was chosen earlier", await page.$$eval("#clientsLegend span", (els) => els.map((e) => e.textContent).join("|")), "Hall|Study");
    await click("#clientsModeButtons button[data-arg=band]");
    await until(() => document.querySelector("#clientsLegend span")?.textContent === "5 GHz");
    const legend = await page.$$eval("#clientsLegend span", (els) => els.map((e) => e.textContent).join("|"));
    eq("legend by band (only bands with clients)", legend, "5 GHz|Wired");
    await page.screenshot({ path: path.join(outDir, "network-light.png") });
    await page.evaluate(() => document.getElementById("networkModal").querySelector(".modal-b").scrollTo(0, 99999));
    await page.screenshot({ path: path.join(outDir, "network-light-bottom.png") });
    await page.setViewport({ width: 390, height: 844 }); // no isMobile: puppeteer would reload the page and close the dialog
    await page.evaluate(() => document.getElementById("networkModal").querySelector(".modal-b").scrollTo(0, 0));
    await until(() => getComputedStyle(document.querySelector("#topologyBody .topo-narrow")).display !== "none");
    eq("phone: topology is the vertical list", await page.$eval("#topologyBody .topo-wide", (e) => getComputedStyle(e).display), "none");
    await page.screenshot({ path: path.join(outDir, "network-phone.png") });
    await page.evaluate(() => document.getElementById("networkModal").querySelector(".modal-b").scrollTo(0, 700));
    await page.screenshot({ path: path.join(outDir, "network-phone-clients.png") });
    await flipMode(); // dark
    await page.screenshot({ path: path.join(outDir, "network-phone-dark.png") });
    await page.setViewport({ width: 1400, height: 1000 });
    await page.evaluate(() => document.getElementById("networkModal").querySelector(".modal-b").scrollTo(0, 0));
    await page.screenshot({ path: path.join(outDir, "network-dark.png") });
    await flipMode(); // and back to light
    await click("[data-action=closeModal][data-arg=networkModal]");
  }],

  ["Router history dialog: one crosshair across all charts, readouts, keyboard, per-device sections", async ({ ROUTER, click, eq, flipMode, ok, outDir, page, path, text, until }) => {
    await click(`${ROUTER} [data-action=openRouterHistory]`);
    await until(() => Number(document.getElementById("routerHistModal").dataset.points) > 20);
    const reads = () => page.$$eval(".rh-read", (els) => els.map((e) => e.textContent));
    ok("no readout before hovering", (await reads()).every((t) => t === ""));
    const box = await (await page.$("#routerHistWanChart")).boundingBox();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2);
    await until(() => [...document.querySelectorAll(".rh-read")].every((e) => e.textContent !== ""));
    const r = await reads();
    ok(`WAN readout (${r[0]})`, /Download .* Upload .* Mbps/.test(r[0]));
    ok(`system readout (${r[1]})`, /CPU \d+% .* Memory \d+%/.test(r[1]));
    ok(`clients readout (${r[2]})`, /Clients /.test(r[2]));
    eq("the three readouts name the same moment", new Set(r.map((t) => t.split("  ·  ")[0])).size, 1);
    // each chart draws its legend when its data arrives: wait for all of them instead of reading them once
    await until(() => [...document.querySelectorAll("#routerHistModal .stack-chart:not([hidden]) .legend")].every((e) => e.children.length >= 1));
    ok("legends drawn", true);
    ok("capacity stated, known or not", /capacity/.test(await text("#routerHistWanSub")));
    await page.screenshot({ path: path.join(outDir, "router-history-hover.png") });
    await page.mouse.move(2, 2);
    await until(() => [...document.querySelectorAll(".rh-read")].every((e) => e.textContent === ""));
    await page.focus("#routerHistSystemChart");
    await page.keyboard.press("End");
    await until(() => [...document.querySelectorAll(".rh-read")].every((e) => e.textContent !== ""));
    const end = (await reads())[0].split("  ·  ")[0];
    await page.keyboard.press("ArrowLeft");
    await until((e) => document.querySelector(".rh-read").textContent.split("  ·  ")[0] !== e, end);
    await page.keyboard.press("Escape");
    await until(() => [...document.querySelectorAll(".rh-read")].every((e) => e.textContent === ""));
    await page.screenshot({ path: path.join(outDir, "router-history-light.png") });
    await page.setViewport({ width: 390, height: 844 }); // no isMobile: puppeteer would reload the page and close the dialog
    await page.screenshot({ path: path.join(outDir, "router-history-phone.png") });
    await flipMode(); // dark
    await page.screenshot({ path: path.join(outDir, "router-history-phone-dark.png") });
    await page.setViewport({ width: 1400, height: 1000 });
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2);
    await page.screenshot({ path: path.join(outDir, "router-history-dark.png") });
    await flipMode(); // and back to light
    await click("[data-action=closeModal][data-arg=routerHistModal]");
  }],

  ["clients modal: a device that used one app almost only says which, next to its name (the router's traffic hint)", async ({ ROUTER, click, ok, page, text, until, waitCount }) => {
    await click(`${ROUTER} [data-f=count]`);
    await until(() => document.getElementById("networkModal").classList.contains("active"));
    await waitCount("#clientsBody tr", 3);
    const rows = await page.$$eval("#clientsBody tr", (trs) => trs.map((tr) => tr.textContent));
    ok(`the desk PC is mostly Sony PlayStation (${rows.join(" | ")})`, rows.some((t) => /Desk PC/.test(t) && /mostly Sony PlayStation/.test(t)));
    ok("a phone that watches three apps has no hint", rows.every((t) => !/Ann's phone.*mostly/.test(t)));
    ok("the table no longer says per-device traffic is missing", !/Per-device traffic is not available/.test(await text("#networkModal")));
    await click("[data-action=closeModal][data-arg=networkModal]");
  }],

  ["every range draws the router history's charts (400-800 points from 7 d on, axis labels of the range's kind), on a desktop, a phone and in every theme", async ({ ROUTER, atEveryRange, axisOk, chartAxis, chooseRange, click, eq, everyLook, noSideScroll, ok, phone, rangeState, settled, until }) => {
    const open = async () => {
      await click(`${ROUTER} [data-action=openRouterHistory]`);
      await until(() => Number(document.getElementById("routerHistModal").dataset.points) > 20);
    };
    const check = (where) => async (id, s) => {
      const what = `${where} ${id}`;
      ok(`${what}: the WAN chart is drawn`, !!s);
      const long = ["7d", "30d", "90d", "all"].includes(id);
      const points = s.sets[0].n;
      ok(`${what}: ${points} points (${long ? "300-820" : "at least 20"})`, long ? points >= 300 && points <= 820 : points >= 20);
      axisOk(what, id, s);
      for (const other of ["routerHistSystemChart", "routerHistClientsChart"]) ok(`${what}: ${other} has its points`, (await chartAxis(`#${other}`)).sets[0].n >= 20);
      ok(`${what}: nothing sticks out sideways`, await noSideScroll("routerHistModal"));
    };
    await open();
    eq("the router keeps 180 days: 1 h to All", (await rangeState("#routerHistRanges")).ids.join(), "1h,24h,7d,30d,90d,all");
    await atEveryRange("#routerHistRanges", "#routerHistWanChart", check("desktop"));
    await everyLook(async (look) => {
      for (const id of ["24h", "all"]) {
        await chooseRange("#routerHistRanges", id);
        await settled();
        await check(look)(id, await chartAxis("#routerHistWanChart"));
      }
    });
    await click("[data-action=closeModal][data-arg=routerHistModal]");
    await phone(async () => {
      await open();
      await atEveryRange("#routerHistRanges", "#routerHistWanChart", check("phone"));
      await click("[data-action=closeModal][data-arg=routerHistModal]");
    });
  }, { variants: ["longhistory"] }],

  ["every range draws the clients chart (the server's 300 points at most), with the axis labels of the range's kind, on a desktop, a phone and in every theme", async ({ ROUTER, atEveryRange, axisOk, chartAxis, chooseRange, click, eq, everyLook, noSideScroll, ok, page, phone, rangeState, settled, signIn, sleep, until, waitCount }) => {
    await signIn(ROUTER); // the clients feature needs the router's password stored
    await page.reload({ waitUntil: "load" });
    await until((s) => document.querySelector(s) && !document.querySelector(s).disabled, `${ROUTER} [data-f=count]`);
    // the server lets the clients routes answer 30 calls a minute (a person choosing ranges asks far less): one choice every 2.2 s
    const paced = async (id) => { await chooseRange("#clientsRangeButtons", id); await sleep(2200); };
    const open = async () => {
      await click(`${ROUTER} [data-f=count]`);
      await until(() => document.getElementById("networkModal").classList.contains("active"));
      await waitCount("#clientsBody tr", 3);
    };
    const check = (where) => async (id, s) => {
      const what = `${where} ${id}`;
      ok(`${what}: the chart is drawn`, !!s);
      ok(`${what}: ${s.count} points (20-320)`, s.count >= 20 && s.count <= 320);
      const shown = await page.evaluate(() => ({ chosen: document.querySelector("#clientsRangeButtons select").value, note: document.getElementById("clientsChartNote").textContent }));
      ok(`${what}: the control says ${shown.chosen}, the chart's note says "${shown.note}"`, shown.chosen === id && !/^Could not/.test(shown.note));
      axisOk(what, id, s);
      ok(`${what}: nothing sticks out sideways`, await noSideScroll("networkModal"));
    };
    await open();
    eq("1 h to All", (await rangeState("#clientsRangeButtons")).ids.join(), "1h,24h,7d,30d,90d,all");
    await atEveryRange("#clientsRangeButtons", "#clientsChart", check("desktop"), { pause: 2200 });
    await everyLook(async (look) => {
      for (const id of ["24h", "all"]) {
        await paced(id);
        await settled();
        await check(look)(id, await chartAxis("#clientsChart"));
      }
    });
    await click("[data-action=closeModal][data-arg=networkModal]");
    await phone(async () => {
      await open();
      await atEveryRange("#clientsRangeButtons", "#clientsChart", check("phone"), { pause: 2200 });
      await click("[data-action=closeModal][data-arg=networkModal]");
    });
  }, { variants: ["longhistory"] }],

  ["All starts at the oldest stored data when that is younger than the retention: router history (42 days of data, then 400), a window of its own, dates then months, no label twice in a row", async ({ ROUTER, chartAxis, chooseRange, click, eq, forgetRequests, ok, page, rangeState, requestsTo, settled, until, youngData }) => {
    for (const { days, kind, age } of [{ days: 42, kind: "date", age: 42 }, { days: 400, kind: "month", age: 180 }]) { // (the router keeps 180 days of tiers: the server answers no more)
      await youngData(days);
      await click(`${ROUTER} [data-action=openRouterHistory]`);
      await until(() => Number(document.getElementById("routerHistModal").dataset.points) > 20);
      eq(`${days}: the retention is a year`, (await rangeState("#routerHistRanges")).words.at(-1), "All · 1 y");
      forgetRequests();
      await chooseRange("#routerHistRanges", "all");
      await settled();
      // (the window's own requests carry max_points; the latency card's last-minute load and its WAN overlay, which a remount
      // in the step before can start late, ask /history too and are not counted)
      const asked = requestsTo(/\/devices\/router\/history\?.*max_points=/);
      eq(`${days}: All asks the router history once (${asked.join(" ")})`, asked.length, 1);
      const s = await chartAxis("#routerHistWanChart");
      const x = await page.evaluate(() => { const c = Chart.getChart(document.getElementById("routerHistWanChart")); const d = c.data.datasets[0].data; return { min: c.scales.x.min / 1000, first: d[0].x / 1000, now: Date.now() / 1000 }; });
      const back = (x.now - x.first) / 86400;
      ok(`${days}: the first point is ${back.toFixed(1)} days back (about ${age}, not 365)`, Math.abs(back - age) < 1.5);
      ok(`${days}: the axis starts there (${((x.now - x.min) / 86400).toFixed(1)} days back)`, Math.abs((x.now - x.min) / 86400 - age) < 1.5);
      ok(`${days}: ${s.sets[0].n} points (400-800)`, s.sets[0].n >= 400 && s.sets[0].n <= 800);
      ok(`${days}: the labels are ${kind}s (${s.labels.join(" | ")})`, s.labels.length >= 2 && [s.labels[0], s.labels.at(-1)].every((l) => (kind === "date" ? /^[A-Z][a-z]{2} \d{1,2}$/ : /^[A-Z][a-z]{2} \d{4}$/).test(l)));
      ok(`${days}: no label twice in a row`, s.labels.every((l, i) => i === 0 || l !== s.labels[i - 1]));
      ok(`${days}: the status says where it starts`, /^all stored, since [A-Z][a-z]{2} \d{1,2}/.test(await page.$eval("#routerHistStatus", (e) => e.textContent)));
      await click("[data-action=closeModal][data-arg=routerHistModal]");
    }
    await youngData(null);
  }, { variants: ["longhistory"] }],

  ["All starts at the oldest stored data when that is younger than the retention: the clients chart (42 days of data, then 400)", async ({ ROUTER, chartAxis, chooseRange, click, eq, forgetRequests, ok, page, rangeState, requestsTo, settled, signIn, sleep, until, waitCount, youngData }) => {
    await signIn(ROUTER);
    await page.reload({ waitUntil: "load" });
    await until((s) => document.querySelector(s) && !document.querySelector(s).disabled, `${ROUTER} [data-f=count]`);
    for (const { days, kind } of [{ days: 42, kind: "date" }, { days: 400, kind: "month" }]) {
      if (days === 400) await sleep(62000); // (the clients routes answer 30 calls a minute)
      await youngData(days);
      await click(`${ROUTER} [data-f=count]`);
      await until(() => document.getElementById("networkModal").classList.contains("active"));
      await waitCount("#clientsBody tr", 3);
      await sleep(2200); // (the clients routes answer 30 calls a minute)
      forgetRequests();
      await chooseRange("#clientsRangeButtons", "all");
      await settled();
      eq(`${days}: All asks the client counts once (${requestsTo(/\/clients\/history\?/).join(" ")})`, requestsTo(/\/clients\/history\?/).length, 1);
      await sleep(2200);
      const s = await chartAxis("#clientsChart");
      const note = await page.$eval("#clientsChartNote", (e) => e.textContent);
      ok(`${days}: ${s.count} points (20-320: the server's 300 at most)`, s.count >= 20 && s.count <= 320);
      const first = await page.evaluate(() => Chart.getChart(document.getElementById("clientsChart")).data.labels[0]);
      const near = await page.evaluate((d, month) => [-1, 0, 1].map((k) => new Date(Date.now() - (d - (month ? 31 * k : k)) * 86400000)
        .toLocaleDateString("en-US", month ? { month: "short", year: "numeric" } : { month: "short", day: "numeric" }).replace(",", "")), Math.min(days, 180), kind === "month");
      ok(`${days}: the first label "${first}" is where the data starts (${near.join(" / ")}), not a year back`, near.includes(String(first)));
      ok(`${days}: the labels are ${kind}s (${s.labels.join(" | ")})`, s.labels.length >= 2 && [s.labels[0], s.labels.at(-1)].every((l) => (kind === "date" ? /^[A-Z][a-z]{2} \d{1,2}$/ : /^[A-Z][a-z]{2} \d{4}$/).test(l)));
      ok(`${days}: no label twice in a row`, s.labels.every((l, i) => i === 0 || l !== s.labels[i - 1]));
      ok(`${days}: the note says where it starts (${note})`, /^all stored, since [A-Z][a-z]{2} \d{1,2}/.test(note));
      await click("[data-action=closeModal][data-arg=networkModal]");
    }
    await youngData(null);
  }, { variants: ["longhistory"] }],
];
