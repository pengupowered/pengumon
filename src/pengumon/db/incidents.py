"""Incidents: create, resolve, annotate, list (cached until one changes) and attach a path trace."""

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Set

import aiosqlite

from pengumon.incidents.loss import loss_span, probe_window
from pengumon.incidents.taxonomy import ISP_LINE_KINDS, kinds_in_scope
from pengumon.incidents.model import Incident
from pengumon.models.probes import PathTraceHop

logger = logging.getLogger("pengumon.db")
_unreadable_traces: Set[int] = set()  # incidents whose stored trace was reported as unreadable (once each)


class IncidentQueries:
    async def create_incident(self, incident: Incident) -> int:
        """Creates a new incident record and returns its ID."""
        async with self._db.write() as db:
            cursor = await db.execute(
                """
                INSERT INTO incidents 
                (start_time, end_time, duration_seconds, cause_code, title, description, severity, affected_targets,
                 resolved, load_pct, expected, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident.start_time,
                    incident.end_time,
                    incident.duration_seconds,
                    incident.cause_code,
                    incident.title,
                    incident.description,
                    incident.severity,
                    json.dumps(incident.affected_targets),
                    1 if incident.resolved else 0,
                    incident.load_pct,
                    1 if incident.expected else 0,
                    incident.kind,
                ),
            )
            await db.commit()
            self._incidents_version += 1
            return cursor.lastrowid

    async def resolve_incident(self, incident_id: int, end_time: float):
        """Marks an incident as resolved and updates duration, then stores how long its probes were lost."""
        await self._resolve_row(incident_id, end_time)
        await self.store_incident_loss(incident_id)

    async def _resolve_row(self, incident_id: int, end_time: float):
        async with self._db.write() as db:
            async with db.execute(
                "SELECT start_time FROM incidents WHERE id = ?", (incident_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    start_time = row[0]
                    duration = max(0.0, end_time - start_time)
                    await db.execute(
                        """
                        UPDATE incidents 
                        SET end_time = ?, duration_seconds = ?, resolved = 1
                        WHERE id = ?
                        """,
                        (end_time, duration, incident_id),
                    )
                    await db.commit()
                    self._incidents_version += 1

    async def store_incident_loss(self, incident_id: int) -> bool:
        """Computes a resolved incident's loss span from the stored probes of its affected targets (incidents.loss) and
        stores it. Leaves it NULL when no such probe is stored (past retention, or no probe target affected). Returns
        whether it stored one."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT start_time, end_time, affected_targets FROM incidents WHERE id = ? AND resolved = 1",
                (incident_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if not row or row[1] is None:
            return False
        try:
            affected = [t for t in json.loads(row[2] or "[]") if isinstance(t, str)]
        except (TypeError, ValueError):
            affected = []
        lo, hi = probe_window(row[0], row[1])
        span = loss_span(await self.probe_loss_stats(lo, hi, affected)) if affected else None
        if span is None:
            return False
        async with self._db.write() as db:
            await db.execute("UPDATE incidents SET loss_start = ?, loss_seconds = ? WHERE id = ?",
                             (span.start, span.seconds, incident_id))
        self._incidents_version += 1
        return True

    async def backfill_incident_loss(self) -> int:
        """The loss span of every resolved incident that has none yet and whose probes may still be stored (it ended
        after the oldest raw sample): incidents from before schema v12, and those closed at start after a crash. Runs on
        every start; an incident with no stored probes stays NULL. Returns how many it filled."""
        async with self._db.read() as db:
            async with db.execute(
                """
                SELECT id FROM incidents
                WHERE resolved = 1 AND loss_seconds IS NULL AND end_time >= (SELECT MIN(ts) FROM samples)
                ORDER BY start_time
                """
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]
        filled = 0
        for incident_id in ids:
            filled += await self.store_incident_loss(incident_id)
        return filled

    _INCIDENT_COLUMNS = """id, start_time, end_time, duration_seconds, cause_code, title, description, severity,
                           affected_targets, resolved, path_trace, load_pct, expected, kind, loss_start, loss_seconds"""

    @staticmethod
    def _incident_from_row(r) -> Incident:
        pt = None
        if r["path_trace"]:
            try:
                pt = [PathTraceHop(**h) for h in json.loads(r["path_trace"])]
            except (ValueError, TypeError):  # not JSON, not a list of hops, or a hop that does not validate
                if r["id"] not in _unreadable_traces:
                    _unreadable_traces.add(r["id"])
                    logger.warning(f"Incident {r['id']}: its stored path trace cannot be read; it is shown without one")
        return Incident(
            id=r["id"],
            start_time=r["start_time"],
            end_time=r["end_time"],
            duration_seconds=r["duration_seconds"],
            cause_code=r["cause_code"],
            title=r["title"],
            description=r["description"],
            severity=r["severity"],
            affected_targets=json.loads(r["affected_targets"]),
            resolved=bool(r["resolved"]),
            path_trace=pt,
            load_pct=r["load_pct"],
            expected=bool(r["expected"]),
            kind=r["kind"],
            loss_start=r["loss_start"],
            loss_seconds=r["loss_seconds"],
        )

    @staticmethod
    def _incident_filter(include_expected: bool, scope: str) -> tuple:
        """The WHERE clause and its parameters for the incident list filters. `scope` is "network" (the kinds
        registered as network kinds, and rows recorded before kinds existed), "service" (the service kinds) or "all".
        Both lists name the kinds they take, so a kind whose feature has not registered it in this process (the
        services feature registers service_down) lands in neither list instead of in the network one."""
        clauses, params = [], []
        if not include_expected:
            clauses.append("COALESCE(expected, 0) = 0")
        if scope != "all":
            kinds = kinds_in_scope(scope)
            if scope == "service":
                clauses.append(f"kind IN ({','.join('?' * len(kinds))})" if kinds else "0")
            else:
                clauses.append(f"(kind IS NULL OR kind IN ({','.join('?' * len(kinds))}))")
            params += list(kinds)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params

    async def get_recent_incidents(self, limit: int = 50, include_expected: bool = True,
                                   scope: str = "all") -> List[Incident]:
        """Retrieves most recent incidents (cached until an incident changes). include_expected=False leaves
        out the ones explained by line load; `scope` picks the network incidents, the service (host app) ones or
        all (see network_incidents and service_incidents)."""
        key = (limit, include_expected, scope)
        cached = self._incidents_cache.get(key)
        if cached and cached[0] == self._incidents_version:
            return cached[1]
        version = self._incidents_version
        where, params = self._incident_filter(include_expected, scope)
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {self._INCIDENT_COLUMNS} FROM incidents {where} ORDER BY start_time DESC LIMIT ?",
                (*params, limit),
            ) as cursor:
                incidents = [self._incident_from_row(r) for r in await cursor.fetchall()]
        self._incidents_cache[key] = (version, incidents)
        return incidents

    async def network_incidents(self, limit: int = 50, include_expected: bool = True) -> List[Incident]:
        """The incidents of the network (everything but the host apps' outages)."""
        return await self.get_recent_incidents(limit, include_expected, scope="network")

    async def service_incidents(self, limit: int = 50, include_expected: bool = True) -> List[Incident]:
        """The outages of the apps on this host (kinds registered with scope "service")."""
        return await self.get_recent_incidents(limit, include_expected, scope="service")

    async def count_incidents(self, include_expected: bool = True, scope: str = "all") -> int:
        where, params = self._incident_filter(include_expected, scope)
        async with self._db.read() as db:
            async with db.execute(f"SELECT COUNT(*) FROM incidents {where}", params) as cursor:
                return (await cursor.fetchone())[0]

    async def count_network_incidents(self, include_expected: bool = True) -> int:
        return await self.count_incidents(include_expected, scope="network")

    async def get_incident_by_id(self, incident_id: int) -> Optional[Incident]:
        """Retrieves a single incident by ID."""
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {self._INCIDENT_COLUMNS} FROM incidents WHERE id = ?", (incident_id,)
            ) as cursor:
                r = await cursor.fetchone()
                return self._incident_from_row(r) if r else None

    async def set_incident_load(self, incident_id: int, load_pct: Optional[float], expected: bool = False):
        """Annotates an incident with the WAN load around it and whether that load explains it."""
        async with self._db.write() as db:
            await db.execute(
                "UPDATE incidents SET load_pct = ?, expected = ? WHERE id = ?",
                (load_pct, 1 if expected else 0, incident_id),
            )
        self._incidents_version += 1

    async def update_incident_classification(
        self, incident_id: int, kind: str, cause_code: str, title: str, description: str, severity: str
    ):
        """Stores a changed diagnosis (an incident that escalated): what the load backfills read is the stored kind."""
        async with self._db.write() as db:
            await db.execute(
                "UPDATE incidents SET kind = ?, cause_code = ?, title = ?, description = ?, severity = ? WHERE id = ?",
                (kind, cause_code, title, description, severity, incident_id),
            )
        self._incidents_version += 1

    async def get_incidents_without_load(self, include_annotated: bool = False) -> List[Dict[str, Any]]:
        """Incidents never annotated with load (load_pct NULL), or all of them with include_annotated,
        oldest first, as plain rows."""
        where = "" if include_annotated else "WHERE load_pct IS NULL"
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT id, start_time, end_time, cause_code, title, kind FROM incidents {where} ORDER BY start_time"
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_isp_line_incidents(self) -> List[Dict[str, Any]]:
        """Not-yet-expected ISP-line incidents (kinds in ISP_LINE_KINDS: loss or outage at the ISP hops), oldest
        first, as plain rows."""
        kinds = tuple(sorted(ISP_LINE_KINDS))
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT id, start_time, end_time, title, kind FROM incidents
                WHERE kind IN ({", ".join("?" for _ in kinds)}) AND COALESCE(expected, 0) = 0
                ORDER BY start_time
                """,
                kinds,
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def set_incident_expected(self, incident_id: int):
        async with self._db.write() as db:
            await db.execute("UPDATE incidents SET expected = 1 WHERE id = ?", (incident_id,))
        self._incidents_version += 1

    async def get_incidents_since(self, start_time: float) -> List[Dict[str, Any]]:
        """Incidents that started at or after start_time, newest first, as plain rows."""
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, start_time, end_time, duration_seconds, cause_code,
                       title, description, severity, affected_targets, load_pct, expected, kind
                FROM incidents
                WHERE start_time >= ?
                ORDER BY start_time DESC
                """,
                (start_time,),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_incidents_touching(self, since: float) -> List[Dict[str, Any]]:
        """The network incidents (not expected, not host apps) that started at or after `since`, or that were still
        open at it or ended at or after it, as plain rows with only what outage grouping reads, oldest first. Not cached:
        the grouping reads a widening window of them (incidents.outage_read)."""
        kinds = kinds_in_scope("network")
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT id, start_time, end_time, cause_code, title, description, severity, kind
                FROM incidents
                WHERE COALESCE(expected, 0) = 0 AND (kind IS NULL OR kind IN ({','.join('?' * len(kinds))}))
                  AND (start_time >= ? OR end_time IS NULL OR end_time >= ?)
                ORDER BY start_time, id
                """,
                (*kinds, since, since),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def update_incident_path_trace(self, incident_id: int, hops: List[PathTraceHop]):
        """Attaches an automated path trace to an incident record."""
        hops_json = json.dumps([h.model_dump() for h in hops])
        async with self._db.write() as db:
            await db.execute(
                "UPDATE incidents SET path_trace = ? WHERE id = ?",
                (hops_json, incident_id),
            )
            await db.commit()
            self._incidents_version += 1
