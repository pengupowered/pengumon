// The layout dialog: which cards are shown and in what order; it also checks the effect on the phone tab bar.
import { ALL_VARIANTS } from "../lib.mjs";

export const subject = "dialog:layout";
export const variants = ["full"];

// what the layout dialog changes, read back from the page
const panelShown = (page, id) => page.$eval(`[data-panel=${id}]`, (e) => !e.hidden);
const storedLayout = (page) => page.evaluate(() => localStorage.getItem("pengumon.layout.v1"));

export const steps = [
  ["layout dialog: cards without a device are greyed; a moved and a hidden card survive a reload; Reset restores", async ({ USUAL_PANELS, bare, click, count, dialogKeys, eq, layoutMounted, ok, openLayout, outDir, page, panelOrder, path, waitCount }) => {
    await layoutMounted();
    await dialogKeys("#headerTools [data-action=openLayoutDialog]", "layoutModal");
    eq("usual order", (await panelOrder()).join(), USUAL_PANELS.join());
    await openLayout();
    eq("one row per available panel", await count("#layoutList .lay-row"), bare ? 8 : 10);
    eq("panels without a device are listed apart", await count("#layoutUnavailable .lay-row"), bare ? 2 : 0);
    if (bare) {
      for (const id of ["line", "router"]) eq(`${id} is not shown`, await panelShown(page, id), false);
      const reasons = await page.$$eval("#layoutUnavailable .layout-reason", (e) => e.map((x) => x.textContent));
      ok(`the reason says where to fix it, in plain words (${reasons.join(" | ")})`, reasons.length === 2 && reasons.every((t) => /Settings > Devices and services/.test(t) && !/access_line|service_checks|[a-z]+_[a-z]+|no enabled device/.test(t)));
    }
    await click("#layoutList [data-action=movePanel][data-arg=report][data-arg2=up]");
    await click("#layoutList [data-change=togglePanel][data-arg=services]");
    const moved = [...USUAL_PANELS.slice(0, 7), "report", "incidents", "matrix"].join();
    eq("report moved above the incident log", (await panelOrder()).join(), moved);
    eq("host services hidden", await panelShown(page, "services"), false);
    ok("the choice is stored", /"hidden":\["services"\]/.test(await storedLayout(page)));
    await page.reload({ waitUntil: "load" });
    await layoutMounted();
    eq("order after reload", (await panelOrder()).join(), moved);
    eq("hidden after reload", await panelShown(page, "services"), false);
    await openLayout();
    eq("the checkbox shows it", await page.$eval("#layoutList [data-arg=services]", (b) => b.checked), false);
    await page.screenshot({ path: path.join(outDir, "layout.png") });
    // a layout change made in another tab rebuilds the list: the control with focus keeps it
    await page.focus("#layoutList [data-action=movePanel][data-arg=report][data-arg2=down]");
    await page.evaluate(() => window.dispatchEvent(new StorageEvent("storage", { key: "pengumon.layout.v1" })));
    eq("a change from another tab keeps focus", await page.evaluate(() => { const a = document.activeElement; return `${a.dataset.action}|${a.dataset.arg}|${a.dataset.arg2}`; }), "movePanel|report|down");
    await click("[data-action=resetLayout]");
    eq("order after Reset", (await panelOrder()).join(), USUAL_PANELS.join());
    await click("[data-action=closeModal][data-arg=layoutModal]");
    await click("[data-panel=services] .panel-menu-btn");
    await click("#panelMenu [data-action=hidePanel]");
    eq("hidden from its own menu", await panelShown(page, "services"), false);
    await openLayout();
    await click("[data-action=resetLayout]");
    eq("host services back", await panelShown(page, "services"), true);
    eq("nothing stored", await storedLayout(page), null);
    await click("[data-action=closeModal][data-arg=layoutModal]");
    await waitCount("#hopMatrixBody tr", 5); // the reload built the rows again
  }, { variants: ALL_VARIANTS }],

  ["a fresh install says how to add a router or modem, with a button to the right Settings section; one with a device does not", async ({ bare, click, eq, ok, page, shown, text, until }) => {
    eq("the hint is shown only without a device", await shown("#setupHint"), bare);
    if (!bare) return;
    ok("it names Settings and the device", /No router or modem added yet/.test(await text("#setupHint")));
    await page.focus("#setupHint button");
    await page.keyboard.press("Enter"); // reachable and usable from the keyboard
    await until(() => document.getElementById("settingsModal").classList.contains("active") && document.querySelector("#set-sec-devices"));
    await until(() => document.querySelector("#set-sec-devices").getBoundingClientRect().top < innerHeight / 2);
    await click("#btnSettingsCancel");
    await until(() => !document.getElementById("settingsModal").classList.contains("active"));
  }, { variants: ["full", "bare"] }],

  ["layout on a phone: hiding both cards of a tab removes the tab; Reset brings it back", async ({ bare, click, eq, layoutMounted, openLayout, page, until }) => {
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
    await layoutMounted();
    await until(() => getComputedStyle(document.getElementById("mobileTabs")).display !== "none");
    const tabs = () => page.$$eval("#mobileTabs [role=tab]", (b) => b.filter((x) => !x.hidden).map((x) => x.dataset.arg).join());
    eq("tabs", await tabs(), bare ? "live,network,incidents" : "live,network,line,incidents");
    await openLayout();
    await click("#layoutList [data-change=togglePanel][data-arg=incidents]");
    eq("one card of the tab left: the tab stays", await tabs().then((t) => t.includes("incidents")), true);
    await click("#layoutList [data-change=togglePanel][data-arg=report]");
    eq("still one: the tab stays", await tabs().then((t) => t.includes("incidents")), true);
    await click("#layoutList [data-change=togglePanel][data-arg=heat]");
    eq("both hidden: the tab is gone", await tabs().then((t) => t.includes("incidents")), false);
    await click("[data-action=resetLayout]");
    eq("Reset: the tab is back", await tabs().then((t) => t.includes("incidents")), true);
    await click("[data-action=closeModal][data-arg=layoutModal]");
    await page.setViewport({ width: 1400, height: 1000 });
  }, { variants: ["full", "bare"] }],
];
