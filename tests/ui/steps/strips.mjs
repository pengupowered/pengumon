// The uptime strips and their window buttons.

export const subject = "panel:strips";
export const variants = ["full"];

export const steps = [
  ["uptime strips: availability per row, 96 blocks a day, the 7 d and 24 h windows", async ({ click, eq, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip").length >= 3);
    ok("availability per row", /\d+\.\d\d%/.test(await text("#stripsBody .s-pct")));
    eq("96 blocks a day", await page.$eval("#stripsBody .strip", (e) => e.children.length), 96);
    const names = await page.$$eval("#stripsBody .s-name", (els) => els.map((e) => `${e.querySelector("span").textContent}|${e.querySelector("small")?.textContent ?? ""}`));
    eq("rows named as Path health names them, the configured name beside", names.slice(0, 3).join(","),
      "Router|Hop 1: Local Router (LAN),Line|Hop 2: ISP Modem / Gateway,Internet|Public: Cloudflare Anycast (1.1.1.1)");
    await click('[data-action=setUptimeWindow][data-arg="7d"]');
    await until(() => document.getElementById("stripsBody").dataset.blocks === "7");
    await click('[data-action=setUptimeWindow][data-arg="24h"]');
    await until(() => document.getElementById("stripsBody").dataset.blocks === "96");
  }],

  ["uptime strips: every block opens its incidents", async ({ click, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip .cell.w, #stripsBody .strip .cell.c").length >= 1);
    ok("every block is a button, fine ones too", await page.$$eval("#stripsBody .strip > *", (els) => els.length > 5 && els.every((e) => e.tagName === "BUTTON"))
      && await page.$$eval("#stripsBody .strip > .cell[data-state=ok]", (els) => els.length > 5));
    await click("#stripsBody .strip .cell.w, #stripsBody .strip .cell.c");
    await until(() => !document.getElementById("incidentWindow").hidden);
    ok("the window names the row and the block", /Line · \d\d:\d\d/.test(await text("#incidentWindow")));
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

  ["uptime strips on a phone: every block keeps a visible width", async ({ ok, page, phone, until }) => {
    await phone(async () => {
      await until(() => document.querySelectorAll("#stripsBody .strip").length >= 3);
      const w = await page.$eval("#stripsBody .strip > *", (e) => e.getBoundingClientRect().width);
      ok(`a block is ${w.toFixed(2)} px wide`, w >= 0.75); // the gaps once left the blocks about 0.1 px
    });
  }],

  ["a hop that ignores ping is neutral in Uptime and its history, says so as the path does, counts no outage and opens no incidents", async ({ click, eq, ok, page, text, until }) => {
    await until(() => document.querySelectorAll("#stripsBody .strip").length >= 3);
    // the router stops answering while the line and the internet still do: the server flags it as ignoring ping
    await page.evaluate(() => {
      window.__smokeMutate = (d) => d.targets.forEach((t) => {
        if (t.target_id === "local_router") Object.assign(t, { status: "down", last_ms: null, recent_latencies: Array(30).fill(null), loss_pct: 100, loss_recent_pct: 100, ignores_ping: true });
      });
    });
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
];
