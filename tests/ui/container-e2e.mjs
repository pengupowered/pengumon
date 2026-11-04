// The browser end to end check of a running image (scripts/test-container.sh): the dashboard loads from the container, the
// page raises no errors, the path chain goes live, and a static file under its versioned URL loads. Small on purpose; the
// full suite is smoke.mjs. Usage: node container-e2e.mjs BASE_URL (PGBROWSER and PUPPETEER_EXECUTABLE_PATH from the runner image).
//
// Chromium aborts in-flight requests with net::ERR_NETWORK_CHANGED when its network namespace gains or loses an interface or
// address (podman is still attaching the runner to the network, IPv6 addresses leave "tentative"). So Chromium is only started
// once this container's network has been unchanged for a while and the app answers; a navigation that still fails with exactly
// that error is retried once, with a notice. Any other failure is reported as it is.
import { createRequire } from "node:module";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";

const base = process.argv[2];
const inCI = process.env.GITHUB_ACTIONS === "true";
const require = createRequire(path.join(process.env.PGBROWSER, "package.json"));
const puppeteer = require("puppeteer-core");
const args = (process.env.PENGUMON_BROWSER_ARGS ?? "").split(/\s+/).filter(Boolean);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const read = (f) => { try { return readFileSync(f, "utf8"); } catch { return ""; } };

// Everything Chromium's NetworkChangeNotifier watches, as one string: addresses (with flags: tentative ones change), routes, link state.
function networkSnapshot() {
  let links = "";
  try { links = readdirSync("/sys/class/net").map((n) => `${n}:${read(`/sys/class/net/${n}/operstate`).trim()}`).join(","); } catch { /* none */ }
  return [read("/proc/net/if_inet6"), read("/proc/net/route"), read("/proc/net/ipv6_route"), links].join("|");
}

// Wait until the network has not changed for quietMs and GET base/api/health works; give up (and go on) after timeoutMs.
async function waitForStableNetwork({ quietMs = 1500, timeoutMs = 20000 } = {}) {
  const t0 = Date.now();
  let last = networkSnapshot(), since = Date.now(), healthy = false;
  while (Date.now() - t0 < timeoutMs) {
    await sleep(200);
    const now = networkSnapshot();
    if (now !== last) { last = now; since = Date.now(); }
    if (!healthy) {
      try { healthy = (await fetch(base + "/api/health", { signal: AbortSignal.timeout(2000) })).ok; } catch { healthy = false; }
    }
    if (healthy && Date.now() - since >= quietMs) return true;
  }
  console.log(`  note: network not stable after ${timeoutMs} ms (healthy: ${healthy}); going on`);
  return false;
}

async function attempt() {
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
    // opened by the container's name over plain HTTP, like a real visit: an insecure context (no Clipboard API, no crypto.subtle)
    const secure = await page.evaluate(() => window.isSecureContext);
    if (secure !== false) problems.push(`isSecureContext is ${secure}: the check must open the app by a host name, not localhost`);

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
  return { problems, results };
}

await waitForStableNetwork();
let { problems, results } = await attempt();
if (problems.length && problems.every((p) => p.includes("net::ERR_NETWORK_CHANGED"))) {
  const msg = `net::ERR_NETWORK_CHANGED in the browser (${problems.length} request(s)); waiting for the network to settle and retrying once`;
  console.log("  note: " + msg);
  if (inCI) console.log(`::notice title=container e2e: retry::${msg}`);
  await waitForStableNetwork();
  ({ problems, results } = await attempt());
}
for (const r of results) console.log("  " + r);
if (problems.length) {
  // first line: the first real error, then the rest (scripts/test-container.sh annotates the tail of this output)
  console.log(`ERROR: ${problems[0]}`);
  console.log(JSON.stringify({ failed: problems }, null, 1));
  process.exit(1);
}
