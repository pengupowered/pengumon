// The latency panel: the live chart fed by the websocket, and the time-range buttons.

export const subject = "panel:latency";
export const variants = ["full"];

export const steps = [
  ["live chart is fed by the websocket", async ({ chartLabels, eq, hasChart, ok, page, text, until }) => {
    await hasChart("latencyChart");
    await hasChart("lossChart");
    const n = await chartLabels("latencyChart");
    await until((i, k) => Chart.getChart(document.getElementById(i)).data.labels.length > k, "latencyChart", n);
    eq("live label", await text("#chartModeLabel"), "last 60 s · every probe");
    const sets = await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.map((d) => d.id).join());
    ok(`three path series and the WAN overlay (${sets})`, /path_router,path_line,path_internet/.test(sets));
    ok("legend", /Internet/.test(await text("#latencyLegend")));
  }],

  ["time ranges switch the main chart", async ({ click, eq, page, until, waitText }) => {
    for (const [arg, label] of [["3600", "last 1 h · avg per 10 s"], ["900", "last 15 min · avg per 2 s"]]) {
      await click(`#timeRangeButtons button[data-arg="${arg}"]`);
      await waitText("#chartModeLabel", new RegExp(`^${label.replace(/[()]/g, "\\$&")}$`));
      await until((i) => Chart.getChart(document.getElementById(i)).data.datasets.length >= 3, "latencyChart");
      eq("active button", await page.$eval("#timeRangeButtons button.active", (b) => b.dataset.arg), arg);
    }
    await click('#timeRangeButtons button[data-arg="live"]');
    await waitText("#chartModeLabel", /^last 60 s · every probe$/);
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.length >= 3);
  }],

  ["the legend says what each line is, the chart fills its card, the loss lane says when it is empty", async ({ eq, ok, page, text, until }) => {
    await until(() => /Router/.test(document.getElementById("latencyLegend").textContent));
    const legend = await text("#latencyLegend");
    ok(`Router and Line name their target: ${legend}`, /Router\s*Hop 1: Local Router \(LAN\)/.test(legend) && /Line\s*Hop 2: ISP Modem \/ Gateway/.test(legend));
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

  ["Each target draws one line per pinged target in its own colour and the legend names them; Path brings the three back", async ({ click, eq, ok, page, text, until }) => {
    await click('#latencyViewButtons button[data-arg="targets"]');
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.some((d) => String(d.id).startsWith("t:")));
    const ids = await page.evaluate(() => Chart.getChart(document.getElementById("latencyChart")).data.datasets.map((d) => d.id).filter((i) => i.startsWith("t:")).join());
    eq("one line per ping target, DNS lookups left out", ids, "t:local_router,t:isp_gateway,t:cloudflare_dns");
    eq("pressed", await page.$eval('#latencyViewButtons button[data-arg="targets"]', (b) => b.getAttribute("aria-pressed")), "true");
    ok("legend uses the short names", /Router.*Line.*Internet/.test(await text("#latencyLegend")));
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
];
