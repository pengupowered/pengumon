// The path editor (Path health > Edit). It runs last: its first step applies a real path (renamed, reordered, with
// added and removed hops), which the steps of other files do not expect. Discovery runs for real, on the made-up network
// of tests/ui/serve.py (fake traces, DNS and LAN pages); the first run stubs GET /api/path to a never-applied path, and
// the conflict is a stubbed 409 (a real one would count as a failed request).

export const subject = "dialog:path-editor";
export const variants = ["full"];
export const runsLast = true;

export const steps = [
  ["path editor: the live path as cards; rename, remove with undo, add at a position, reorder, group, test, apply goes live", async ({ click, count, eq, modalOpen, ok, page, shots, text, until, waitText }) => {
    const before = await page.evaluate(async () => (await (await fetch("/api/path")).json()));
    ok("a stored path", before.revision >= 1 && before.hops.length >= 3);
    await click("#btnEditPath");
    await until(() => document.querySelectorAll("#pathEditorHops .pe-hop").length > 0);
    ok("open", await modalOpen("pathEditorModal"));
    eq("one card per hop, in path order", await page.$$eval("#pathEditorHops .pe-hop", (els) => els.map((e) => e.dataset.hop).join()), before.hops.map((h) => h.id).join());
    eq("roles in plain words", await page.$eval("#pathEditorHops .pe-hop select[data-field=category] option:checked", (o) => o.textContent), "Your network");

    // rename the first hop (typing keeps the focus in the field)
    await page.$eval("#pathEditorHops .pe-hop:first-child .pe-name", (i) => { i.value = ""; i.focus(); });
    await page.keyboard.type("Home router");
    eq("focus stays while typing", await page.evaluate(() => document.activeElement.classList.contains("pe-name")), true);

    // remove the last hop, undo it, remove it again and delete its history with it
    const last = before.hops[before.hops.length - 1].id;
    await click(`#pathEditorHops [data-action=removePathHop][data-arg="${last}"]`);
    await until((id) => document.querySelector(`#pathEditorRemoved [data-removed="${id}"]`), last);
    await click(`#pathEditorRemoved [data-action=undoRemovePathHop][data-arg="${last}"]`);
    await until((id) => document.querySelector(`#pathEditorHops [data-hop="${id}"]`) && document.getElementById("pathEditorRemoved").hidden, last);
    await click(`#pathEditorHops [data-action=removePathHop][data-arg="${last}"]`);
    await page.$eval(`#pathEditorRemoved [data-removed="${last}"] input[type=checkbox]`, (c) => c.click());

    // add two provider hops before hop 2, as one node
    for (const address of ["127.0.0.1", "localhost"]) {
      await page.type("#pathAddAddress", address);
      await page.select("#pathAddRole", "isp");
      await page.select("#pathAddPosition", "1");
      await page.evaluate(() => document.querySelector("#pathAddForm button[type=submit]").click());
      await until((a) => [...document.querySelectorAll("#pathEditorHops .pe-addr")].some((e) => e.textContent === a), address);
    }
    const ids = await page.$$eval("#pathEditorHops .pe-hop", (els) => els.map((e) => e.dataset.hop));
    eq("added before hop 2", ids.slice(1, 3).join(), "isp_localhost,isp_127_0_0_1");
    // reorder: the second added hop down one place, then group it with the one before
    await click('#pathEditorHops [data-arg="isp_localhost"][data-action=movePathHop][data-dir="1"]');
    await until(() => document.querySelectorAll("#pathEditorHops .pe-hop")[2]?.dataset.hop === "isp_localhost");
    eq("the move keeps the focus on the button", await page.evaluate(() => document.activeElement.dataset.dir), "1");
    await page.$eval('#pathEditorHops [data-arg="isp_localhost"][data-field=joined]', (c) => c.click());
    await page.select('#pathEditorHops [data-arg="isp_localhost"][data-field=ignores_ping]', "yes");
    // a test probe of a hop
    await click('#pathEditorHops [data-arg="isp_127_0_0_1"][data-action=testPathHop]');
    await waitText('#pathEditorHops [data-hop="isp_127_0_0_1"] .pe-test', /Answered in 1\.5 ms/);
    await shots("path-editor");

    // nothing is live before Apply
    eq("not applied yet", (await page.evaluate(async () => (await (await fetch("/api/path")).json()))).revision, before.revision);
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    await waitText("#wsText", /^Live/);
    const after = await page.evaluate(async () => (await (await fetch("/api/path")).json()));
    eq("revision", after.revision, before.revision + 1);
    eq("the applied order", after.hops.map((h) => h.id).join(), [before.hops[0].id, "isp_127_0_0_1", "isp_localhost", ...before.hops.slice(1, -1).map((h) => h.id)].join());
    eq("renamed", after.hops[0].name, "Home router");
    ok("grouped", after.hops[1].group && after.hops[1].group === after.hops[2].group);
    eq("the owner's ignores-ping answer", after.hops[2].ignores_ping, true);
    const live = await page.evaluate(async () => (await (await fetch("/api/targets")).json()).map((t) => t.target_id));
    ok("the added hops are probed", live.includes("isp_127_0_0_1") && live.includes("isp_localhost"));
    ok("the removed hop is not", !live.includes(last));
    await until(() => [...document.querySelectorAll("#pathChain .node-kind")].some((e) => e.textContent === "ISP hop 1 · 2"));
    eq("one ISP node for the group", await count('#pathChain .node[data-node^="ISP:"]'), 1);
    ok("the new name on Path health", /Home router/.test(await text("#pathChain")));
  }],

  ["path editor: a stale revision says so and applies nothing; Load the current path starts over", async ({ click, eq, page, until, waitText }) => {
    const before = await page.evaluate(async () => (await (await fetch("/api/path")).json()));
    await click("#btnEditPath");
    await until(() => document.querySelectorAll("#pathEditorHops .pe-hop").length > 0);
    await page.$eval("#pathEditorHops .pe-hop:first-child .pe-name", (i) => { i.value = "Not applied"; i.dispatchEvent(new Event("input", { bubbles: true })); });
    await page.evaluate(() => {
      window.__realFetch = window.fetch;
      window.fetch = (url, opts) => (opts && opts.method === "PUT" && String(url).endsWith("/api/path")
        ? Promise.resolve(new Response(JSON.stringify({ detail: "changed", revision: 99 }), { status: 409 })) : window.__realFetch(url, opts));
    });
    await click("#btnApplyPath");
    await waitText("#pathEditorNote", /changed elsewhere/);
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    eq("still open", await page.$eval("#pathEditorModal", (m) => m.classList.contains("active")), true);
    eq("nothing applied", JSON.stringify(await page.evaluate(async () => (await (await fetch("/api/path")).json()))), JSON.stringify(before));
    await click("#pathEditorNote [data-action=reloadPathEditor]");
    await until(() => { const n = document.querySelector("#pathEditorHops .pe-name"); return n && n.value !== "Not applied" && document.getElementById("pathEditorNote").textContent === ""; });
    eq("the current path again", await page.$eval("#pathEditorHops .pe-name", (i) => i.value), before.hops[0].name);
    await page.focus("#pathEditorHops .pe-name");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("pathEditorModal").classList.contains("active"));
  }],

  ["path editor: Rediscover shows the real draft with confidence, reasons, a suggested driver, the IPv6 group and the difference; Connect opens device setup", async ({ click, count, eq, ok, page, shots, text, until, waitText }) => {
    const before = await page.evaluate(async () => (await (await fetch("/api/path")).json()));
    await click("#btnEditPath");
    await until(() => document.querySelectorAll("#pathEditorHops .pe-hop").length > 0);
    eq("the ASN lookup is ticked by default", await page.$eval("#pathDiscAsn", (c) => c.checked), true);
    ok("with its disclosure", /Team Cymru/.test(await text(".pe-disclose")));
    await page.evaluate(() => {
      window.__posted = [];
      window.__realFetch = window.fetch;
      window.fetch = (url, opts) => {
        if (String(url).endsWith("/api/discovery") && opts?.method === "POST") window.__posted.push(JSON.parse(opts.body));
        return window.__realFetch(url, opts);
      };
    });
    await click("#btnRediscover");
    await waitText("#pathEditorNote", /Discovery found \d+ hops/);
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    eq("the options sent", JSON.stringify(await page.evaluate(() => window.__posted[0])), JSON.stringify({ asn_lookup: true, snmp: false }));
    const found = Number((await text("#pathEditorNote")).match(/Discovery found (\d+) hops/)[1]);
    eq("a card per hop", await count("#pathEditorHops .pe-hop"), found);
    ok("the difference: the router, two provider hops and six servers are new; the live hops it did not see are marked", /9 new, \d+ not found/.test(await text("#pathEditorNote")));
    ok("the notes", /IPv6 trace got no answer/.test(await text("#pathEditorNote")));
    const router = '#pathEditorHops [data-hop="hop_192_168_1_1"]';
    eq("the router's badges", await page.$$eval(`${router} .chip`, (els) => els.map((e) => e.textContent).join("|")), "Sure|New");
    eq("its reasons", await page.$$eval(`${router} .pe-ev li`, (els) => els.length), 3);
    eq("its role", await page.$eval(`${router} select[data-field=category]`, (s) => s.value), "local");
    ok("a provider hop with its reasons", (await page.$$eval('#pathEditorHops [data-hop="hop_198_51_100_1"] .pe-ev li', (els) => els.length)) >= 4);
    ok("the IPv6 servers are marked IPv6", /IPv6/.test(await text('#pathEditorHops [data-hop="cloudflare_dns_v6"] .pe-hop-sub')));
    ok("and one node", await page.$eval('#pathEditorHops [data-arg="google_dns_v6"][data-field=joined]', (c) => c.checked));
    ok("a live hop not found is marked", await page.$eval('#pathEditorHops [data-hop="cloudflare_dns"]', (e) => e.classList.contains("gone")));
    ok("the suggested driver", /Looks like a device the asuswrt driver can read: connect it for more detail/.test(await text(`${router} .pe-suggest`)));
    await shots("path-editor-discovery");
    await click('#pathEditorHops [data-action=connectPathDevice][data-arg="hop_192_168_1_1"]');
    await until(() => document.getElementById("deviceSetupModal").classList.contains("active"));
    ok("device setup is on top", await page.evaluate(() => {
      const r = document.querySelector("#deviceSetupModal .modal-content").getBoundingClientRect();
      return document.getElementById("deviceSetupModal").contains(document.elementFromPoint(r.x + r.width / 2, r.y + 20));
    }));
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
    eq("linked to the device", await page.$eval(`${router} [data-field=device_id]`, (s) => s.value), "router");
    await click("[data-action=cancelPathEditor]");
    eq("nothing applied", (await page.evaluate(async () => (await (await fetch("/api/path")).json()))).revision, before.revision);
  }],

  ["path editor: at 390 px the cards fit without sideways scroll, in both themes", async ({ click, noSideScroll, ok, phone, setTheme, until }) => {
    await phone(async () => {
      for (const theme of ["dark", "light"]) {
        await setTheme(theme);
        await click("#btnEditPath");
        await until(() => document.querySelectorAll("#pathEditorHops .pe-hop").length > 0);
        ok(`no sideways scroll (${theme})`, await noSideScroll("pathEditorModal"));
        await click("[data-action=cancelPathEditor]");
      }
    });
  }],

  ["path editor: IPv6: a literal and a host name probed as IPv6, the family applied, an unavailable IPv6 hop tested neutral, the IPv6 servers their own node", async ({ click, eq, ok, page, text, until, waitText }) => {
    await click("#btnEditPath");
    await until(() => document.querySelectorAll("#pathEditorHops .pe-hop").length > 0);
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
    await page.evaluate(() => {
      window.__tests = [];
      window.__realFetch = window.fetch;
      window.fetch = (url, opts) => {
        if (String(url).endsWith("/api/path/test")) {
          window.__tests.push(JSON.parse(opts.body));
          return Promise.resolve(new Response(JSON.stringify({ kind: "ipv6_unavailable", rtt_ms: null, responder: null, address: null, error: "IPv6 unavailable on this host" }), { status: 200 }));
        }
        return window.__realFetch(url, opts);
      };
    });
    await click(`#pathEditorHops [data-arg="${name}"][data-action=testPathHop]`);
    await waitText(`#pathEditorHops [data-hop="${name}"] .pe-test`, /IPv6 unavailable on this host: not probed, not an outage/);
    ok("shown neutral, not as a failure", await page.$eval(`#pathEditorHops [data-hop="${name}"] .pe-test .quiet`, () => true));
    eq("the test asks for the hop's family", JSON.stringify(await page.evaluate(() => window.__tests[0])), JSON.stringify({ address: "v6.example.com", family: 6 }));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await page.$eval(`#pathEditorHops [data-arg="${name}"][data-field=joined]`, (c) => c.click()); // the two IPv6 servers as one node
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnApplyPath")]);
    const hops = (await page.evaluate(async () => (await (await fetch("/api/path")).json()))).hops;
    eq("families applied", JSON.stringify(hops.filter((h) => [lit, name].includes(h.id)).map((h) => [h.id, h.family, h.group])), JSON.stringify([[lit, 4, "internet_v6"], [name, 6, "internet_v6"]]));
    await until(() => [...document.querySelectorAll("#pathChain .node-kind")].some((e) => e.textContent === "Internet (IPv6) · 2"));
    ok("the IPv4 servers keep their own Internet node", [...await page.$$eval("#pathChain .node-kind", (els) => els.map((e) => e.textContent))].some((t) => /^Internet( ·|$)/.test(t)));
  }],

  ["first run: a path never applied starts discovery, and the banner's Review opens its draft", async ({ click, ok, page, stubFetch, text, until, waitText }) => {
    const live = await page.evaluate(async () => (await (await fetch("/api/path")).json()));
    await stubFetch({ "GET /api/path": { body: { ...live, revision: 0 } } });
    await waitText("#pathBanner", /Pengumon found your network path/);
    ok("the automatic run sends no hop addresses out", await page.evaluate(() => window.__fetched.some(([k, b]) => k === "POST /api/discovery" && b === JSON.stringify({ asn_lookup: false, snmp: false, first_run: true }))));
    await click("#pathBanner [data-action=openPathEditor]");
    await waitText("#pathEditorNote", /Discovery found \d+ hops/);
    const ids = await page.$$eval("#pathEditorHops .pe-hop", (els) => els.map((e) => e.dataset.hop));
    ok("the draft's cards", ids.includes("hop_192_168_1_1") && ids.includes("hop_198_51_100_2") && ids.includes("quad9_dns_v6"));
    ok("a draft", /draft from discovery/.test(await text("#pathEditorMeta")));
    ok("judged without the operator lookup", /ASN lookup was off/.test(await text("#pathEditorNote")));
    await click("[data-action=cancelPathEditor]");
    await stubFetch(null);
    await until(() => document.getElementById("pathBanner").hidden);
  }],
];
