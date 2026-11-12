"""`replay` pinned on seeded random samples with a gap, a stored speed test, a path change, alert rules and outage
grouping: the digest of every result document and every incident must not change when `replay` is restructured. A
legitimate change of the replay's rules changes the digest; update it then, after reading what moved."""

import hashlib
import json
import sqlite3

import pytest

from pengumon.db.migrations.v014_path_versions import BASELINE_SUMMARY
from pengumon.engine.replay import replay

from test_classifier_characterization import T0
from test_replay import RANDOM_DRAFTS, TARGETS5, _document, _random_rows, make_db, spec_for

EXPECTED_DIGEST = "f32d3aae453cdfde6d5b8cec6ccf9449bdc008b54688d8c29e58a093b3823b4a"


async def test_replay_matches_its_recorded_digest(tmp_path):
    digest = hashlib.sha256()
    seen = {"gaps": 0, "incidents": 0, "busy": 0, "alerts": 0, "changes": 0}
    for seed in (1, 2):
        for number, draft in enumerate(RANDOM_DRAFTS):
            db = tmp_path / f"r{seed}-{number}.db"
            rows = [r for r in _random_rows(seed) if not T0 + 1200 <= r[0] < T0 + 1260]  # the app was down
            rates = [(T0 + k * 10, 100.0 + (k % 7) * 150.0, 5.0) for k in range(300)]
            await make_db(db, rows, rates)
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO speed_tests (ts, duration_s, trigger, provider, status) VALUES (?, 40.0, "
                         "'scheduled', 'cloudflare', 'ok')", (T0 + 700,))
            conn.execute("INSERT INTO path_versions (revision, applied_at, hop_count, summary, hops) VALUES "
                         "(1, ?, 6, ?, '[]')", (T0 + 4, BASELINE_SUMMARY))
            conn.execute("INSERT INTO path_versions (revision, applied_at, hop_count, summary, hops) VALUES "
                         "(2, ?, 6, '3 hops added', '[]')", (T0 + 900,))
            conn.commit()
            spec = spec_for(TARGETS5, T0, T0 + 3000, draft=draft, incidents={"group_gap_minutes": 10})
            spec["rules"]["draft"]["alerts"] = {"delay_seconds": 5, "repeat_window_seconds": 60}
            document, incidents = _document(*await replay(spec, conn))
            seen["gaps"] += len(document["window"]["gaps"])
            seen["changes"] += len(document["changes"])
            for side in ("current", "draft"):
                seen["incidents"] += document[side]["incidents"]
                seen["busy"] += document[side]["busy_hidden"]
                seen["alerts"] += document[side]["alerts"]
            digest.update(json.dumps(document, sort_keys=True).encode())
            digest.update(repr(incidents).encode())
    assert all(seen.values()), seen  # every part of the answer is exercised
    assert digest.hexdigest() == EXPECTED_DIGEST
