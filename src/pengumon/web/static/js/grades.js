// Grades of the colored metrics come from the server, never from numbers compared here: snapshots carry `grades`
// (metric -> good / warn / critical, also per unit of a line) and /api/devices lists each device's `ranges` (unit,
// min, good_low, good_high, max, critical_low, critical_high) for drawing the range gauges. A metric without a grade
// is shown neutral.

export const rangeOf = (device, metric) => (device && device.ranges && device.ranges[metric]) || null;

export const gradeOf = (snapshot, metric) => (snapshot && snapshot.grades && snapshot.grades[metric]) || null;

// good / warn / critical -> the design's status level ('unknown' without a grade).
export const levelOfGrade = (g) => ({ good: 'good', warn: 'warn', critical: 'crit' }[g] || 'unknown');

// The worst of some grades (null when none is known).
export function worstGrade(grades) {
  const rank = { good: 0, warn: 1, critical: 2 };
  return grades.filter((g) => g in rank).reduce((a, b) => (a === null || rank[b] > rank[a] ? b : a), null);
}

// Where the server sends readings without grades (a line's history rows), the device's ranges from the server
// grade them, by the server's rule (pengumon.grades.Range.grade: the edge of the good zone is good, a critical limit
// warn). No threshold is written here.
export function gradeByRange(range, v) {
  if (!range || v === null || v === undefined) return null;
  const has = (x) => x !== null && x !== undefined;
  if ((has(range.critical_low) && v < range.critical_low) || (has(range.critical_high) && v > range.critical_high)) return 'critical';
  if ((has(range.good_low) && v < range.good_low) || (has(range.good_high) && v > range.good_high)) return 'warn';
  return 'good';
}
