// The ISP outage dossier dialog.

export const subject = "dialog:dossier";
export const variants = ["full"];

export const steps = [
  ["dossier modal renders Markdown, changes window, copies and downloads", async ({ click, eq, fs, ok, outDir, page, path, text, until, waitText }) => {
    await click("[data-action=openDossierModal]");
    await until(() => /^#{1,3} /m.test(document.getElementById("dossierOutput").textContent));
    await page.select("#dossierWindowSelect", "72");
    await until(() => performance.getEntriesByType("resource").some((e) => e.name.includes("/api/incidents/dossier?hours=72")));
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

  ["dossier: the report as a document, the Markdown view, copy and download unchanged, dark and light", async ({ click, count, dialogShot, eq, modalOpen, ok, page, setTheme, shown, tabStaysIn, text, until }) => {
    await setTheme("dark");
    await click("[data-action=openDossierModal]");
    await until(() => document.querySelectorAll("#dossierDoc h4").length >= 4);
    eq("four summary figures", await count("#dossierDoc .doc-stats .stat"), 4);
    ok("loss per hop as a table", (await count("#dossierDoc table tbody tr")) >= 3);
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
