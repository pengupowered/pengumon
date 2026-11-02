// The layout dialog: which cards are shown and in what order; it also checks the effect on the phone tab bar.
import { ALL_VARIANTS } from "../lib.mjs";

export const subject = "dialog:layout";
export const variants = ["full"];

// what the layout dialog changes, read back from the page
const panelShown = (page, id) => page.$eval(`[data-panel=${id}]`, (e) => !e.hidden);
const storedLayout = (page) => page.evaluate(() => localStorage.getItem("pengumon.layout.v1"));

export const steps = [
  ["layout dialog: cards without a device are greyed; a moved and a hidden card survive a reload; Reset restores", async ({ USUAL_PANELS, bare, click, count, eq, layoutMounted, ok, openLayout, outDir, page, panelOrder, path, waitCount }) => {
    await layoutMounted();
    eq("usual order", (await panelOrder()).join(), USUAL_PANELS.join());
    await openLayout();
    eq("one row per available panel", await count("#layoutList .lay-row"), bare ? 8 : 10);
    eq("panels without a device are listed apart", await count("#layoutUnavailable .lay-row"), bare ? 2 : 0);
    if (bare) {
      for (const id of ["line", "router"]) eq(`${id} is not shown`, await panelShown(page, id), false);
      ok("the reason is given", (await page.$$eval("#layoutList .layout-reason", (e) => e.map((x) => x.textContent))).every((t) => t.length > 10));
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
