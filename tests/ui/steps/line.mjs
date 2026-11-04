// The line card (DOCSIS modem in the full variant) and its Channels dialog.

export const subject = "panel:line";
export const variants = ["full"];

export const steps = [
  ["line card: gauges from the server's ranges, errors in plain words", async ({ LINE, count, eq, ok, page, text, waitText }) => {
    await waitText(`${LINE} [data-f=badge]`, /Warning/);
    await waitText(`${LINE} [data-f=g_ds_power] [data-f=val]`, /^1\.3$/);
    ok("gauge zones drawn", (await count(`${LINE} [data-f=g_ds_power] .g-zone`)) >= 1);
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

  ["channels modal History tab: heatmap canvas, channel detail chart, range and metric", async ({ LINE, click, eq, hasChart, ok, outDir, page, path, shown, until }) => {
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
    await click('#chHistRanges button[data-arg="604800"]');
    await until(() => document.querySelector("#chHistRanges button.active")?.dataset.arg === "604800" && /readings/.test(document.getElementById("chHistStatus").textContent));
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
];
