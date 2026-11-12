"""A fake AsusWRT router for the traffic and status-page tests: the appGet hooks of tests/test_router.py and the pages
of tests/fixtures/asuswrt/ (made-up values in the shapes a real router answers with). It records every request and
models the router's hourly refresh of its traffic statistics."""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "asuswrt"
T0 = 1_794_128_400  # an hour boundary (2026-11-08 09:00 UTC)
HOUR = 3600


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


APPGET = {
    "netdev()": {"netdev": {"INTERNET_rx": "0x5f5e100", "INTERNET_tx": "0x2faf080"}},
    "cpu_usage()": {"cpu_usage": {"cpu1_total": "1000", "cpu1_usage": "250"}},
    "memory_usage()": {"memory_usage": {"mem_total": "1000", "mem_free": "400", "mem_used": "600"}},
    "get_clientlist()": {"get_clientlist": {
        "02:00:00:00:00:03": {"mac": "02:00:00:00:00:03", "isOnline": "1", "nickName": "Den console", "type": "7",
                              "vendor": "Example Games"},
        "02:00:00:00:00:01": {"mac": "02:00:00:00:00:01", "isOnline": "1", "name": "laptop", "type": "34"},
        "maclist": ["02:00:00:00:00:03", "02:00:00:00:00:01"]}},
    "uptime()": json.loads(fixture("appget_uptime.json")),
}
UPTIME_S = 6122380


def statistics(rows) -> str:
    """A Traffic Analyzer page body: BOM, the declaration, the assignment."""
    return "﻿var array_statistics = new Array();\narray_statistics = " + json.dumps(rows) + ";\n"


class FakeAsus:
    """`pages` maps a page name to a body, or to None for a page this router does not have (404). The traffic pages are
    answered from the fixtures, shifted to the hour the request's `date` asks for; `refresh_s` after the hour the
    router publishes the hour that just ended (before that, the newest slot is still empty). `changing` clients get a
    new 24-hour total every hour (so a round fetches them again); the others keep theirs."""

    def __init__(self, clock, *, pages: Optional[Dict[str, Optional[str]]] = None, refresh_s: float = 60.0,
                 changing: Tuple[str, ...] = ()):
        self.clock = clock
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []  # (method, path, query) of every request
        self.timeouts: Dict[str, Optional[float]] = {}  # path -> the read timeout of its last request
        self.pages = {"ajax_ethernet_ports.asp": fixture("ajax_ethernet_ports.asp"),
                      "ajax_coretmp.asp": fixture("ajax_coretmp.asp"), **(pages or {})}
        self.refresh_s = refresh_s
        self.changing = changing
        self.fail: Optional[Exception] = None  # raised for the next traffic page request
        self.status: Optional[int] = None      # answered for the next traffic page request
        self.wan_hour_rows = json.loads(fixture("getWanTraffic_all_hour_24.asp").split("= ", 2)[2].rstrip(";\n"))

    def count(self, path: str, **query) -> int:
        return sum(1 for _, p, q in self.requests if p == path and all(q.get(k) == v for k, v in query.items()))

    def paths(self) -> List[str]:
        return [p for _, p, _ in self.requests]

    def _published_hour(self, date: int) -> int:
        """The hour slot 23 holds for a request with this `date`: the last complete one, once it is published."""
        newest = date // HOUR * HOUR - HOUR
        return newest if self.clock() >= newest + HOUR + self.refresh_s else newest - HOUR

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        query = dict(request.url.params)
        self.requests.append((request.method, path, query))
        self.timeouts[path] = request.extensions.get("timeout", {}).get("read")
        if path == "login.cgi":
            return httpx.Response(200, json={"asus_token": "tok"}, headers={"content-type": "application/json"})
        if path == "appGet.cgi":
            body: dict = {}
            for hook in parse_qs(request.content.decode())["hook"][0].split(";"):
                body.update(APPGET[hook])
            return httpx.Response(200, json=body)
        if path in ("getWanTraffic.asp", "getAppTraffic.asp"):
            if self.fail is not None:
                error, self.fail = self.fail, None
                raise error
            if self.status is not None:
                status, self.status = self.status, None
                return httpx.Response(status, text="")
            if path in self.pages and self.pages[path] is None:
                return httpx.Response(404, text=fixture("not_found_404.html"))
            if path in self.pages:
                return httpx.Response(200, text=self.pages[path])
            return self._traffic(path, query)
        if path in self.pages:
            body_text = self.pages[path]
            if body_text is None:
                return httpx.Response(404, text=fixture("not_found_404.html"), headers={"content-type": "text/html"})
            return httpx.Response(200, text=body_text, headers={"content-type": "application/json;charset=UTF-8"})
        return httpx.Response(404, text=fixture("not_found_404.html"))

    def _traffic(self, path: str, query: Dict[str, str]) -> httpx.Response:
        date = int(query["date"])
        published = self._published_hour(date)
        shift = (date // HOUR * HOUR - HOUR) - published  # 0 once published, an hour before
        bump = published // HOUR - T0 // HOUR  # how many hours past T0 the router has published
        if path == "getAppTraffic.asp":
            rows = json.loads(fixture("getAppTraffic_all_detail_24.asp").split("= ", 2)[2].rstrip(";\n"))
            rows = [[mac, up, down + (bump * 1000 if mac in self.changing else 0)] for mac, up, down in rows]
            return httpx.Response(200, text=statistics(rows))
        client, mode = query["client"], query["mode"]
        if mode == "hour":
            if client == "all":
                rows = self.wan_hour_rows
            else:
                rows = json.loads(fixture("getWanTraffic_client_hour_24.asp").split("= ", 2)[2].rstrip(";\n"))
            if shift:
                rows = rows[1:] + [[0, 0]]  # not refreshed yet: the newest slot is still empty
            return httpx.Response(200, text=statistics(rows))
        name = "getWanTraffic_all_detail_24.asp" if client == "all" else "getWanTraffic_client_detail_24.asp"
        return httpx.Response(200, text=fixture(name))
