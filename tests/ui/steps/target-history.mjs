// The target history dialog (opened from a hop row or a path node).
import { ALL_VARIANTS } from "../lib.mjs";

export const subject = "dialog:target-history";
export const variants = ["full"];

export const steps = [
  ["clicking a target row opens its history modal with data; the range buttons refetch", async ({ click, eq, ok, outDir, page, path, text, until }) => {
    await click("#hopMatrixBody tr:first-child");
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    ok("title", /Local Router/.test(await text("#targetHistTitle")));
    ok("a single target has no switcher", await page.$eval("#targetHistSwitch", (e) => e.hidden));
    const points = (id) => page.evaluate((i) => Chart.getChart(document.getElementById(i))?.data.datasets[0]?.data.length ?? 0, id);
    await until(() => (Chart.getChart(document.getElementById("targetHistLatency"))?.data.datasets[0]?.data.length ?? 0) > 20);
    ok("loss chart has the same points", (await points("targetHistLoss")) > 20);
    ok("1 h is active", await page.$eval('#targetHistRanges button[data-arg="3600"]', (b) => b.classList.contains("active")));
    await click('#targetHistRanges button[data-arg="86400"]');
    await until(() => document.querySelector('#targetHistRanges button[data-arg="86400"]').classList.contains("active"));
    await until(() => /points, one per 10 min/.test(document.getElementById("targetHistStatus").textContent));
    await page.screenshot({ path: path.join(outDir, "target-history.png") });
    await click("[data-action=closeModal][data-arg=targetHistModal]");
    eq("closed", await page.$eval("#targetHistModal", (m) => m.classList.contains("active")), false);
  }, { variants: ALL_VARIANTS }],

  ["target detail dialog: stats, latency chart with band, loss, 24 h strip; five ranges; Live refreshes; Escape closes", async ({ click, count, eq, ok, page, shots, until }) => {
    await click('#pathChain .node[data-node^="Line"]');
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    eq("five ranges", await count("#targetHistRanges button"), 5);
    eq("ranges", await page.$$eval("#targetHistRanges button", (b) => b.map((x) => x.textContent).join(",")), "Live,15 m,1 h,24 h,7 d");
    await until(() => document.querySelectorAll("#targetHistStrip .strip i").length === 96);
    await until(() => /ms/.test(document.querySelectorAll("#targetHistStats .val")[1].textContent));
    eq("five figures", await count("#targetHistStats .stat"), 5);
    ok("a status badge", /Healthy|Degraded|Down|Busy|No data/.test(await page.$eval("#targetHistStats .val", (e) => e.textContent)));
    ok("the strip has an accessible name", /available over 24 hours/.test(await page.$eval("#targetHistStrip .strip", (e) => e.getAttribute("aria-label"))));
    eq("line first", await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets[0].id), "line");
    const bars = await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLoss")).data.datasets[0].data.map((p) => p.y));
    ok(`a bar only where probes were lost (${bars.filter((y) => y > 0).length} of ${bars.length})`, bars.length > 20 && !bars.includes(0));
    for (const [sec, word] of [[300, /one per 2 s/], [900, /one per 10 s/], [604800, /one per 60 min/]]) {
      await click(`#targetHistRanges button[data-arg="${sec}"]`);
      await until((w) => new RegExp(w).test(document.getElementById("targetHistStatus").textContent), word.source);
      eq(`pressed ${sec}`, await page.$eval(`#targetHistRanges button[data-arg="${sec}"]`, (b) => b.getAttribute("aria-pressed")), "true");
    }
    await click('#targetHistRanges button[data-arg="300"]');
    await shots("target-detail");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("targetHistModal").classList.contains("active"));
  }],

  ["the Internet node opens its three servers together: All draws a line each and the median, the switcher shows one alone as a single target; keyboard and phone width", async ({ click, eq, ok, page, shots, text, until, waitCount, noSideScroll }) => {
    await waitCount("#pathChain .node[data-node]", 6);
    await click('#pathChain .node[data-node^="Internet"]');
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    eq("the title says what the node says", await text("#targetHistTitle"), "Internet · median of 3 public servers");
    // the number on the node is in the dialog: opened on Live, the node's own figure shown as it is now, the window said
    eq("a path node opens on Live", await page.$eval('#targetHistRanges button[data-arg="300"]', (b) => b.getAttribute("aria-pressed")), "true");
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
    eq("each member in its own colour, the switcher's key the same", keys.join(), sets.slice(0, 3).map((d) => d.color).join());
    ok("three distinct colours", new Set(keys).size === 3);
    eq("loss per member", await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLoss")).data.datasets.length), 3);
    await until(() => document.querySelectorAll("#targetHistStrip .tgt-strips .strip").length === 3 || document.querySelectorAll("#targetHistStrip .tgt-strips > .tgt-sn").length === 3);
    ok("the figures say the group's rules", /\(median\)/.test(await text("#targetHistStats")));
    eq("All is pressed", await page.$eval('#targetHistSwitch button[data-arg="all"]', (b) => b.getAttribute("aria-pressed")), "true");
    await shots("internet-history");
    await page.setViewport({ width: 390, height: 844 });
    await new Promise((r) => setTimeout(r, 400));
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
    // ten targets and eight series colours: the two DNS lookups (ninth and tenth) still differ from each other, not grey
    const colours = [];
    for (const row of [9, 10]) {
      const name = await page.$eval(`#hopMatrixBody tr:nth-child(${row}) [data-f=name]`, (e) => e.textContent);
      ok(`row ${row} is a DNS lookup (${name})`, /DNS/i.test(name));
      await click(`#hopMatrixBody tr:nth-child(${row})`);
      await until((n) => document.getElementById("targetHistModal")?.classList.contains("active") && document.getElementById("targetHistTitle").textContent === n, name);
      // the colour the dialog draws this target in is the one the page gives it (util.js getTargetInfo)
      await until(() => /points|No samples/.test(document.getElementById("targetHistStatus").textContent));
      colours.push(await page.evaluate(() => Chart.getChart(document.getElementById("targetHistLatency")).data.datasets[0].borderColor));
      await click("[data-action=closeModal][data-arg=targetHistModal]");
      await until(() => !document.getElementById("targetHistModal").classList.contains("active"));
    }
    const grey = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--ink-3").trim());
    ok(`the DNS lookups have distinct series colours (${colours}, grey ${grey})`, colours[0] !== colours[1] && !colours.includes(grey));
  }, { variants: ["longpath"] }],

  ["a folded run of ISP hops opens its hops together; a single public server still opens alone", async ({ click, eq, ok, page, text, until }) => {
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
];
