// The Internet use card (the router's traffic: tests/test_ui_smoke.py seed_router_traffic: 36 hours up to the newest the router
// published (a range ends there), Desk PC downloading 75 GB in one hour, mostly one app, and three 24-hour app counts), its device dialog, the card in other states and
// themes, and the incident inspector's Traffic and Router events blocks (the Long evening outage covers hours the router has
// published and hours it has not, and the router's restart and WAN link events).

export const subject = "panel:traffic";
export const variants = ["full"];

const TRAFFIC_CARD = "[data-panel=traffic]";
const TRAFFIC_BODY = `${TRAFFIC_CARD} .panel-b`;

// The card's chart, as the steps compare it: one number per hour, null for an hour with no figure.
const trafficChart = (page) => page.evaluate(() => {
  const c = Chart.getChart(document.querySelector("[data-panel=traffic] canvas"));
  const set = (label) => c.data.datasets.find((d) => d.label === label);
  const n = c.data.labels.length;
  return {
    n, down: set("Download").data, up: set("Upload").data,
    unpublished: set("Not published yet")?.data.map((v) => (v === null ? 0 : 1)).join("") ?? null,
    notStored: set("Not stored")?.data.filter((v) => v !== null).length ?? 0
  };
});

// What sticks out of the card at the page's width: the page itself, or any part of the card past its right edge.
const trafficOverflow = (page) => page.evaluate((sel) => {
  const card = document.querySelector(`${sel} .panel`).getBoundingClientRect();
  const out = [];
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1) out.push(`page ${document.documentElement.scrollWidth} > ${document.documentElement.clientWidth}`);
  document.querySelectorAll(`${sel} .panel-b *`).forEach((e) => {
    const r = e.getBoundingClientRect();
    if (r.width && r.right > card.right + 1) out.push(`${e.tagName.toLowerCase()}.${String(e.className.baseVal ?? e.className).split(" ")[0]} right ${Math.round(r.right)} > ${Math.round(card.right)}`);
  });
  return out;
}, TRAFFIC_CARD);

// An answer of the traffic route the page gets instead of the app's (stubFetch).
const trafficAnswer = (over) => ({
  available: true, reason: null, read_at: null, interval_seconds: 3600, hours: 24, start: 1767139200, end: 1767225600, window_end: null, stored_at: null,
  wan: [], totals: { up: 0, down: 0 }, clients: [], apps: [], apps_window_end: null, privacy: "full", ...over
});

export const steps = [
  ["byte counts are written in the router's decimal units, three figures", async ({ eq, moduleUrl, page }) => {
    const sizes = await page.evaluate(async (u) => { const { formatBytes } = await import(u); return [0, 999, 1000, 1500, 999950, 350e6, 2.19e9, 75e9, 99.96e9, 211e9, 4.5e12, null, undefined].map(formatBytes).join("|"); }, await moduleUrl("format.js"));
    eq("sizes", sizes, "0 B|999 B|1 kB|1.5 kB|1 MB|350 MB|2.2 GB|75 GB|100 GB|211 GB|4.5 TB|–|–");
  }],

  ["internet use card: the 75 GB device on top with its app, the apps with their window, and a range that ends at the newest hour the router published", async ({ count, eq, ok, page, text, until, waitText }) => {
    await until(() => { const c = document.querySelector("[data-panel=traffic] canvas"); return !!(c && Chart.getChart(c)); });
    await waitText(`${TRAFFIC_BODY} [data-v=range]`, /^Last 24 hours, from .+ to .+$/);
    ok("the sums", /^[\d.]+ GB downloaded · [\d.]+ GB uploaded$/.test(await text(`${TRAFFIC_BODY} [data-v=totals]`)));
    const first = `${TRAFFIC_BODY} .trf-devices .trf-rows > :first-child`;
    eq("the top device", await text(`${first} [data-v=name]`), "Desk PC");
    eq("its download, in the page's units", await text(`${first} [data-v=down]`), "75 GB");
    eq("its app", await text(`${first} [data-v=app]`), "mostly Sony PlayStation");
    eq("the next device", await text(`${TRAFFIC_BODY} .trf-devices .trf-rows > :nth-child(2) [data-v=name]`), "Ann's phone");
    ok("the next device's kind and maker", /^Phone · Example$/.test(await text(`${TRAFFIC_BODY} .trf-devices .trf-rows > :nth-child(2) [data-v=kind]`)));
    eq("the top app", await text(`${TRAFFIC_BODY} .trf-apps .trf-rows > :first-child [data-v=name]`), "Sony PlayStation");
    ok("the apps say which 24 hours they cover", /^last 24 hours, to .*\d\d:\d\d/.test(await text(`${TRAFFIC_BODY} [data-v=appsWindow]`)));
    eq("the apps end with what the router did not break down, muted", await text(`${TRAFFIC_BODY} .trf-apps .trf-rows > :last-child [data-v=name]`), "Not broken down by app");
    eq("it says why", await page.$eval(`${TRAFFIC_BODY} .trf-apps .trf-rows > :last-child`, (e) => e.title), "The router counts apps per 24 hours; hours not covered by one of its counts in this range");
    ok("it is muted", await page.$eval(`${TRAFFIC_BODY} .trf-apps .trf-rows > :last-child`, (e) => e.classList.contains("rest")));
    ok("the busiest hour", /^Busiest hour .+: 76\.2 GB down$/.test(await text(`${TRAFFIC_BODY} [data-v=busiest]`)));
    ok("the header sums the range", /^[\d.]+ GB down · [\d.]+ GB up$/.test(await text("#trafficSummary")));
    ok("when it was read and how often", /^read \d\d:\d\d.* · every 60 min$/.test(await text("#trafficFoot")));
    const chart = await trafficChart(page);
    eq("one bar per hour", chart.n, 24);
    ok("the newest bar is the newest hour the router published: drawn", chart.down[23] > 0 && chart.up[23] > 0);
    eq("no hour is hatched as not published: the range ends where the figures do", chart.unpublished, null);
    eq("no hour is called not stored", chart.notStored, 0);
    eq("no sentence about hours not published", await count(`${TRAFFIC_BODY} [data-v=unpublished]`), 0);
    eq("no sentence about hours not stored", await count(`${TRAFFIC_BODY} [data-v=nodata]`), 0);
  }],

  ["internet use card: an hour the router has not published yet (an answer whose range ends before the figures do) is hatched and said, never drawn as 0", async ({ count, eq, ok, page, stubFetch, text, waitText }) => {
    const end = 1767225600;
    const wan = [{ hour: end - 7200, up: 1, down: 5000000 }, { hour: end - 3600, up: null, down: null }];
    await stubFetch({ "GET /api/devices/router/traffic": { body: trafficAnswer({ read_at: end + 300, wan, totals: { up: 1, down: 5000000 }, clients: [], apps: [{ app: "Example", up: 1, down: 5000000 }], apps_unattributed: { up: 0, down: 0 }, apps_covered: { hours: 24, windows: [end] }, apps_window_end: end }) } });
    await waitText(`${TRAFFIC_BODY} [data-v=unpublished]`, /^The newest hour \(.+\) is not published by the router yet\.$/);
    const chart = await trafficChart(page);
    eq("the unpublished hour has no download figure", chart.down[1], null);
    eq("nor an upload figure", chart.up[1], null);
    ok("the hour before it is drawn", chart.down[0] > 0);
    eq("only the newest hour is hatched as not published", chart.unpublished, "01");
    ok("the legend names it", /Not published yet/.test(await text(`${TRAFFIC_BODY} .trf-legend`)));
    eq("no unattributed row when the apps add up", await count(`${TRAFFIC_BODY} .trf-apps .trf-row.rest`), 0);
    await stubFetch(null);
  }],

  ["internet use card: 7 days shows the hours that were never stored as such, and 24 h comes back", async ({ chooseRange, click, eq, ok, page, rangeState, text, until, waitText }) => {
    // the card draws again after the step before restored the real answers: wait for its range buttons; if they never
    // come, say what the card shows (seen twice under a full parallel run, 2026-11-15 ~01:20 UTC, not reproduced since)
    const button = `${TRAFFIC_BODY} select.range-select`;
    await page.waitForSelector(button, { timeout: 10000 }).catch(async () => {
      const shown = await page.evaluate((b) => document.querySelector(b)?.textContent.replace(/\s+/g, " ").slice(0, 600), TRAFFIC_BODY);
      throw new Error(`no 7 d button on the card at ${new Date().toISOString()}; it shows: ${shown}`);
    });
    await chooseRange(TRAFFIC_BODY, "7d");
    await until(() => Chart.getChart(document.querySelector("[data-panel=traffic] canvas")).data.labels.length === 168);
    await waitText(`${TRAFFIC_BODY} [data-v=range]`, /^Last 7 days, from /);
    const chart = await trafficChart(page);
    eq("the 36 stored hours: the rest were not stored", chart.notStored, 132);
    ok("the sentence says how many", /^132 hours in this range were not stored/.test(await text(`${TRAFFIC_BODY} [data-v=nodata]`)));
    eq("the range control shows the choice", (await rangeState(TRAFFIC_BODY)).value, "7d");
    ok("a labelled hour has its weekday", /^[A-Z][a-z]{2} \d\d:\d\d/.test(await page.evaluate(() => Chart.getChart(document.querySelector("[data-panel=traffic] canvas")).data.labels[100])));
    await chooseRange(TRAFFIC_BODY, "24h");
    await until(() => Chart.getChart(document.querySelector("[data-panel=traffic] canvas")).data.labels.length === 24);
    await waitText(`${TRAFFIC_BODY} [data-v=range]`, /^Last 24 hours, from /);
  }],

  ["internet use card: 30 days are a bar per calendar day with the ends cut by the range, and the apps of 7 and 30 days add up the router's counts inside them, ending with what is not broken down", async ({ chooseRange, click, eq, ok, page, rangeState, text, until, waitText }) => {
    await chooseRange(TRAFFIC_BODY, "7d");
    await waitText(`${TRAFFIC_BODY} [data-v=appsWindow]`, /^counted over 72 of 168 h, to /);
    eq("headed as the devices are", await text(`${TRAFFIC_BODY} [data-v=devicesWindow]`), "last 7 days");
    eq("no sentence under the apps", await page.$eval(`${TRAFFIC_BODY} [data-v=appsMore]`, (e) => e.hidden), true);
    eq("the top app", await text(`${TRAFFIC_BODY} .trf-apps .trf-rows > :first-child [data-v=name]`), "Sony PlayStation");
    eq("its download over the three counts", await text(`${TRAFFIC_BODY} .trf-apps .trf-rows > :first-child [data-v=down]`), "75 GB");
    eq("the next app adds up the three counts", await text(`${TRAFFIC_BODY} .trf-apps .trf-rows > :nth-child(2) [data-v=down]`), "6 GB");
    eq("the last row is what the counts do not cover", await text(`${TRAFFIC_BODY} .trf-apps .trf-rows > :last-child [data-v=name]`), "Not broken down by app");
    eq("its figure is the week's total (132.2 GB) less the apps (82.8 GB)", await text(`${TRAFFIC_BODY} .trf-apps .trf-rows > :last-child [data-v=down]`), "49.4 GB");
    await chooseRange(TRAFFIC_BODY, "30d");
    await waitText(`${TRAFFIC_BODY} [data-v=range]`, /^Last 30 days, from /);
    await until(() => { const n = Chart.getChart(document.querySelector("[data-panel=traffic] canvas")).data.labels.length; return n === 30 || n === 31; });
    const chart = await trafficChart(page);
    ok("the days with a stored hour are drawn (the seed's 36 hours reach two or three days)", chart.notStored >= 27 && chart.notStored <= 29);
    ok("the newest day is drawn", chart.down[chart.n - 1] > 0);
    ok("a label is a date", /^[A-Z][a-z]{2} \d+$/.test(await page.evaluate(() => { const c = Chart.getChart(document.querySelector("[data-panel=traffic] canvas")); return c.data.labels[c.data.labels.length - 1]; })));
    ok("the days not stored are said", /^2[789] days in this range were not stored/.test(await text(`${TRAFFIC_BODY} [data-v=nodata]`)));
    const bars = await page.evaluate(() => {
      const c = Chart.getChart(document.querySelector("[data-panel=traffic] canvas"));
      const shares = c.options.plugins.partialDays.shares;
      const w = c.getDatasetMeta(0).data.map((b) => b.width);
      return { shares, first: w[0], middle: w[Math.floor(w.length / 2)], last: w[w.length - 1] };
    });
    ok("one bar per calendar day the range touches", bars.shares.length === 30 || bars.shares.length === 31);
    ok("a cut day's bar is narrower in proportion to its hours", bars.shares.every((s, i) => (s < 1 ? (i === 0 ? bars.first : bars.last) < bars.middle : true)));
    ok("the busiest day", /^Busiest day [A-Z][a-z]{2}, [A-Z][a-z]{2} \d+: [\d.]+ GB down$/.test(await text(`${TRAFFIC_BODY} [data-v=busiest]`)));
    ok("the notes say the ends are cut (when they are)", bars.shares.every((s) => s === 1) || /begins and ends inside a day/.test(await text(`${TRAFFIC_BODY} [data-v=cut]`)));
    await waitText(`${TRAFFIC_BODY} [data-v=appsWindow]`, /^counted over 72 of 720 h, to /);
    eq("the range control shows the choice", (await rangeState(TRAFFIC_BODY)).value, "30d");
    await chooseRange(TRAFFIC_BODY, "24h");
    await until(() => Chart.getChart(document.querySelector("[data-panel=traffic] canvas")).data.labels.length === 24);
    await waitText(`${TRAFFIC_BODY} [data-v=appsWindow]`, /^last 24 hours, to /);
    eq("no sentence under the apps over 24 hours", await page.$eval(`${TRAFFIC_BODY} [data-v=appsMore]`, (e) => e.hidden), true);
  }],

  ["device dialog: opens from a row with focus inside and back on the row after Escape, in the card's range; 7 and 30 days from its own buttons; the apps and the days; Back closes it", async ({ chooseRange, click, dialogKeys, eq, ok, page, rangeState, text, until, waitText }) => {
    const row = `${TRAFFIC_BODY} .trf-devices .trf-rows > :first-child`;
    await dialogKeys(row, "trafficClientModal");
    await page.$eval(row, (e) => e.click());
    await waitText("#trafficClientTitle", /^Desk PC$/);
    await until(() => !document.getElementById("trafficClientBody").hidden);
    eq("downloaded", await text("#trafficClientStats [data-v=down] .val"), "75 GB");
    eq("uploaded", await text("#trafficClientStats [data-v=up] .val"), "1.5 GB");
    eq("mostly", await text("#trafficClientStats [data-v=hint] .val"), "Sony PlayStation");
    const labels = () => page.evaluate(() => Chart.getChart(document.getElementById("trafficClientChart")).data.labels.length);
    eq("it opens in the card's range: 24 hours, hour by hour", await labels(), 24);
    eq("that range is chosen", (await rangeState("#trafficClientRange")).value, "24h");
    eq("its apps", await page.$$eval("#trafficClientApps [data-v=name]", (e) => e.map((x) => x.textContent).join()), "Sony PlayStation,General");
    ok("the apps say their window", /^last 24 hours, to .*\d\d:\d\d/.test(await text("#trafficClientAppsWindow")));
    ok("the apps of the day", /Sony PlayStation 75 GB/.test(await text("#trafficClientDays")));
    await chooseRange("#trafficClientRange", "7d");
    await until(() => Chart.getChart(document.getElementById("trafficClientChart")).data.labels.length === 168);
    ok("the hours never stored are said", /hours in this range were not stored/.test(await text("#trafficClientNotes")));
    eq("the totals say the range", await text("#trafficClientStats [data-v=down] .sub"), "last 7 days");
    ok("the apps say how many hours the router counted them over", /^counted over 72 of 168 h, to /.test(await text("#trafficClientAppsWindow")));
    eq("its apps over the range (the two counts before the latest hold nothing of it)", await page.$$eval("#trafficClientApps [data-v=name]", (e) => e.map((x) => x.textContent).join()), "Sony PlayStation,General");
    await chooseRange("#trafficClientRange", "30d");
    await waitText("#trafficClientPer", /^Per day$/);
    await until(() => { const n = Chart.getChart(document.getElementById("trafficClientChart")).data.labels.length; return n === 30 || n === 31; });
    eq("a bar per day", await text("#trafficClientPer"), "Per day");
    eq("the busiest day, not hour", await text("#trafficClientStats [data-v=busiest] .lbl"), "Busiest day");
    ok("the apps over 30 days", /^counted over 72 of 720 h, to /.test(await text("#trafficClientAppsWindow")));
    eq("the card's own range is untouched", (await rangeState(TRAFFIC_BODY)).value, "24h");
    const where = await page.evaluate(() => location.href);
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("trafficClientModal").classList.contains("active"));
    eq("Back closed it and stayed on the page", await page.evaluate(() => location.href), where);
  }],

  ["device dialog: the pencil gives the device a name in Pengumon, shown in the dialog and on the card; Escape leaves the rename, not the dialog; the router's name comes back", async ({ click, eq, ok, page, text, until, waitText }) => {
    const row = `${TRAFFIC_BODY} .trf-devices .trf-rows > :first-child`;
    await page.$eval(row, (e) => e.click());
    await waitText("#trafficClientTitle", /^Desk PC$/);
    await until(() => !document.getElementById("trafficClientRename").hidden);
    await click("#trafficClientRename");
    await until(() => !document.getElementById("trafficClientNameForm").hidden);
    eq("the input has the focus", await page.evaluate(() => document.activeElement.id), "trafficClientNameInput");
    ok("it says what the name replaces", /router's name \(Desk PC\)/.test(await text("#trafficClientNameNote")));
    await page.keyboard.press("Escape");
    await until(() => document.getElementById("trafficClientNameForm").hidden);
    eq("Escape left the rename, not the dialog", await page.$eval("#trafficClientModal", (m) => m.classList.contains("active")), true);
    await click("#trafficClientRename");
    await until(() => !document.getElementById("trafficClientNameForm").hidden);
    await page.type("#trafficClientNameInput", "Office PC");
    await page.keyboard.press("Enter");
    await waitText("#trafficClientTitle", /^Office PC$/);
    ok("the router's own name is still said", /the router calls it Desk PC/.test(await text("#trafficClientMeta")));
    await waitText(`${row} [data-v=name]`, /^Office PC$/);
    await click("#trafficClientRename");
    await until(() => !document.getElementById("trafficClientNameClear").hidden);
    await click("#trafficClientNameClear");
    await waitText("#trafficClientTitle", /^Desk PC$/);
    ok("no router name note any more", !/router calls it/.test(await text("#trafficClientMeta")));
    await waitText(`${row} [data-v=name]`, /^Desk PC$/);
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("trafficClientModal").classList.contains("active"));
  }],

  ["device dialog: a device the router has nothing for says so", async ({ eq, expectStatus, page, until, waitText }) => {
    expectStatus(404); // the router's answer for a client it has nothing stored for
    await page.evaluate(async () => { const b = document.createElement("button"); b.dataset.action = "openTrafficClient"; b.dataset.arg = "router"; b.dataset.arg2 = "nobody"; document.body.appendChild(b); b.click(); b.remove(); });
    await waitText("#trafficClientStatus", /no traffic stored for this device/);
    expectStatus();
    eq("the body is not shown", await page.$eval("#trafficClientBody", (b) => b.hidden), true);
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("trafficClientModal").classList.contains("active"));
  }],

  ["internet use card: when the router gives no traffic, the reason is on the card; with nothing read yet it says so", async ({ count, eq, ok, stubFetch, text, waitText }) => {
    const reason = "the router's Traffic Analyzer has no data: it may be switched off in its settings";
    await stubFetch({ "GET /api/devices/router/traffic": { body: trafficAnswer({ available: false, reason }) } });
    await waitText(`${TRAFFIC_BODY} [data-item=unavailable] [data-v=reason]`, /Traffic Analyzer has no data/);
    eq("the word", await text(`${TRAFFIC_BODY} [data-item=unavailable] [data-v=status]`), "Not available");
    eq("no chart", await count(`${TRAFFIC_BODY} canvas`), 0);
    eq("no empty lists", await count(`${TRAFFIC_BODY} .trf-row`), 0);
    ok("the footer says it was never read", /^not read since Pengumon started · every 60 min$/.test(await text("#trafficFoot")));
    ok("the header says it", (await text("#trafficSummary")) === "unavailable");
    await stubFetch({ "GET /api/devices/router/traffic": { body: trafficAnswer({}) } });
    await waitText(`${TRAFFIC_BODY} [data-v=empty]`, /No traffic has been read yet/);
    eq("no unavailable notice", await count(`${TRAFFIC_BODY} [data-item=unavailable]`), 0);
    eq("no chart", await count(`${TRAFFIC_BODY} canvas`), 0);
    await stubFetch(null);
  }],

  ["router-supplied strings (app names, device names, makers, hints) are shown as text, never as markup; a device is fetched by its key", async ({ eq, ok, page, stubFetch, text, until, waitText }) => {
    const evil = "<img src=x onerror=window.__pwned=1>\"'&<b>";
    const hour = 1767225600 - 3600;
    const apps = [{ app: evil, up: 1, down: 5000000 }];
    const client = { key: "evilkey", label: evil, kind: "game", vendor: evil, up: 1, down: 5000000, apps, traffic_hint: evil };
    await stubFetch({
      "GET /api/devices/router/traffic": { body: trafficAnswer({ read_at: 1767225900, wan: [{ hour, up: 1, down: 5000000 }], totals: { up: 1, down: 5000000 }, clients: [client], apps, apps_window_end: 1767225600 }) },
      "GET /api/devices/router/traffic/clients/evilkey": { body: { ...trafficAnswer({ apps, apps_window_end: 1767225600, hours: 168 }), client, series: [{ hour, up: 1, down: 5000000 }], totals: { up: 1, down: 5000000 }, days: [{ day: "2026-01-01", window_end: 1767225600, apps }] } }
    });
    await waitText(`${TRAFFIC_BODY} .trf-devices [data-v=name]`, /<img/);
    eq("the name is text", await text(`${TRAFFIC_BODY} .trf-devices [data-v=name]`), evil);
    eq("so is the hint", await text(`${TRAFFIC_BODY} .trf-devices [data-v=app]`), `mostly ${evil}`);
    eq("and the app", await text(`${TRAFFIC_BODY} .trf-apps [data-v=name]`), evil);
    eq("no element was made of them", await page.$$eval(`${TRAFFIC_BODY} img, ${TRAFFIC_BODY} b b`, (e) => e.length), 0);
    await page.$eval(`${TRAFFIC_BODY} .trf-devices .trf-row`, (e) => e.click());
    await waitText("#trafficClientTitle", /<img/);
    await until(() => !document.getElementById("trafficClientBody").hidden);
    eq("the dialog asked for the key, not a MAC", await page.evaluate(() => window.__fetched.some(([k]) => k === "GET /api/devices/router/traffic/clients/evilkey")), true);
    eq("the dialog's title is text", await text("#trafficClientTitle"), evil);
    eq("no element in the dialog", await page.$$eval("#trafficClientModal img, #trafficClientModal b b", (e) => e.length), 0);
    ok("nothing ran", await page.evaluate(() => window.__pwned === undefined));
    await page.evaluate(() => history.back());
    await until(() => !document.getElementById("trafficClientModal").classList.contains("active"));
    await stubFetch(null);
  }],

  ["internet use card: every theme draws it (chart with ink, devices, apps), on a desktop and a phone, without sideways scroll", async ({ DESKTOP, SHOTS, eq, ok, outDir, page, path, pickLook, showCard, until }) => {
    const looks = await page.evaluate(() => JSON.parse(document.documentElement.dataset.looks).map((t) => (t.id === document.documentElement.dataset.lookDefault ? "" : t.id)));
    ok(`the theme menu lists themes (${looks.length})`, looks.length >= 5 && looks.includes("starship") && looks.includes("mission-control") && looks.includes("amber-crt"));
    const drawn = () => page.evaluate(() => {
      const b = document.querySelector("[data-panel=traffic] .panel-b");
      const c = b.querySelector("canvas");
      const ink = c ? c.getContext("2d").getImageData(0, 0, c.width, c.height).data.some((v, i) => i % 4 === 3 && v > 0) : false;
      return [b.querySelector(".trf-devices [data-v=name]")?.textContent, b.querySelector(".trf-apps [data-v=name]")?.textContent, b.querySelectorAll(".trf-row").length, ink].join("|");
    });
    for (const look of looks) {
      await pickLook(look);
      await showCard("traffic");
      await until(() => { const b = document.querySelector("[data-panel=traffic] .panel-b"); return b.dataset.view && b.dataset.view !== "loading" && !!Chart.getChart(b.querySelector("canvas")); });
      eq(`${look || "default"}: first device, first app, rows and chart ink`, await drawn(), "Desk PC|Sony PlayStation|8|true");
      eq(`${look || "default"}: nothing sticks out`, JSON.stringify(await trafficOverflow(page)), "[]");
      if (SHOTS) await (await page.$(`${TRAFFIC_CARD} .panel`)).screenshot({ path: path.join(outDir, `ps-traffic-${look || "default"}.png`) });
      await page.setViewport({ width: 390, height: 844 });
      try {
        await page.evaluate(() => { location.hash = "#network"; });
        await until(() => !document.querySelector("[data-panel=traffic]").hidden);
        await showCard("traffic");
        eq(`${look || "default"} on a phone: nothing sticks out`, JSON.stringify(await trafficOverflow(page)), "[]");
        if (SHOTS) await (await page.$(`${TRAFFIC_CARD} .panel`)).screenshot({ path: path.join(outDir, `ps-traffic-${look || "default"}-phone.png`) });
      } finally {
        await page.setViewport(DESKTOP);
        await page.evaluate(() => { history.replaceState(null, "", location.pathname); });
      }
    }
    await pickLook("");
  }],

  ["device dialog on a phone: no sideways scroll, in the default theme and Starship", async ({ DESKTOP, click, eq, noSideScroll, page, pickLook, until, waitText }) => {
    for (const look of ["", "starship"]) {
      await pickLook(look);
      await page.setViewport({ width: 390, height: 844 });
      try {
        await page.evaluate(() => { location.hash = "#network"; });
        await until(() => !document.querySelector("[data-panel=traffic]").hidden);
        await click(`${TRAFFIC_BODY} .trf-devices .trf-rows > :first-child`);
        await waitText("#trafficClientTitle", /^Desk PC$/);
        await until(() => !document.getElementById("trafficClientBody").hidden);
        eq(`${look || "default"}: the dialog fits`, await noSideScroll("trafficClientModal"), true);
        eq(`${look || "default"}: the page does not scroll sideways`, await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), true);
        await click("[data-action=closeModal][data-arg=trafficClientModal]");
        await until(() => !document.getElementById("trafficClientModal").classList.contains("active"));
      } finally {
        await page.setViewport(DESKTOP);
        await page.evaluate(() => { history.replaceState(null, "", location.pathname); });
      }
    }
    await pickLook("");
  }],

  ["incident inspector: the router's traffic in the incident's hours (an hour not published yet says so) and its restart and WAN link events", async ({ click, count, eq, inspectNamed, ok, page, text, until, waitText }) => {
    await inspectNamed("Long evening outage");
    await waitText("#inspCauseTitle", /^Long evening outage$/);
    await until(() => !document.getElementById("inspTrafficCard").hidden && !document.getElementById("inspDeviceEventsCard").hidden);
    const hours = await page.$$eval("#inspTraffic > li", (els) => els.map((e) => ({ state: e.dataset.state, text: e.textContent })));
    ok(`an hour the router has published names its device and share (${JSON.stringify(hours)})`, hours.some((h) => h.state === "ok" && /Ann's phone/.test(h.text) && /\d+ % of all/.test(h.text)));
    ok("an hour it has not published says so", hours.some((h) => h.state === "not yet available" && /not yet available/.test(h.text)));
    ok("an hour not published is not shown as a figure", hours.filter((h) => h.state === "not yet available").every((h) => !/ down ·/.test(h.text)));
    const events = await page.$$eval("#inspDeviceEvents li", (els) => els.map((e) => e.textContent));
    ok(`the restart (${events.join(" | ")})`, events.some((t) => /^.*Restarted \(up since /.test(t)));
    ok("the link going down", events.some((t) => /WAN link went down \(was 2\.5 Gb\/s\)/.test(t)));
    ok("the link coming back", events.some((t) => /WAN link came back at 2\.5 Gb\/s/.test(t)));
    ok("each event says how far from the start it was", events.every((t) => /from the start\)/.test(t)));
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await until(() => !document.getElementById("incidentInspectorModal").classList.contains("active"));
    // an incident the router's events are not near: no events block, and its hours are all ones not published yet
    await inspectNamed("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    await until(() => !document.getElementById("inspTrafficCard").hidden);
    eq("no events block", await page.$eval("#inspDeviceEventsCard", (e) => e.hidden), true);
    eq("its hours are not published yet", await page.$$eval("#inspTraffic > li", (els) => els.map((e) => e.dataset.state).filter((s) => s !== "not yet available").length), 0);
    eq("the traffic block is not empty", (await count("#inspTraffic > li")) > 0, true);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["every range draws the Internet use chart (a bar per hour, per day, then per week past 90 days) and the device dialog's, on a desktop, a phone and in every theme", async ({ atEveryRange, axisOk, chartAxis, chooseRange, click, dialogKeys, eq, everyLook, noSideScroll, noSideways, ok, openPanel, page, phone, rangeState, settled, showCard, until, waitText }) => {
    const CHART = `${TRAFFIC_CARD} canvas`;
    // bars: 24 hours, 168 hours, 30 or 31 days, 90 or 91 days, about 27 weeks (the first and last cut by the range); the axis labels of
    // 24 h are clock times, of 7 d weekday and clock, of the days and weeks dates
    const BARS = { "24h": [24, 24], "7d": [168, 168], "30d": [30, 31], "90d": [90, 91], all: [26, 28] };
    const KINDS = { "24h": ["clock"], "7d": ["datetime"], "30d": ["date"], "90d": ["date"], all: ["date"] };
    const check = (where) => async (id, s) => {
      const what = `${where} ${id}`;
      ok(`${what}: the chart is drawn`, !!s);
      const [lo, hi] = BARS[id];
      ok(`${what}: ${s.count} bars (${lo}-${hi})`, s.count >= lo && s.count <= hi);
      const down = s.sets.find((d) => d.label === "Download");
      ok(`${what}: bars with a download figure (${down ? down.n : 0})`, !!down && down.n >= (id === "24h" ? 20 : id === "7d" ? 20 : 20));
      axisOk(what, id, s, KINDS[id]);
      ok(`${what}: nothing sticks out sideways`, await noSideways());
    };
    await showCard("traffic");
    eq("24 h to All", (await rangeState(TRAFFIC_BODY)).ids.join(), "24h,7d,30d,90d,all");
    await atEveryRange(TRAFFIC_BODY, CHART, check("desktop"));
    ok("past 90 days the bars are weeks, said in the card", /per week/.test(await page.$eval(CHART, (c) => c.getAttribute("aria-label"))));
    await everyLook(async (look) => {
      await showCard("traffic");
      for (const id of ["24h", "all"]) {
        await chooseRange(TRAFFIC_BODY, id);
        await settled();
        await check(look)(id, await chartAxis(CHART));
      }
    });
    await phone(async () => {
      await openPanel("traffic", "network");
      await atEveryRange(TRAFFIC_BODY, CHART, check("phone"));
    });
    // the device dialog: the same control and ranges
    await chooseRange(TRAFFIC_BODY, "24h");
    await settled();
    const row = `${TRAFFIC_BODY} .trf-devices .trf-rows > :first-child`;
    await page.$eval(row, (e) => e.click());
    await until(() => !document.getElementById("trafficClientBody").hidden);
    const checkDialog = (where) => async (id, s) => {
      const what = `${where} device ${id}`;
      ok(`${what}: the chart is drawn`, !!s);
      const [lo, hi] = BARS[id];
      ok(`${what}: ${s.count} bars (${lo}-${hi})`, s.count >= lo && s.count <= hi);
      axisOk(what, id, s, KINDS[id]);
      ok(`${what}: nothing sticks out sideways`, await noSideScroll("trafficClientModal"));
    };
    eq("the dialog offers the same ranges", (await rangeState("#trafficClientRange")).ids.join(), "24h,7d,30d,90d,all");
    await atEveryRange("#trafficClientRange", "#trafficClientChart", checkDialog("desktop"));
    ok("past 90 days the dialog says per week", /Per week/.test(await page.$eval("#trafficClientPer", (e) => e.textContent)));
    await click("[data-action=closeModal][data-arg=trafficClientModal]");
    await phone(async () => {
      await openPanel("traffic", "network");
      await page.$eval(row, (e) => e.click());
      await until(() => !document.getElementById("trafficClientBody").hidden);
      await atEveryRange("#trafficClientRange", "#trafficClientChart", checkDialog("phone"));
      await click("[data-action=closeModal][data-arg=trafficClientModal]");
    });
  }, { variants: ["longhistory"] }],
  ["All starts at the first stored day when that is younger than the retention: the Internet use card and the device dialog (42 days of data: a bar per day; 200: a bar per week)", async ({ chartAxis, chooseRange, click, ok, page, settled, showCard, sleep, until, youngData }) => {
    await sleep(62000); // (the traffic routes answer 60 calls a minute: the step before this one used them up)
    const CHART = `${TRAFFIC_CARD} canvas`;
    await showCard("traffic");
    for (const { days, bars, per, label } of [{ days: 42, bars: [40, 46], per: "day", label: /^[A-Z][a-z]{2} \d{1,2}$/ }, { days: 400, bars: [26, 31], per: "week", label: /^[A-Z][a-z]{2} \d{1,2}$/ }]) {
      await youngData(days);
      await chooseRange(TRAFFIC_BODY, "all");
      await settled();
      const s = await chartAxis(CHART);
      ok(`${days}: ${s.count} bars (${bars.join("-")}; 365 days would be 53 weeks, hatched but for the last)`, s.count >= bars[0] && s.count <= bars[1]);
      ok(`${days}: a bar per ${per}, said in the card`, new RegExp(`per ${per}`).test(await page.$eval(CHART, (c) => c.getAttribute("aria-label"))));
      const down = s.sets.find((d) => d.label === "Download");
      ok(`${days}: nearly every bar has a download figure (${down.n} of ${s.count})`, down.n >= s.count - 4); // (the seed has no hours for its newest 3 days)
      ok(`${days}: the labels are dates (${s.labels.join(" | ")})`, s.labels.length >= 2 && [s.labels[0], s.labels.at(-1)].every((l) => label.test(l)));
      ok(`${days}: no label twice in a row`, s.labels.every((l, i) => i === 0 || l !== s.labels[i - 1]));
      const words = await page.$eval(`${TRAFFIC_BODY} [data-v=range]`, (e) => e.textContent);
      ok(`${days}: the card says what it holds (${words})`, /^All stored, since [A-Z][a-z]{2} \d{1,2}/.test(words));
      // the device dialog does the same
      await page.$eval(`${TRAFFIC_BODY} .trf-devices .trf-rows > :first-child`, (e) => e.click());
      await until(() => !document.getElementById("trafficClientBody").hidden);
      const d = await chartAxis("#trafficClientChart");
      ok(`${days}: the dialog draws ${d.count} bars too (${bars.join("-")})`, d.count >= bars[0] && d.count <= bars[1]);
      ok(`${days}: its heading says per ${per}`, new RegExp(`Per ${per}`).test(await page.$eval("#trafficClientPer", (e) => e.textContent)));
      await click("[data-action=closeModal][data-arg=trafficClientModal]");
    }
    await youngData(null);
  }, { variants: ["longhistory"] }],

];
