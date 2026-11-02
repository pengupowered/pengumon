// The browser end to end check of a running image (scripts/test-container.sh): the dashboard loads from the container, the
// page raises no errors, the path chain goes live, and a static file under its versioned URL loads. Small on purpose; the
// full suite is smoke.mjs. Usage: node container-e2e.mjs BASE_URL (PGBROWSER and PUPPETEER_EXECUTABLE_PATH from the runner image).
import { createRequire } from "node:module";
import path from "node:path";

const base = process.argv[2];
const require = createRequire(path.join(process.env.PGBROWSER, "package.json"));
const puppeteer = require("puppeteer-core");
const args = (process.env.PENGUMON_BROWSER_ARGS ?? "").split(/\s+/).filter(Boolean);
const browser = await puppeteer.launch({ executablePath: process.env.PUPPETEER_EXECUTABLE_PATH, args, headless: true });
const problems = [];
const results = [];
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 1000 });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") problems.push(`console.error: ${m.text()}`); });
  page.on("requestfailed", (r) => problems.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => { if (r.url().startsWith(base) && r.status() >= 400) problems.push(`HTTP ${r.status()}: ${r.url()}`); });
  await page.goto(base + "/", { waitUntil: "load" });
  results.push(`title: ${await page.title()}`);

  // the page's live indicator and the path chain: nodes with a status other than the loading skeleton
  await page.waitForFunction(() => document.querySelector("#wsStatus")?.className === "live live-live", { timeout: 30000 });
  results.push("live indicator: live");
  await page.waitForFunction(() => document.querySelectorAll("#pathChain .node.st-good").length >= 2, { timeout: 30000 });
  results.push(`path chain nodes good: ${await page.$$eval("#pathChain .node.st-good", (n) => n.length)}`);

  // a static file under the versioned URL, as the page itself references it
  const asset = await page.evaluate(() => [...document.querySelectorAll("script[src],link[href]")].map((e) => e.src || e.href).find((u) => /\/static\/v\/[0-9a-f]+\//.test(u)));
  if (!asset) problems.push("no /static/v/<hash>/ URL in the page");
  else {
    const res = await page.evaluate(async (u) => { const r = await fetch(u); return { status: r.status, cache: r.headers.get("cache-control"), bytes: (await r.text()).length }; }, asset);
    results.push(`asset ${new URL(asset).pathname}: ${res.status}, ${res.bytes} bytes, ${res.cache}`);
    if (res.status !== 200 || !res.bytes) problems.push(`versioned asset did not load: ${JSON.stringify(res)}`);
  }
} catch (e) {
  problems.push(`exception: ${e.message}`);
} finally {
  await browser.close();
}
for (const r of results) console.log("  " + r);
if (problems.length) { console.log(JSON.stringify({ failed: problems }, null, 1)); process.exit(1); }
