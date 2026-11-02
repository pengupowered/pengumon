import sqlite3, time, os
c = sqlite3.connect("file:/app/data/pengumon.db?mode=ro", uri=True)
now = time.time()
print("db file: %.0f MB, wal: %.1f MB" % (os.path.getsize("/app/data/pengumon.db") / 1e6, (os.path.getsize("/app/data/pengumon.db-wal") if os.path.exists("/app/data/pengumon.db-wal") else 0) / 1e6))
pc = c.execute("pragma page_count").fetchone()[0]; fl = c.execute("pragma freelist_count").fetchone()[0]; ps = c.execute("pragma page_size").fetchone()[0]
print("pages %d x %d B | free pages %d (%.0f MB reusable) | journal=%s | synchronous=%s" % (pc, ps, fl, fl * ps / 1e6, c.execute("pragma journal_mode").fetchone()[0], c.execute("pragma synchronous").fetchone()[0]))
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
print("\n%-16s %12s %10s %10s %s" % ("table", "rows", "oldest", "newest", "(age in days)"))
for t in tables:
    cols = [r[1] for r in c.execute("pragma table_info(%s)" % t)]
    tcol = next((x for x in ("timestamp", "start_time", "ts", "created_at", "time") if x in cols), None)
    n = c.execute("select count(*) from %s" % t).fetchone()[0]
    if tcol:
        lo, hi = c.execute("select min(%s), max(%s) from %s" % (tcol, tcol, t)).fetchone()
        try:
            print("%-16s %12s %9.1fd %9.1fd   (%s)" % (t, "{:,}".format(n), (now - float(lo)) / 86400, (now - float(hi)) / 86400, tcol))
        except (TypeError, ValueError):
            print("%-16s %12s %s .. %s   (%s)" % (t, "{:,}".format(n), lo, hi, tcol))
    else:
        print("%-16s %12s   (no time column: %s)" % (t, "{:,}".format(n), ",".join(cols[:6])))
try:
    rows = c.execute("select name, sum(pgsize) from dbstat group by name order by 2 desc limit 8").fetchall()
    print("\nbytes by table/index (dbstat):")
    for n, b in rows: print("  %-34s %7.1f MB" % (n, b / 1e6))
except sqlite3.Error as e:
    print("\n(dbstat unavailable: %s)" % e)
# Schema v2 (0.2.0): samples(ts, target, ok, ...) clustered by time, plus per-minute rollup_1m.
# Older databases still have probe_samples(timestamp, ...).
sample_table, ts_col = ("samples", "ts") if "samples" in tables else ("probe_samples", "timestamp")
print("\nschema version (user_version):", c.execute("pragma user_version").fetchone()[0])
print("samples per second right now:", c.execute("select count(*) from %s where %s > ?" % (sample_table, ts_col), (now - 60,)).fetchone()[0] / 60.0)
if "rollup_1m" in tables:
    lo, hi, n = c.execute("select min(minute), max(minute), count(*) from rollup_1m").fetchone()
    if n:
        print("rollup_1m: %s rows, %.1f days of history" % ("{:,}".format(n), (hi - lo) / 1440.0))
for t in tables:
    if t.startswith("docsis"):
        cols = [r[1] for r in c.execute("pragma table_info(%s)" % t)]
        tcol = next((x for x in ("timestamp", "ts") if x in cols), None)
        if tcol:
            print("%s rows in last 60s: %d" % (t, c.execute("select count(*) from %s where %s > ?" % (t, tcol), (now - 60,)).fetchone()[0]))
