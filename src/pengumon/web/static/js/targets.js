// The configured targets: their order, names and colors, read from /api/targets.

import { token } from './chart-theme.js';

// Target order, names and colors come from /api/targets (the probe path, then the DNS probes; `color` per target).
// Both are filled by initTargets() before the first render and mutated in place, so importers keep their reference.
export const canonicalTargetOrder = [];

export function sortTargetsByCanonical(targetIds) {
  return Array.from(targetIds).sort((a, b) => {
    const idxA = canonicalTargetOrder.indexOf(a);
    const idxB = canonicalTargetOrder.indexOf(b);
    return (idxA !== -1 ? idxA : 999) - (idxB !== -1 ? idxB : 999);
  });
}

const targetMeta = Object.create(null); // (keyed by target id: an id like "constructor" must not find an inherited member)

export function getTargetInfo(tid) {
  if (targetMeta[tid]) return targetMeta[tid];
  return { name: tid, color: '#58a6ff' };
}

// The color of a target without a configured one: the next categorical slot (tokens --s1..--s8) in path order, so the
// first eight such targets all differ (one color per category made three public servers three identical lines). Past
// the eighth a target takes the first slot no other target of its category has yet, so the members of one group (the
// public servers of the Internet node, a run of ISP hops, the DNS lookups) still differ from each other; a category
// with more than eight is drawn in the neutral ink from the ninth on and told apart by its name. Read from the tokens
// when used (cached until the theme changes), so it follows the theme.
const SLOTS = 8;
const tokenColor = (name) => token(name) || '#7d8a9b'; // cached by token(), which forgets its values on a theme change

export async function initTargets() {
  try {
    const res = await fetch('/api/targets');
    if (res.ok) {
      const targets = await res.json();
      if (Array.isArray(targets)) {
        canonicalTargetOrder.length = 0; // in place: the path's order now (an applied edit can reorder or remove)
        let slot = 0;
        const taken = Object.create(null); // category -> the slots its targets have
        targets.forEach(t => {
          const tid = t.target_id || t.id;
          if (!tid) return;
          let token = null;
          if (!t.color) {
            const mine = (taken[t.category] ||= new Set());
            const free = Array.from({ length: SLOTS }, (_, k) => `--s${k + 1}`).find((s) => !mine.has(s));
            token = slot < SLOTS ? `--s${++slot}` : (free || '--ink-3');
            mine.add(token);
          }
          targetMeta[tid] = { name: t.name || tid, category: t.category, address: t.address || '', enabled: t.enabled !== false, group: t.group || '', shortLabel: t.short_label || '',
            get color() { return t.color || tokenColor(token); } };
          if (!canonicalTargetOrder.includes(tid)) canonicalTargetOrder.push(tid);
        });
      }
    }
  } catch (e) {
    console.warn('Failed to load targets config:', e);
  }
}

// The configured targets in canonical order, as [{id, category, name, address, enabled, color}].
export function targetList() {
  return canonicalTargetOrder.map((id) => ({ id, ...targetMeta[id] }));
}
