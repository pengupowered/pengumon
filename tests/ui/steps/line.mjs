// The line card (DOCSIS modem in the full variant) and its Channels dialog.

export const subject = "panel:line";
export const variants = ["full"];

export const steps = [
  ["line card: gauges from the server's ranges, errors in plain words", async ({ LINE, count, eq, ok, page, text, waitText }) => {
    await waitText(`${LINE} [data-f=badge]`, /Warning/);
    await waitText(`${LINE} [data-f=g_ds_power] [data-f=val]`, /^1\.3$/);
    ok("gauge zones drawn", (await count(`${LINE} [data-f=g_ds_power] .g-zone`)) >= 1);
    eq("the gauge's status shows its word beside the icon", await page.$eval(`${LINE} [data-f=g_ds_power] .g-name [data-v=status] .st-word`, (e) => e.textContent), "Healthy");
    eq("ds power graded by the server", await page.$eval(`${LINE} [data-f=g_ds_power]`, (e) => e.className), "gauge st-good");
    await waitText(`${LINE} [data-f=note]`, /^Brief error burst on 1 channel \(537 MHz\) in the last 30 min, no measurable loss\.$/);
    eq("a short burst is not a big red number", await page.$eval(`${LINE} [data-f=g_uncorr]`, (e) => e.classList.contains("serious")), false);
    ok("server note underneath", /short burst/.test(await text(`${LINE} [data-f=detail]`)));
    ok("channel summary", /3 down · 1 up locked/.test(await text(`${LINE} [data-f=chanTxt]`)));
  }],

  ["channels modal lists every channel and flags the ingress", async ({ LINE, click, count, eq, ok, outDir, page, path, text, waitCount }) => {
    await click(`${LINE} [data-action=openChannelsModal]`);
    await waitCount("#channelsTableBody tr", 4);
    ok("summary", /3 down · 1 up locked/.test(await text("#channelsSummaryBadge")));
    eq("a burst graded good raises no alert", await page.$eval("#channelsAlertBox", (e) => e.hidden), true);
    ok("the channel with errors first", /Down 2/.test(await text("#channelsTableBody tr:first-child")) && /\+42/.test(await text("#channelsTableBody tr:first-child")));
    eq("three gauges", await count("#channelsGauges .gauge"), 3);
    await page.screenshot({ path: path.join(outDir, "channels.png") });
    await click("[data-action=closeModal][data-arg=channelsModal]");
    eq("closed", await page.$eval("#channelsModal", (m) => m.classList.contains("active")), false);
  }],

  ["channels modal History tab: heatmap canvas, channel detail chart, range and metric", async ({ LINE, chooseRange, click, eq, hasChart, ok, outDir, page, path, rangeState, shown, until }) => {
    await click(`${LINE} [data-action=openChannelsModal]`);
    eq("tabs shown", await shown("#channelsTabs"), true);
    await click("#btnChannelsHistory");
    await until(() => Number(document.getElementById("chHistCanvas").dataset.rows) >= 2);
    eq("downstream rows only for SNR", await page.$eval("#chHistCanvas", (c) => c.dataset.rows), "2");
    const painted = () => page.$eval("#chHistCanvas", (c) => {
      const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      let n = 0;
      for (let i = 3; i < d.length; i += 4) if (d[i] !== 0) n++;
      return n;
    });
    ok("heatmap painted", (await painted()) > 2000);
    await click('#chHistMetrics button[data-arg="power"]');
    await until(() => document.getElementById("chHistCanvas").dataset.rows === "3");
    await click('#chHistMetrics button[data-arg="snr"]');
    await until(() => document.getElementById("chHistCanvas").dataset.rows === "2");
    await page.evaluate(() => {
      const c = document.getElementById("chHistCanvas");
      const r = c.getBoundingClientRect();
      c.dispatchEvent(new MouseEvent("click", { clientX: r.left + 100, clientY: r.top + 18 + 3, bubbles: true }));
    });
    await hasChart("chHistDetailChart");
    eq("detail datasets", await page.evaluate(() => Chart.getChart(document.getElementById("chHistDetailChart")).data.datasets.length), 3);
    ok("detail has points", (await page.evaluate(() => Chart.getChart(document.getElementById("chHistDetailChart")).data.datasets[0].data.length)) > 20);
    await page.screenshot({ path: path.join(outDir, "channels-history.png") });
    await chooseRange("#chHistRanges", "7d");
    await until(() => document.querySelector("#chHistRanges select")?.value === "7d" && /readings/.test(document.getElementById("chHistStatus").textContent));
    eq("24 h to All (readings every 30 min: no 1 h), the 180 day retention", (await rangeState("#chHistRanges")).ids.join(","), "24h,7d,30d,90d,all");
    await chooseRange("#chHistRanges", "30d");
    await until(() => document.querySelector("#chHistRanges select")?.value === "30d" && /readings|No channel readings/.test(document.getElementById("chHistStatus").textContent));
    const asked = await page.evaluate(() => {
      const q = new URL(performance.getEntriesByType("resource").filter((e) => e.name.includes("/channels/history?")).pop().name).searchParams;
      return { span: Number(q.get("end_time")) - Number(q.get("start_time")), lag: Date.now() / 1000 - Number(q.get("end_time")) };
    });
    eq("30 d asks for 30 days", asked.span, 30 * 86400);
    ok(`ending now (${asked.lag.toFixed(0)} s ago)`, asked.lag >= 0 && asked.lag < 60);
    ok("and the heatmap is drawn from what is stored", Number(await page.$eval("#chHistCanvas", (c) => c.dataset.drawn)) > 0);
    await click("[data-action=closeModal][data-arg=channelsModal]");
  }],

  ["line card without a model: a generic title and the host shown once", async ({ LINE, eq, ok, text }) => {
    eq("title", await text(`${LINE} [data-f=title]`), "Cable modem");
    const meta = (await text(`${LINE} [data-f=meta]`)).trim();
    ok(`technology then host once: ${meta}`, /^DOCSIS · [\d.]+$/.test(meta));
  }],

  ["channels dialog: per-channel power and SNR charts with the server's good zone and grades, list, dark and light", async ({ LINE, click, count, dialogShot, eq, modalOpen, ok, page, setTheme, shown, tabStaysIn, text, waitCount }) => {
    await setTheme("dark");
    await click(`${LINE} [data-action=openChannelsModal]`);
    await waitCount("#channelsPowerChart svg rect[data-tip]", 3);
    eq("snr bars", await count("#channelsSnrChart svg rect[data-tip]"), 3);
    eq("upstream chart", await shown("#channelsUpSection"), true);
    ok("good zone drawn from the ranges", (await count("#channelsPowerChart svg rect[fill='var(--good)']")) === 1);
    ok("zone named from the server's range", /good .+ \(shaded\)/.test(await text("#channelsPowerNote")));
    const fills = await page.$$eval("#channelsSnrChart svg rect[data-tip]", (els) => els.map((e) => e.getAttribute("fill")));
    ok(`the channel the server grades down is not drawn as good: ${fills}`, fills.some((f) => f !== "var(--s1)") && fills.some((f) => f === "var(--s1)"));
    eq("keyboard focus starts on close", await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Close");
    await dialogShot("channels-dark");
    await setTheme("light");
    await dialogShot("channels-light");
    await setTheme("dark");
    await tabStaysIn("channelsModal");
    await page.click("#channelsModal .modal-t");
    await page.keyboard.press("Escape");
    eq("escape closes it", await modalOpen("channelsModal"), false);
  }],

  ["channels dialog History tab: heatmap with the grade legend", async ({ LINE, click, dialogShot, ok, text, until }) => {
    await click(`${LINE} [data-action=openChannelsModal]`);
    await click("#btnChannelsHistory");
    await until(() => Number(document.getElementById("chHistCanvas").dataset.rows) >= 2);
    ok("legend", /good.*warning.*critical.*no reading/.test(await text("#channelsHistoryPane .scale-legend")));
    await dialogShot("channels-history-dark");
    await click("[data-action=closeModal][data-arg=channelsModal]");
  }],

  ["metric help popovers: DOCSIS SNR (with its thresholds), an outside click and a second click close them", async ({ click, eq, ok, openHelp, page, popoverText, until }) => {
    const snr = await openHelp("docsis.snr");
    ok(`snr text: ${snr}`, /33 dB/.test(snr) && /30 dB/.test(snr));
    await page.evaluate(() => document.getElementById("statusPillText").click());
    eq("outside click closes", await popoverText(), null);

    await click('.info[data-help="docsis.uncorr"]');
    await until(() => !document.getElementById("helpPopover").hidden);
    await click('.info[data-help="docsis.uncorr"]');
    eq("second click closes", await popoverText(), null);
  }],

  ["the channels dialog as a bottom sheet on a phone: it fits the width and scrolls inside", async ({ LINE, click, dialogShot, eq, noSideScroll, ok, page, phone, setTheme, waitCount }) => {
    await phone(async () => {
      await setTheme("dark");
      await click(`${LINE} [data-action=openChannelsModal]`);
      await waitCount("#channelsPowerChart svg rect[data-tip]", 3);
      eq("channels fit", await noSideScroll("channelsModal"), true);
      ok("channel cards, not a wide table", await page.$eval("#channelsTableBody tr", (tr) => getComputedStyle(tr).display === "grid"));
      await dialogShot("channels-phone");
      await click("[data-action=closeModal][data-arg=channelsModal]");
      await setTheme(null);
    });
  }],

  ["a line of a technology without an adapter: the card's common part only", async ({ LINE, count, eq, ok, text, waitCount, waitText }) => {
    await waitCount("[data-panel=line] .panel", 1);
    await waitText(`${LINE} [data-f=badge]`, /Warning/);
    eq("no technology widgets", await count(`${LINE} .gauge`), 0);
    eq("no channels view", await count(`${LINE} [data-action=openChannelsModal]`), 0);
    ok("says why", /no detail view for Fiber lines/.test(await text(LINE)));
    ok("setup still there", (await count(`${LINE} [data-action=openDeviceSetup]`)) === 1);
  }, { variants: ["unknownline"] }],

  ["unknown line technology: generic title, host once", async ({ LINE, eq, ok, text }) => {
    eq("title", await text(`${LINE} [data-f=title]`), "Line");
    ok("meta", /^Fiber · [\d.]+$/.test((await text(`${LINE} [data-f=meta]`)).trim()));
  }, { variants: ["unknownline"] }],

  ["every range draws the channel history (a heatmap of every channel and the detail chart of one), on a desktop and a phone, in every theme", async ({ LINE, atEveryRange, axisOk, chartAxis, chooseRange, click, eq, everyLook, noSideScroll, ok, page, phone, rangeState, settled, until }) => {
    const open = async () => {
      await click(`${LINE} [data-action=openChannelsModal]`);
      await click("#btnChannelsHistory");
      await until(() => Number(document.getElementById("chHistCanvas").dataset.rows) >= 2);
      await page.evaluate(() => { // a click on the first channel's row draws its detail chart
        const c = document.getElementById("chHistCanvas");
        const r = c.getBoundingClientRect();
        c.dispatchEvent(new MouseEvent("click", { clientX: r.left + 100, clientY: r.top + 18 + 3, bubbles: true }));
      });
      await until(() => !!Chart.getChart(document.getElementById("chHistDetailChart")));
    };
    const check = (where) => async (id, s) => {
      const what = `${where} ${id}`;
      ok(`${what}: the detail chart is drawn`, !!s);
      const points = s.sets[0].n;
      ok(`${what}: the detail chart has ${points} points (at most 840: a long range is thinned)`, points >= 3 && points <= 840);
      axisOk(what, id, s);
      const heat = await page.$eval("#chHistCanvas", (c) => ({ rows: Number(c.dataset.rows), drawn: Number(c.dataset.drawn), w: c.width }));
      ok(`${what}: the heatmap holds ${heat.drawn} readings in ${heat.rows} rows`, heat.rows >= 2 && heat.drawn >= heat.rows);
      ok(`${what}: nothing sticks out sideways`, await noSideScroll("channelsModal"));
    };
    await open();
    eq("the line keeps 180 days: 24 h to All", (await rangeState("#chHistRanges")).ids.join(), "24h,7d,30d,90d,all");
    await atEveryRange("#chHistRanges", "#chHistDetailChart", check("desktop"));
    await everyLook(async (look) => {
      for (const id of ["7d", "all"]) {
        await chooseRange("#chHistRanges", id);
        await settled();
        await check(look)(id, await chartAxis("#chHistDetailChart"));
      }
    });
    await click("[data-action=closeModal][data-arg=channelsModal]");
    await phone(async () => {
      await open();
      await atEveryRange("#chHistRanges", "#chHistDetailChart", check("phone"));
      await click("[data-action=closeModal][data-arg=channelsModal]");
    });
  }, { variants: ["longhistory"] }],

  ["All starts at the oldest stored reading when that is younger than the retention: the channel history (42 days of data, then 400)", async ({ LINE, chartAxis, chooseRange, click, eq, forgetRequests, ok, page, rangeState, requestsTo, settled, until, youngData }) => {
    for (const { days, kind, age } of [{ days: 42, kind: "date", age: 42 }, { days: 400, kind: "month", age: 180 }]) { // (the line keeps readings for 180 days: the server answers no more)
      await youngData(days);
      await click(`${LINE} [data-action=openChannelsModal]`);
      await click("#btnChannelsHistory");
      await until(() => Number(document.getElementById("chHistCanvas").dataset.rows) >= 2);
      await page.evaluate(() => { // a click on the first channel's row draws its detail chart
        const c = document.getElementById("chHistCanvas");
        const r = c.getBoundingClientRect();
        c.dispatchEvent(new MouseEvent("click", { clientX: r.left + 100, clientY: r.top + 18 + 3, bubbles: true }));
      });
      await until(() => !!Chart.getChart(document.getElementById("chHistDetailChart")));
      eq(`${days}: the retention is a year`, (await rangeState("#chHistRanges")).words.at(-1), "All · 1 y");
      forgetRequests();
      await chooseRange("#chHistRanges", "all");
      await settled();
      eq(`${days}: All asks the channel history once (${requestsTo(/\/channels\/history\?/).join(" ")})`, requestsTo(/\/channels\/history\?/).length, 1);
      const s = await chartAxis("#chHistDetailChart");
      const from = await page.$eval("#chHistCanvas", (c) => (Date.now() / 1000 - Number(c.dataset.from)) / 86400);
      ok(`${days}: the heatmap starts ${from.toFixed(1)} days back (about ${age}, not 365)`, Math.abs(from - age) < 1.5);
      const x = await page.evaluate(() => (Date.now() - Chart.getChart(document.getElementById("chHistDetailChart")).scales.x.min) / 86400000);
      ok(`${days}: so does the detail chart (${x.toFixed(1)} days back)`, Math.abs(x - age) < 1.5);
      ok(`${days}: the labels are ${kind}s (${s.labels.join(" | ")})`, s.labels.length >= 2 && [s.labels[0], s.labels.at(-1)].every((l) => (kind === "date" ? /^[A-Z][a-z]{2} \d{1,2}$/ : /^[A-Z][a-z]{2} \d{4}$/).test(l)));
      ok(`${days}: no label twice in a row`, s.labels.every((l, i) => i === 0 || l !== s.labels[i - 1]));
      ok(`${days}: the status says where it starts`, /^all stored, since [A-Z][a-z]{2} \d{1,2}/.test(await page.$eval("#chHistStatus", (e) => e.textContent)));
      await click("[data-action=closeModal][data-arg=channelsModal]");
    }
    await youngData(null);
  }, { variants: ["longhistory"] }],
];
