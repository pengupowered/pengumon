// The Ask AI dialog. The endpoint (/api/ai-pack) is answered by the page itself here (aiStub): the steps are about the
// dialog, so the answer is made to the design's shape and the steps can hold it back or refuse it.

export const subject = "dialog:ai-pack";
export const variants = ["full"];

// window.__ai lists the query of each call; window.__aiMode is "ok", "busy" (a 429), "slow" (answers when window.__aiRelease() is called) or "fail".
const aiStub = (page) => page.evaluate(() => {
  const real = window.fetch.bind(window);
  window.__ai = [];
  window.__aiMode = "ok";
  window.fetch = (url, opts = {}) => {
    const u = new URL(String(url), location.href);
    if (u.pathname !== "/api/ai-pack") return real(url, opts);
    const q = u.searchParams;
    window.__ai.push(u.search.slice(1).replace(/&tz_offset_minutes=-?\d+/, "").replace(/&tz=[^&]*/, ""));
    window.__aiTz = q.get("tz");
    const level = q.get("level");
    const markdown = [`# Network troubleshooting pack from Pengumon`, "", `Level: ${level}. Form: ${q.get("detail")}. Scope: ${q.get("scope")} ${q.get("hours") || q.get("incident_id")}.`, "",
      ...Array.from({ length: 120 }, (_, i) => `- Finding ${i + 1}: ${level === "full" ? "Example-Mesh hall" : "Mesh node 2"} lost 0.${i}% of probes.`)].join("\n");
    const answer = { markdown, counts: level === "full" ? "" : "7 addresses, 4 names", modules: [{ id: "docsis", title: "Cable line", trimmed: false }, { id: "wifi", title: "Wi-Fi and mesh", trimmed: true }],
      roles: [{ role: "Router", name: "Example-Router" }, { role: "Mesh node 2", name: "Example-Mesh hall" }] };
    const reply = () => new Response(JSON.stringify(answer), { status: 200, headers: { "content-type": "application/json" } });
    if (window.__aiMode === "busy") return Promise.resolve(new Response("{}", { status: 429, headers: { "Retry-After": "2" } }));
    if (window.__aiMode === "fail") return Promise.resolve(new Response("{}", { status: 503 }));
    if (window.__aiMode === "slow") return new Promise((resolve) => { window.__aiRelease = () => resolve(reply()); });
    return Promise.resolve(reply());
  };
});

export const steps = [
  ["Ask AI: starts at Full, the preview is what Copy and Download give, the previous preview stays while a new one loads, a failure is said in view", async ({ click, eq, fs, ok, outDir, page, path, setTheme, snap, tabStaysIn, text, until, visible, waitText }) => {
    await setTheme("dark");
    await aiStub(page);
    await page.emulateTimezone("America/New_York");   // the pack's times follow the viewer's zone when Settings has none
    await page.$eval("#headerTools [data-action=openAiPackModal]", (b) => b.focus());
    await click("#headerTools [data-action=openAiPackModal]");
    await until(() => document.getElementById("aiPackText").value.length > 100);
    eq("the first call: 24 hours, Full, Short", await page.evaluate(() => window.__ai[0]), "scope=window&hours=24&level=full&detail=short");
    eq("the browser's zone is sent", await page.evaluate(() => window.__aiTz), "America/New_York");
    await page.emulateTimezone();
    eq("Full is selected", await page.$eval("input[name=aiPackLevel]:checked", (r) => r.value), "full");
    eq("each level says what it includes", await page.$$eval(".ai-level-d", (e) => e.filter((d) => d.textContent.length > 60).length), 3);
    eq("keyboard focus starts on close", await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Close");
    ok("the size is told", /^about [\d.]+k? tokens \(\d[\d,]* characters\)$/.test(await text("#aiPackSize")));
    ok("the modules are told", /Includes: Cable line, Wi-Fi and mesh \(shortened\)\./.test(await text("#aiPackIncludes")));
    ok("the plain-HTTP note is in the footer", /plain HTTP/.test(await text("#aiPackNote")));
    await snap("aipack-dark.png");
    await setTheme("light");
    await snap("aipack-light.png");
    await setTheme("dark");

    // Copy and Download give exactly the preview
    const shown = await page.$eval("#aiPackText", (t) => t.value);
    await click("#aiPackCopy");
    await waitText("#aiPackNote", /^Copied/);
    eq("clipboard", await page.evaluate(() => window.__clip()), shown);
    const file = path.join(outDir, "pengumon_ai_pack_24h_full_short.md");
    fs.rmSync(file, { force: true });
    await click("#aiPackDownload");
    for (let i = 0; i < 50 && !fs.existsSync(file); i++) await new Promise((r) => setTimeout(r, 100));
    ok("downloaded file", fs.existsSync(file) && fs.readFileSync(file, "utf8") === shown);
    ok("the download note says why Chrome may block it", /plain HTTP/.test(await text("#aiPackNote")));

    // keyboard only: arrow keys move the level; a slow answer keeps the old text and switches Copy and Download off
    await page.evaluate(() => { window.__aiMode = "slow"; });
    await page.$eval("input[name=aiPackLevel][value=full]", (r) => r.focus());
    await page.keyboard.press("ArrowDown");
    await until(() => window.__ai.length === 2);
    eq("Vague asked for", await page.evaluate(() => window.__ai[1]), "scope=window&hours=24&level=vague&detail=short");
    eq("the old preview is still shown", await page.$eval("#aiPackText", (t) => t.value), shown);
    eq("busy", await page.$eval("#aiPackText", (t) => t.getAttribute("aria-busy")), "true");
    eq("Copy is off while the text is for other choices", await page.$eval("#aiPackCopy", (b) => b.disabled), true);
    eq("Download is off too", await page.$eval("#aiPackDownload", (b) => b.disabled), true);
    await page.evaluate(() => window.__aiRelease());
    await until(() => !document.getElementById("aiPackCopy").disabled);
    ok("the new preview is Vague", /Level: vague/.test(await page.$eval("#aiPackText", (t) => t.value)));
    ok("the counts line is shown at Vague", /Replaced in the text: 7 addresses, 4 names\./.test(await text("#aiPackCounts")));

    // another build running (429): the earlier text stays, the footer says so and the page asks again by itself
    await page.evaluate(() => { window.__aiMode = "busy"; });
    await page.select("#aiPackForm", "detailed");
    await waitText("#aiPackNote", /^Busy, retrying in 2 s/);
    ok("the busy message is in view", await visible("#aiPackNote"));
    eq("Copy is off meanwhile", await page.$eval("#aiPackCopy", (b) => b.disabled), true);
    await page.evaluate(() => { window.__aiMode = "ok"; });
    await until(() => !document.getElementById("aiPackCopy").disabled);

    // a refusal: said in the footer, in view; the earlier text stays but cannot be copied
    await page.evaluate(() => { window.__aiMode = "fail"; });
    await page.select("#aiPackForm", "short");
    await waitText("#aiPackNote", /Could not update the preview \(Pengumon has no stored history yet\)/);
    ok("the message is in view", await visible("#aiPackNote"));
    ok("the earlier text is kept", /Level: vague/.test(await page.$eval("#aiPackText", (t) => t.value)));
    eq("and cannot be copied", await page.$eval("#aiPackCopy", (b) => b.disabled), true);
    await snap("aipack-error.png");
    await page.evaluate(() => { window.__aiMode = "ok"; });
    await page.select("#aiPackForm", "detailed");
    await until(() => !document.getElementById("aiPackCopy").disabled);
    eq("it recovers", (await text("#aiPackNote")).includes("Could not"), false);

    // scrolled: the footer and its messages stay in view
    await page.$eval("#aiPackBody", (b) => { b.scrollTop = b.scrollHeight; });
    await page.$eval("#aiPackText", (t) => { t.scrollTop = 400; });
    await click("#aiPackCopy");
    await waitText("#aiPackNote", /^Copied/);
    ok("the message is in view after scrolling", await visible("#aiPackNote"));
    await snap("aipack-scrolled.png");
    await page.$eval("#aiPackWho", (d) => { d.open = true; });
    ok("the key lists the roles", /Mesh node 2/.test(await text("#aiPackWhoList")));
    await tabStaysIn("aiPackModal");
    await page.keyboard.press("Escape");
    eq("escape closes it", await page.$eval("#aiPackModal", (m) => m.classList.contains("active")), false);
    eq("focus is back on the header button", await page.evaluate(() => document.activeElement?.dataset.action), "openAiPackModal");

    // a reopening starts at Full again
    await click("#headerTools [data-action=openAiPackModal]");
    await until(() => window.__ai.length > 6);
    eq("starts at Full again", await page.$eval("input[name=aiPackLevel]:checked", (r) => r.value), "full");
    ok("without the incident choice", await page.$eval("#aiPackIncidentOption", (o) => o.disabled && o.hidden));
    await click("[data-action=closeModal][data-arg=aiPackModal]");
    await setTheme(null);
  }],

  ["Ask AI from the incident inspector covers that incident, and closes alone; the dossier points to it", async ({ click, eq, inspectNamed, ok, page, until }) => {
    await aiStub(page);
    await inspectNamed("Short gateway drop");
    await click("#btnInspAskAi");
    await until(() => window.__ai.length === 1);
    ok("an incident id is asked for", /^scope=incident&incident_id=\d+&level=full&detail=short$/.test(await page.evaluate(() => window.__ai[0])));
    eq("the window choice says so", await page.$eval("#aiPackWindow", (s) => s.value), "incident");
    await page.keyboard.press("Escape");
    eq("the dialog closed", await page.$eval("#aiPackModal", (m) => m.classList.contains("active")), false);
    eq("the inspector stays open beneath it", await page.$eval("#incidentInspectorModal", (m) => m.classList.contains("active")), true);
    await click("[data-action=closeModal][data-arg=incidentInspectorModal]");
    await click("[data-action=openDossierModal]");
    await click("#dossierModal [data-action=openAiPackModal]");
    await until(() => window.__ai.length === 2);
    ok("from the dossier it covers a window", /^scope=window&hours=24&/.test(await page.evaluate(() => window.__ai[1])));
    await click("[data-action=closeModal][data-arg=aiPackModal]");
    await click("[data-action=closeModal][data-arg=dossierModal]");
  }],

  ["Ask AI as a bottom sheet on a phone: it fits the width and the footer message stays in view", async ({ click, dialogShot, eq, noSideScroll, ok, page, phone, setTheme, until, visible }) => {
    await phone(async () => {
      await setTheme("dark");
      await aiStub(page);
      await click(".tb-more");
      await click("#headerMenu [data-action=openAiPackModal]");
      await until(() => document.getElementById("aiPackText").value.length > 100);
      eq("it fits", await noSideScroll("aiPackModal"), true);
      ok("Copy is in view", await visible("#aiPackCopy"));
      await dialogShot("aipack-phone");
      await page.$eval("#aiPackBody", (b) => { b.scrollTop = b.scrollHeight; });
      ok("the footer message is in view when scrolled", await visible("#aiPackNote"));
      await dialogShot("aipack-phone-scrolled");
      await click("[data-action=closeModal][data-arg=aiPackModal]");
      await setTheme(null);
    });
  }],
];
