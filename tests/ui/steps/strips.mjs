// The uptime strips and their window buttons.

export const subject = "panel:strips";
export const variants = ["full"];

// The model's own fetch of /api/uptime answered with made-up answers, in the page: window.__answer(answer, window) presses
// the window's button (the model fetches it then) and resolves once the answer is drawn; window.__answered() puts the
// real fetch back and shows the 24 h window again.
const answering = () => {
  const real = window.fetch;
  let next = null;
  let served = 0;
  window.fetch = (u, ...a) => (String(u).startsWith("/api/uptime") && next ? (served++, Promise.resolve(new Response(JSON.stringify(next)))) : real(u, ...a));
  window.__answer = async (answer, win = "24h") => {
    next = answer;
    const n = served;
    const select = document.querySelector("#stripsRanges select");
    select.value = win;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    while (served === n) await new Promise((r) => setTimeout(r, 20));
    await new Promise((r) => setTimeout(r, 100));
  };
  window.__answered = () => {
    window.fetch = real;
    const select = document.querySelector("#stripsRanges select");
    select.value = "24h";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  };
};

export const steps = [
  ["uptime strips: availability per row, 96 blocks a day, the 7 d and 24 h windows", async ({ chooseRange, eq, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip").length >= 3);
    ok("availability per row", /\d+\.\d\d%/.test(await text("#stripsBody .s-pct")));
    eq("96 blocks a day", await page.$eval("#stripsBody .strip", (e) => e.children.length), 96);
    const names = await page.$$eval("#stripsBody .s-name", (els) => els.map((e) => `${e.querySelector("span").textContent}|${e.querySelector("small")?.textContent ?? ""}`));
    eq("rows named as Path health names them, the configured name beside", names.slice(0, 3).join(","),
      "Router|Hop 1: Local Router (LAN),Gateway|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    await chooseRange("#stripsRanges", "7d");
    // the last 7 days ending now: 8 calendar days, the first and last cut (7 when now is exactly a midnight)
    await until(() => ["7", "8"].includes(document.getElementById("stripsBody").dataset.blocks));
    const cells = await page.$$eval("#stripsBody .strip:first-of-type .cell", (els) => els.map((e) => ({ tip: e.dataset.tip, w: e.getBoundingClientRect().width })));
    ok("the first and last days are cut: their tooltips say which hours, and they are narrower than a whole day",
      cells.length === 7 || (/\d\d:\d\d–24:00/.test(cells[0].tip) && /00:00–\d\d:\d\d/.test(cells[cells.length - 1].tip)
        && cells[0].w < cells[3].w && cells[cells.length - 1].w < cells[3].w));
    ok("a whole day's tooltip names no hours", !/\d\d:\d\d–/.test(cells[Math.floor(cells.length / 2)].tip));
    await chooseRange("#stripsRanges", "24h");
    await until(() => document.getElementById("stripsBody").dataset.blocks === "96");
  }],

  ["uptime strips: an answer that fails says so in the card (no page error), and the next one draws the blocks again", async ({ chooseRange, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip").length >= 3);
    await page.evaluate(() => {
      window.__realFetch = window.fetch;
      window.fetch = (u, ...a) => (String(u).startsWith("/api/uptime") ? Promise.resolve(new Response("", { status: 503 })) : window.__realFetch(u, ...a));
    });
    await chooseRange("#stripsRanges", "7d");
    await until(() => /Could not load the uptime: HTTP 503/.test(document.getElementById("stripsBody").textContent));
    ok("the key is still there", /No loss/.test(await text("[data-panel=strips] .strip-legend")));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await chooseRange("#stripsRanges", "24h");
    await until(() => document.getElementById("stripsBody").dataset.blocks === "96");
  }],

  ["uptime strips: the same answer again does not rebuild the blocks; a different one does", async ({ ok, page, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip .cell").length >= 288);
    // the model fetches the answers (made up here) when a window button asks it to
    await page.evaluate(answering);
    const result = await page.evaluate(async () => {
      const real = await (await fetch("/api/uptime?window=24h")).json();
      const cell = () => document.querySelector("#stripsBody .strip .cell");
      await window.__answer(real);
      cell().dataset.mark = "kept";
      await window.__answer(JSON.parse(JSON.stringify(real))); // what the five-minute refresh brings when nothing moved
      const same = cell().dataset.mark === "kept";
      const next = JSON.parse(JSON.stringify(real));
      next.targets[0].cells[0] = next.targets[0].cells[0] === "outage" ? "ok" : "outage";
      await window.__answer(next);
      return { same, changed: cell().dataset.mark === undefined };
    }).finally(() => page.evaluate(() => window.__answered()));
    ok("an identical answer leaves the blocks alone", result.same);
    ok("a changed block is drawn", result.changed);
    await until(() => document.getElementById("stripsBody").dataset.blocks === "96");
  }],

  ["uptime strips: a day block opens the server's day, on the clock of the zone it used, also after a 25-hour day", async ({ eq, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip .cell").length >= 288);
    // Berlin, 25 October 2026: the clocks go back (25 hours), so 26 October begins at 23:00 UTC, not 22:00; this
    // browser runs in another zone (UTC), and the labels are Berlin's dates all the same
    await page.evaluate(answering);
    const got = await page.evaluate(async () => {
      const real = await (await fetch("/api/uptime?window=24h")).json();
      const at = (d, h) => Date.UTC(2026, 9, d, h) / 1000;
      const bounds = [at(23, 22), at(24, 22), at(25, 23), at(26, 23), at(27, 23)];
      await window.__answer({ ...real, window: "7d", block_seconds: 86400, cell: "day", blocks: 4, start: bounds[0], end: bounds[4], bounds, zone: "Europe/Berlin",
        tz_offset_minutes: 60, targets: [{ ...real.targets[1], cells: ["ok", "ok", "outage", "ok"] }] }, "7d");
      const cell = document.querySelectorAll("#stripsBody .strip .cell")[2];
      cell.click();
      return { browser: Intl.DateTimeFormat().resolvedOptions().timeZone, start: Number(cell.dataset.start), end: Number(cell.dataset.end), want: [at(25, 23), at(26, 23)],
        day: new Intl.DateTimeFormat([], { weekday: "short", month: "short", day: "numeric", timeZone: "Europe/Berlin" }).format(at(25, 23) * 1000) };
    }).finally(() => page.evaluate(() => window.__answered()));
    ok(`the browser runs in another zone (${got.browser})`, got.browser !== "Europe/Berlin");
    eq("the block's window is the server's 26 October", `${got.start}-${got.end}`, got.want.join("-"));
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok(`the window names Berlin's day (${got.day})`, (await text("#incidentWindow")).includes(got.day));
    await page.evaluate(() => document.querySelector("[data-action=clearIncidentWindow]").click());
    await until(() => document.getElementById("incidentWindow").hidden);
    await until(() => document.getElementById("stripsBody").dataset.blocks === "96");
  }],

  ["uptime strips: every block opens its incidents", async ({ click, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip .cell.w, #stripsBody .strip .cell.c").length >= 1);
    ok("every block is a button, fine ones too", await page.$$eval("#stripsBody .strip > *", (els) => els.length > 5 && els.every((e) => e.tagName === "BUTTON"))
      && await page.$$eval("#stripsBody .strip > .cell[data-state=ok]", (els) => els.length > 5));
    await click("#stripsBody .strip .cell.w, #stripsBody .strip .cell.c");
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok("the window names the row and the block", /Gateway · \d\d:\d\d/.test(await text("#incidentWindow")));
    ok("the seeded drop is listed", /Short gateway drop/.test(await text("#incidentsBody")));
    await click("[data-action=clearIncidentWindow]");
    await until(() => document.getElementById("incidentWindow").hidden);
  }],

  ["on a phone a block opens the Incidents tab and scrolls to the list", async ({ ok, page, phone, until }) => {
    await phone(async () => {
      await page.evaluate(() => { location.hash = "live"; });
      await until(() => document.querySelectorAll("#stripsBody .strip .cell").length >= 1 && !document.querySelector("[data-panel=strips]").hidden);
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await page.evaluate(() => document.querySelector("#stripsBody .strip .cell").click());
      await until(() => location.hash === "#incidents" && !document.querySelector("[data-panel=incidents]").hidden);
      // the card, not the section: the section is display: contents and always measures 0
      await until(() => Math.abs(document.querySelector("[data-panel=incidents] .panel").getBoundingClientRect().top) < 80);
      ok("the window bar is shown", await page.$eval("#incidentWindow", (e) => !e.hidden));
      await page.evaluate(() => { document.querySelector("[data-action=clearIncidentWindow]").click(); location.hash = "live"; });
      await until(() => document.getElementById("incidentWindow").hidden);
    });
  }],

  ["a block of a quiet time lists nothing and says why", async ({ ok, page, text, until }) => {
    await page.evaluate(() => {
      const b = document.createElement("button");
      Object.assign(b.dataset, { action: "showIncidentWindow", start: "1000", end: "1900", label: "Quiet test", lost: "3", sent: "900" });
      document.body.append(b);
      b.click();
      b.remove();
    });
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok("explains the missing incident", /3 of 900 probes were lost/.test(await text("#incidentsBody")));
    await page.evaluate(() => document.querySelector("[data-action=clearIncidentWindow]").click());
    await until(() => document.getElementById("incidentWindow").hidden);
  }],

  ["a fine block of a quiet time says nothing failed", async ({ ok, page, text, until }) => {
    await page.evaluate(() => {
      const b = document.createElement("button");
      Object.assign(b.dataset, { action: "showIncidentWindow", start: "1000", end: "1900", label: "Quiet test", state: "ok" });
      document.body.append(b);
      b.click();
      b.remove();
    });
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok("says nothing failed", /this target lost no probes/.test(await text("#incidentsBody")));
    await page.evaluate(() => document.querySelector("[data-action=clearIncidentWindow]").click());
    await until(() => document.getElementById("incidentWindow").hidden);
  }],

  ["uptime strips on a phone: each name and availability over its strip, blocks of 5 px or more that scroll sideways together, opened at now (also when drawn on a hidden tab), names in view when scrolled back", async ({ chooseRange, eq, noSideways, ok, page, pageReady, phone, until }) => {
    await phone(async () => {
      // drawn while another tab is shown: the strips have no width then, and open at now when their tab is
      await page.evaluate(() => { location.hash = "incidents"; });
      await page.reload({ waitUntil: "load" });
      await pageReady();
      await until(() => document.querySelectorAll("#stripsBody .strip .cell").length >= 288 && document.querySelector("[data-panel=strips]").hidden);
      await page.evaluate(() => { location.hash = "live"; });
      await until(() => !document.querySelector("[data-panel=strips]").hidden);
      await page.$eval("[data-panel=strips] .panel", (e) => e.scrollIntoView());
      await new Promise((r) => setTimeout(r, 400));
      const look = () => page.evaluate(() => {
        const box = document.getElementById("stripsBody");
        const b = box.getBoundingClientRect();
        const strips = [...box.querySelectorAll(".strip")];
        const name = box.querySelector(".s-name").getBoundingClientRect();
        const pct = box.querySelector(".s-pct").getBoundingClientRect();
        const strip = strips[0].getBoundingClientRect();
        return { over: box.scrollWidth - box.clientWidth, left: box.scrollLeft, cell: Math.min(...[...strips[0].children].slice(1, -1).map((c) => c.getBoundingClientRect().width)),
          stacked: name.bottom <= strip.top + 1 && pct.bottom <= strip.top + 1 && pct.left > name.right, together: strips.every((s) => Math.abs(s.getBoundingClientRect().left - strip.left) < 1),
          namesIn: [...box.querySelectorAll(".s-name")].every((n) => n.getBoundingClientRect().left >= b.left - 1 && n.getBoundingClientRect().right <= b.right + 1),
          axis: box.querySelector(".s-axis").getBoundingClientRect().width >= strip.width - 1 };
      });
      const now = await look();
      ok(`the name and availability on a line over the strip`, now.stacked);
      ok(`the narrowest block is ${now.cell.toFixed(1)} px (at least 5)`, now.cell >= 5);
      ok(`96 blocks do not fit: the strips scroll sideways (${now.over} px)`, now.over > 100);
      ok(`opened at now, on a tab that was hidden when they were drawn (${now.left} of ${now.over} px)`, now.left >= now.over - 2);
      ok("the strips and the axis scroll together", now.together && now.axis);
      ok("nothing else sticks out sideways", await noSideways());
      await page.evaluate(() => { document.getElementById("stripsBody").scrollLeft = 0; });
      await new Promise((r) => setTimeout(r, 200));
      const back = await look();
      eq("scrolled back to the start", back.left, 0);
      ok("the names stay in view", back.namesIn && back.together);
      // another range opens at now again
      await chooseRange("#stripsRanges", "7d");
      await until(() => ["7", "8"].includes(document.getElementById("stripsBody").dataset.blocks));
      ok("a week fits without scrolling", (await look()).over <= 0);
      await chooseRange("#stripsRanges", "24h");
      await until(() => document.getElementById("stripsBody").dataset.blocks === "96");
      await new Promise((r) => setTimeout(r, 200));
      const again = await look();
      ok(`24 h again opens at now (${again.left} of ${again.over} px)`, again.over > 100 && again.left >= again.over - 2);
      await page.evaluate(() => { location.hash = "live"; });
    });
  }],

  ["a hop that ignores ping is neutral in Uptime and its history, says so as the path does, counts no outage and opens no incidents", async ({ click, eq, ok, page, showCard, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip").length >= 3);
    // the router stops answering while the line and the internet still do: the server flags it as ignoring ping
    await page.evaluate(() => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => {
        if (t.target_id === "local_router") Object.assign(t, { status: "down", last_ms: null, recent_latencies: Array(30).fill(null), loss_pct: 100, loss_recent_pct: 100, ignores_ping: true });
      });
    });
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await until(() => document.querySelector("#pathChain .node[data-node^='Router'] .node-st")?.title === "Ignores ping");
    await until(() => document.querySelectorAll("#stripsBody .strip-quiet").length === 1);
    const row = await page.evaluate(() => {
      const quiet = document.querySelector("#stripsBody .strip-quiet");
      const name = quiet.previousElementSibling;
      return { name: name.querySelector("span").textContent, text: quiet.textContent, label: quiet.getAttribute("aria-label"), pct: quiet.nextElementSibling.textContent,
        cells: quiet.querySelectorAll("button, .cell").length, others: document.querySelectorAll("#stripsBody .strip").length };
    });
    eq("the router's row", row.name, "Router");
    ok(`the same words as the path (${row.text})`, /^Ignores ping/.test(row.text) && /not counted as an outage/.test(row.label));
    eq("no availability figure", row.pct, "–");
    eq("no blocks to open incidents with", row.cells, 0);
    ok("the other rows keep their blocks", row.others >= 2);
    await click("#stripsBody .s-name[data-arg=local_router]");
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    await until(() => document.querySelector("#targetHistStrip .strip-quiet") && /Ignores ping/.test(document.querySelector("#targetHistStats .val").textContent));
    eq("its history counts no availability or loss", await page.$$eval("#targetHistStats .val", (v) => `${v[3].textContent}|${v[4].textContent}`), "–|–");
    await click("[data-action=closeModal][data-arg=targetHistModal]");
    await page.evaluate(() => { window.__smokeMutate = null; });
    await until(() => document.querySelectorAll("#stripsBody .strip-quiet").length === 0);
    ok("answering again: blocks again", (await text("#stripsBody")).includes("%"));
  }],

  ["an IPv6 target while this host has no IPv6 is neutral on the path, in Uptime, Hop details and its history, with its own words", async ({ click, eq, ok, page, showCard, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip").length >= 3);
    // the internet target as an IPv6 one the server cannot probe (engine/prober.py): its last figures are stale, its
    // stale status "down" must not show; and a second public target keeps answering, so the Internet node stays judged
    await page.evaluate(() => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => {
        if (t.target_id === "cloudflare_dns") Object.assign(t, { address: "2606:4700:4700::1111", status: "down", last_ms: null, recent_latencies: Array(30).fill(null), loss_pct: 100, loss_recent_pct: 100, ipv6_unavailable: true });
      });
    });
    await showCard("diagnosis"); // Path health is not drawn while it is off screen
    await until(() => document.querySelector("#pathChain .node[data-node^='Internet'] .node-st")?.title === "IPv6 unavailable on this host");
    const node = await page.$eval("#pathChain .node[data-node^='Internet']", (n) => ({ cls: n.className, val: n.querySelector(".node-val").textContent,
      meta: n.querySelector(".node-meta").textContent, why: n.querySelector(".node-meta span").title }));
    ok(`the Internet node is neutral, not down (${node.cls})`, /st-unknown/.test(node.cls) && !/alert/.test(node.cls));
    eq("it says it is not probed", node.val, "Not probed");
    eq("and why, in a line", node.meta, "not probed · no IPv6 here");
    ok("the tooltip says it is no outage and is checked again", /not an outage/.test(node.why) && /every few minutes/.test(node.why));
    await until(() => document.querySelectorAll("#stripsBody .strip-quiet").length === 1);
    const row = await page.evaluate(() => {
      const quiet = document.querySelector("#stripsBody .strip-quiet");
      return { name: quiet.previousElementSibling.querySelector("span").textContent, text: quiet.textContent, pct: quiet.nextElementSibling.textContent, cells: quiet.querySelectorAll("button, .cell").length };
    });
    eq("the internet's uptime row", row.name, "Internet");
    ok(`with the same words (${row.text})`, /^IPv6 unavailable on this host · not probed, not counted$/.test(row.text));
    eq("no availability, no blocks", `${row.pct}|${row.cells}`, "–|0");
    await showCard("matrix"); // not drawn while it is off screen
    const hop = await page.evaluate(() => {
      const tr = [...document.querySelectorAll("#hopMatrixBody tr")].find((r) => r.textContent.includes("Cloudflare"));
      return { loss: tr.querySelector("[data-f=loss]").textContent, title: tr.querySelector("[data-f=loss]").title, last: tr.querySelector("[data-f=last]").textContent };
    });
    eq("Hop details: no IPv6, not LOST", `${hop.loss}|${hop.last}`, "no IPv6|not probed");
    ok("Hop details explains it", /no IPv6 route/.test(hop.title));
    await click("#stripsBody .s-name[data-arg=cloudflare_dns]");
    await until(() => document.getElementById("targetHistModal")?.classList.contains("active"));
    await until(() => document.querySelector("#targetHistStrip .strip-quiet") && /IPv6 unavailable/.test(document.querySelector("#targetHistStats .val").textContent));
    eq("its history counts no availability or loss", await page.$$eval("#targetHistStats .val", (v) => `${v[3].textContent}|${v[4].textContent}`), "–|–");
    await click("[data-action=closeModal][data-arg=targetHistModal]");
    // a second public target that answers: the Internet node is judged by it alone; when it fails too, it is down
    const second = (status) => page.evaluate((st) => {
      window.__smokeMutate = (d) => {
        const cf = d.targets.find((t) => t.target_id === "cloudflare_dns");
        Object.assign(cf, { status: "up", last_ms: 50, recent_latencies: Array(30).fill(50), loss_pct: 0, loss_recent_pct: 0, ipv6_unavailable: true });
        const down = st === "down";
        d.targets.push({ ...cf, target_id: "x_quad9", name: "Public: Example Resolver (192.0.2.9)", address: "192.0.2.9", ipv6_unavailable: false, status: st,
          last_ms: down ? null : 10, recent_latencies: Array(30).fill(down ? null : 10), loss_pct: down ? 100 : 0, loss_recent_pct: down ? 100 : 0 });
      };
    }, status);
    await showCard("diagnosis"); // back to Path health: the chain is not drawn while it is off screen
    await second("up");
    await until(() => /^10\.0ms$/.test(document.querySelector("#pathChain .node[data-node^='Internet'] .node-val")?.textContent ?? ""));
    eq("the unprobed target's stale 50 ms is no part of the Internet figure", await text("#pathChain .node[data-node^='Internet'] .node-val"), "10.0ms");
    await second("down");
    await until(() => /st-crit/.test(document.querySelector("#pathChain .node[data-node^='Internet']")?.className ?? ""));
    ok("its stale \"up\" does not keep the Internet node up", /st-crit/.test(await page.$eval("#pathChain .node[data-node^='Internet']", (n) => n.className)));
    await page.evaluate(() => { window.__smokeMutate = null; });
    await until(() => document.querySelectorAll("#stripsBody .strip-quiet").length === 0);
    ok("IPv6 back: blocks again", (await text("#stripsBody")).includes("%"));
  }],

  ["every window draws the uptime strips (cells wide enough to see, week cells past 90 days, dates on the axis), on a desktop, a phone and in every theme", async ({ atEveryRange, chooseRange, eq, everyLook, noSideways, ok, openPanel, page, phone, rangeState, settled, showCard }) => {
    const WANT = { "24h": [96, 96], "7d": [7, 8], "30d": [30, 31], "90d": [90, 91], all: [26, 27] };
    const check = (where) => async (id) => {
      const what = `${where} ${id}`;
      const cells = await page.$$eval("#stripsBody .strip:first-of-type .cell", (els) => els.map((e) => ({ tip: e.dataset.tip, w: e.getBoundingClientRect().width })));
      const [lo, hi] = WANT[id];
      ok(`${what}: ${cells.length} cells (${lo}-${hi})`, cells.length >= lo && cells.length <= hi);
      const narrowest = Math.min(...cells.slice(1, -1).map((c) => c.w)); // (the first and last cell hold only the hours the window reaches: narrower by design)
      ok(`${what}: the narrowest whole cell is ${narrowest.toFixed(1)} px (at least 2.5, 5 on a phone: its strips scroll sideways rather than draw them thinner)`, narrowest >= (where === "phone" ? 5 : 2.5));
      if (id === "all") ok(`${what}: week cells say "week of" (${cells[1].tip})`, cells.every((c) => /week of [A-Z][a-z]{2} \d{1,2}/.test(c.tip)));
      const axis = await page.$$eval("#stripsBody .s-axis span", (e) => e.map((x) => x.textContent));
      ok(`${what}: the axis names dates (${axis.join(" | ")})`, id === "24h" ? axis.slice(0, -1).every((l) => /^\d{1,2}:\d{2}/.test(l)) : axis.slice(0, -1).every((l) => /^[A-Z][a-z]{2} \d{1,2}$/.test(l)));
      ok(`${what}: the header says what the cells are (${await page.$eval("#stripsMeta", (e) => e.textContent)})`, new RegExp(id === "24h" ? "15-min blocks" : id === "all" ? "a block per week" : "a block per day").test(await page.$eval("#stripsMeta", (e) => e.textContent)));
      ok(`${what}: nothing sticks out sideways`, await noSideways());
    };
    await showCard("strips");
    eq("24 h to All", (await rangeState("#stripsRanges")).ids.join(), "24h,7d,30d,90d,all");
    await atEveryRange("#stripsRanges", "#stripsBody", check("desktop"));
    await everyLook(async (look) => {
      await showCard("strips");
      for (const id of ["24h", "all"]) {
        await chooseRange("#stripsRanges", id);
        await settled();
        await check(look)(id);
      }
    });
    await phone(async () => {
      await openPanel("strips", "live");
      await atEveryRange("#stripsRanges", "#stripsBody", check("phone"));
    });
  }, { variants: ["longhistory"] }],
];
