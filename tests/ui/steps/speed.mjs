// The Speed card: the seeded speed tests (tests/test_ui_smoke.py seed_speed_tests: a 300/20 Mbps plan, results from 3 h to
// 20 d old), the confirmation before Run now, and the card in other states and themes. No step starts a test: Run now's POST is
// answered by the page itself (stubFetch), and the app's own next test is 12 h away.

export const subject = "panel:speed";
export const variants = ["full"];

const CARD = "[data-panel=speed]";
const BODY = `${CARD} .panel-b`;
let speedReal = null; // the app's own answer, read once, which the states below change a little

// What sticks out of the card at the page's width: the page itself, or any part of the card past its right edge.
const speedOverflow = (page) => page.evaluate((sel) => {
  const card = document.querySelector(`${sel} .panel`).getBoundingClientRect();
  const out = [];
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1) out.push(`page ${document.documentElement.scrollWidth} > ${document.documentElement.clientWidth}`);
  document.querySelectorAll(`${sel} .panel-b *`).forEach((e) => {
    const r = e.getBoundingClientRect();
    if (r.width && r.right > card.right + 1) out.push(`${e.tagName.toLowerCase()}.${String(e.className.baseVal ?? e.className).split(" ")[0]} right ${Math.round(r.right)} > ${Math.round(card.right)}`);
  });
  return out;
}, CARD);

// On a phone the card is on the Network tab: opened the way a link to it is (the hash), and waited for.
const speedTab = async (page, until) => {
  await page.evaluate(() => { location.hash = "#network"; });
  await until(() => !document.querySelector("[data-panel=speed]").hidden);
};

// The app's answer with `patch` applied (a function gets a copy and changes it).
const speedAnswer = (patch) => {
  const body = JSON.parse(JSON.stringify(speedReal));
  patch(body);
  return body;
};

export const steps = [
  ["speed card: the last result against the plan, its delay grade, the next test and this month's data, from the seeded tests", async ({ count, eq, moduleUrl, ok, page, text, until, waitText }) => {
    // the plan ("Your line") is set through the settings API for this step only: with it set the line load is known, which
    // other steps (the incident inspector's "Line load unknown") rely on not being
    const plan = (values) => page.evaluate(async (v) => {
      const state = await (await fetch("/api/settings")).json();
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_revision: state.revision, values: v, lists: {}, secrets: {} }) });
      if (!res.ok) throw new Error(`PUT ${res.status}`);
    }, values);
    await plan({ "line.down_mbps": 300, "line.up_mbps": 20 });
    try {
      await page.evaluate(async (u) => (await import(u)).refresh(), await moduleUrl("../panels/speed/model.js"));
      await waitText(`${BODY} [data-item=down] [data-v=plan]`, /^79 % of 300 Mbps$/);
      speedReal = await page.evaluate(async () => (await fetch("/api/speedtests?hours=720&limit=500")).json());
      eq("the upload", await text(`${BODY} [data-item=up] [data-v=num]`), "18.7");
      eq("download against the plan", await text(`${BODY} [data-item=down] [data-v=plan]`), "79 % of 300 Mbps");
      eq("upload against the plan", await text(`${BODY} [data-item=up] [data-v=plan]`), "94 % of 20 Mbps");
      eq("when", await text(`${BODY} [data-v=when]`), "3 h ago");
      ok("the full time is on hover", /20\d\d/.test(await page.$eval(`${BODY} [data-v=when]`, (e) => e.title)));
      eq("where from", await text(`${BODY} [data-v=server]`), "Cloudflare (EXA)");
      eq("the delay grade is a word with its icon", await page.$eval(`${BODY} [data-item=bufferbloat] [data-st]`, (e) => `${e.dataset.st}|${e.textContent.trim()}|${!!e.querySelector("svg")}`), "warn|Grade B|true");
      eq("the delay in words", await text(`${BODY} [data-item=bufferbloat] [data-v=delay]`), "idle 12 ms, under load +49 ms");
      eq("a whole result has no status line", await count(`${BODY} .spd-why`), 0);
      eq("no failure is shown above a good newest test", await count(`${BODY} .spd-problem`), 0);
      ok("the next test", /^Next test in 1[12] h$/.test(await text(`${BODY} [data-v=next]`)));
      const used = await text(`${BODY} [data-v=usage]`);
      ok(`the data use against the limit (${used})`, /^\d[\d,]* MB of 2,000 MB this month$/.test(used));
      // counted again here from the tests' own bytes (the server's clock is UTC in the container)
      const counted = await page.evaluate(async () => {
        const body = await (await fetch("/api/speedtests?hours=720&limit=500")).json();
        const d = new Date();
        const from = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / 1000;
        return body.tests.filter((t) => t.ts >= from).reduce((a, t) => a + (t.bytes_down || 0) + (t.bytes_up || 0), 0) / 1e6;
      });
      eq("this month's MB match the tests' bytes", Number((used.match(/^[\d,]+/)[0]).replace(/,/g, "")), Math.round(counted));
      eq("the header names the provider", await text("#speedSummary"), "Cloudflare");
      eq("the newest test was good", await page.$eval("#speedCard", (c) => c.dataset.status), "good");
      await until(() => { const c = document.querySelector("[data-panel=speed] canvas"); return !!(c && Chart.getChart(c)); });
      const sets = () => page.evaluate(() => Chart.getChart(document.querySelector("[data-panel=speed] canvas")).data.datasets.map((d) => `${d.label}:${d.data.length}`).join());
      eq("7 days: both lines, the plan, a failed and a skipped mark", await sets(), "Download plan:2,Upload plan:2,Download:3,Upload:2,Failed:1,Skipped:1");
      eq("the chart has ink", (await page.$eval(`${CARD} canvas`, (c) => c.getContext("2d").getImageData(0, 0, c.width, c.height).data.some((v, i) => i % 4 === 3 && v > 0))), true);
      await page.$eval(`${CARD} [data-action=speedRange][data-arg="30"]`, (b) => b.click());
      await until(() => document.querySelector("[data-panel=speed] [data-action=speedRange][data-arg='30']").getAttribute("aria-pressed") === "true");
      eq("30 days: the older tests are in", await sets(), "Download plan:2,Upload plan:2,Download:5,Upload:4,Failed:1,Skipped:1");
      eq("7 d is no longer pressed", await page.$eval(`${CARD} [data-action=speedRange][data-arg="7"]`, (b) => b.getAttribute("aria-pressed")), "false");
      ok("the chart says what it shows", /30 days, 6 tests/.test(await page.$eval(`${CARD} canvas`, (c) => c.getAttribute("aria-label"))) || /30 days, 7 tests/.test(await page.$eval(`${CARD} canvas`, (c) => c.getAttribute("aria-label"))));
      await page.$eval(`${CARD} [data-action=speedRange][data-arg="7"]`, (b) => b.click());
    } finally {
      await plan({ "line.down_mbps": null, "line.up_mbps": null });
    }
  }],

  ["speed card: Run now asks first and says the data it uses; Cancel sends nothing; Run test sends one request and shows progress", async ({ click, count, eq, page, path, outDir, stubFetch, text, until, waitText }) => {
    // the page answers the POST itself (no test starts)
    await stubFetch({ "POST /api/speedtests/run": { status: 202, body: { started: true } } });
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^236$/);
    const run = `${BODY} [data-action=speedAskRun]`;
    eq("Run now is ready", await page.$eval(run, (b) => b.disabled), false);
    eq("no question yet", await count(`${BODY} .spd-confirm`), 0);
    await page.focus(run); // a person's click leaves the focus on the button; the step's click() does not
    await click(run);
    await until(() => document.querySelector("[data-panel=speed] .spd-confirm"));
    eq("the question names the data", await text(`${BODY} .spd-confirm [data-v=data]`), "Uses up to 25 MB down and 10 MB up of data.");
    eq("the question is an alert dialog with a name", await page.$eval(`${BODY} .spd-confirm`, (q) => `${q.getAttribute("role")}|${q.getAttribute("aria-label")}`), "alertdialog|Run a speed test now?");
    eq("focus moved to the button that runs it", await page.evaluate(() => document.activeElement.dataset.action), "speedRunConfirm");
    await page.screenshot({ path: path.join(outDir, "speed-confirm.png") });
    await click(`${BODY} [data-action=speedRunCancel]`);
    await until(() => !document.querySelector("[data-panel=speed] .spd-confirm"));
    eq("focus is back on Run now", await page.evaluate(() => document.activeElement.dataset.action), "speedAskRun");
    eq("Cancel sent nothing", await page.evaluate(() => window.__fetched.filter(([k]) => k === "POST /api/speedtests/run").length), 0);
    await click(run);
    await until(() => document.querySelector("[data-panel=speed] [data-action=speedRunConfirm]"));
    await click(`${BODY} [data-action=speedRunConfirm]`);
    await until(() => window.__stubbed.some(([k]) => k === "POST /api/speedtests/run"));
    eq("one request was sent", await page.evaluate(() => window.__fetched.filter(([k]) => k === "POST /api/speedtests/run").length), 1);
    eq("the question is gone", await count(`${BODY} .spd-confirm`), 0);
  }],

  ["speed card: while a test runs the card says so, Run now is off with the reason, and the new result replaces it when it ends", async ({ count, eq, page, stubFetch, text, waitText }) => {
    const running = speedAnswer((b) => { b.running = true; });
    await stubFetch({ "GET /api/speedtests": { body: running } });
    await waitText(`${BODY} .spd-progress`, /Testing download, then upload/);
    const run = `${BODY} [data-action=speedAskRun]`;
    eq("Run now is off", await page.$eval(run, (b) => b.disabled), true);
    eq("and says why", await page.$eval(run, (b) => b.title), "A speed test is running.");
    eq("progress is announced politely", await page.$eval(`${BODY} .spd-progress`, (p) => p.getAttribute("role")), "status");
    eq("the next-test line says it", await text(`${BODY} [data-v=next]`), "Test running now");
    // it ends: the next poll (the page polls every 3 s while it runs) brings the finished test, with new figures
    const done = speedAnswer((b) => { b.tests[0] = { ...b.tests[0], id: 99, ts: Date.now() / 1000 - 5, down_mbps: 250.5, up_mbps: 19.5, headline_down_mbps: 250.5, headline_up_mbps: 19.5, trigger: "manual" }; });
    await page.evaluate((body) => {
      const real = window.fetch;
      window.fetch = (url, opts) => (String(url).includes("/api/speedtests?") ? Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })) : real(url, opts));
    }, done);
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^251$|^250$/);
    eq("the progress is gone", await count(`${BODY} .spd-progress`), 0);
    eq("Run now is back", await page.$eval(run, (b) => b.disabled), false);
    eq("the new figure is shown against the plan", await text(`${BODY} [data-item=up] [data-v=plan]`), "98 % of 20 Mbps");
  }],

  ["speed card: a refused start is said in words (the server's reason), and a limit that is reached turns Run now off", async ({ click, eq, ok, page, stubFetch, until, waitText }) => {
    await stubFetch({ "POST /api/speedtests/run": { status: 409, body: { started: false, reason: "a speed test is already running" } } });
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^236$/);
    await click(`${BODY} [data-action=speedAskRun]`);
    await until(() => document.querySelector("[data-panel=speed] [data-action=speedRunConfirm]"));
    await click(`${BODY} [data-action=speedRunConfirm]`);
    await waitText(`${BODY} [data-v=notice]`, /did not start: a speed test is already running\./);
    eq("the notice has a status colour class and text", await page.$eval(`${BODY} [data-v=notice]`, (p) => p.classList.contains("st-warn")), true);
    const capped = speedAnswer((b) => { b.month_used_mb = 2000.4; });
    await stubFetch({ "GET /api/speedtests": { body: capped } });
    await waitText(`${BODY} [data-v=usage]`, /^2,000 MB of 2,000 MB this month$/);
    const run = `${BODY} [data-action=speedAskRun]`;
    eq("Run now is off at the limit", await page.$eval(run, (b) => b.disabled), true);
    ok("and says so", /data limit is reached/.test(await page.$eval(run, (b) => b.title)));
    eq("the use is flagged as a warning, in words", await page.$eval(`${BODY} [data-v=usage]`, (p) => p.classList.contains("st-warn")), true);
  }],

  ["speed card: off, with and without tests; no test yet; a failed or skipped newest test says why", async ({ click, count, eq, ok, page, stubFetch, text, until, waitText }) => {
    // off, nothing recorded: one helpful sentence, no chart, a link to the section in Settings
    await stubFetch({ "GET /api/speedtests": { body: speedAnswer((b) => { b.enabled = false; b.tests = []; b.next_run = null; }) } });
    await waitText(`${BODY} [data-v=empty]`, /^Speed tests are off\./);
    eq("no chart when there is nothing to draw", await count(`${CARD} canvas`), 0);
    eq("the header says off", await text("#speedSummary"), "off");
    eq("Run now is off, with the reason", await page.$eval(`${BODY} [data-action=speedAskRun]`, (b) => `${b.disabled}|${b.title}`), "true|The speed test is off. Turn it on in Settings first.");
    eq("the card says off", await text(`${BODY} [data-v=off]`), "Speed test is off");
    await click(`${BODY} [data-action=openSettingsAt][data-arg=speedtest]`);
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.getElementById("set-sec-speedtest"));
    await until(() => { const r = document.getElementById("set-sec-speedtest").getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0; });
    await click("#btnSettingsCancel");
    // on, nothing recorded yet
    await stubFetch({ "GET /api/speedtests": { body: speedAnswer((b) => { b.tests = []; }) } });
    await waitText(`${BODY} [data-v=empty]`, /^No speed test has run yet\./);
    eq("still no chart", await count(`${CARD} canvas`), 0);
    eq("Run now can be pressed", await page.$eval(`${BODY} [data-action=speedAskRun]`, (b) => b.disabled), false);
    // off, with old results: they stay, with the way to turn it on
    await stubFetch({ "GET /api/speedtests": { body: speedAnswer((b) => { b.enabled = false; b.next_run = null; }) } });
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^236$/);
    eq("old results with the off notice", await text(`${BODY} [data-v=off]`), "Speed test is off");
    eq("and the chart", await count(`${CARD} canvas`), 1);
    // the newest test failed: the reason, with an icon and a word; the last good figures stay under it
    const failed = speedAnswer((b) => { b.tests = b.tests.slice(1); });
    await stubFetch({ "GET /api/speedtests": { body: failed } });
    await waitText(`${BODY} [data-item=problem] [data-v=reason]`, /the provider refused the test \(rate limit\)/);
    eq("failed is a word with an icon", await page.$eval(`${BODY} [data-item=problem] [data-st]`, (e) => `${e.dataset.st}|${e.textContent.trim()}|${!!e.querySelector("svg")}`), "serious|Failed|true");
    eq("the card is not shown as healthy (and raises no tab dot: only warn and worse do)", await page.$eval("#speedCard", (c) => c.dataset.status), "unknown");
    ok("the last good figures are still there", /^\d+$/.test(await text(`${BODY} [data-item=down] [data-v=num]`)));
    // skipped
    const skipped = speedAnswer((b) => { b.tests = b.tests.slice(4); });
    await stubFetch({ "GET /api/speedtests": { body: skipped } });
    await waitText(`${BODY} [data-item=problem] [data-v=reason]`, /line busy/);
    eq("skipped is a word with an icon", await page.$eval(`${BODY} [data-item=problem] [data-st]`, (e) => `${e.dataset.st}|${e.textContent.trim()}|${!!e.querySelector("svg")}`), "unknown|Skipped|true");
    // a partial result: the figure it has, and why the rest is missing
    const partial = speedAnswer((b) => { b.tests = b.tests.slice(3); });
    await stubFetch({ "GET /api/speedtests": { body: partial } });
    await waitText(`${BODY} .spd-why [data-v=reason]`, /upload: timed out/);
    eq("partial is a word with an icon", await page.$eval(`${BODY} .spd-why [data-st]`, (e) => `${e.dataset.st}|${e.textContent.trim()}|${!!e.querySelector("svg")}`), "warn|Partial|true");
    eq("the missing upload is a dash", await text(`${BODY} [data-item=up] [data-v=num]`), "–");
    // a remark the server gives a result (the providers' confidence note) is shown under the figures
    const noted = speedAnswer((b) => { b.tests[0].note = "Too little data to measure a line this fast."; });
    await stubFetch({ "GET /api/speedtests": { body: noted } });
    await waitText(`${BODY} [data-v=remark]`, /^Too little data to measure a line this fast\.$/);
    // the answer cannot be read: the card says so
    await stubFetch({ "GET /api/speedtests": { status: 500, body: {} } });
    await waitText(`${BODY} .empty`, /Could not load the speed tests\./);
    await stubFetch(null);
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^236$/);
  }],

  ["speed card: a test the router measured shows the line's rate and says so, with the test's own figure under it; a test-only one says so", async ({ count, eq, ok, page, phone, showCard, stubFetch, text, until, waitText }) => {
    // the seeded tests, as the app answers: the one 51 h ago was measured by the router, the newest by the test alone
    const real = await page.evaluate(async () => (await fetch("/api/speedtests?hours=720&limit=500")).json());
    const by = Object.fromEntries(real.tests.filter((t) => t.status === "ok").map((t) => [Math.round((Date.now() / 1000 - t.ts) / 3600), t]));
    eq("the router-measured seed: the line's rate is the headline", [by[51].measured_by, by[51].headline_down_mbps, by[51].down_mbps].join("|"), "router|305|301.2");
    eq("the newest seed: the test alone", [by[3].measured_by, by[3].headline_down_mbps].join("|"), "test|236.4");
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^236$/);
    eq("a test-only result says so", await text(`${BODY} [data-v=by]`), "Test only");
    eq("and has no second figure", await count(`${BODY} [data-v=own]`), 0);
    // the newest test measured by the router, with the household's traffic on the line
    const note = "The test itself got download 300 of the 905 Mbps the line carried: the rest was other traffic on your network at the same time, not a slow line.";
    await stubFetch({ "GET /api/speedtests": { body: speedAnswer((b) => {
      b.tests[0] = { ...b.tests[0], down_mbps: 300.0, up_mbps: 18.1, bytes_down: 300000000, bytes_up: 18000000, wan_down_mbps: 905.0, wan_up_mbps: 19.4,
        headline_down_mbps: 905.0, headline_up_mbps: 19.4, down_measured_by: "router", up_measured_by: "router", measured_by: "router", traffic_note: note, note: null };
    }) } });
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^905$/);
    eq("the upload is the line's too", await text(`${BODY} [data-item=up] [data-v=num]`), "19.4");
    eq("how it was measured", await text(`${BODY} [data-v=by]`), "Line, measured by your router");
    ok("with the reason on hover", /read from your router/.test(await page.$eval(`${BODY} [data-v=by]`, (e) => e.title)));
    eq("the test's own download under it", await text(`${BODY} [data-item=down] [data-v=own]`), "test alone 300 Mbps");
    eq("and its upload", await text(`${BODY} [data-item=up] [data-v=own]`), "test alone 18.1 Mbps");
    eq("the household's share is said in words", await text(`${BODY} [data-v=traffic]`), note);
    await phone(async () => {
      await speedTab(page, until);
      await waitText(`${BODY} [data-item=down] [data-v=num]`, /^905$/);
      await showCard("speed");
      eq("a phone shows it without sideways scroll", JSON.stringify(await speedOverflow(page)), "[]");
    });
    await stubFetch(null);
  }],

  ["speed card: a phone shows it whole, with the question open, without sideways scroll", async ({ click, eq, page, phone, showCard, until, waitText }) => {
    await phone(async () => {
      await speedTab(page, until);
      await waitText(`${BODY} [data-item=down] [data-v=num]`, /^236$/);
      await showCard("speed");
      eq("nothing sticks out", JSON.stringify(await speedOverflow(page)), "[]");
      await click(`${BODY} [data-action=speedAskRun]`);
      await until(() => document.querySelector("[data-panel=speed] .spd-confirm"));
      eq("nothing sticks out with the question open", JSON.stringify(await speedOverflow(page)), "[]");
      eq("the figures fit side by side or stack", await page.$$eval(`${BODY} .spd-fig`, (f) => f.every((e) => e.getBoundingClientRect().width > 60)), true);
    });
  }],

  ["speed card: every theme draws it (figures, grade, chart with ink), on a desktop and a phone, without sideways scroll", async ({ DESKTOP, SHOTS, eq, ok, outDir, page, path, pickLook, showCard, stubFetch, until, waitText }) => {
    // every theme the page knows (<html data-looks>); the default one is picked as "" in the menu
    const looks = await page.evaluate(() => JSON.parse(document.documentElement.dataset.looks).map((t) => (t.id === document.documentElement.dataset.lookDefault ? "" : t.id)));
    ok(`the theme menu lists themes (${looks.length})`, looks.length >= 5 && looks.includes("starship") && looks.includes("mission-control"));
    // the app's answer with its plan (step one saw it; the plan is not set any more), so the plan line and the dashed lines are drawn
    await stubFetch({ "GET /api/speedtests": { body: speedReal } });
    await waitText(`${BODY} [data-item=down] [data-v=plan]`, /^79 % of 300 Mbps$/);
    const drawn = () => page.evaluate(() => {
      const b = document.querySelector("[data-panel=speed] .panel-b");
      const c = b.querySelector("canvas");
      const ink = c ? c.getContext("2d").getImageData(0, 0, c.width, c.height).data.some((v, i) => i % 4 === 3 && v > 0) : false;
      return [b.querySelector("[data-item=down] [data-v=num]")?.textContent, b.querySelector("[data-item=down] [data-v=plan]")?.textContent, b.querySelector("[data-item=bufferbloat] [data-st]")?.textContent.trim(), ink].join("|");
    });
    for (const look of looks) {
      await pickLook(look);
      await showCard("speed");
      await until(() => { const b = document.querySelector("[data-panel=speed] .panel-b"); return b.dataset.view && b.dataset.view !== "loading"; });
      eq(`${look || "default"}: figures, grade and chart`, await drawn(), "236|79 % of 300 Mbps|Grade B|true");
      eq(`${look || "default"}: nothing sticks out`, JSON.stringify(await speedOverflow(page)), "[]");
      if (SHOTS) await (await page.$(`${CARD} .panel`)).screenshot({ path: path.join(outDir, `ps-speed-${look || "default"}.png`) });
      // a phone's width, by resizing only (every theme: a reload each would meet the history route's rate limit)
      await page.setViewport({ width: 390, height: 844 });
      try {
        await speedTab(page, until);
        await showCard("speed");
        eq(`${look || "default"} on a phone: nothing sticks out`, JSON.stringify(await speedOverflow(page)), "[]");
        if (SHOTS) await (await page.$(`${CARD} .panel`)).screenshot({ path: path.join(outDir, `ps-speed-${look || "default"}-phone.png`) });
      } finally {
        await page.setViewport(DESKTOP);
        await page.evaluate(() => { history.replaceState(null, "", location.pathname); });
      }
    }
    await pickLook("");
    await stubFetch(null);
  }],

  ["speed card: a viewer without the password sees Run now but cannot use it", async ({ click, count, eq, page, waitText }) => {
    await waitText(`${BODY} [data-item=down] [data-v=num]`, /^236$/);
    const run = `${BODY} [data-action=speedAskRun]`;
    eq("it is marked and says why", await page.$eval(run, (b) => `${b.getAttribute("aria-disabled")}|${b.title}`), "true|View only · Sign in to edit");
    await click(run);
    eq("pressing it asks nothing", await count(`${BODY} .spd-confirm`), 0);
  }, { variants: ["viewonly"] }],
];
