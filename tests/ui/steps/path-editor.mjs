// The path editor (Path health > Edit). It runs last: its steps apply real paths (renamed, reordered, with added,
// removed and re-addressed hops), which the steps of other files do not expect. Discovery runs for real, on the made-up
// network of tests/ui/serve.py (fake traces, DNS and LAN pages); the first run stubs GET /api/path to a never-applied
// path, and the conflict is a stubbed 409 (a real one would count as a failed request). Preview and the earlier paths are
// the server's; the route-change notice (a background discovery) and a finished preview are answered by the page.

export const subject = "dialog:path-editor";
export const variants = ["full"];
export const runsLast = true;

// The live path, read by the page.
const livePath = (page) => page.evaluate(async () => (await (await fetch("/api/path")).json()));

// Opens the editor and waits for its cards.
async function openEditor({ click, until }) {
  await click("#btnEditPath");
  await until(() => document.querySelectorAll("#pathEditorHops .pe-hop").length > 0);
}

// Answers some fetch calls in the page itself ({"METHOD /path": body, or {seq: [body...]} for one answer per call, the
// last one repeated}) until the next reload, and keeps every call it saw in window.__calls ([key, body]).
const answerFetch = (page, stubs) => page.evaluate((s) => {
  window.__realFetch = window.__realFetch || window.fetch;
  window.__calls = [];
  window.fetch = (url, opts = {}) => {
    const key = `${(opts.method || "GET").toUpperCase()} ${new URL(String(url), location.href).pathname}`;
    window.__calls.push([key, opts.body ?? null]);
    const hit = s[key];
    if (hit === undefined) return window.__realFetch(url, opts);
    const body = hit && Array.isArray(hit.seq) ? (hit.seq.length > 1 ? hit.seq.shift() : hit.seq[0]) : hit;
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
  };
}, stubs);
const realFetch = (page) => page.evaluate(() => { if (window.__realFetch) window.fetch = window.__realFetch; });

export const steps = [
  ["path editor: every card shows what the prober measures now, in Path health's words and colors, updating while open; there is no ignores-ping control", async ({ click, count, eq, ok, page, text, until, waitText }) => {
    await openEditor({ click, until });
    const ms = /^\d+(\.\d)? ms · 0% loss \(last minute\)$/;
    for (const id of ["local_router", "isp_gateway", "cloudflare_dns"]) {
      await waitText(`#pathEditorHops [data-live="${id}"]`, ms);
      ok(`${id}: healthy`, await page.$eval(`#pathEditorHops [data-live="${id}"]`, (e) => e.classList.contains("st-good")));
      ok(`${id}: the chain node has the figure`, /^\d+(\.\d)? ms$/.test(await text(`#pathEditorChain [data-chain="${id}"]`)));
    }
    eq("no ignores-ping control", await count('#pathEditorModal [data-field=ignores_ping], #pathEditorModal select[aria-label*="ignore"]'), 0);
    ok("nor its words", !/Decide automatically/.test(await text("#pathEditorModal")));
    // the live message changes: the router stops answering while later hops do (the server flags it as ignoring
    // ping), the gateway is down, and the public server is IPv6 this host cannot reach
    await page.evaluate(() => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => {
        const silent = { last_ms: null, recent_latencies: Array(30).fill(null), loss_pct: 100, loss_recent_pct: 100, status: "down" };
        if (t.target_id === "local_router") Object.assign(t, silent, { ignores_ping: true });
        if (t.target_id === "isp_gateway") Object.assign(t, silent);
        if (t.target_id === "cloudflare_dns") Object.assign(t, { ipv6_unavailable: true });
      });
    });
    await waitText('#pathEditorHops [data-live="local_router"]', /^No reply · ignores ping \(not counted as an outage\)$/);
    ok("ignoring ping is neutral, with Path health's reason", await page.$eval('#pathEditorHops [data-live="local_router"]', (e) => e.classList.contains("st-unknown") && /hops after it do/.test(e.title)));
    await waitText('#pathEditorHops [data-live="isp_gateway"]', /^Not answering · 100% loss \(last minute\)$/);
    ok("a real outage is red", await page.$eval('#pathEditorHops [data-live="isp_gateway"]', (e) => e.classList.contains("st-crit")));
    await waitText('#pathEditorHops [data-live="cloudflare_dns"]', /^IPv6 unavailable on this host$/);
    eq("the chain says it too", await text('#pathEditorChain [data-chain="isp_gateway"]'), "No reply");
    // typing in a name keeps its focus while the figures update
    await page.$eval("#pathEditorHops .pe-name", (i) => i.focus());
    await page.evaluate(() => { window.__smokeMutate = null; });
    await waitText('#pathEditorHops [data-live="isp_gateway"]', ms);
    eq("focus kept across live updates", await page.evaluate(() => document.activeElement.classList.contains("pe-name")), true);
    await click("[data-action=cancelPathEditor]");
  }],

  ["path editor: each role says what it changes; \"Hop N:\" prefixes are offered for removal", async ({ click, eq, ok, page, text, until }) => {
    await openEditor({ click, until });
    ok("the router's role explained", /outage is placed in your home network/.test(await text('#pathEditorHops [data-hop="local_router"] .pe-role-fx')));
    await page.select('#pathEditorHops [data-arg="local_router"][data-field=category]', "custom");
    ok("follows the role", /never blamed for an outage/.test(await text('#pathEditorHops [data-hop="local_router"] .pe-role-fx')));
    await page.select("#pathAddRole", "isp");
    ok("and in Add a hop", /not counted as packet loss/.test(await text("#pathAddRoleFx")));
    ok("the offer counts the names", /^2 names start with "Hop N:"/.test(await text("#pathEditorClean")));
    await click("#pathEditorClean [data-action=cleanPathNames]");
    eq("the prefixes dropped", await page.$$eval("#pathEditorHops .pe-name", (els) => els.map((e) => e.value).slice(0, 2).join("|")), "Local Router (LAN)|ISP Modem / Gateway");
    eq("the offer is gone", await text("#pathEditorClean"), "");
    eq("nothing applied", (await livePath(page)).hops[0].name, "Hop 1: Local Router (LAN)");
    await page.type("#pathAddAddress", "127.0.0.1"); // an address a hop already has: the hint names that hop
    await page.keyboard.press("Enter");
    ok("the duplicate hint names the other hop", /^Added, but \S.* already has this address\./.test(await text("#pathAddMsg")));
    await click("[data-action=cancelPathEditor]");
    await click("#pathEditorNote [data-action=discardPathEditor]");
  }],

  ["path editor: the live path as cards; rename, remove with undo, add at a position, reorder, merge in the chain, test, apply goes live", async ({ click, count, eq, modalOpen, ok, page, shots, text, until, waitText }) => {
    const before = await livePath(page);
    ok("a stored path", before.revision >= 1 && before.hops.length >= 3);
    await openEditor({ click, until });
    ok("open", await modalOpen("pathEditorModal"));
    eq("one card per hop, in path order", await page.$$eval("#pathEditorHops .pe-hop", (els) => els.map((e) => e.dataset.hop).join()), before.hops.map((h) => h.id).join());
    eq("one chain node per hop", await page.$$eval("#pathEditorChain [data-action=focusPathHop]", (els) => els.map((e) => e.dataset.arg).join()), before.hops.map((h) => h.id).join());
    eq("roles in plain words", await page.$eval("#pathEditorHops .pe-hop select[data-field=category] option:checked", (o) => o.textContent), "Your network");

    // a node in the chain goes to its card
    await click('#pathEditorChain [data-action=focusPathHop][data-arg="isp_gateway"]');
    await until(() => document.activeElement?.closest("[data-hop]")?.dataset.hop === "isp_gateway");

    // rename the first hop (typing keeps the focus in the field)
    await page.$eval("#pathEditorHops .pe-hop:first-child .pe-name", (i) => { i.value = ""; i.focus(); });
    await page.keyboard.type("Home router");
    eq("focus stays while typing", await page.evaluate(() => document.activeElement.classList.contains("pe-name")), true);
    eq("the chain follows the name", await text('#pathEditorChain [data-arg="local_router"] .pe-node-n'), "Home router");

    // remove the last hop, undo it, remove it again and delete its history with it
    const last = before.hops[before.hops.length - 1].id;
    await click(`#pathEditorHops [data-action=removePathHop][data-arg="${last}"]`);
    await until((id) => document.querySelector(`#pathEditorRemoved [data-removed="${id}"]`), last);
    await click(`#pathEditorRemoved [data-action=undoRemovePathHop][data-arg="${last}"]`);
    await until((id) => document.querySelector(`#pathEditorHops [data-hop="${id}"]`) && document.getElementById("pathEditorRemoved").hidden, last);
    await click(`#pathEditorHops [data-action=removePathHop][data-arg="${last}"]`);
    await page.$eval(`#pathEditorRemoved [data-removed="${last}"] input[type=checkbox]`, (c) => c.click());

    // add two provider hops before hop 2: not probed yet, with a Test where the figures go
    for (const address of ["127.0.0.1", "localhost"]) {
      await page.type("#pathAddAddress", address);
      await page.select("#pathAddRole", "isp");
      await page.select("#pathAddPosition", "1");
      await page.evaluate(() => document.querySelector("#pathAddForm button[type=submit]").click());
      await until((a) => [...document.querySelectorAll("#pathEditorHops .pe-addr")].some((e) => e.textContent === a), address);
    }
    const ids = await page.$$eval("#pathEditorHops .pe-hop", (els) => els.map((e) => e.dataset.hop));
    eq("added before hop 2", ids.slice(1, 3).join(), "isp_localhost,isp_127_0_0_1");
    ok("a new hop is not probed yet", /^Not probed yet · Test$/.test(await text('#pathEditorHops [data-live="isp_localhost"]')));
    // reorder: the second added hop down one place, then merge it with the one before, in the chain
    await click('#pathEditorHops [data-arg="isp_localhost"][data-action=movePathHop][data-dir="1"]');
    await until(() => document.querySelectorAll("#pathEditorHops .pe-hop")[2]?.dataset.hop === "isp_localhost");
    eq("the move keeps the focus on the button", await page.evaluate(() => document.activeElement.dataset.dir), "1");
    eq("only two hops of one role can merge", await page.$$eval("#pathEditorChain [data-action=mergePathHops]", (els) => els.map((e) => e.dataset.arg).join()), "isp_localhost");
    await click('#pathEditorChain [data-action=mergePathHops][data-arg="isp_localhost"]');
    await until(() => document.querySelector('#pathEditorChain .pe-node.merged [data-action=splitPathHops][data-arg="isp_localhost"]'));
    ok("the card says it", /one node with 127\.0\.0\.1/.test(await text('#pathEditorHops [data-hop="isp_localhost"] .pe-hop-sub')));
    // a test probe of a new hop, shown where its figures go; a new hop at an address probed already has its figures
    await click('#pathEditorHops [data-live="isp_localhost"] [data-action=testPathHop]');
    await waitText('#pathEditorHops [data-live="isp_localhost"]', /Answered in 1\.5 ms/);
    ok("the address the router has is measured already", / loss \(last minute\)$/.test(await text('#pathEditorHops [data-live="isp_127_0_0_1"]')));
    await shots("path-editor");

    // nothing is live before Apply
    eq("not applied yet", (await livePath(page)).revision, before.revision);
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    await waitText("#wsText", /^Live/);
    const after = await livePath(page);
    eq("revision", after.revision, before.revision + 1);
    eq("the applied order", after.hops.map((h) => h.id).join(), [before.hops[0].id, "isp_127_0_0_1", "isp_localhost", ...before.hops.slice(1, -1).map((h) => h.id)].join());
    eq("renamed", after.hops[0].name, "Home router");
    ok("grouped", after.hops[1].group && after.hops[1].group === after.hops[2].group);
    ok("no hop carries an ignores-ping setting", after.hops.every((h) => !("ignores_ping" in h)));
    const live = await page.evaluate(async () => (await (await fetch("/api/targets")).json()).map((t) => t.target_id));
    ok("the added hops are probed", live.includes("isp_127_0_0_1") && live.includes("isp_localhost"));
    ok("the removed hop is not", !live.includes(last));
    await until(() => [...document.querySelectorAll("#pathChain .node-kind")].some((e) => e.textContent === "ISP hop 1 · 2"));
    eq("one ISP node for the group", await count('#pathChain .node[data-node^="ISP:"]'), 1);
    ok("the new name on Path health", /Home router/.test(await text("#pathChain")));
  }],

  ["path editor: a device line only on home hops, with human labels; Connect and Change; a hop at a device's address is linked by itself", async ({ click, count, eq, ok, page, text, until, waitText }) => {
    await openEditor({ click, until });
    const devs = await page.$$eval("#pathEditorHops .pe-hop", (els) => els.filter((e) => e.querySelector(".pe-dev")).map((e) => e.dataset.hop));
    eq("only the router and the gateway", devs.join(), "local_router,isp_gateway");
    ok("says what connecting gives", /Connect a configured device to see its status here/.test(await text('#pathEditorHops [data-hop="local_router"] .pe-dev')));
    ok("none yet", /No device connected · Connect/.test(await text('#pathEditorHops [data-hop="local_router"] .pe-dev-l')));
    // two devices configured: Connect asks which
    await click('#pathEditorHops [data-action=connectPathDevice][data-arg="local_router"]');
    await until(() => document.querySelector('#pathEditorHops [data-change=pickPathDevice][data-arg="local_router"]'));
    eq("human labels", await page.$$eval('#pathEditorHops [data-change=pickPathDevice][data-arg="local_router"] option', (o) => o.map((x) => x.textContent).join("|")), "No device|Xfinity gateway · modem|ASUS router · Example Router");
    await page.select('#pathEditorHops [data-change=pickPathDevice][data-arg="local_router"]', "router");
    // the device's own status, as its card says it (serve.py's router: healthy, CPU 23 %, three clients online)
    await waitText('#pathEditorHops [data-hop="local_router"] .pe-dev-l', /^Readings from: ASUS router · Example Router · Healthy · CPU 23% · 3 clients\s*Change$/);
    ok("and where its details are", /^Pengumon also reads this device directly; its details are in the Router panel\.$/.test(await text('#pathEditorHops [data-hop="local_router"] .pe-dev-why')));
    eq("signed in already: no setup opens", await page.$eval("#deviceSetupModal", (m) => m.classList.contains("active")), false);
    // the link goes to the Router panel: the draft has a change, so it asks first, then the editor closes
    await click('#pathEditorHops [data-hop="local_router"] [data-action=showDevicePanel]');
    await waitText("#pathEditorNote", /Your changes are not applied/);
    await click('#pathEditorHops [data-hop="local_router"] [data-action=showDevicePanel]');
    await until(() => !document.getElementById("pathEditorModal").classList.contains("active"));
    await until(() => { const r = document.querySelector("[data-panel=router] .panel").getBoundingClientRect(); return r.height > 0 && r.top < innerHeight && r.bottom > 0; });
    await openEditor({ click, until });
    await click('#pathEditorHops [data-action=connectPathDevice][data-arg="local_router"]');
    await page.select('#pathEditorHops [data-change=pickPathDevice][data-arg="local_router"]', "router");
    await waitText('#pathEditorHops [data-hop="local_router"] .pe-dev-l', /Readings from: ASUS router/);
    // a hop added at the router's address is linked by itself; another role has no device line
    await page.type("#pathAddAddress", "192.168.98.1");
    await page.select("#pathAddRole", "local");
    await page.select("#pathAddPosition", "1");
    await page.evaluate(() => document.querySelector("#pathAddForm button[type=submit]").click());
    const added = '#pathEditorHops [data-hop="local_192_168_98_1"]';
    await waitText(`${added} .pe-dev-l`, /Readings from: ASUS router · Example Router · Healthy · CPU 23% · 3 clients · linked by its address/);
    await page.select('#pathEditorHops [data-arg="local_192_168_98_1"][data-field=category]', "isp");
    eq("no device line on a provider hop", await count(`${added} .pe-dev`), 0);
    await page.select('#pathEditorHops [data-arg="local_192_168_98_1"][data-field=category]', "local");
    await waitText(`${added} .pe-dev-l`, /Readings from: ASUS router/);
    // Change: back to none on the first hop
    await click('#pathEditorHops [data-action=changePathDevice][data-arg="local_router"]');
    await page.select('#pathEditorHops [data-change=pickPathDevice][data-arg="local_router"]', "");
    await waitText('#pathEditorHops [data-hop="local_router"] .pe-dev-l', /No device connected/);
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    await waitText("#wsText", /^Live/);
    const hops = Object.fromEntries((await livePath(page)).hops.map((h) => [h.id, h]));
    eq("the added hop is linked", hops.local_192_168_98_1.device_id, "router");
    eq("the one set to none is not", hops.local_router.device_id, null);
    eq("and stays so: no automatic link", hops.local_router.device_unlinked, true);
    // Path health's Router node names the device in its tooltip
    await until(() => /Readings from: ASUS router · Example Router · Healthy/.test(document.querySelector('#pathChain .node[data-node^="Router"] .node-name')?.title || ""));
    // the server's proposal (it resolves names) for a home hop with no device, when the editor opens
    const path = await livePath(page);
    await answerFetch(page, { "GET /api/path": { ...path, device_matches: { isp_gateway: "modem" } } });
    await openEditor({ click, until });
    await waitText('#pathEditorHops [data-hop="isp_gateway"] .pe-dev-l', /^Readings from: Xfinity gateway · modem · (Line up|Warning) · SNR 35\.9 dB · 4 channels · linked by its address/);
    ok("the gateway's details are in the Line panel", /in the Line panel\.$/.test(await text('#pathEditorHops [data-hop="isp_gateway"] .pe-dev-why')));
    ok("the hop set to No device is not proposed again", /No device connected/.test(await text('#pathEditorHops [data-hop="local_router"] .pe-dev-l')));
    await realFetch(page);
    await click("[data-action=cancelPathEditor]");
  }],

  ["path editor: a hop's address replaced in place keeps its id (and so its history)", async ({ click, eq, ok, page, text, until, waitText }) => {
    await openEditor({ click, until });
    const card = '#pathEditorHops [data-hop="isp_localhost"]';
    await click(`${card} [data-action=editPathAddress]`);
    await until((c) => document.activeElement?.closest(".pe-addr-f") && document.activeElement.closest(c), card);
    ok("explained", /keeps its id and its history/.test(await text(`${card} .pe-addr-f`)));
    await page.$eval(`${card} .pe-addr-f [data-f=address]`, (i) => { i.value = "bad; address"; });
    await page.evaluate((c) => document.querySelector(`${c} .pe-addr-f button[type=submit]`).click(), card);
    ok("a bad address is refused", await page.$eval(`${card} .pe-addr-f`, (f) => f.classList.contains("invalid")));
    await page.$eval(`${card} .pe-addr-f [data-f=address]`, (i) => { i.value = "127.0.0.2"; });
    await page.evaluate((c) => document.querySelector(`${c} .pe-addr-f button[type=submit]`).click(), card);
    await waitText(`${card} .pe-addr`, /^127\.0\.0\.2$/);
    ok("marked, with the old address", await page.$eval(`${card} .chip.st-warn`, (c) => c.textContent === "New address" && /Was localhost/.test(c.title)));
    ok("not probed at the new address yet", /^Not probed yet/.test(await text(`${card} .pe-live`)));
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    await waitText("#wsText", /^Live/);
    const hop = (await livePath(page)).hops.find((h) => h.id === "isp_localhost");
    eq("same id, new address", hop && hop.address, "127.0.0.2");
    const events = await page.evaluate(async () => (await (await fetch("/api/path/events?target_id=isp_localhost")).json()));
    ok("recorded as an event", events.some((e) => e.kind === "address_changed" && e.old === "localhost" && e.new === "127.0.0.2"));
    await until(() => document.querySelector('#hopMatrixBody tr[data-arg="isp_localhost"]'));
    await click('#hopMatrixBody tr[data-arg="isp_localhost"]');
    await waitText("#targetHistEvents", /Address changed: localhost → 127\.0\.0\.2/);
    ok("marked on the strip", await page.$eval("#targetHistStrip .tgt-mark", (m) => /localhost → 127\.0\.0\.2/.test(m.title)).catch(() => false));
    await click("[data-action=closeModal][data-arg=targetHistModal]");
  }],

  ["path editor: Preview probes the hops not probed yet for 30 s and shows them on their cards; closing stops it", async ({ click, eq, ok, page, text, until, waitText }) => {
    await openEditor({ click, until });
    for (const address of ["198.51.100.77", "198.51.100.78"]) {
      await page.type("#pathAddAddress", address);
      await page.select("#pathAddRole", "isp");
      await page.evaluate(() => document.querySelector("#pathAddForm button[type=submit]").click());
      await until((a) => [...document.querySelectorAll("#pathEditorHops .pe-addr")].some((e) => e.textContent === a), address);
    }
    const [a, b] = ["isp_198_51_100_77", "isp_198_51_100_78"];
    // the server's preview, for real: the stand-in ICMP backend answers in 1.5 ms
    await answerFetch(page, {});
    await click("#btnPreviewPath");
    await waitText(`#pathEditorHops [data-live="${a}"]`, /^1\.5 ms · 0% loss \(previewing, \d+ s\)$/);
    await waitText(`#pathEditorHops [data-live="${b}"]`, /^1\.5 ms · 0% loss \(previewing, \d+ s\)$/);
    const posted = JSON.parse((await page.evaluate(() => window.__calls.find(([k]) => k === "POST /api/path/preview")))[1]);
    ok("the draft's hops are sent", posted.hops.some((h) => h.id === a && h.address === "198.51.100.77") && posted.hops.every((h) => !("joined" in h)));
    ok("a hop probed live keeps its live figures", / loss \(last minute\)$/.test(await text('#pathEditorHops [data-live="local_router"]')));
    const id = await page.evaluate(() => window.__calls.find(([k]) => k.startsWith("GET /api/path/preview/"))[0].split("/").pop());
    // closing the editor stops it
    await click("[data-action=cancelPathEditor]"); // the draft has an added hop: Cancel asks first
    await click("#pathEditorNote [data-action=discardPathEditor]");
    await until((p) => window.__calls.some(([k]) => k === `DELETE /api/path/preview/${p}`), id);
    eq("stopped on the server", await page.evaluate(async (p) => (await (await window.__realFetch(`/api/path/preview/${p}`)).json()).state, id), "done");
    eq("nothing applied", (await livePath(page)).hops.some((h) => h.id === a), false);
    // a finished preview, and a hop a router calls unreachable (answered by the page: 30 s of waiting otherwise)
    await openEditor({ click, until });
    await page.type("#pathAddAddress", "198.51.100.77");
    await page.select("#pathAddRole", "isp");
    await page.evaluate(() => document.querySelector("#pathAddForm button[type=submit]").click());
    await until(() => document.querySelector('#pathEditorHops [data-live="isp_198_51_100_77"]'));
    const done = { state: "done", elapsed_s: 30, hops: { [a]: { sent: 30, received: 0, loss_pct: 100, median_ms: null, last_ms: null, kind: "unreachable" } } };
    await answerFetch(page, { "POST /api/path/preview": { id: "pv1", probed: [a] }, "GET /api/path/preview/pv1": done });
    await click("#btnPreviewPath");
    await waitText(`#pathEditorHops [data-live="${a}"]`, /^Unreachable \(preview, 30 s\)$/);
    await waitText("#pathEditorNote", /Preview done/);
    await realFetch(page);
    await click("[data-action=cancelPathEditor]");
  }],

  ["path editor: an earlier path loads as a draft to restore; nothing is applied until Apply", async ({ click, eq, ok, page, text, until, waitText }) => {
    const path = await livePath(page);
    // Export shows the YAML with Copy and a download link, not a bare download: Chrome blocks a file downloaded over plain HTTP
    await openEditor({ click, until });
    ok("Export is a button", await page.$eval("#pathExport", (e) => e.tagName === "BUTTON"));
    await click("#pathExport");
    await until(() => document.getElementById("pathExportText")?.value.startsWith("#"));
    eq("the download link", await page.$eval("#pathExportView a[download]", (e) => e.getAttribute("href")), "/api/path/export");
    ok("the plain-HTTP note", /served over plain HTTP/.test(await text("#pathExportView")));
    await click("[data-action=pathExportCopy]");
    await waitText("#pathExportNote", /^Copied\.$/);
    eq("what was copied", await page.evaluate(() => window.__clip()), await page.$eval("#pathExportText", (e) => e.value));
    await click("#pathExport");
    ok("Export closes again", await page.$eval("#pathExportView", (e) => e.hidden));
    await click("[data-action=cancelPathEditor]");
    const versions = await page.evaluate(async () => (await (await fetch("/api/path/versions")).json()));
    eq("every Apply kept, newest first", versions[0].revision, path.revision);
    const previous = await page.evaluate(async (r) => (await (await fetch(`/api/path/versions/${r}`)).json()), versions[1].revision);
    await page.evaluate(() => history.pushState({ here: 1 }, ""));
    await openEditor({ click, until });
    await until(() => !document.getElementById("pathVersions").hidden);
    ok("the earlier ones listed with what changed, the current one not", (await text("#pathVersionList")).includes(`Revision ${versions[1].revision}`)
      && (await text("#pathVersionList")).includes(versions[1].summary) && !(await text("#pathVersionList")).includes(`Revision ${path.revision} `));
    await click("#pathVersionList [data-action=loadPathVersion]");
    await waitText("#pathEditorMeta", new RegExp(`revision ${versions[1].revision} as a draft`));
    eq("its hops", await page.$$eval("#pathEditorHops .pe-hop", (els) => els.map((e) => e.dataset.hop).join()), previous.hops.map((h) => h.id).join());
    const gone = path.hops.filter((h) => !previous.hops.some((p) => p.id === h.id)).map((h) => h.id).sort().join();
    eq("the others removed when applied, with an undo", (await page.$$eval("#pathEditorRemoved [data-removed]", (els) => els.map((e) => e.dataset.removed))).sort().join(), gone);
    eq("nothing applied", (await livePath(page)).revision, path.revision);
    // a loaded version is not what is applied: Cancel asks, and so do Back and a reload
    await click("[data-action=cancelPathEditor]");
    ok("Cancel asks and the editor stays open", await page.$eval("[data-action=discardPathEditor]", () => document.getElementById("pathEditorModal").classList.contains("active")));
    await page.evaluate(() => history.back());
    await until(() => /not applied/.test(document.getElementById("pathEditorNote").textContent) && history.state && history.state.pathEditor && !history.state.here);
    ok("Back kept the draft", await page.evaluate(() => document.getElementById("pathEditorModal").classList.contains("active")));
    ok("a reload would ask", await page.evaluate(() => { const ev = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(ev); return ev.defaultPrevented; }));
    await click("[data-action=discardPathEditor]");
    await until(() => !document.getElementById("pathEditorModal").classList.contains("active"));
    await until(() => history.state && history.state.here); // back on the entry the editor was opened from: no dead Back press is left
  }],

  ["path editor: loading an earlier path over edits asks first; Keep editing keeps the draft, Replace loads the path", async ({ click, eq, ok, page, text, until, waitText }) => {
    const versions = await page.evaluate(async () => (await (await fetch("/api/path/versions")).json()));
    await openEditor({ click, until });
    await until(() => !document.getElementById("pathVersions").hidden);
    const meta = () => page.$eval("#pathEditorMeta", (e) => e.textContent);
    // no edit yet: Load replaces the draft at once, as before (the step above); here an edit comes first
    await page.$eval("#pathEditorHops .pe-hop:first-child .pe-name", (i) => { i.value = ""; i.focus(); });
    await page.keyboard.type("Edited name");
    await click("#pathVersionList [data-action=loadPathVersion]");
    await waitText("#pathEditorNote", /Replace the draft with revision \d+\?/);
    eq("the draft is still the live path's", await meta(), "nothing changes until Apply");
    eq("Keep editing has the focus", await page.evaluate(() => document.activeElement.dataset.action), "keepPathDraft");
    await page.keyboard.press("Enter"); // keyboard only: Enter on Keep editing
    await until(() => document.getElementById("pathEditorNote").textContent === "");
    eq("the edit is still there", await page.$eval("#pathEditorHops .pe-hop:first-child .pe-name", (i) => i.value), "Edited name");
    eq("still the live path's draft", await meta(), "nothing changes until Apply");
    // Escape on the question is Keep editing too, and the editor stays open
    await click("#pathVersionList [data-action=loadPathVersion]");
    await waitText("#pathEditorNote", /Replace the draft/);
    await page.keyboard.press("Escape");
    await until(() => document.getElementById("pathEditorNote").textContent === "");
    ok("Escape on the question leaves the editor open", await page.evaluate(() => document.getElementById("pathEditorModal").classList.contains("active")));
    // Replace the draft: the earlier path is the draft now
    await click("#pathVersionList [data-action=loadPathVersion]");
    await waitText("#pathEditorNote", /Replace the draft/);
    await click("[data-action=replacePathDraft]");
    await waitText("#pathEditorMeta", /revision \d+ as a draft/);
    ok("the edit is gone with the replaced draft", !(await page.$$eval("#pathEditorHops .pe-name", (els) => els.some((e) => e.value === "Edited name"))));
    ok(`a version was loaded (${versions.length} kept)`, /as a draft/.test(await meta()));
    await click("[data-action=cancelPathEditor]");
    await click("[data-action=discardPathEditor]");
    await until(() => !document.getElementById("pathEditorModal").classList.contains("active"));
  }],

  ["path editor: a stale revision says so and applies nothing; Load the current path starts over", async ({ click, eq, ok, page, until, waitText }) => {
    const before = await livePath(page);
    await openEditor({ click, until });
    await page.$eval("#pathEditorHops .pe-hop:first-child .pe-name", (i) => { i.value = "Not applied"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    await page.evaluate(() => {
      window.__realFetch = window.__realFetch || window.fetch;
      window.fetch = (url, opts) => (opts && opts.method === "PUT" && String(url).endsWith("/api/path")
        ? Promise.resolve(new Response(JSON.stringify({ detail: "changed", revision: 99 }), { status: 409 })) : window.__realFetch(url, opts));
    });
    await page.$eval("#pathEditorModal .modal-b", (b) => { b.scrollTop = b.scrollHeight; }); // scrolled down, as in real use
    await click("#btnApplyPath");
    await waitText("#pathEditorNote", /changed elsewhere/);
    ok("the message is in view where the owner is looking", await page.evaluate(() => {
      const n = document.getElementById("pathEditorNote").getBoundingClientRect();
      const b = document.querySelector("#pathEditorModal .modal-b").getBoundingClientRect();
      return n.top >= b.top - 1 && n.bottom <= b.bottom + 1;
    }));
    await realFetch(page);
    eq("still open", await page.$eval("#pathEditorModal", (m) => m.classList.contains("active")), true);
    eq("nothing applied", JSON.stringify(await livePath(page)), JSON.stringify(before));
    await click("#pathEditorNote [data-action=reloadPathEditor]");
    await until(() => { const n = document.querySelector("#pathEditorHops .pe-name"); return n && n.value !== "Not applied" && document.getElementById("pathEditorNote").textContent === ""; });
    eq("the current path again", await page.$eval("#pathEditorHops .pe-name", (i) => i.value), before.hops[0].name);
    await page.focus("#pathEditorHops .pe-name");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("pathEditorModal").classList.contains("active"));
    // an edited draft is not thrown away by Esc: it asks first
    await openEditor({ click, until });
    await page.focus("#pathEditorHops .pe-name");
    await page.keyboard.type("x");
    await page.keyboard.press("Escape");
    await waitText("#pathEditorNote", /not applied/);
    eq("still open", await page.$eval("#pathEditorModal", (m) => m.classList.contains("active")), true);
    await click("#pathEditorNote [data-action=discardPathEditor]");
    await until(() => !document.getElementById("pathEditorModal").classList.contains("active"));
  }],

  ["path editor: Rediscover shows the real draft with confidence, reasons, a suggested driver, the IPv6 group and the difference; Connect opens device setup", async ({ click, count, eq, ok, page, shots, text, until, waitText }) => {
    const before = await livePath(page);
    await openEditor({ click, until });
    eq("the ASN lookup is off, as Settings has it by default", await page.$eval("#pathDiscAsn", (c) => c.checked), false);
    ok("with its disclosure", /Team Cymru/.test(await text(".pe-disclose")));
    await answerFetch(page, {});
    await click("#btnRediscover");
    await waitText("#pathEditorNote", /Discovery found \d+ hops/);
    eq("the options sent", await page.evaluate(() => window.__calls.find(([k]) => k === "POST /api/discovery")[1]), JSON.stringify({ asn_lookup: false, snmp: false }));
    await realFetch(page);
    const found = Number((await text("#pathEditorNote")).match(/Discovery found (\d+) hops/)[1]);
    eq("a card per hop found (not the live hops kept as not found)", await count("#pathEditorHops .pe-hop:not(.gone)"), found);
    ok("the difference: the router, two provider hops and six servers are new; the live hops it did not see are marked", /9 new, \d+ not found/.test(await text("#pathEditorNote")));
    ok("the notes", /IPv6 trace got no answer/.test(await text("#pathEditorNote")));
    const router = '#pathEditorHops [data-hop="hop_192_168_1_1"]';
    eq("the router's badges", await page.$$eval(`${router} .chip`, (els) => els.map((e) => e.textContent).join("|")), "Sure|New");
    eq("its reasons", await page.$$eval(`${router} .pe-ev li`, (els) => els.length), 3);
    eq("its role", await page.$eval(`${router} select[data-field=category]`, (s) => s.value), "local");
    ok("a provider hop with its reasons", (await page.$$eval('#pathEditorHops [data-hop="hop_198_51_100_1"] .pe-ev li', (els) => els.length)) >= 4);
    ok("the IPv6 servers are marked IPv6", /IPv6/.test(await text('#pathEditorHops [data-hop="cloudflare_dns_v6"] .pe-hop-sub')));
    ok("and one node in the chain", await page.$eval('#pathEditorChain [data-action=splitPathHops][data-arg="google_dns_v6"]', (b) => !!b.closest(".pe-node.merged")));
    ok("a live hop not found is marked", await page.$eval('#pathEditorHops [data-hop="isp_localhost"]', (e) => e.classList.contains("gone")));
    ok("the suggested driver", /Discovery thinks this is a device Pengumon can read: ASUS router\./.test(await text(`${router} .pe-dev`)));
    await shots("path-editor-discovery");
    await click(`${router} [data-action=connectPathDevice]`);
    await until(() => document.getElementById("deviceSetupModal").classList.contains("active"));
    ok("device setup is on top", await page.evaluate(() => {
      const r = document.querySelector("#deviceSetupModal .modal-content").getBoundingClientRect();
      return document.getElementById("deviceSetupModal").contains(document.elementFromPoint(r.x + r.width / 2, r.y + 20));
    }));
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
    eq("linked to the device", await text(`${router} .pe-dev-name`), "ASUS router · Example Router");
    await click("[data-action=cancelPathEditor]");
    await click("[data-action=discardPathEditor]"); // a Rediscover draft is not what is applied: Cancel asks first
    eq("nothing applied", (await livePath(page)).revision, before.revision);
  }],

  ["path editor: at 390 px the chain and the cards fit without sideways scroll, in both themes", async ({ click, noSideScroll, ok, page, phone, setTheme, until }) => {
    await phone(async () => {
      for (const theme of ["dark", "light"]) {
        await setTheme(theme);
        await openEditor({ click, until });
        await click('#pathEditorHops [data-action=changePathDevice], #pathEditorHops [data-action=editPathAddress]');
        ok(`no sideways scroll (${theme})`, await noSideScroll("pathEditorModal"));
        ok(`the chain runs down the page (${theme})`, await page.$eval("#pathEditorChain", (c) => getComputedStyle(c).flexDirection === "column" && c.scrollWidth <= c.clientWidth + 1));
        await click("[data-action=cancelPathEditor]");
      }
    });
  }],

  ["path editor: IPv6: a literal and a host name probed as IPv6, the family applied, an unavailable IPv6 hop tested neutral, the IPv6 servers their own node", async ({ click, eq, ok, page, text, until, waitText }) => {
    await openEditor({ click, until });
    for (const [address, family] of [["2001:db8::5", "4"], ["v6.example.com", "6"]]) {
      await page.type("#pathAddAddress", address);
      await page.select("#pathAddRole", "public");
      await page.select("#pathAddFamily", family);
      await page.evaluate(() => document.querySelector("#pathAddForm button[type=submit]").click());
      await until((a) => [...document.querySelectorAll("#pathEditorHops .pe-addr")].some((e) => e.textContent === a), address);
    }
    const lit = "public_2001_db8_5";
    const name = "public_v6_example_com";
    ok("an IPv6 literal is marked IPv6", /IPv6/.test(await text(`#pathEditorHops [data-hop="${lit}"] .pe-hop-sub`)));
    eq("a literal has no family to pick", await page.$$eval(`#pathEditorHops [data-arg="${lit}"][data-field=family]`, (e) => e.length), 0);
    eq("the host name's family", await page.$eval(`#pathEditorHops [data-arg="${name}"][data-field=family]`, (s) => s.value), "6");
    await answerFetch(page, { "POST /api/path/test": { kind: "ipv6_unavailable", rtt_ms: null, responder: null, address: null, error: "IPv6 unavailable on this host" } });
    await click(`#pathEditorHops [data-live="${name}"] [data-action=testPathHop]`);
    await waitText(`#pathEditorHops [data-live="${name}"]`, /IPv6 unavailable on this host: not probed, not an outage/);
    ok("shown neutral, not as a failure", await page.$eval(`#pathEditorHops [data-live="${name}"] .quiet`, () => true));
    eq("the test asks for the hop's family", await page.evaluate(() => window.__calls.find(([k]) => k === "POST /api/path/test")[1]), JSON.stringify({ address: "v6.example.com", family: 6 }));
    await realFetch(page);
    await click(`#pathEditorChain [data-action=mergePathHops][data-arg="${name}"]`); // the two IPv6 servers as one node
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    const hops = (await livePath(page)).hops;
    eq("families applied", JSON.stringify(hops.filter((h) => [lit, name].includes(h.id)).map((h) => [h.id, h.family, h.group])), JSON.stringify([[lit, 4, "internet_v6"], [name, 6, "internet_v6"]]));
    await until(() => [...document.querySelectorAll("#pathChain .node-kind")].some((e) => e.textContent === "Internet (IPv6) · 2"));
    ok("the IPv4 servers keep their own Internet node", [...await page.$$eval("#pathChain .node-kind", (els) => els.map((e) => e.textContent))].some((t) => /^Internet( ·|$)/.test(t)));
  }],

  ["Path health: \"Your path changed · Review\" opens the route check's draft; Dismiss hides it", async ({ click, eq, ok, page, stubFetch, text, until, waitText }) => {
    const draftHops = [{ id: "local_router", name: "Home router", address: "127.0.0.1", category: "local", enabled: true, position: 0, status: "same", evidence: [] },
      { id: "hop_198_51_100_5", name: "Provider hop (198.51.100.5)", address: "198.51.100.5", category: "isp", enabled: true, position: 1, status: "new", confidence: "likely", evidence: [{ kind: "rdns", text: "a provider name" }] }];
    await stubFetch({
      "GET /api/path/drift": { body: { checked_at: 1767225600, state: "changed", discovery_id: "rc1", changes: [{ kind: "new", hop: draftHops[1], text: "your provider's route changed: hop 2 is new" }, { kind: "gone", hop: {}, text: "hop 3 is gone" }] } },
      "GET /api/discovery/rc1": { body: { id: "rc1", state: "done", progress: "", draft: { hops: draftHops }, notes: [] } },
      "POST /api/path/drift/dismiss": { body: {} }
    });
    await waitText("#pathBanner", /Your path changed · your provider's route changed: hop 2 is new \(and 1 more\)/);
    ok("a warning, not an alarm", await page.$eval("#pathBanner .result", (r) => r.classList.contains("st-warn")));
    await click("#pathBanner [data-action=openPathEditor]");
    await waitText("#pathEditorNote", /Discovery found 2 hops/);
    eq("the draft's new hop", await text('#pathEditorHops [data-hop="hop_198_51_100_5"] .chip.st-good'), "New");
    await click("[data-action=cancelPathEditor]");
    await click("[data-action=discardPathEditor]"); // the route check's draft is not what is applied: Cancel asks
    await click("#pathBanner [data-action=dismissPathDrift]");
    await until(() => document.getElementById("pathBanner").hidden);
    ok("dismissed on the server", await page.evaluate(() => window.__stubbed.some(([k]) => k === "POST /api/path/drift/dismiss")));
    await stubFetch(null);
  }],

  ["view only: Edit opens nothing; the editor, reached anyway, has every control that changes something off", async ({ click, eq, ok, page, sleep, until }) => {
    await until(() => document.documentElement.hasAttribute("data-view-only"));
    eq("Edit is off for a viewer", await page.$eval("#btnEditPath", (b) => b.getAttribute("aria-disabled")), "true");
    // the editor as any other way in would open it (the dispatcher's own action, not the blocked button)
    await page.evaluate(() => { const b = document.createElement("button"); b.dataset.action = "openPathEditor"; b.hidden = true; document.body.appendChild(b); b.click(); b.remove(); });
    await until(() => document.querySelectorAll("#pathEditorHops .pe-hop").length > 0);
    await sleep(300);
    ok("it says View only", await page.$eval("#pathEditorReadOnly", (e) => !e.hidden && /View only/.test(e.textContent)));
    const state = await page.evaluate(() => {
      const modal = document.getElementById("pathEditorModal");
      const controls = [...modal.querySelectorAll("[data-needs-edit]")].flatMap((e) => (e.tagName === "FORM" ? [...e.querySelectorAll("button[type=submit]")] : [e]));
      return { count: controls.length, on: controls.filter((e) => e.getAttribute("aria-disabled") !== "true").map((e) => e.dataset.action || e.id || e.textContent.trim()),
        actions: [...new Set(controls.map((e) => e.dataset.action || (e.type === "submit" ? "addPathHop" : "")))].sort().join(),
        disabled: ["btnApplyPath", "btnPreviewPath", "btnRediscover"].map((id) => document.getElementById(id).disabled) };
    });
    ok(`every edit control is marked off (${state.on.join(", ")})`, state.count > 10 && !state.on.length);
    for (const a of ["applyPath", "importPath", "movePathHop", "previewPath", "rediscoverPath", "removePathHop", "addPathHop"]) ok(`${a} is among them (${state.actions})`, state.actions.split(",").includes(a));
    eq("Apply, Preview and Rediscover are disabled", state.disabled.join(), "true,true,true");
    const first = await page.$eval("#pathEditorHops .pe-hop", (e) => e.dataset.hop);
    await click(`#pathEditorHops [data-action=removePathHop][data-arg="${first}"]`);
    await sleep(200);
    ok("Remove does nothing", await page.$eval("#pathEditorRemoved", (e) => e.hidden));
    await click("[data-action=cancelPathEditor]");
  }, { variants: ["viewonly"] }],

  ["first run: a path never applied starts discovery, and the banner's Review opens its draft", async ({ click, ok, page, stubFetch, text, until, waitText }) => {
    const live = await livePath(page);
    await stubFetch({ "GET /api/path": { body: { ...live, revision: 0 } } });
    await waitText("#pathBanner", /Pengumon found your network path/);
    ok("the automatic run sends no hop addresses out", await page.evaluate(() => window.__fetched.some(([k, b]) => k === "POST /api/discovery" && b === JSON.stringify({ asn_lookup: false, snmp: false, first_run: true }))));
    ok("no route-change check on a first run", !(await page.evaluate(() => window.__fetched.some(([k]) => k === "GET /api/path/drift"))));
    await click("#pathBanner [data-action=openPathEditor]");
    await waitText("#pathEditorNote", /Discovery found \d+ hops/);
    const ids = await page.$$eval("#pathEditorHops .pe-hop", (els) => els.map((e) => e.dataset.hop));
    ok("the draft's cards", ids.includes("hop_192_168_1_1") && ids.includes("hop_198_51_100_2") && ids.includes("quad9_dns_v6"));
    ok("a draft", /draft from discovery/.test(await text("#pathEditorMeta")));
    ok("judged without the operator lookup", /ASN lookup was off/.test(await text("#pathEditorNote")));
    await click("[data-action=cancelPathEditor]");
    await click("[data-action=discardPathEditor]"); // a first-run draft is not applied yet: Cancel asks
    await stubFetch(null);
    await until(() => document.getElementById("pathBanner").hidden);
  }],

  ["path editor: More > Warn above is empty with the role's level as its placeholder; a value is applied (color only) and survives a reload", async ({ click, eq, ok, page, text, until }) => {
    await openEditor({ click, until });
    const hop = await page.$eval("#pathEditorHops .pe-hop", (e) => e.dataset.hop);
    const card = `#pathEditorHops [data-hop="${hop}"]`;
    const field = `${card} [data-field=warn_latency_ms]`;
    ok("More is closed while nothing is set", await page.$eval(`${card} .pe-more`, (d) => !d.open));
    await click(`${card} .pe-more summary`);
    eq("empty by default", await page.$eval(field, (i) => i.value), "");
    ok("the placeholder is the role's level", /^role level: \d+(\.\d+)? ms$/.test(await page.$eval(field, (i) => i.placeholder)));
    ok("the fine print says color only", /Only this hop's color\. Incidents still use the role's level\./.test(await text(`${card} .pe-warn-fx`)));
    await page.type(field, "0");
    ok("a bad value is said at once", /above 0/.test(await text(`${card} .pe-warn-fx`)) && await page.$eval(field, (i) => i.getAttribute("aria-invalid") === "true"));
    await page.$eval(field, (i) => { i.select(); });
    await page.keyboard.press("Backspace");
    await page.type(field, "33");
    eq("typing keeps the focus", await page.evaluate(() => document.activeElement.dataset.field), "warn_latency_ms");
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    await until(() => document.querySelector("[data-panel=report]")?.dataset.layoutHidden !== undefined);
    eq("the live hop has it", (await livePath(page)).hops.find((h) => h.id === hop).warn_latency_ms, 33);
    await openEditor({ click, until });
    ok("More opens by itself when a value is set", await page.$eval(`${card} .pe-more`, (d) => d.open));
    eq("the value survived the reload", await page.$eval(field, (i) => i.value), "33");
    // clear it again: the hop goes back to its role's level
    await page.$eval(field, (i) => { i.focus(); i.select(); });
    await page.keyboard.press("Backspace");
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    await until(() => document.querySelector("[data-panel=report]")?.dataset.layoutHidden !== undefined);
    eq("cleared", (await livePath(page)).hops.find((h) => h.id === hop).warn_latency_ms ?? null, null);
  }],
];
