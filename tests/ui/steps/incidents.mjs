// The incident log and its Inspector dialog. The inspector steps open incidents from the table, so they keep the page size the table step chose.

export const subject = "panel:incidents";
export const variants = ["full"];

export const steps = [
  ["incident table: paging, search, page size, expected-events toggle", async ({ click, eq, ok, page, shown, text, waitCount, waitText }) => {
    await waitCount("#incidentsBody tr.incident-row", 10);
    eq("count", await text("#incidentCountBadge"), "14 recorded events");
    eq("info", await text("#incidentPaginationInfo"), "Showing 1–10 of 14 events");
    await click("#btnPageNext");
    await waitText("#incidentPageNum", /^Page 2 of 2$/);
    await waitCount("#incidentsBody tr.incident-row", 4);
    await click("#btnPageFirst");
    await waitText("#incidentPageNum", /^Page 1 of 2$/);
    await page.type("#incidentSearchInput", "blip 1");
    await waitCount("#incidentsBody tr.incident-row", 4); // blips 1, 10, 11, 12
    await waitText("#incidentPaginationInfo", /\(filtered\)/);
    await page.$eval("#incidentSearchInput", (i) => { i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); });
    await page.select("#incidentPageSizeSelect", "50");
    await waitCount("#incidentsBody tr.incident-row", 14);
    eq("toggle visible", await shown("#btnToggleExpected"), true);
    ok("toggle label", /Show 1 expected/.test(await text("#btnToggleExpected")));
    await click("#btnToggleExpected");
    await waitCount("#incidentsBody tr.incident-row", 15);
    await waitText("#incidentCountBadge", /^15 recorded events$/);
    ok("busy badge", /Expected/.test(await text("#incidentsBody")));
    await click("#btnToggleExpected");
    await waitCount("#incidentsBody tr.incident-row", 14);
  }],

  ["incident rows: one line each, the details are the inspector's, and a click anywhere on the row opens it", async ({ click, count, eq, ok, page, until, waitText }) => {
    eq("no description line in the list", await count("#incidentsBody .sub"), 0);
    eq("no load or busy chips in the list", /load|busy line/.test(await page.$eval("#incidentsBody", (e) => e.textContent)), false);
    const heights = await page.$$eval("#incidentsBody tr.incident-row", (els) => els.map((e) => e.getBoundingClientRect().height));
    ok(`rows stay small (${Math.max(...heights).toFixed(0)} px)`, Math.max(...heights) <= 44);
    await page.evaluate(() => [...document.querySelectorAll("#incidentsBody tr")].find((tr) => tr.textContent.includes("Short gateway drop")).querySelector(".when").click());
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    ok("the description is in the inspector", /home-side address/.test(await page.$eval("#inspCauseDesc", (e) => e.textContent)));
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await until(() => !document.getElementById("incidentInspectorModal").classList.contains("active"));
  }],

  ["incident timeline: bars, lanes by cause", async ({ count, ok, until }) => {
    await until(() => document.querySelectorAll("#incidentTimeline .tl-bar").length >= 10);
    ok("lanes by cause", (await count("#incidentTimeline .tl-lane")) >= 1);
  }],

  ["lanes use Path health's words: Router, Gateway, Line, ISP, Internet, DNS, Load, Service; the cause help explains them", async ({ eq, ok, page, until }) => {
    const r = await page.evaluate(async () => {
      const main = [...document.scripts].find((s) => s.type === "module").src;
      const m = await import(main.replace(/js\/main\.js$/, "panels/incidents/incidents.js"));
      const api = await import(main.replace(/js\/main\.js$/, "js/path-api.js"));
      return { lanes: api.CAUSES.map(([k, w]) => `${k}=${w}`).join(","), words: ["router", "gateway", "line", "isp"].map((c) => m.causeOf({ cause: c })).join(">") };
    });
    eq("lane keys and words", r.lanes, "router=Router,gateway=Gateway,line=Line,isp=ISP,internet=Internet,dns=DNS,load=Load,service=Service,unknown=Other");
    eq("the server's router, gateway, line and isp categories", r.words, "Router>Gateway>Line>ISP");
    await until(() => document.querySelectorAll("#incidentTimeline .tl-l").length >= 1);
    ok("no lane is still called Home", !/Home/.test(await page.$eval("#incidentTimeline", (e) => e.textContent)));
  }],

  ["an incident lasts as long as its probes were lost: the list shows the loss, the detected span on hover; the inspector both", async ({ click, eq, inspectNamed, ok, page, text, until, waitText }) => {
    // seeded: detected 3 s after the gateway's probes start failing, closed 8 s after they answer again (98 s), the
    // probes lost for about 90 s; the blips have no affected targets, so only their recorded duration is known
    const rows = await page.$$eval("#incidentsBody tr.incident-row", (trs) => Object.fromEntries(trs.map((tr) => {
      const d = tr.querySelector(".dur");
      return [tr.querySelector("td.lead").textContent.trim(), { text: d.textContent, title: d.title }];
    })));
    const drop = rows["Short gateway drop"];
    ok(`the loss, not the detected span: ${JSON.stringify(drop)}`, /^1m (29|30|31)s lost$/.test(drop.text) && /open 1m 38s/.test(drop.title) && /first to last lost probe/.test(drop.title));
    eq("an incident without a loss span keeps its duration", rows["Seeded blip 1"].text, "20s");
    ok("and says why on hover", /no stored probes/.test(rows["Seeded blip 1"].title));
    await inspectNamed("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    ok(`the inspector gives both: ${await text("#inspDuration")}`, /^Loss 1m (29|30|31)s · incident 1m 38s$/.test((await text("#inspDuration")).trim()));
    ok("so does its header", /Loss 1m \d\ds · incident 1m 38s$/.test(await text("#inspTimeWindowLabel")));
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["inspector: hop table, path trace, DOCSIS card and four charts", async ({ chartLabels, count, eq, hasChart, inspect, ok, outDir, page, path, shown, text, waitText }) => {
    await inspect("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    eq("modal", await page.$eval("#incidentInspectorModal", (m) => m.classList.contains("active")), true);
    eq("hop rows", await count("#inspHopTableBody tr"), 3);
    const names = await page.$$eval("#inspHopTableBody tr td:first-child .primary", (els) => els.map((e) => `${e.textContent.trim()}|${e.title}`));
    eq("canonical hop order, the page's short names, the configured name on hover", names.join(","),
      "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    eq("path trace rows", await count("#inspPathTraceBody tr"), 2);
    eq("path trace shown", await shown("#inspPathTraceContainer"), true);
    eq("docsis card shown", await shown("#inspDocsisCard"), true);
    ok("docsis badge", (await text("#inspDocsisBadge")).trim().length > 0);
    for (const id of ["incidentZoomChart", "incidentZoomLossChart", "incidentZoomDocsisChart", "incidentZoomRouterChart"]) await hasChart(id);
    ok("zoom chart has data", (await chartLabels("incidentZoomChart")) > 100);
    eq("docsis track shown", await shown("#incidentZoomDocsisContainer"), true);
    eq("router track shown", await shown("#incidentZoomRouterContainer"), true);
    await page.screenshot({ path: path.join(outDir, "inspector.png") });
  }],

  ["inspector: 'view in main history chart' loads the window and closes the modal", async ({ click, eq, page, until, waitText }) => {
    await click("#btnSnapDashboard");
    eq("closed", await page.$eval("#incidentInspectorModal", (m) => m.classList.contains("active")), false);
    await waitText("#chartModeLabel", /^incident window /);
    await until(() => Chart.getChart(document.getElementById("latencyChart")).data.labels.length > 50);
  }],

  ["inspector: a two hour incident comes back as buckets and renders", async ({ chartLabels, click, hasChart, inspect, ok, waitText }) => {
    await inspect("Long evening outage");
    await waitText("#inspCauseTitle", /^Long evening outage$/);
    await hasChart("incidentZoomChart");
    const n = await chartLabels("incidentZoomChart");
    ok(`bucketed labels ${n}`, n > 100 && n <= 1900);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["inspector: a gateway verdict is not blamed on the line; a line verdict reads the line's errors (desktop and phone)", async ({ click, eq, inspectNamed, noSideScroll, ok, page, phone, snap, text, waitText }) => {
    // the seed: the gateway drop has uncorrectable errors in its window, the line incident none
    const look = async (title, file) => {
      await inspectNamed(title);
      await waitText("#inspCauseTitle", new RegExp(`^${title}$`));
      await waitText("#inspDocsisDesc", /\S/);
      const seen = { desc: await text("#inspCauseDesc"), line: await text("#inspDocsisDesc"), evidence: await text("#inspEvidence") };
      ok(`${title}: no sideways scroll`, await noSideScroll("incidentInspectorModal"));
      await snap(file);
      await page.$eval("#inspDocsisCard", (e) => e.scrollIntoView({ block: "center" }));
      await snap(file.replace(".png", "-line-card.png"));
      await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
      return seen;
    };
    for (const [suffix, run] of [["desktop", (fn) => fn()], ["phone", phone]]) {
      await run(async () => {
        const gw = await look("Short gateway drop", `verdict-gateway-${suffix}.png`);
        ok(`gateway: the verdict says its probes do not use the line (${gw.desc})`, /do not use the provider's line/.test(gw.desc));
        ok(`gateway: the line's errors are not said to explain it (${gw.line})`, /does not use the line, so these errors do not explain it/.test(gw.line) && !/fits this packet loss/.test(gw.line));
        ok(`gateway: the evidence names the gateway (${gw.evidence})`, /Gateway/.test(gw.evidence));
        const line = await look("Line or ISP Hub Packet Loss", `verdict-line-${suffix}.png`);
        ok(`line: the clean line places it past the modem (${line.line})`, /The line was clean/.test(line.line) && /provider's first router/.test(line.line));
        eq("line: the verdict keeps its words", /the local router and the gateway answer normally/.test(line.desc), true);
      });
    }
  }],

  ["inspector: verdict, lanes per hop, what happened, synced tracks, evidence, hop figures", async ({ count, dialogShot, eq, hasChart, inspectNamed, modalOpen, ok, page, setTheme, tabStaysIn, text, until, waitText }) => {
    await setTheme("dark");
    await inspectNamed("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    await until(() => document.querySelectorAll("#inspLanes .strip").length >= 3);
    ok("verdict chips name the cause", /Cause: /.test(await text("#inspChips")));
    eq("one lane per hop", await count("#inspLanes .strip"), 3);
    ok("the gateway lane shows lost probes", (await count("#inspLanes .strip:nth-of-type(2) i.w, #inspLanes .strip:nth-of-type(2) i.c")) > 3);
    ok("the router lane has none", (await count("#inspLanes .strip:nth-of-type(1) i.w, #inspLanes .strip:nth-of-type(1) i.c")) === 0);
    ok("events: window, first loss, end", (await count("#inspEvents li")) >= 3 && /First lost probes at/.test(await text("#inspEvents")));
    ok("evidence: line, load and first hop with loss", (await count("#inspEvidence li")) >= 4 && /Line load/.test(await text("#inspEvidence")));
    ok("an incident without a load figure says so", /unknown/.test(await text("#inspEvidence")));
    for (const id of ["incidentZoomChart", "incidentZoomLossChart", "incidentZoomDocsisChart", "incidentZoomRouterChart"]) await hasChart(id);
    // one gutter for every track: the plot areas line up
    const areas = await page.evaluate(() => ["incidentZoomChart", "incidentZoomLossChart", "incidentZoomDocsisChart", "incidentZoomRouterChart"].map((i) => { const a = Chart.getChart(document.getElementById(i)).chartArea; return `${Math.round(a.left)}-${Math.round(a.right)}`; }));
    ok(`tracks share one plot width (${areas})`, new Set(areas).size === 1);
    // one reading per hop in every probe round (the seeded probes straddle the half second), losses where they happened,
    // a lost probe breaking the latency line, and tooltips that describe values, never "null"
    const tracks = await page.evaluate(() => {
      const lat = Chart.getChart(document.getElementById("incidentZoomChart"));
      const loss = Chart.getChart(document.getElementById("incidentZoomLossChart"));
      // the router and Cloudflare never lose a probe in the seed: a hole in their lines is a round split in two
      const none = [0, 2].map((k) => lat.data.datasets[k].data.slice(1, -1).filter((v) => v === null).length);
      const i = loss.data.datasets[1].data.findIndex((v) => v !== null);
      const t = loss.options.plugins.tooltip;
      const lines = loss.data.datasets.map((d, k) => ({ datasetIndex: k, dataIndex: i, raw: d.data[i], parsed: { y: d.data[i] }, dataset: d }))
        .filter(t.filter || (() => true)).map(t.callbacks.label);
      const latLines = lat.data.datasets.map((d) => lat.options.plugins.tooltip.callbacks.label({ dataset: d, dataIndex: i, parsed: { y: d.data[i] } }));
      return { none: none.join(","), lines: lines.join(" | "), latLines: latLines.join(" | "), gaps: lat.data.datasets.every((d) => d.spanGaps === false) };
    });
    eq("every probe round has a reading from the hops that never lost one", tracks.none, "0,0");
    ok(`the loss tooltip names only the hop that lost, by its short name (${tracks.lines})`, /^Gateway lost 100%$/.test(tracks.lines));
    ok(`the latency tooltip: router answered, gateway lost, internet answered (${tracks.latLines})`,
      /^Router: \d+\.\d+ ms \| Gateway: lost \| Internet: \d+\.\d+ ms$/.test(tracks.latLines));
    // every label in the inspector names a hop as the rest of the page does, the configured name only on hover
    const labels = await page.evaluate(() => ({
      lanes: [...document.querySelectorAll("#inspLanes .lane-n")].map((e) => `${e.textContent}|${e.title}`).join(","),
      tip: document.querySelector("#inspLanes .strip:nth-of-type(2) i").dataset.tip,
      legend: [...document.querySelectorAll("#inspLatLegend > span")].map((e) => `${e.textContent}|${e.title}`).join(","),
      text: ["#inspChips", "#inspEvents", "#inspEvidence"].map((s) => document.querySelector(s).textContent).join(" ")
    }));
    eq("lanes", labels.lanes, "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    ok(`a lane's tooltip (${labels.tip})`, /^Gateway · /.test(labels.tip));
    eq("chart legend", labels.legend, "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    ok(`no configured name in the chips, events or evidence: ${labels.text}`, !/Hop \d:|Public:/.test(labels.text) && /First lost probes at Gateway/.test(labels.text));
    eq("a lost probe breaks the latency line", tracks.gaps, true);
    eq("keyboard focus starts on close", await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Close");
    await dialogShot("inspector-dark");
    const box = await page.$eval("#incidentZoomChart", (c) => { const r = c.getBoundingClientRect(); return { x: r.left + r.width * 0.5, y: r.top + r.height * 0.5 }; });
    await page.mouse.move(box.x, box.y);
    await dialogShot("inspector-hover-dark");
    await page.mouse.move(2, 2);
    await setTheme("light");
    await dialogShot("inspector-light");
    await setTheme("dark");
    await tabStaysIn("incidentInspectorModal");
    await page.click("#incidentInspectorModal .modal-t"); // plain text: focus goes to the body, Escape still closes
    await page.keyboard.press("Escape");
    eq("escape closes it", await modalOpen("incidentInspectorModal"), false);
  }],

  ["inspector: a busy-line incident and one with an unusable load figure", async ({ click, count, dialogShot, eq, inspectNamed, ok, text, until, waitText }) => {
    await inspectNamed("Busy line slowdown");
    await waitText("#inspCauseTitle", /^Busy line slowdown$/);
    await until(() => document.querySelectorAll("#inspLanes .strip").length >= 3);
    ok("load shown", /96% load/.test(await text("#inspChips")) && /96% of capacity/.test(await text("#inspEvidence")));
    ok("lanes hatch the busy period", (await count("#inspLanes .strip i.b")) > 3);
    await dialogShot("inspector-busy-dark");
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await inspectNamed("Short gateway drop", { suspectLoad: true });
    await until(() => /not a valid figure/.test(document.getElementById("inspChips").textContent));
    ok("evidence says the same", /load: not a valid figure/.test(await text("#inspEvidence")));
    eq("no figure shown", /912/.test(await text("#incidentInspectorModal")), false);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["inspector: the dossier opens on top and Escape closes only the dossier", async ({ click, eq, inspectNamed, modalOpen, page, until, waitText }) => {
    await inspectNamed("Short gateway drop");
    await waitText("#inspCauseTitle", /^Short gateway drop$/);
    await click("#incidentInspectorModal [data-action=openDossierModal]");
    await until(() => document.getElementById("dossierModal").classList.contains("active"));
    await page.click("#dossierModal .modal-t"); // plain text: focus goes to the body, Escape still closes the topmost dialog
    await page.keyboard.press("Escape");
    eq("dossier closed", await modalOpen("dossierModal"), false);
    eq("inspector still open", await modalOpen("incidentInspectorModal"), true);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
  }],

  ["the inspector as a bottom sheet on a phone: it fits the width and scrolls inside", async ({ click, dialogShot, eq, hasChart, inspectNamed, noSideScroll, page, phone, setTheme, until }) => {
    await phone(async () => {
      await setTheme("dark");
      await page.evaluate(() => { location.hash = "incidents"; }); // the chart is on the Live tab, out of sight
      await until(() => document.querySelector("[data-panel=latency]").hidden);
      await inspectNamed("Short gateway drop");
      await until(() => document.querySelectorAll("#inspLanes .strip").length >= 3);
      await hasChart("incidentZoomChart");
      eq("inspector fits", await noSideScroll("incidentInspectorModal"), true);
      await dialogShot("inspector-phone");
      // "Show in latency chart" leaves the Incidents tab for the Live tab, where the chart is
      await click("#btnSnapDashboard");
      await until(() => !document.querySelector("[data-panel=latency]").hidden && /^incident window /.test(document.getElementById("chartModeLabel").textContent));
      await setTheme(null);
    });
  }],

  ["the list loads in pages: one small page at start, older ones for the 7-day timeline, a search, the last page and a window older than the page", async ({ click, eq, ok, page, text, until, waitText }) => {
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length > 0);
    const first = await page.evaluate(() => performance.getEntriesByType("resource").map((r) => r.name).filter((n) => /\/api\/incidents\?/.test(n)).map((n) => n.replace(/^.*\/api/, "/api")));
    eq("the first fetch is a page of 50, not 500", first.join(" "), "/api/incidents?limit=50");
    // a server with 130 incidents, one every 2 h (84 of them in the 7 days), newest first, honouring limit and offset
    await page.evaluate(() => {
      const now = Date.now() / 1000;
      const all = Array.from({ length: 130 }, (_, i) => ({ id: 9000 + i, kind: "gateway_down", title: i === 120 ? "Old needle" : `Synthetic ${i}`, description: "", severity: "warning", cause_code: "unknown_outage", cause: "isp",
        start_time: now - 600 - i * 7200, end_time: now - 560 - i * 7200, duration_seconds: 40, expected: i === 3 || i === 7, affected_targets: [] })); // two busy-line ones, so 98 of the first 100 show
      const real = window.fetch;
      window.__asked = [];
      window.__realFetch = real;
      window.fetch = (url, opts) => {
        const u = new URL(String(url), location.href);
        if (u.pathname === "/api/incidents/count") return Promise.resolve(new Response(JSON.stringify({ total: all.length, expected: 2 }), { headers: { "content-type": "application/json" } }));
        if (u.pathname !== "/api/incidents") return real(url, opts);
        const limit = Number(u.searchParams.get("limit")), offset = Number(u.searchParams.get("offset") || 0);
        window.__asked.push(`${limit}@${offset}`);
        return Promise.resolve(new Response(JSON.stringify(all.slice(offset, offset + limit)), { headers: { "content-type": "application/json" } }));
      };
    });
    const asked = () => page.evaluate(() => window.__asked.join(" "));
    await new Promise((r) => setTimeout(r, 2500)); // the first message has set the version the panel compares with
    // the server says the list changed: what is loaded so far (one page) is fetched again, then the timeline asks for the week
    await page.evaluate(() => { window.__smokeMutate = new Function("d", "d.incidents_version = (d.incidents_version || 0) + 1000"); });
    await until(() => window.__asked.length >= 2);
    await page.evaluate(() => { window.__smokeMutate = null; });
    eq("a page of 50 and the next page, which reaches back over the 7 days of the timeline", (await asked()).split(" ").slice(0, 2).join(" "), "50@0 50@50");
    await waitText("#incidentCountBadge", /^128 recorded events$/); // the server's count (130 less the 2 hidden), not the 98 loaded
    eq("the footer shows the same total as the badge (not 98+)", await text("#incidentPaginationInfo"), "Showing 1–10 of 128 events");
    // Next from a last loaded page that is not full: the page fills up, no row is skipped (it showed 91-98, then 101-110)
    for (let n = 2; n <= 10; n++) {
      await click("#btnPageNext");
      await waitText("#incidentPageNum", new RegExp(`^Page ${n} of 10$`));
    }
    eq("a short last page", await page.$$eval("#incidentsBody tr.incident-row", (r) => r.length), 8);
    await click("#btnPageNext");
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 10);
    await waitText("#incidentPaginationInfo", /^Showing 91–100 of /);
    await click("#btnPageNext");
    await waitText("#incidentPaginationInfo", /^Showing 101–110 of /);
    // the timeline is the 7 calendar days ending tonight at midnight, so how many of the synthetic incidents fall in it
    // depends on the time of day (about 72 just after midnight, 84 just before): count them the same way (the two
    // busy-line ones may be hidden)
    const inWindow = await page.evaluate(() => {
      const end = new Date(); end.setHours(24, 0, 0, 0);
      const start = end.getTime() / 1000 - 7 * 86400, now = Date.now() / 1000;
      return Array.from({ length: 130 }, (_, i) => now - 560 - i * 7200).filter((t) => t >= start).length;
    });
    ok(`the timeline has the week's incidents (${inWindow - 2} or more)`, await page.$$eval("#incidentTimeline .tl-bar", (b) => b.length) >= inWindow - 2);
    // a search looks through everything: 130 loaded, the old one found
    await page.type("#incidentSearchInput", "needle");
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 1);
    ok("the old incident is found", /Old needle/.test(await page.$eval("#incidentsBody", (e) => e.textContent)));
    await waitText("#incidentCountBadge", /^128 recorded events$/);
    await page.$eval("#incidentSearchInput", (i) => { i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); });
    // Next on the last loaded page loads more; with everything loaded the pager ends
    await page.select("#incidentPageSizeSelect", "50");
    await waitText("#incidentPageNum", /^Page 1 of 3$/);
    await click("#btnPageLast");
    await waitText("#incidentPageNum", /^Page 3 of 3$/);
    eq("the last page", await page.$$eval("#incidentsBody tr.incident-row", (r) => r.length), 28);
    eq("no Next past the end", await page.$eval("#btnPageNext", (b) => b.disabled), true);
    // put the real server back
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await page.reload({ waitUntil: "load" });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length === 10);
  }],

  ["one zone for every time: Settings names Tokyo, this browser is in New York, the incident times are Tokyo's", async ({ eq, page, stubFetch, until }) => {
    await page.emulateTimezone("America/New_York");
    const info = await page.evaluate(async () => (await (await fetch("/api/info")).json()));
    await stubFetch({ "GET /api/info": { body: { ...info, timezone: "Asia/Tokyo" } } });
    const row = await page.evaluate(async () => {
      const first = (await (await fetch("/api/incidents?limit=1")).json())[0];
      const at = (timeZone) => new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", timeZone }).format(first.start_time * 1000);
      return { id: first.id, tokyo: at("Asia/Tokyo"), newYork: at("America/New_York") };
    });
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length > 0);
    const shown = await page.$eval("#incidentsBody tr.incident-row .when", (e) => e.textContent);
    eq(`the first incident's time is Tokyo's (${shown})`, shown.endsWith(row.tokyo), true);
    eq("and not the browser's", shown.endsWith(row.newYork), false);
    await stubFetch(null);
    await page.emulateTimezone();
  }, { chromiumOnly: "page.emulateTimezone is CDP only" }],

  ["a change of the page's time zone (a Save in Settings) redraws the times already shown: the incidents and a device card's read time", async ({ LINE, eq, moduleUrl, ok, page, until }) => {
    await until((l) => document.querySelectorAll("#incidentsBody tr.incident-row").length > 0 && document.querySelector(`${l}`)?.dataset.readAt, LINE);
    const shown = await page.evaluate(async (u, l) => {
      const { setPageZone } = await import(u);
      const first = (await (await fetch("/api/incidents?limit=1")).json())[0];
      const zone = (await (await fetch("/api/info")).json()).timezone || undefined; // the page's zone now (Settings)
      const card = document.querySelector(l);
      const at = (ms, timeZone) => new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", timeZone }).format(ms);
      const read = Number(card.dataset.readAt) * 1000;
      const now = () => ({ when: document.querySelector("#incidentsBody tr.incident-row .when").textContent, read: card.querySelector("[data-f=read]").textContent });
      setPageZone("Asia/Tokyo"); // as fetchNetworkInfo does with the zone Settings names; read at once, before any new data
      const tokyo = now();
      setPageZone(zone);
      const back = now();
      return { tokyo, back, want: { when: at(first.start_time * 1000, "Asia/Tokyo"), read: at(read, "Asia/Tokyo") },
        before: { when: at(first.start_time * 1000, zone), read: at(read, zone) } };
    }, await moduleUrl("util.js"), LINE);
    ok(`Tokyo's clock differs from the page's here (${shown.want.when} / ${shown.before.when})`, shown.want.when !== shown.before.when);
    ok(`the first incident at once on Tokyo's clock (${shown.tokyo.when}, want ${shown.want.when})`, shown.tokyo.when.endsWith(shown.want.when));
    ok(`the line card's read time at once on Tokyo's clock (${shown.tokyo.read})`, shown.tokyo.read.startsWith(`read ${shown.want.read}`));
    eq("and back on the page's own clock", [shown.back.when.endsWith(shown.before.when), shown.back.read.startsWith(`read ${shown.before.read}`)].join(), "true,true");
  }],

  ["card model and view: the five cards of this group have fixtures that draw marks; a refresh of the list keeps unchanged rows and the focus on a row", async ({ eq, moduleUrl, ok, page, until }) => {
    // docs/themes.md, "Drawing a card": incidents, heat, matrix, strips and report are drawn from a view model
    const cards = await page.evaluate(async (u) => {
      const { drawApart, registeredCards } = await import(u);
      const out = {};
      for (const c of registeredCards().filter((r) => ["incidents", "heat", "matrix", "strips", "report"].includes(r.panel))) {
        const drawn = drawApart(c.panel, c.standard, c.fixture());
        out[c.panel] = { values: Object.keys(drawn.marks.values).length, status: Object.keys(drawn.marks.status).length, actions: drawn.marks.actions.length };
        drawn.remove();
      }
      return out;
    }, await moduleUrl("cards.js"));
    eq("every card has a fixture", Object.keys(cards).sort().join(), "heat,incidents,matrix,report,strips");
    for (const [panel, n] of Object.entries(cards)) ok(`${panel}: ${JSON.stringify(n)}`, n.values >= 5 && (n.actions >= 1 || panel === "report"));
    ok("incidents and the hop rows mark a status", cards.incidents.status >= 3 && cards.matrix.status >= 3);
    await until(() => document.querySelectorAll("#incidentsBody tr.incident-row").length >= 3);
    const kept = await page.evaluate(async (u) => {
      const { updateIncidentsTable } = await import(u);
      const list = await (await fetch("/api/incidents?limit=500")).json();
      const rows = [...document.querySelectorAll("#incidentsBody tr.incident-row")];
      rows.forEach((tr, i) => { tr.__kept = i; });
      const first = rows[0].querySelector("button.primary");
      first.focus();
      updateIncidentsTable(JSON.parse(JSON.stringify(list))); // the same list again: nothing is drawn again
      const same = [...document.querySelectorAll("#incidentsBody tr.incident-row")].every((tr, i) => tr.__kept === i) && document.activeElement === first;
      const last = rows[rows.length - 1];
      const changed = JSON.parse(JSON.stringify(list));
      changed.find((i) => String(i.id) === last.dataset.arg).title = "Renamed incident";
      updateIncidentsTable(changed); // one incident changed: its row is drawn again, the others stay
      const now = [...document.querySelectorAll("#incidentsBody tr.incident-row")];
      const result = { same, kept: now.slice(0, -1).every((tr, i) => tr.__kept === i), redrawn: now[now.length - 1].__kept === undefined, title: now[now.length - 1].textContent.includes("Renamed incident"),
        focus: document.activeElement === first };
      updateIncidentsTable(list);
      return result;
    }, await moduleUrl("../panels/incidents/model.js"));
    ok("the same list leaves every row and the focus alone", kept.same);
    ok("a changed incident redraws only its row", kept.kept && kept.redrawn && kept.title && kept.focus);
  }],
];
