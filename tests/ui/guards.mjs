// Guards on the step files (steps/*.mjs), run by smoke.mjs before any step and by tests/test_ui_steps.py on made-up files.
//   1. a step file declares no top-level helper (const / let / var / function / class) that another step file also
//      declares, or that lib.mjs already owns: shared helpers live in lib.mjs, once;
//   2. a step file is named after its subject, and a panel or dialog it names exists.
// CLI: node guards.mjs STEPS_DIR [LIB_FILE]   prints the problems and exits 1 when there are any.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const CONTRACT = new Set(["subject", "variants", "runsLast", "steps"]); // what every step file exports

// names a source declares at the top level (column 0); destructuring declarations count each name
export function topLevelNames(source) {
  const names = [];
  for (const line of source.split("\n")) {
    const m = line.match(/^(?:export\s+)?(?:async\s+)?(?:(?:const|let|var)\s+([A-Za-z_$][\w$]*)|function\*?\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*))/);
    if (m) { names.push(m[1] ?? m[2] ?? m[3]); continue; }
    const d = line.match(/^(?:export\s+)?(?:const|let|var)\s+[{[]([^}\]]*)[}\]]/);
    if (d) for (const part of d[1].split(",")) { const n = part.split(/[:=]/).pop().trim(); if (n) names.push(n); }
  }
  return names;
}

// the names lib.mjs owns: its exports, and the helpers it builds inside createContext (two-space indent)
export function libNames(source) {
  const names = new Set(topLevelNames(source));
  for (const m of source.matchAll(/^ {2}(?:const|async function|function)\s+([A-Za-z_$][\w$]*)/gm)) names.add(m[1]);
  return names;
}

// files: [{ file: "line.mjs", source: "..." }]; returns a list of problems (empty when all is well)
export function checkSources(files, lib) {
  const problems = [];
  const owner = new Map();
  for (const { file, source } of files) {
    for (const name of topLevelNames(source)) {
      if (CONTRACT.has(name)) continue;
      if (lib.has(name)) problems.push(`${file}: declares "${name}", which lib.mjs already provides (use ctx.${name})`);
      else if (owner.has(name)) problems.push(`${file}: declares "${name}", which ${owner.get(name)} declares too (move the helper to lib.mjs)`);
      else owner.set(name, file);
    }
  }
  return problems;
}

// modules: [{ file, subject }]; panels / dialogs: the ids that exist
export function checkSubjects(modules, panels, dialogs) {
  const problems = [];
  for (const { file, subject } of modules) {
    const [kind, id] = String(subject).split(":");
    if (subject === "core") { if (file !== "core.mjs") problems.push(`${file}: subject "core" belongs in core.mjs`); continue; }
    if (kind !== "panel" && kind !== "dialog") { problems.push(`${file}: subject must be "core", "panel:<id>" or "dialog:<id>", got ${JSON.stringify(subject)}`); continue; }
    if (file !== `${id}.mjs`) problems.push(`${file}: named after "${id}.mjs" for the subject ${subject}`);
    if (!(kind === "panel" ? panels : dialogs).includes(id)) problems.push(`${file}: there is no ${kind} "${id}" (${kind}s: ${(kind === "panel" ? panels : dialogs).join(", ")})`);
  }
  return problems;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const dir = process.argv[2];
  const libFile = process.argv[3] ?? path.join(path.dirname(fileURLToPath(import.meta.url)), "lib.mjs");
  const files = fs.readdirSync(dir).filter((f) => f.endsWith(".mjs")).sort().map((f) => ({ file: f, source: fs.readFileSync(path.join(dir, f), "utf8") }));
  const problems = checkSources(files, libNames(fs.readFileSync(libFile, "utf8")));
  for (const p of problems) console.error(p);
  process.exit(problems.length ? 1 : 0);
}
