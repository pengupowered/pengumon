// The hop matrix: a row per target, its sparklines, the sparkline cost, and the hop help popovers.
import { ALL_VARIANTS } from "../lib.mjs";

export const subject = "panel:matrix";
export const variants = ["full"];

export const steps = [
  ["hop details has one row per target (3 ICMP + the 2 default DNS probes) and updates live", async ({ text, until, waitCount, waitText }) => {
    await waitCount("#hopMatrixBody tr", 5);
    await waitText("#hopMatrixBody tr:first-child td:first-child [data-f=name]", /Local Router/);
    await waitText("#hopMatrixBody tr:first-child [data-f=last]", /^\d+(\.\d+)? ms$/);
    const before = await text("#hopMatrixBody tr:first-child [data-f=total]");
    await until((b) => document.querySelector("#hopMatrixBody tr:first-child [data-f=total]").textContent !== b, before);
  }, { variants: ALL_VARIANTS }],

  ["hop matrix rows have a sparkline each, drawn and redrawn when a message arrives", async ({ canvasSig, count, eq, inkedPixels, ok, until, waitCount }) => {
    await waitCount("#hopMatrixBody tr canvas.sparkline", 5);
    const sel = "#hopMatrixBody tr:first-child canvas.sparkline";
    await until((s) => document.querySelector(s)?.dataset.sig, sel);
    for (let i = 1; i <= 5; i++) ok(`row ${i} sparkline has ink`, (await inkedPixels(`#hopMatrixBody tr:nth-child(${i}) canvas.sparkline`)) > 0);
    const before = await canvasSig(sel);
    await until((s, b) => document.querySelector(s).dataset.sig !== b, sel, before);
    const rows = await count("#hopMatrixBody tr");
    eq("rows are not recreated", rows, 5);
  }, { variants: ALL_VARIANTS }],

  ["Last says LOST when the newest probe got no answer, though last_ms still holds the previous answer", async ({ page, waitText }) => {
    await page.evaluate(() => { window.__smokeMutate = (d) => d.targets.forEach((t) => { if (t.target_id === "isp_gateway") { t.recent_latencies = [...(t.recent_latencies || []).slice(-5), null]; t.last_ms = 9; } }); });
    await waitText("#hopMatrixBody tr:nth-child(2) [data-f=last]", /^LOST$/);
    await page.evaluate(() => { window.__smokeMutate = null; });
    await waitText("#hopMatrixBody tr:nth-child(2) [data-f=last]", /^\d+(\.\d+)? ms$/);
  }],

  ["sparkline marks losses as red ticks and breaks the line at them", async ({ moduleUrl, ok, page }) => {
    const red = await page.evaluate(async (src) => {
      const { createSparkline, drawSparkline } = await import(src);
      const canvas = createSparkline("test");
      drawSparkline(canvas, [5, 6, null, 7, 5, null, null, 6], "#58a6ff");
      const d = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i] > 170 && d[i + 1] < 120 && d[i + 2] < 120 && d[i + 3] > 200) n++;
      return n;
    }, await moduleUrl("sparkline.js"));
    ok(`red loss ticks drawn (${red} red pixels)`, red > 10);
  }, { variants: ALL_VARIANTS }],

  ["sparkline update cost per message stays small", async ({ report, moduleUrl, ok, page }) => {
    await page.waitForFunction(() => (window.__pengumonPerf?.matrixMs.length ?? 0) >= 20, { timeout: 40000 }); // about one message a second
    const perf = await page.evaluate(() => window.__pengumonPerf);
    const sorted = perf.matrixMs.slice(3).sort((a, b) => a - b); // the first messages build the rows
    const mean = sorted.reduce((a, b) => a + b, 0) / sorted.length;
    // the drawing alone: 5 sparklines of 60 changing samples, 300 rounds
    const draw = await page.evaluate(async (src) => {
      const { createSparkline, drawSparkline } = await import(src);
      const canvases = Array.from({ length: 5 }, () => createSparkline("bench"));
      const t0 = performance.now();
      for (let r = 0; r < 300; r++) canvases.forEach((c, k) => drawSparkline(c, Array.from({ length: 60 }, (_, i) => ((i + r + k) % 9 === 0 ? null : 5 + ((i * r) % 7))), "#58a6ff"));
      return (performance.now() - t0) / 300;
    }, await moduleUrl("sparkline.js"));
    report.perf = { draw_5_sparklines_ms: +draw.toFixed(3), targets: perf.targets, messages: sorted.length, mean_ms: +mean.toFixed(3), p95_ms: +sorted[Math.floor(sorted.length * 0.95)].toFixed(3), max_ms: +sorted[sorted.length - 1].toFixed(3) };
    ok(`update of ${perf.targets} rows took ${mean.toFixed(2)} ms on average`, mean < 25);
  }],

  ["metric help popovers: hop jitter; Escape closes and removes aria-describedby", async ({ eq, ok, openHelp, page, popoverText }) => {
    const jitter = await openHelp("hop.jitter");
    ok("jitter text", jitter.length > 40 && /jitter/i.test(jitter) && /ms/.test(jitter));
    eq("button expanded", await page.$eval('.info[data-help="hop.jitter"]', (b) => b.getAttribute("aria-expanded") + b.getAttribute("aria-describedby")), "truehelpPopover");
    eq("popover role", await page.$eval("#helpPopover", (e) => e.getAttribute("role")), "tooltip");
    await page.keyboard.press("Escape");
    eq("escape closes", await popoverText(), null);
    eq("describedby removed", await page.$eval('.info[data-help="hop.jitter"]', (b) => b.hasAttribute("aria-describedby")), false);
    await new Promise((r) => setTimeout(r, 300)); // Escape gives focus back to the button, which may scroll the page (and a scroll closes popovers)
  }],

  ["rows are named as Path health names them; min, avg and max cover the last 60 probes; help sits in the column headers", async ({ count, eq, ok, page, until }) => {
    await until(() => /ms$/.test(document.querySelector("#hopMatrixBody tr:first-child [data-f=rangeText]")?.textContent ?? ""));
    const labels = await page.$$eval("#hopMatrixBody [data-f=label]", (els) => els.map((e) => e.textContent).join(","));
    eq("short names", labels, "Router,Line,Internet,DNS lookup 1,DNS lookup 2");
    const rows = await page.evaluate(() => {
      return [...document.querySelectorAll("#hopMatrixBody tr")].map((tr) => tr.querySelector("[data-f=rangeText]").textContent);
    });
    for (const r of rows) {
      const m = r.match(/^([\d.]+) · ([\d.]+) · ([\d.]+) ms$/);
      ok(`min ≤ avg ≤ max: ${r}`, m && +m[1] <= +m[2] && +m[2] <= +m[3]);
    }
    // the server's min_ms / max_ms are since start: a spike long ago must not show as the current range
    await page.evaluate(() => { window.__smokeMutate = (d) => { d.targets[0].max_ms = 999; d.targets[0].min_ms = 0.001; }; });
    await new Promise((r) => setTimeout(r, 2500));
    const shown = await page.$eval("#hopMatrixBody tr:first-child [data-f=rangeText]", (e) => e.textContent);
    await page.evaluate(() => { window.__smokeMutate = null; });
    ok(`the since-start extremes are not shown: ${shown}`, !/999|0\.001/.test(shown));
    eq("help buttons in the column headers", await count(".hops thead .info"), 4);
    eq("the card header's copies are hidden on a wide card", await page.$eval(".matrix-panel .narrow-help", (e) => getComputedStyle(e).display), "none");
  }],

  ["columns hold still when a figure changes width (LOST, a large jitter), and a name that says its address does not repeat it", async ({ eq, ok, page, until }) => {
    await until(() => /ms$/.test(document.querySelector("#hopMatrixBody tr:first-child [data-f=rangeText]")?.textContent ?? ""));
    const edges = () => page.$$eval(".hops thead th", (els) => els.map((e) => Math.round(e.getBoundingClientRect().left)).join(","));
    const before = await edges();
    await page.evaluate(() => { window.__smokeMutate = (d) => { d.targets[0].recent_latencies = [...(d.targets[0].recent_latencies || []), null]; d.targets[1].jitter_ms = 12345.678; d.targets[2].sent = 987654321; }; });
    await until(() => document.querySelector("#hopMatrixBody tr:first-child [data-f=last]").textContent === "LOST");
    await until(() => /12345/.test(document.querySelector("#hopMatrixBody tr:nth-child(2) [data-f=jitter]").textContent));
    const during = await edges();
    await page.evaluate(() => { window.__smokeMutate = null; });
    eq("column edges unchanged", during, before);
    const rows = await page.$$eval("#hopMatrixBody tr", (trs) => trs.map((tr) => ({ name: tr.querySelector("[data-f=name]").textContent, addr: tr.querySelector("[data-f=addr]").textContent, hidden: tr.querySelector("[data-f=addrWrap]").hidden })));
    for (const r of rows) eq(`${r.name}: address ${r.hidden ? "hidden" : "shown"}`, r.hidden, r.name.includes(r.addr.split(/[\s@]/)[0]));
    const says = await page.evaluate(async () => {
      const main = [...document.scripts].find((s) => s.type === "module").src;
      const { nameSaysAddress } = await import(main.replace(/js\/main\.js$/, "panels/matrix/panel.js"));
      return [["Public: Cloudflare Anycast (1.1.1.1)", "1.1.1.1"], ["DNS Lookup (google.com - System DNS)", "google.com @system"],
        ["Hop 1: Example Router (LAN)", "192.168.1.1"], ["DNS Lookup (example.org via 9.9.9.9)", "example.net @9.9.9.9"]].map(([n, a]) => nameSaysAddress(n, a)).join(",");
    });
    eq("names that carry the address or the domain", says, "true,true,false,false");
  }],
];
