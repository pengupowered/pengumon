// The ISP outage dossier dialog.

export const subject = "dialog:dossier";
export const variants = ["full"];

export const steps = [
  ["dossier modal renders Markdown, changes window, copies and downloads", async ({ click, eq, fs, ok, outDir, page, path, text, until, waitText }) => {
    await click("[data-action=openDossierModal]");
    await until(() => /^#{1,3} /m.test(document.getElementById("dossierOutput").textContent));
    await page.select("#dossierWindowSelect", "72");
    await until(() => performance.getEntriesByType("resource").some((e) => e.name.includes("/api/incidents/dossier?hours=72")));
    await until(() => !document.getElementById("btnCopyDossier").disabled); // copy and download wait for the window's answer
    await page.screenshot({ path: path.join(outDir, "dossier.png") });
    await click("#btnCopyDossier");
    await waitText("#btnCopyDossier", /Copied/);
    eq("clipboard", await page.evaluate(() => window.__clip()), await text("#dossierOutput"));
    const file = path.join(outDir, "example_isp_outage_dossier_72h.md");
    fs.rmSync(file, { force: true });
    await click("[data-action=downloadDossierMarkdown]");
    for (let i = 0; i < 50 && !fs.existsSync(file); i++) await new Promise((r) => setTimeout(r, 100));
    ok("downloaded file", fs.existsSync(file) && fs.readFileSync(file, "utf8") === (await text("#dossierOutput")));
    await click("[data-action=closeModal][data-arg=dossierModal]");
  }],

  ["dossier: an older, slower answer does not replace the selected window's; copy and download are that window's", async ({ click, eq, fs, ok, outDir, page, path, text, until }) => {
    await page.evaluate(() => { // the 30-day answer is slow
      window.__dossierFetch = window.fetch;
      window.__dossierSeen = [];
      window.fetch = async (input, init) => {
        const url = String(input);
        const res = await window.__dossierFetch(input, init);
        if (url.includes("/api/incidents/dossier?")) {
          window.__dossierSeen.push(url);
          if (url.includes("hours=168")) window.__week = (await res.clone().json()).formatted_markdown;
          if (url.includes("hours=720")) await new Promise((r) => setTimeout(r, 1500));
        }
        return res;
      };
    });
    await click("[data-action=openDossierModal]");
    await until(() => !document.getElementById("btnCopyDossier").disabled);
    const select = (v) => page.select("#dossierWindowSelect", v);
    await select("720");
    await select("168");
    await until(() => window.__dossierSeen.length >= 3 && !document.getElementById("btnCopyDossier").disabled);
    await new Promise((r) => setTimeout(r, 2000)); // the slow answer has come in by now
    const week = await page.evaluate(() => window.__week);
    ok("the 7-day text is known", typeof week === "string" && week.length > 0);
    eq("the window says 7 days", await page.$eval("#dossierWindowSelect", (e) => e.value), "168");
    eq("the document is the 7-day one", await text("#dossierOutput"), week);
    await click("#btnCopyDossier");
    eq("the copy is the 7-day one", await page.evaluate(() => window.__clip()), week);
    const file = path.join(outDir, "example_isp_outage_dossier_168h.md");
    fs.rmSync(file, { force: true });
    await click("[data-action=downloadDossierMarkdown]");
    for (let i = 0; i < 50 && !fs.existsSync(file); i++) await new Promise((r) => setTimeout(r, 100));
    ok("the download is named and filled for 7 days", fs.existsSync(file) && fs.readFileSync(file, "utf8") === week);
    await page.evaluate(() => { window.fetch = window.__dossierFetch; });
    await click("[data-action=closeModal][data-arg=dossierModal]");
  }],

  ["dossier: a window chosen while one is being built waits for that answer; the server builds one at a time", async ({ click, eq, page, text, until }) => {
    await page.evaluate(() => {
      window.__dossierFetch = window.fetch;
      window.__dossierLive = 0;
      window.__dossierMax = 0;
      window.__dossierCodes = [];
      window.fetch = async (input, init) => {
        if (!String(input).includes("/api/incidents/dossier?")) return window.__dossierFetch(input, init);
        window.__dossierMax = Math.max(window.__dossierMax, ++window.__dossierLive);
        try {
          const res = await window.__dossierFetch(input, init);
          window.__dossierCodes.push(res.status);
          return res;
        } finally { window.__dossierLive--; }
      };
    });
    await click("[data-action=openDossierModal]");
    await until(() => !document.getElementById("btnCopyDossier").disabled);
    await page.evaluate(() => { window.__dossierMax = 0; window.__dossierCodes = []; });
    for (const v of ["720", "72", "168"]) await page.select("#dossierWindowSelect", v);
    await until(() => window.__dossierLive === 0 && !document.getElementById("btnCopyDossier").disabled);
    eq("never two dossier requests at once", await page.evaluate(() => window.__dossierMax), 1);
    eq("none was refused", await page.evaluate(() => window.__dossierCodes.every((c) => c === 200)), true);
    eq("the window is the last one chosen", await page.$eval("#dossierWindowSelect", (e) => e.value), "168");
    await until(() => /168 hours|7 days/.test(document.getElementById("dossierOutput").textContent));
    eq("the document is for that window", (await text("#dossierOutput")).length > 0, true);
    await page.evaluate(() => { window.fetch = window.__dossierFetch; });
    await click("[data-action=closeModal][data-arg=dossierModal]");
  }],

  ["dossier: the ISP name is read at every opening, so a change in Settings shows in the title", async ({ click, eq, page, until }) => {
    await click("[data-action=openDossierModal]"); // the first opening has read the name
    await until(() => !document.getElementById("btnCopyDossier").disabled);
    await click("[data-action=closeModal][data-arg=dossierModal]");
    await page.evaluate(() => {
      window.__infoFetch = window.fetch;
      window.fetch = (url, opts) => (String(url) === "/api/info"
        ? Promise.resolve(new Response(JSON.stringify({ isp_name: "Renamed Telecom" }), { status: 200, headers: { "content-type": "application/json" } })) : window.__infoFetch(url, opts));
    });
    await click("[data-action=openDossierModal]");
    await until(() => /Renamed Telecom/.test(document.getElementById("dossierTitleText").textContent));
    eq("the title", await page.$eval("#dossierTitleText", (e) => e.textContent), "Renamed Telecom Outage & Escalation Dossier");
    await page.evaluate(() => { window.fetch = window.__infoFetch; });
    await click("[data-action=closeModal][data-arg=dossierModal]");
  }],

  ["dossier: the report as a document, the Markdown view, copy and download unchanged, dark and light", async ({ click, count, dialogShot, eq, modalOpen, ok, page, setTheme, shown, tabStaysIn, text, until }) => {
    await setTheme("dark");
    await click("[data-action=openDossierModal]");
    await until(() => document.querySelectorAll("#dossierDoc h4").length >= 4);
    eq("four summary figures", await count("#dossierDoc .doc-stats .stat"), 4);
    ok("loss per hop as a table", (await count("#dossierDoc table tbody tr")) >= 3);
    ok("the hop column counts the hops, it does not print target ids", await page.$$eval("#dossierDoc table tbody tr td:first-child", (cells) => cells.slice(0, 3).every((c) => /^Hop \d+$/.test(c.textContent.trim()))));
    ok("a path of hops", (await count("#dossierDoc .doc-path .dp")) >= 3);
    ok("no raw Markdown marks in the document", !/\*\*|^#{1,3} |\| :---/m.test(await page.$eval("#dossierDoc", (e) => e.innerText)));
    eq("the raw text is not shown", await shown("#dossierOutput"), false);
    eq("keyboard focus starts on close", await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Close");
    await dialogShot("dossier-dark");
    await setTheme("light");
    await dialogShot("dossier-light");
    await setTheme("dark");
    await click('#dossierViews button[data-arg="md"]');
    eq("markdown shown", await shown("#dossierOutput"), true);
    eq("document hidden", await shown("#dossierDoc"), false);
    ok("it is the copied text", /^# /.test(await text("#dossierOutput")));
    await click("#btnCopyDossier");
    eq("copy works from the Markdown view", await page.evaluate(() => window.__clip()), await text("#dossierOutput"));
    await click('#dossierViews button[data-arg="doc"]');
    eq("back to the document", await shown("#dossierDoc"), true);
    await tabStaysIn("dossierModal");
    await page.click("#dossierModal .modal-t");
    await page.keyboard.press("Escape");
    eq("escape closes it", await modalOpen("dossierModal"), false);
    await setTheme(null);
  }],

  ["the dossier as a bottom sheet on a phone: it fits the width and scrolls inside", async ({ click, dialogShot, eq, noSideScroll, phone, setTheme, until }) => {
    await phone(async () => {
      await setTheme("dark");
      await click("[data-action=openDossierModal]");
      await until(() => document.querySelectorAll("#dossierDoc h4").length >= 4);
      eq("dossier fits", await noSideScroll("dossierModal"), true);
      await dialogShot("dossier-phone");
      await click("[data-action=closeModal][data-arg=dossierModal]");
      await setTheme(null);
    });
  }],
];
