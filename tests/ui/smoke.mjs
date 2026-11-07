// Browser smoke test of the dashboard: the runner. Usage: node smoke.mjs BASE_URL OUT_DIR VARIANT [SHARD]
// SHARD "i/n" (1 based) runs only the i-th of n slices of the step files, so the long `full` variant can run as several
// apps and browsers side by side (test_ui_smoke.py); the slices are balanced by the weights in SHARD_WEIGHT below.
// (PGBROWSER = dir with node_modules/puppeteer-core; variants: full, bare, tworouters, unknownline, longpath, token, viewonly, firstrun, see test_ui_smoke.py)
// Supported way to run it: scripts/test-ui.sh (locked-down container, see Dockerfile.ui); the image sets PGBROWSER,
// PUPPETEER_EXECUTABLE_PATH (Debian's chromium) and PENGUMON_BROWSER_ARGS. Alternative: an installed Chrome (e.g. on
// Windows) named by PUPPETEER_EXECUTABLE_PATH, with puppeteer-core installed from tests/ui/runner/package-lock.json into $PGBROWSER.
// PUPPETEER_EXECUTABLE_PATH is required. It is launched with puppeteer's default arguments plus the space separated
// PENGUMON_BROWSER_ARGS (in the container: --no-sandbox).
//
// This file only launches the browser (lib.mjs), finds the step files (steps/*.mjs, see docs/testing.md), checks them
// (guards.mjs) and runs the steps of this variant. The steps drive the page the way a person does (header buttons, the
// incident table, the inspector, forms). The run fails on any failed step, page error, console error, CSP violation or
// failed same-origin request, and reports every failure, not only the first.
//
// Order: core.mjs first, then the panels in the order they have on the page, then the dialogs alphabetically; a file
// that exports `runsLast = true` goes after all of those (add-target.mjs: it adds a real target). Before each file
// after the first the page is reset (desktop size, system theme, nothing stored, a fresh page load), so a step file
// only relies on what its own earlier steps did. A step that fails ends its file (the page state is unknown) but not
// the run; a step marked `fatal` ends the run.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createContext } from "./lib.mjs";
import { checkSources, checkSubjects, libNames } from "./guards.mjs";

const [base, outDir, variant, shardArg = "1/1"] = process.argv.slice(2);
const [shardIndex, shardCount] = shardArg.split("/").map(Number);
// Seconds each step file takes in the `full` variant (measured; the run prints them as fileMs), only to balance the slices.
const SHARD_WEIGHT = { "settings.mjs": 34, "path-editor.mjs": 26, "core.mjs": 21, "matrix.mjs": 12, "strips.mjs": 9, "router.mjs": 8, "device-setup.mjs": 8,
  "incidents.mjs": 6, "diagnosis.mjs": 6, "trace.mjs": 5.5, "target-history.mjs": 5, "add-target.mjs": 5, "alerts.mjs": 4.5, "line.mjs": 4,
  "report.mjs": 3, "dossier.mjs": 3, "layout.mjs": 3, "latency.mjs": 2, "heat.mjs": 2, "sign-out.mjs": 1, "services.mjs": 1 };
// PENGUMON_BROWSER=firefox: one pass in Firefox (WebDriver BiDi), only these step files (docs/testing.md). A step that needs
// a Chromium-only API says so with the option `chromiumOnly: "<reason>"`; Firefox skips it and prints the reason.
const firefox = process.env.PENGUMON_BROWSER === "firefox";
const FIREFOX_FILES = ["core.mjs", "settings.mjs"];
const here = path.dirname(fileURLToPath(import.meta.url));
const stepsDir = path.join(here, "steps");
const staticDir = path.resolve(here, "../../src/pengumon/web/static");

// ---- find and check the step files before opening a browser ----
const names = fs.readdirSync(stepsDir).filter((f) => f.endsWith(".mjs")).sort();
const modules = [];
for (const file of names) modules.push({ file, ...(await import(pathToFileURL(path.join(stepsDir, file)).href)) });
const early = checkSources(names.map((file) => ({ file, source: fs.readFileSync(path.join(stepsDir, file), "utf8") })), libNames(fs.readFileSync(path.join(here, "lib.mjs"), "utf8")));
for (const m of modules) if (!Array.isArray(m.steps) || typeof m.subject !== "string") early.push(`${m.file}: must export subject and steps`);
const dialogIds = fs.readdirSync(path.join(staticDir, "dialogs"));

const ctx = await createContext({ base, outDir, variant });
const { page, stats, problems } = ctx;
const fail = async (message, extra = {}) => {
  console.log(JSON.stringify({ failed: message, passed: stats.steps, problems, ...extra }, null, 1));
  await ctx.browser.close();
  process.exit(1);
};

// panel ids come from the page itself (the panels the server discovered)
await page.goto(base + "/", { waitUntil: "load" });
if (variant === "token") await ctx.signInWithToken(); // the app has an access token: its sign-in page comes first
await ctx.layoutMounted();
const panelIds = await ctx.panelOrder();
early.push(...checkSubjects(modules.filter((m) => typeof m.subject === "string"), panelIds, dialogIds));
if (early.length) await fail("step files", { problems: early });

const rank = (m) => (m.runsLast ? 3 : m.subject === "core" ? 0 : m.subject.startsWith("panel:") ? 1 : 2);
const panelRank = (m) => (m.subject.startsWith("panel:") ? panelIds.indexOf(m.subject.slice(6)) : 0);
modules.sort((a, b) => rank(a) - rank(b) || panelRank(a) - panelRank(b) || a.file.localeCompare(b.file));

// ---- run ----
const todoOf = (m) => m.steps.filter(([, , o]) => (o?.variants ?? m.variants ?? ["full"]).includes(variant) && !(firefox && o?.chromiumOnly));
let mine = modules.filter((m) => (!firefox || FIREFOX_FILES.includes(m.file)) && todoOf(m).length);
if (shardCount > 1) {
  // Greedy: the heaviest unit first, into the lightest slice; each slice keeps the order above. The files that run last
  // build on one another (add-target adds a target the path editor's steps then see), so they are one unit.
  const unitOf = (m) => (m.runsLast ? "runsLast" : m.file);
  const units = new Map();
  for (const m of mine) units.set(unitOf(m), (units.get(unitOf(m)) ?? 0) + weightOf(m));
  const load = Array(shardCount).fill(0);
  const slice = new Map();
  for (const [unit, w] of [...units].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))) {
    const k = load.indexOf(Math.min(...load));
    load[k] += w;
    slice.set(unit, k + 1);
  }
  mine = mine.filter((m) => slice.get(unitOf(m)) === shardIndex);
}
function weightOf(m) { return SHARD_WEIGHT[m.file] ?? todoOf(m).length; }
const skipped = firefox ? mine.flatMap((m) => m.steps.filter(([, , o]) => o?.chromiumOnly).map(([n, , o]) => `${m.subject}: ${n} (not in Firefox: ${o.chromiumOnly})`)) : [];
const fileMs = {};
let started = false;
run: for (const m of mine) {
  const todo = todoOf(m);
  const t0 = Date.now();
  if (started) await ctx.resetPage();
  started = true;
  let fatal = false;
  for (const [i, [name, fn, opts]] of todo.entries()) {
    const label = `${m.subject}: ${name}`;
    if (await ctx.step(label, fn)) continue;
    if (opts?.fatal) fatal = true;
    else skipped.push(...todo.slice(i + 1).map(([n]) => `${m.subject}: ${n}`));
    break; // the rest of this file starts from a page in an unknown state
  }
  fileMs[m.file] = Date.now() - t0;
  if (fatal) break run;
}

// ---- finish ----
await new Promise((r) => setTimeout(r, 2500)); // late errors (websocket ticks, timers)
const csp = await page.evaluate(() => window.__csp);
const scripts = await page.$$eval("script[src]", (s) => s.map((e) => `${e.type || "classic"} ${e.getAttribute("src")}`));
if (csp.length) problems.push(`CSP violations: ${csp.join("; ")}`);
if (ctx.dialogs.length) problems.push(`unexpected dialogs: ${ctx.dialogs.join("; ")}`);
const bad = stats.failed.length > 0 || problems.length > 0;
console.log(JSON.stringify({
  steps: stats.steps.length, assertions: stats.assertions, failed: stats.failed, skipped, passed: stats.steps, scripts, perf: ctx.report.perf, fileMs, problems,
}, null, 1));
await ctx.browser.close();
process.exit(bad ? 1 : 0);
