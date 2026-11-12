// The diagnosis card: verdict, the path chain (a node per stage), the header pill's numbers.
import { ALL_VARIANTS } from "../lib.mjs";

export const subject = "panel:diagnosis";
export const variants = ["full"];


const measurePath = (page) => page.evaluate(() => {
  const chain = document.getElementById("pathChain");
  const nodes = [...chain.querySelectorAll(".node")];
  const origin = chain.querySelector(".origin");
  return {
    count: nodes.length,
    chainOverflows: chain.scrollWidth > chain.clientWidth + 1,
    kinds: nodes.map((n) => { const k = n.querySelector(".node-kind"); return { text: k.textContent, cut: k.scrollWidth > k.clientWidth }; }),
    named: nodes.filter((n) => n.dataset.node).every((n) => { const e = n.querySelector(".node-name"); return !e || !e.textContent || e.title === e.textContent || e.title.startsWith("Median of "); }),
    originCut: origin.scrollWidth > origin.clientWidth + 1 || [...origin.children].some((e) => e.scrollWidth > e.clientWidth + 1),
    originWidth: Math.round(origin.getBoundingClientRect().width),
    hostOverlap: nodes.some((n) => { // any node header: its label against its status icon
      const k = n.querySelector(".node-kind").getBoundingClientRect(); const s = n.querySelector(".node-st").getBoundingClientRect();
      return k.right > s.left + 0.5 && k.left < s.right && k.bottom > s.top && k.top < s.bottom;
    }),
    wordless: nodes.filter((n) => { const e = n.querySelector(".node-st"); return !e.querySelector(".st-word") || e.querySelector(".st-word").textContent !== e.title || e.scrollWidth > e.clientWidth + 1; }).length, // a status is its icon and its word, whole
    valuesCut: nodes.some((n) => [...n.querySelectorAll(".node-val, .node-meta")].some((e) => e.scrollWidth > e.clientWidth + 1))
  };
});

export const steps = [
  ["path health: verdict, one node per path stage with median, loss and a sparkline, header pill with numbers", async ({ click, count, eq, ok, page, showCard, text, until, waitCount }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 3); // router (local), line (gateway), internet (public); the DNS probes stay out
    const kinds = await page.$$eval("#pathChain .node .node-kind", (els) => els.map((e) => e.textContent).join(">"));
    eq("path order", kinds, "Router>Gateway>Internet");
    eq("origin marker, no status on it", await page.$eval("#pathChain .origin", (e) => `${e.querySelector(".origin-t").textContent}|${e.querySelector(".node-st, .badge") === null}`), "This host|true");
    await until(() => /^\d+(\.\d+)?ms$/.test(document.querySelector("#pathChain .node[data-node] .node-val")?.textContent ?? ""));
    await until(() => document.querySelectorAll("#pathChain .node[data-node] svg.spark path").length >= 6);
    eq("verdict badge", (await text("#statusBadge")).trim(), "Healthy");
    ok("status pill", /ms to internet/.test(await text("#statusPill")));
    eq("deltas on the links", await count("#pathChain .link .delta"), 3);
    await click('#pathChain .node[data-node^="Gateway"]');
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    ok("the gateway's history", /ISP Modem/.test(await text("#targetHistTitle")));
    eq("opened on Live, the range of the node's figure", await page.$eval('#targetHistRanges button[data-arg="300"]', (b) => b.getAttribute("aria-pressed")), "true");
    await until(() => {
      const v = document.querySelector('#pathChain .node[data-node^="Gateway"] .node-val').textContent.replace(/ms$/, "");
      return document.getElementById("targetHistNow").textContent.startsWith(`On the path now: ${v} ms (the median of its last 60 probes). The figures above cover the last 5 min.`);
    });
    await click("[data-action=closeModal][data-arg=targetHistModal]");
  }, { variants: ALL_VARIANTS }],

  ["the header is written only when the verdict or its figures change: messages that change nothing leave the pill (a live region), the bar, the card and the tab title alone", async ({ eq, nextMessages, page }) => {
    await page.evaluate(() => {
      let verdict = null;
      window.__smokeMutate = (d) => {
        d.targets.forEach((t) => Object.assign(t, { recent_latencies: Array(60).fill(5), last_ms: 5, loss_pct: 0, loss_recent_pct: 0, jitter_ms: 0.5, status: "up" }));
        if (d.diagnosis) d.diagnosis = (verdict ||= d.diagnosis);
      };
    });
    await nextMessages(2); // the fixed figures are drawn
    const writes = await page.evaluate(() => new Promise((resolve) => {
      const seen = [];
      const watch = new MutationObserver((records) => records.forEach((r) => seen.push(`${r.type} ${r.target.id || r.target.nodeName} ${r.attributeName || ""}`)));
      watch.observe(document.getElementById("statusPill"), { subtree: true, childList: true, characterData: true, attributes: true });
      watch.observe(document.getElementById("topbar"), { attributes: true, attributeFilter: ["class"] });
      watch.observe(document.body, { attributes: true, attributeFilter: ["data-status"] });
      watch.observe(document.querySelector("title"), { subtree: true, childList: true, characterData: true });
      watch.observe(document.getElementById("diagnosisBanner"), { attributes: true, attributeFilter: ["class", "data-status"] });
      const socket = window.__smokeSockets.at(-1);
      let n = 0;
      socket.addEventListener("message", function counted() {
        if (++n < 3) return;
        socket.removeEventListener("message", counted);
        setTimeout(() => { watch.disconnect(); resolve(seen); }, 50);
      });
    }));
    await page.evaluate(() => { window.__smokeMutate = null; });
    eq("writes to the header over three messages that change nothing", writes.join("; "), "");
  }],

  ["while this host cannot send pings, the path's nodes say Not probed (not No reply) and are not drawn as down", async ({ eq, page, showCard, until, waitCount }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 3);
    // as in real use: no new probes, so the last figures stay in the stats (stale) while the flag says why
    await page.evaluate(() => { window.__smokeMutate = (d) => d.targets.forEach((t) => { t.probing_unavailable = true; t.status = "down"; }); });
    await until(() => [...document.querySelectorAll("#pathChain .node[data-node] .node-val")].every((e) => e.textContent === "Not probed"));
    eq("no node is drawn down", await page.$$eval("#pathChain .node.st-crit", (els) => els.length), 0);
    eq("the header pill shows no stale figures", await page.$eval("#statusPillMetrics", (e) => e.textContent.trim()), "");
    await page.evaluate(() => { window.__smokeMutate = null; });
    await until(() => /^\d/.test(document.querySelector("#pathChain .node[data-node] .node-val")?.textContent ?? ""));
  }],

  ["path health with seven long-named nodes: short labels uncut, configured name as tooltip, no overlap, at 1440, 1024 and phone width", async ({ eq, layoutSettled, ok, outDir, page, path, showCard, until, waitCount }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 6);
    await until(() => /^\d/.test(document.querySelector("#pathChain .node[data-node] .node-val")?.textContent ?? ""));
    for (const [w, shot] of [[1440, "path-long-1440.png"], [1024, "path-long-1024.png"], [390, "path-long-390.png"]]) {
      await page.setViewport({ width: w, height: 1000 });
      await layoutSettled();
      const m = await measurePath(page);
      eq(`${w}: hop nodes`, m.count, 6);
      eq(`${w}: labels`, m.kinds.map((k) => k.text).join(">"), "Router>Gateway>ISP hop 1>ISP hop 2>ISP hop 3>Internet · 3");
      ok(`${w}: a label is cut: ${JSON.stringify(m.kinds)}`, m.kinds.every((k) => !k.cut));
      ok(`${w}: the chain overflows sideways`, !m.chainOverflows);
      ok(`${w}: the configured name is not in the tooltip`, m.named);
      ok(`${w}: a node label overlaps its status icon`, !m.hostOverlap);
      ok(`${w}: the origin marker is cut`, !m.originCut);
      ok(`${w}: the origin marker is as wide as a hop (${m.originWidth}px)`, w < 800 || m.originWidth <= 80);
      ok(`${w}: a value or meta line is cut`, !m.valuesCut);
      eq(`${w}: nodes whose status word is missing, differs from its title or is cut`, m.wordless, 0);
      await (await page.$("#diagnosisBanner")).screenshot({ path: path.join(outDir, shot) });
    }
    await page.setViewport({ width: 1400, height: 1000 });
  }, { variants: ["longpath"] }],

  ["path health with the IPv6 servers not probed (seven nodes): \"Not probed\" stays on one line at 1440, 1024 and phone width", async ({ eq, layoutSettled, ok, page, showCard, until, waitCount }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 6);
    // the third public server is the IPv6 group, and this host has no IPv6 (as strips.mjs makes it)
    await page.evaluate(() => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => {
        if (t.target_id === "lp_public3") Object.assign(t, { group: "internet_v6", address: "2001:db8::53", status: "down", last_ms: null, recent_latencies: Array(30).fill(null), loss_pct: 100, loss_recent_pct: 100, ipv6_unavailable: true });
      });
    });
    await waitCount("#pathChain .node[data-node]", 7);
    await until(() => document.querySelector('#pathChain .node[data-node$="internet_v6"] .node-val')?.textContent === "Not probed");
    for (const w of [1440, 1024, 390]) {
      await page.setViewport({ width: w, height: 1000 });
      await layoutSettled();
      const m = await page.evaluate(() => {
        const word = document.querySelector('#pathChain .node[data-node$="internet_v6"] .node-val b');
        const line = parseFloat(getComputedStyle(word).lineHeight) || word.getBoundingClientRect().height;
        return { lines: word.getClientRects().length, height: Math.round(word.getBoundingClientRect().height), line: Math.round(line), cut: word.scrollWidth > word.parentElement.clientWidth + 1,
          chain: document.getElementById("pathChain").scrollWidth > document.getElementById("pathChain").clientWidth + 1 };
      });
      eq(`${w}: nodes (the ISP hops fold into one below 1200 px)`, await page.$$eval("#pathChain .node", (n) => n.length), w >= 1200 ? 7 : w >= 800 ? 5 : 7);
      ok(`${w}: Not probed on one line (${JSON.stringify(m)})`, m.height <= m.line * 1.6 && !m.cut);
      ok(`${w}: the chain does not overflow sideways`, !m.chain);
    }
    await page.evaluate(() => { window.__smokeMutate = null; });
    await page.setViewport({ width: 1400, height: 1000 });
  }, { variants: ["longpath"] }],

  ["screenshots of the page with three public servers a few ms apart (PENGUMON_SMOKE_SHOTS=1)", async ({ outDir, page, path, SHOTS, showCard, until, waitCount }) => {
    if (!SHOTS) return;
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 6);
    const v = { lp_router: 5, lp_modem: 8, lp_isp1: 9, lp_isp2: 6.8, lp_isp3: 7, lp_public: 10, lp_public2: 20, lp_public3: 40 };
    await page.evaluate((x) => { window.__smokeMutate = (d) => d.targets.forEach((t) => { if (x[t.target_id] !== undefined) { t.recent_latencies = Array(30).fill(x[t.target_id]); t.last_ms = x[t.target_id]; } }); }, v);
    await new Promise((r) => setTimeout(r, 1500));
    for (const [w, h] of [[1440, 1000], [1024, 1000], [390, 844]]) {
      await page.setViewport({ width: w, height: h });
      await new Promise((r) => setTimeout(r, 600));
      await page.screenshot({ path: path.join(outDir, `shot-${w}-full.png`), fullPage: true });
      await (await page.$("#diagnosisBanner")).screenshot({ path: path.join(outDir, `shot-${w}-hero.png`) });
      await (await page.$("[data-panel=incidents] .panel")).screenshot({ path: path.join(outDir, `shot-${w}-incidents.png`) }).catch(() => {});
    }
    await page.evaluate(() => { window.__smokeMutate = null; });
    await page.setViewport({ width: 1400, height: 1000 });
  }, { variants: ["longpath"] }],

  ["one Internet figure: the median across the public targets in the hero node and the header pill, the node names them; a faster later hop reads as about 0 ms with a tooltip; the pills stay on their links at 1024", async ({ eq, layoutSettled, ok, page, showCard, until, waitCount }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 6);
    const series = { lp_router: 5, lp_modem: 8, lp_isp1: 9, lp_isp2: 6.8, lp_isp3: 7, lp_public: 10, lp_public2: 20, lp_public3: 40 };
    await page.evaluate((v) => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => { if (v[t.target_id] !== undefined) { t.recent_latencies = Array(30).fill(v[t.target_id]); t.last_ms = v[t.target_id]; } });
    }, series);
    await until(() => /^20\.0ms$/.test(document.querySelector('#pathChain .node[data-node^="Internet"] .node-val')?.textContent ?? ""));
    const node = await page.$eval('#pathChain .node[data-node^="Internet"]', (n) => ({ kind: n.querySelector(".node-kind").textContent, name: n.querySelector(".node-name").textContent, title: n.querySelector(".node-name").title }));
    eq("the node names its three targets", node.name, "Internet 1, Internet 2, Internet 3");
    ok(`the tooltip names the configured targets: ${node.title}`, /^Median of .*Example Public Resolver.*Second Example.*Third Example/.test(node.title));
    ok("the header pill has the same figure", /20\.0 ms to internet/.test(await page.$eval("#statusPill", (e) => e.textContent)));
    const deltas = await page.$$eval("#pathChain .link .delta", (els) => els.map((e) => ({ text: e.textContent, title: e.title })));
    eq("deltas", deltas.map((d) => d.text).join("|"), "5.0 ms|+3.0 ms|+1.0 ms|≈0 ms|+0.2 ms|+13.0 ms");
    const zero = deltas.find((d) => d.text === "≈0 ms");
    ok(`the near-zero pill explains itself: ${zero && zero.title}`, zero && /faster than the one before/.test(zero.title) && /not a fault/.test(zero.title));
    ok("no negative sign on a small negative delta", !deltas.some((d) => /^−\d(\.\d)? ms$/.test(d.text)));
    for (const w of [1024, 1440]) {
      await page.setViewport({ width: w, height: 1000 });
      await layoutSettled();
      const off = await page.$$eval("#pathChain .link", (links) => links.map((l) => {
        const a = l.getBoundingClientRect(); const d = l.querySelector(".delta").getBoundingClientRect(); const c = (r) => r.left + r.width / 2;
        return { dx: Math.abs(c(a) - c(d)), gap: d.top - a.bottom };
      }));
      ok(`${w}: a pill is centered on its link (${JSON.stringify(off)})`, off.every((o) => o.dx < 1.5));
      ok(`${w}: a pill is at most 30px below its link (${JSON.stringify(off)})`, off.every((o) => o.gap < 30));
      if (w === 1024) ok("1024: a stem ties each pill to its wire", await page.$eval("#pathChain .link", (l) => getComputedStyle(l, "::after").content !== "none"));
    }
    await page.evaluate(() => { window.__smokeMutate = null; });
    await page.setViewport({ width: 1400, height: 1000 });
  }, { variants: ["longpath"] }],
  ["a long path folds its ISP hops into one node, a hop that ignores ping is not down, other targets show under the chain", async ({ eq, ok, page, showCard, text, until }) => {
    // the live message gains n ISP hops after the gateway (the second one never answers ping, which the server flags:
    // engine/path_rules.py) and a custom target
    const mutate = (n, flag = true) => page.evaluate((count, quietFlag) => {
      window.__smokeMutate = (d) => {
        const gw = d.targets.find((t) => t.category === "gateway");
        const hops = Array.from({ length: count }, (_, k) => ({ ...gw, target_id: `x_isp${k}`, name: `Hop ${k + 3}: Example provider router ${k + 1}`, category: "isp",
          address: `198.51.100.${k + 1}`, last_ms: 5 + k, recent_latencies: Array(30).fill(5 + k), loss_pct: 0, loss_recent_pct: 0, status: "up" }));
        Object.assign(hops[1], { last_ms: null, recent_latencies: Array(30).fill(null), loss_pct: 100, loss_recent_pct: 100, status: "down", ignores_ping: quietFlag });
        const custom = { ...gw, target_id: "x_nas", name: "Example NAS", category: "custom", address: "192.0.2.50", last_ms: 0.4, recent_latencies: Array(30).fill(0.4), status: "up" };
        d.targets.splice(d.targets.indexOf(gw) + 1, 0, ...hops);
        d.targets.push(custom);
      };
    }, n, flag);
    const chain = () => page.evaluate(() => {
      const c = document.getElementById("pathChain");
      return { kinds: [...c.querySelectorAll(".node .node-kind")].map((e) => e.textContent), overflow: c.scrollWidth > c.clientWidth + 1,
        cut: [...c.querySelectorAll(".node-val, .node-meta, .node-kind")].some((e) => e.scrollWidth > e.clientWidth + 1) };
    });
    await page.setViewport({ width: 1440, height: 1000 });
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await mutate(9);
    await until(() => document.querySelector('#pathChain .node[data-node="ISP:run"]'));
    const long = await chain();
    eq("nine ISP hops fold into one node", long.kinds.join(">"), "Router>Gateway>ISP · 9>Internet");
    ok("nothing overflows or is cut", !long.overflow && !long.cut);
    ok("the folded node says what it holds", /9 hops, 8 answer · 1 ignores ping/.test(await text('#pathChain .node[data-node="ISP:run"] .node-name')));
    ok("the header pill counts no loss from the hop that ignores ping", !/loss/.test(await text("#statusPillMetrics")));
    eq("the custom target is under the chain", await page.$$eval("#pathExtra:not([hidden]) .pe", (els) => els.map((e) => e.textContent).join()), "Example NAS0.4 ms");
    await mutate(3);
    await until(() => document.querySelectorAll("#pathChain .node[data-node^='ISP:x_isp']").length === 3);
    const quiet = await page.evaluate(() => {
      const node = document.querySelector("#pathChain .node[data-node='ISP:x_isp1']");
      const link = document.querySelector("#pathChain .link[data-link='ISP:x_isp1']");
      const next = document.querySelector("#pathChain .link[data-link='ISP:x_isp2'] .delta");
      return { cls: node.className, word: node.querySelector(".node-st").title, broken: link.classList.contains("broken"), next: next.textContent };
    });
    ok("a hop that ignores ping is neutral, not down", /st-unknown/.test(quiet.cls) && !/alert/.test(quiet.cls) && quiet.word === "Ignores ping" && !quiet.broken);
    eq("the next link counts from the last hop that answered (5 ms to 7 ms)", quiet.next, "+2.0 ms");
    await showCard("matrix"); // not drawn while it is off screen
    await until(() => [...document.querySelectorAll("#hopMatrixBody tr")].some((r) => /no ping/.test(r.textContent)));
    const silentRows = await page.$$eval("#hopMatrixBody tr", (rs) => rs.map((r) => r.textContent).filter((t) => t.includes("Example provider router 2")));
    ok("Hop details says no ping, not lost", silentRows.length === 1 && /no ping/.test(silentRows[0]) && !/LOST/.test(silentRows[0]));
    // the page follows the server's flag, not a rule of its own: the same silent hop unflagged is down
    await showCard("diagnosis"); // back to Path health: the chain is not drawn while it is off screen
    await mutate(3, false);
    await until(() => document.querySelector("#pathChain .node[data-node='ISP:x_isp1'] .node-st")?.title === "Down");
    ok("an unflagged silent hop is down", /st-crit/.test(await page.$eval("#pathChain .node[data-node='ISP:x_isp1']", (n) => n.className)));
    await page.evaluate(() => { window.__smokeMutate = null; });
    await until(() => document.querySelectorAll("#pathChain .node[data-node]").length === 3 && document.getElementById("pathExtra").hidden);
    await page.setViewport({ width: 1400, height: 1000 });
  }],
  ["on a phone the header pill says the verdict in one word, uncut", async ({ eq, ok, page, phone, until }) => {
    await phone(async () => {
      await until(() => document.getElementById("statusPillWord").offsetParent !== null && document.getElementById("statusPillWord").textContent !== "Connecting");
      const pill = await page.evaluate(() => {
        const w = document.getElementById("statusPillWord");
        return { word: w.textContent, cut: w.scrollWidth > w.clientWidth + 1, title: document.getElementById("statusPillText").offsetParent !== null };
      });
      eq("the word", pill.word, "Healthy");
      ok("not cut, and the long title is left to the hero card", !pill.cut && !pill.title);
    });
  }],
  ["on a phone a long host name in the verdict sentence wraps and does not widen the page", async ({ ok, page, phone }) => {
    await phone(async () => {
      const wide = await page.evaluate(() => {
        const desc = document.getElementById("diagnosisDesc");
        const before = desc.textContent;
        desc.textContent = `40% of recent probes to the ${"a".repeat(63)}.${"b".repeat(63)}.${"c".repeat(63)} were lost.`;
        const width = document.documentElement.scrollWidth - document.documentElement.clientWidth;
        desc.textContent = before;
        return width;
      });
      ok("no sideways scroll", wide <= 1);
    });
  }],
  ["the always-on animations (flowing wire dashes, pulsing status dots) only animate transform and opacity, so they run on the compositor", async ({ eq, ok, page, showCard, until, waitCount }) => {
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await waitCount("#pathChain .node[data-node]", 3);
    await until(() => document.querySelector("#pathChain.is-live") !== null && document.querySelector("#pathChain .link:not(.broken) .wirebox") !== null);
    const found = await page.evaluate(() => document.getAnimations().map((a) => ({
      name: a.animationName, props: [...new Set(a.effect.getKeyframes().flatMap((k) => Object.keys(k)))].filter((p) => !["offset", "easing", "composite", "computedOffset"].includes(p)).sort().join(","),
      target: a.effect.target?.className || a.effect.target?.tagName
    })).filter((a) => ["flow", "breathe"].includes(a.name)));
    ok(`the wire dashes flow: ${JSON.stringify(found)}`, found.filter((a) => a.name === "flow").length === 3);
    ok(`the status dot pulses: ${JSON.stringify(found)}`, found.some((a) => a.name === "breathe"));
    eq("what flow and breathe animate", [...new Set(found.map((a) => `${a.name}:${a.props}`))].sort().join(" "), "breathe:opacity,transform flow:transform");
    // a broken link has no moving dashes
    const live = await page.$eval("#pathChain .link:not(.broken) .wirebox", (e) => getComputedStyle(e, "::after").animationName);
    eq("a live link's strip is animated", live, "flow");
  }],

  ["Path health: a failed route check leaves a standing line (when, why, which check the path is from), with or without unreviewed changes, and it goes when a check succeeds", async ({ eq, ok, page, stubFetch, text, until, waitText }) => {
    const at = Math.floor(Date.now() / 1000);
    const failed = { checked_at: at - 120, state: "failed", discovery_id: null, changes: [], good_at: at - 86400,
      error: "No trace reached a public server, so the route was not compared (the connection may have been down)." };
    await stubFetch({ "GET /api/path/drift": { body: failed } });
    await waitText("#pathBanner", /The last route check failed at/);
    const line = (await text("#pathBanner")).replace(/\s+/g, " ");
    ok(`the line says when, why and from when (${line})`, /The last route check failed at .+ \(no trace reached the internet, so the connection may have been down\); the path shown is from .+\./.test(line));
    ok("no address in it", !/\d+\.\d+\.\d+\.\d+|[0-9a-f:]{4,}::/.test(line));
    eq("quiet: no Review or Dismiss to press", await page.$$eval("#pathBanner button", (b) => b.length), 0);
    ok("not an alarm", !(await page.$eval("#pathBanner .result", (r) => r.classList.contains("st-warn") || r.classList.contains("st-crit"))));
    // a later check that succeeded: the line goes (the page asks again on a zone change as it does every five minutes)
    await page.evaluate(() => {
      const real = window.fetch;
      window.fetch = (url, opts) => (new URL(String(url), location.href).pathname === "/api/path/drift"
        ? Promise.resolve(new Response(JSON.stringify({ checked_at: Date.now() / 1000, state: "same", changes: [], discovery_id: "ok1", good_at: Date.now() / 1000, error: null }), { headers: { "content-type": "application/json" } }))
        : real(url, opts));
      document.dispatchEvent(new Event("zonechange"));
    });
    await until(() => document.getElementById("pathBanner").hidden);
    await stubFetch(null);
  }],
];
