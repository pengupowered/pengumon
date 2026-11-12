"""The speed test providers (speedtest/providers/): against fake servers only, never the internet. HTTP providers run on
httpx.MockTransport (streams of a controllable size); ndt7 on a real websockets server on the loopback interface and a
fake locate service. The clock is a fake one the servers advance as data flows, so time limits are tested without waiting.

The byte-cap tests are the ones that matter: a server that would send ten times what was asked for must not make the
provider read, count or send more than the cap (plus the slack each module documents)."""

import asyncio
import json
import socket
import zlib
from urllib.parse import parse_qs

import httpx
import pytest
import websockets
from websockets.asyncio.server import serve

from pengumon import __version__
from pengumon.speedtest.providers.base import MAX_REQUEST, STREAMS, UPLOAD_ROUNDS, Meter, ProviderError, request_size
from pengumon.speedtest.providers.cloudflare import CloudflareProvider
from pengumon.speedtest.providers.librespeed import MIB, LibreSpeedProvider
from pengumon.speedtest.providers.mlab import SUBPROTOCOL, MLabProvider
from pengumon.speedtest.providers.registry import REGISTRY, make_provider
from pengumon.speedtest.settings import SpeedtestConfig

SERVED_CHUNK = 16 * 1024  # what the fake servers hand over at a time: the slack of "one chunk"
LIMIT = 15.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Served(httpx.AsyncByteStream):
    """A response body of `total` bytes in SERVED_CHUNK pieces, produced as the client pulls them; every piece advances
    the fake clock by piece / rate and is counted in `log.served`. `wait`: seconds before the first piece (the time a
    real server takes to start an answer)."""

    def __init__(self, total, log, clock, rate, wait=0.0):
        self.total, self.log, self.clock, self.rate, self.wait = total, log, clock, rate, wait

    async def __aiter__(self):
        if self.wait:
            self.clock.advance(self.wait)
            await asyncio.sleep(0)
        left = self.total
        while left > 0:
            piece = min(SERVED_CHUNK, left)
            left -= piece
            self.log.served += piece
            self.clock.advance(piece / self.rate)
            yield b"\0" * piece
            await asyncio.sleep(0)


class Log:
    def __init__(self):
        self.served = 0
        self.asked = []          # the sizes the client asked for, in bytes
        self.received = 0        # bytes of upload bodies
        self.requests = []       # (method, path)
        self.headers = []


class Streaming(httpx.AsyncBaseTransport):
    """Like httpx.MockTransport, but the handler (sync or async) gets the request with its body unread, so a handler
    that reads it piece by piece sees the client sending it piece by piece."""

    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request):
        answer = self.handler(request)
        return await answer if asyncio.iscoroutine(answer) else answer


async def take(request, clock, log, rate):
    """Reads an upload body as it is sent, advancing the fake clock; returns its size."""
    size = 0
    async for piece in request.stream:
        size += len(piece)
        clock.advance(len(piece) / rate)
    log.received += size
    log.asked.append(size)
    return size


def cloudflare_server(clock, log, rate=10e6, overshoot=1, status=200, colo="FRA", wait=0.0, header="cf-meta-colo"):
    async def handler(request):
        log.requests.append((request.method, request.url.path))
        log.headers.append(dict(request.headers))
        if status != 200:
            return httpx.Response(status)
        if request.url.path == "/__down":
            asked = int(request.url.params["bytes"])
            log.asked.append(asked)
            return httpx.Response(200, headers={header: colo},
                                  stream=Served(asked * overshoot, log, clock, rate, wait))
        await take(request, clock, log, rate)
        return httpx.Response(200, headers={"cf-meta-colo": colo}, content=b"")
    return handler


def cloudflare(handler, clock):
    return CloudflareProvider(clock=clock, transport=Streaming(handler))


# --- the meter ---------------------------------------------------------------------------------------------------

def test_the_meter_never_counts_past_the_cap():
    clock = Clock()
    meter = Meter(1000, LIMIT, clock)
    assert [meter.add(400), meter.add(400), meter.add(400), meter.add(5)] == [400, 400, 200, 0]
    assert meter.total == 1000 and meter.done


def test_the_speed_leaves_out_the_wait_for_the_first_byte_the_slow_start_and_a_lagging_tail():
    clock = Clock()
    meter = Meter(10**9, LIMIT, clock)
    clock.advance(2.0)                     # connecting and waiting for the answer: nothing moves
    main = meter.request()
    # the first byte at 2 s, a slow start (1 MB/s) to 4 s, then 10 MB/s for eight seconds
    for second in range(11):
        meter.add(1_000_000 if second < 3 else 10_000_000, main)
        if second < 10:
            clock.advance(1.0)
    lagging = meter.request()              # one request that the server slows to a trickle: 1 MB/s from 13 s to 21 s
    for _ in range(9):
        clock.advance(1.0)
        meter.add(1_000_000, lagging)
    assert meter.total == 92_000_000 and meter.active == pytest.approx(18.0)
    # bytes 20 % (18.4 MB, counted at 23 MB at 6 s) to 90 % (82.8 MB, counted at 83 MB at 12 s): 60 MB in 6 s; with the
    # trickle, 68 MB in 14 s would read 39 Mbps, and the plain average since the start 35
    assert meter.mbps() == pytest.approx(80.0, rel=0.001)
    assert meter.transfer("x").span_s == pytest.approx(18.0)


def test_the_time_no_request_moved_data_is_not_part_of_the_speed():
    clock = Clock()
    meter = Meter(10**9, LIMIT, clock, ramp=0.0)     # (an upload's way: every byte counts, up to the last moment)
    first, second = meter.request(), meter.request()
    for _ in range(11):                    # 1 MB every 0.1 s: 11 MB in a second
        meter.add(1_000_000, first)
        clock.advance(0.1)
    clock.advance(1.9)                     # the second request waits for its answer: nothing moves
    for _ in range(11):
        meter.add(1_000_000, second)
        clock.advance(0.1)
    assert meter.active == pytest.approx(2.0)
    assert meter.mbps() == pytest.approx(88.0)                  # 22 MB in the 2 s data moved, not in the 3.9 s


def test_without_a_ramp_the_speed_is_the_plain_average_up_to_the_answer():
    clock = Clock()
    meter = Meter(10**9, LIMIT, clock, ramp=0.0)          # an upload: bytes counted as handed to the socket
    clock.advance(1.0)
    meter.add(4_000_000)                                  # the socket buffer takes the first ones at once ...
    for _ in range(4):
        clock.advance(1.0)
        meter.add(1_000_000)
    clock.advance(1.0)
    meter.mark()                                          # ... and the server's answer says it has them all
    assert meter.mbps() == pytest.approx(8_000_000 * 8 / 5.0 / 1e6)


def test_an_upload_counts_the_first_block_of_every_request():
    """Bytes handed to the socket are sent after the moment they are counted: with many requests per stream, leaving out
    the first block of each (as a download does with its first chunk, which arrived before it was counted) would read
    20 % low with 64 KiB blocks of 320 kB requests."""
    clock = Clock()
    meter = Meter(10**9, LIMIT, clock, ramp=0.0)
    for _ in range(10):                                   # ten requests of 5 blocks of 64 kB, at 3.2 MB/s
        key = meter.request()
        for _ in range(5):
            meter.add(64_000, key)
            clock.advance(0.02)
        meter.mark(key)
    assert meter.mbps() == pytest.approx(3_200_000 * 8 / 1e6 * 10 / 10, rel=0.01)


def test_nothing_moved_is_no_speed():
    assert Meter(1000, LIMIT, Clock()).mbps() is None


def test_a_request_is_the_cap_spread_over_the_streams_under_10_mb_in_whole_units():
    assert STREAMS == 8
    assert request_size(25_000_000) == 3_125_000                     # one round of the eight streams
    assert request_size(1_000_000) == 125_000
    # each under 10 MB (Cloudflare rate-limits larger ones): a large cap is many requests, asked for until the time is up
    assert request_size(1_000_000_000) == MAX_REQUEST < 10_000_000
    assert request_size(50_000_000, rounds=UPLOAD_ROUNDS) == 1_562_500    # an upload: four rounds, answered often
    assert request_size(25_000_000, unit=MIB) == 2 * MIB               # whole MiB, within the cap
    assert request_size(3 * MIB, unit=MIB) == MIB and request_size(MIB - 1, unit=MIB) == 0
    for cap in (1, 5, 999_999, 10_000_000, 99_999_999, 250_000_000, 10**10):
        for unit in (1, MIB):
            size = request_size(cap, unit=unit)
            assert size <= max(cap, 0) and size % unit == 0 and size <= max(MAX_REQUEST // unit, 1) * unit


def test_the_registry_has_the_three_providers_and_builds_the_selected_one():
    assert set(REGISTRY) == {"cloudflare", "mlab", "librespeed"}
    assert isinstance(make_provider(SpeedtestConfig()), CloudflareProvider)
    assert isinstance(make_provider(SpeedtestConfig(provider="mlab")), MLabProvider)
    ls = make_provider(SpeedtestConfig(provider="librespeed", librespeed_url="http://speed.example.com"))
    assert isinstance(ls, LibreSpeedProvider) and ls.server == "speed.example.com"


# --- Cloudflare --------------------------------------------------------------------------------------------------

async def test_cloudflare_download_measures_the_speed_and_labels_the_server():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=10e6), clock)
    result = await provider.download(25_000_000, LIMIT)
    assert result.bytes == 25_000_000 and result.server == "FRA"
    assert result.mbps == pytest.approx(80.0, rel=0.05)         # 10 MB/s
    assert sum(log.asked) == 25_000_000 and all(path == "/__down" for _, path in log.requests)
    assert log.headers[0]["user-agent"] == f"Pengumon/{__version__}"
    await provider.aclose()


async def test_cloudflare_upload_posts_the_bytes_and_measures_the_speed():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=5e6), clock)
    result = await provider.upload(10_000_000, LIMIT)
    assert result.bytes == log.received == 10_000_000
    assert result.mbps == pytest.approx(40.0, rel=0.05)
    assert all(method == "POST" and path == "/__up" for method, path in log.requests)
    assert all(h["content-length"] == str(n) for h, n in zip(log.headers, log.asked))
    await provider.aclose()


async def test_cloudflare_uses_eight_streams_of_one_request_each_for_a_cap_it_reaches():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log), clock)
    await provider.download(25_000_000, LIMIT)
    assert log.asked == [3_125_000] * 8
    assert provider.meter.tail == 0.125      # the speed leaves out the last request's worth of bytes: one may lag
    await provider.aclose()


async def test_a_direction_runs_for_its_target_time_when_the_cap_would_last_longer():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=10e6), clock)
    result = await provider.download(1_000_000_000, LIMIT, target_s=8.0)     # 1 GB would take 100 s at 10 MB/s
    assert 8.0 <= provider.meter.elapsed < 9.0 and result.bytes < 100_000_000   # about 8 s of data, far from the cap
    assert result.mbps == pytest.approx(80.0, rel=0.05)
    assert all(n == MAX_REQUEST for n in log.asked) and len(log.asked) > STREAMS   # requests one after the other
    assert sum(log.asked) <= 1_000_000_000
    await provider.aclose()


async def test_the_cap_still_ends_a_direction_before_its_target_time():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=10e6), clock)
    result = await provider.download(25_000_000, LIMIT, target_s=8.0)
    assert result.bytes == 25_000_000 == sum(log.asked) and provider.meter.elapsed < 3.0
    up = await provider.upload(10_000_000, LIMIT, target_s=8.0)
    assert up.bytes == 10_000_000 and log.received == 10_000_000
    await provider.aclose()


async def test_an_upload_runs_for_its_target_time_and_its_speed_is_the_steady_rate():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=5e6), clock)   # a 40 Mbps upload
    result = await provider.upload(100_000_000, LIMIT, target_s=8.0)         # 100 MB would take 20 s
    # no request starts after 8 s; the ones sent finish (the server has every byte counted)
    assert 40_000_000 < result.bytes < 70_000_000 and result.bytes == log.received
    assert result.mbps == pytest.approx(40.0, rel=0.05)
    await provider.aclose()


async def test_an_upload_answered_late_after_the_target_time_does_not_read_as_a_slow_line():
    """The requests sent when the 8 s are up finish one by one; Cloudflare answers one now and then seconds late. The
    speed is the rate while all streams were sending, not the average up to the last answer (which would read 30)."""
    clock, log = Clock(), Log()
    serve = cloudflare_server(clock, log, rate=5e6)
    late = []

    async def handler(request):
        answer = await serve(request)
        if clock.now - 1000.0 > 8.0 and not late:                      # the first answer after the target time
            late.append(True)
            clock.advance(3.0)
        return answer

    provider = cloudflare(handler, clock)
    result = await provider.upload(100_000_000, LIMIT, target_s=8.0)
    assert late and result.mbps == pytest.approx(40.0, rel=0.05)
    assert result.bytes * 8 / provider.meter.active / 1e6 < 34.0        # (the average up to the answers)
    await provider.aclose()


async def test_a_request_that_stalls_is_cut_off_at_the_target_time_and_does_not_slow_the_result():
    """Cloudflare now and then stops serving one request. The other streams keep the line busy; the stalled one is cut
    off when the direction is done, instead of holding it to the time limit, and the speed is the data that flowed."""
    clock, log = Clock(), Log()
    stalled = asyncio.Event()
    serve = cloudflare_server(clock, log, rate=10e6)

    class Stalls(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"\0" * SERVED_CHUNK
            stalled.set()
            await asyncio.Event().wait()                                 # never another byte

    async def handler(request):
        if request.url.path == "/__down" and len(log.asked) == 2:       # the third request stalls after one piece
            log.asked.append(int(request.url.params["bytes"]))
            return httpx.Response(200, headers={"colo": "FRA"}, stream=Stalls())
        return await serve(request)

    provider = cloudflare(handler, clock)
    result = await asyncio.wait_for(provider.download(1_000_000_000, LIMIT, target_s=8.0), 5.0)
    assert stalled.is_set() and result.mbps == pytest.approx(80.0, rel=0.1)
    await provider.aclose()


async def test_a_stream_that_drops_out_costs_nothing_when_the_others_ran_the_whole_time_and_fails_it_otherwise():
    def dropping(clock, log):
        serve = cloudflare_server(clock, log, rate=10e6)

        async def handler(request):
            if request.url.path == "/__down" and len(log.asked) == 3:      # the fourth request is reset by the server
                log.asked.append(0)
                raise httpx.ReadError("reset")
            return await serve(request)
        return handler

    clock, log = Clock(), Log()
    timed = cloudflare(dropping(clock, log), clock)
    result = await timed.download(1_000_000_000, LIMIT, target_s=8.0)  # the other seven kept data flowing for 8 s
    assert result.mbps == pytest.approx(80.0, rel=0.05)
    await timed.aclose()
    clock, log = Clock(), Log()
    capped = cloudflare(dropping(clock, log), clock)
    with pytest.raises(ProviderError, match="on one of the streams") as raised:   # the cap came first: it would read low
        await capped.download(25_000_000, LIMIT, target_s=8.0)
    assert raised.value.moved > 0
    await capped.aclose()


async def test_the_wait_for_each_answer_to_start_does_not_count_as_a_slow_line():
    """Cloudflare takes 40 to 450 ms to start each answer. That time moves nothing: many small requests measured it more
    than the line (25 MB read 64 Mbps on a 1 Gbps line). One request per stream, timed from the first byte."""
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=10e6, wait=0.4), clock)
    result = await provider.download(25_000_000, LIMIT)
    assert result.mbps == pytest.approx(80.0, rel=0.1)                # 10 MB/s, the waits left out
    assert result.elapsed_s > result.span_s
    await provider.aclose()


async def test_cloudflare_labels_the_server_by_the_colo_header_it_sends_now():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, colo="IAD", header="colo"), clock)
    assert (await provider.download(1_000_000, LIMIT)).server == "IAD"
    await provider.aclose()


async def test_cloudflare_without_the_colo_header_is_just_cloudflare():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, colo=""), clock)
    assert (await provider.download(1_000_000, LIMIT)).server == "Cloudflare"
    await provider.aclose()


@pytest.mark.parametrize("cap", [1_000_000, 10_000_000, 25_000_000, 100_000_000])
async def test_cloudflare_download_never_reads_more_than_the_cap_from_a_server_that_sends_ten_times_more(cap):
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, overshoot=10), clock)
    result = await provider.download(cap, LIMIT)
    assert sum(log.asked) <= cap                                    # never asked for more
    assert result.bytes <= cap
    assert log.served <= cap + SERVED_CHUNK * len(log.asked)         # the pieces pulled: at most one extra per request
    assert log.served < 10 * cap / 2                                # nowhere near the whole oversized bodies
    await provider.aclose()


@pytest.mark.parametrize("cap", [1_000_000, 5_000_000, 50_000_000])
async def test_cloudflare_upload_never_sends_more_than_the_cap(cap):
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log), clock)
    result = await provider.upload(cap, LIMIT)
    assert log.received <= cap and result.bytes <= cap and sum(log.asked) <= cap
    assert result.bytes == log.received
    await provider.aclose()


async def test_cloudflare_download_stops_at_the_time_limit():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=1e6, overshoot=1), clock)
    result = await provider.download(250_000_000, LIMIT)             # 250 MB at 1 MB/s would take 250 s
    assert LIMIT <= result.elapsed_s < LIMIT + 1.0
    assert 14_000_000 <= result.bytes <= 17_000_000 and result.mbps == pytest.approx(8.0, rel=0.1)
    await provider.aclose()


async def test_cloudflare_upload_stops_at_the_time_limit():
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, rate=1e6), clock)
    result = await provider.upload(50_000_000, LIMIT)
    assert result.elapsed_s < LIMIT + 1.0 and result.bytes < 50_000_000
    await provider.aclose()


@pytest.mark.parametrize("status", [429, 403])
async def test_a_refusal_by_cloudflare_says_so_and_suggests_another_provider(status):
    clock, log = Clock(), Log()
    provider = cloudflare(cloudflare_server(clock, log, status=status), clock)
    for call in (provider.download, provider.upload):
        with pytest.raises(ProviderError) as e:
            await call(5_000_000, LIMIT)
        assert "Cloudflare refused" in e.value.reason and "rate limit" in e.value.reason
        assert "another provider" in e.value.reason
    await provider.aclose()


async def test_a_server_error_and_a_redirect_are_failures_with_short_reasons():
    clock, log = Clock(), Log()
    for status, text in ((500, "HTTP 500"), (302, "redirect")):
        provider = cloudflare(cloudflare_server(clock, log, status=status), clock)
        with pytest.raises(ProviderError, match=text):
            await provider.download(5_000_000, LIMIT)
        await provider.aclose()


async def test_a_dns_or_connection_failure_is_a_failure_not_an_exception_of_another_kind():
    def refuse(request):
        raise httpx.ConnectError("name resolution failed for speed.cloudflare.com")
    provider = CloudflareProvider(clock=Clock(), transport=httpx.MockTransport(refuse))
    with pytest.raises(ProviderError, match="could not connect to Cloudflare") as e:
        await provider.download(5_000_000, LIMIT)
    assert "speed.cloudflare.com" not in e.value.reason       # (the reason never carries the URL)
    await provider.aclose()


async def test_one_failing_stream_fails_the_direction_and_keeps_the_bytes_the_others_moved():
    """Three streams' total alone reads as a slower line than the one there is: a failure says so, not a low speed."""
    clock, log = Clock(), Log()
    inner = cloudflare_server(clock, log)
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 2:
            raise httpx.ReadError("reset")
        return inner(request)
    provider = cloudflare(flaky, clock)
    with pytest.raises(ProviderError, match="one of the streams") as e:
        await provider.download(25_000_000, LIMIT)
    assert 0 < e.value.moved <= 25_000_000 and e.value.moved == log.served
    await provider.aclose()


@pytest.mark.parametrize("status", [403, 429])
async def test_a_refusal_of_one_stream_fails_the_direction_with_the_refusal(status):
    """Cloudflare starts refusing in the middle of a burst of requests: no silent slow result from the streams it let
    through."""
    clock, log = Clock(), Log()
    inner = cloudflare_server(clock, log)
    calls = []

    def limited(request):
        calls.append(1)
        return httpx.Response(status) if len(calls) == 3 else inner(request)
    provider = cloudflare(limited, clock)
    with pytest.raises(ProviderError, match="Cloudflare refused") as e:
        await provider.download(25_000_000, LIMIT)
    assert e.value.moved > 0
    await provider.aclose()


# --- LibreSpeed --------------------------------------------------------------------------------------------------

def librespeed_server(clock, log, rate=10e6, overshoot=1, status=200):
    async def handler(request):
        log.requests.append((request.method, request.url.path))
        if status != 200:
            return httpx.Response(status)
        if request.url.path == "/speedtest/garbage.php":
            mib = int(request.url.params["ckSize"])
            log.asked.append(mib * MIB)
            return httpx.Response(200, stream=Served(mib * MIB * overshoot, log, clock, rate))
        await take(request, clock, log, rate)
        return httpx.Response(200, content=b"")
    return handler


def librespeed(handler, clock):
    return LibreSpeedProvider("http://speed.example.com/speedtest/", clock=clock, transport=Streaming(handler))


async def test_librespeed_download_asks_whole_mebibytes_within_the_cap_and_labels_the_server_by_its_host():
    clock, log = Clock(), Log()
    provider = librespeed(librespeed_server(clock, log), clock)
    result = await provider.download(25_000_000, LIMIT)
    assert result.server == "speed.example.com"
    assert all(n % MIB == 0 for n in log.asked) and sum(log.asked) <= 25_000_000
    assert result.bytes == sum(log.asked) and result.bytes > 20_000_000        # (less than a MiB of the cap is left unused)
    assert result.mbps == pytest.approx(80.0, rel=0.05)
    assert all(path == "/speedtest/garbage.php" for _, path in log.requests)
    await provider.aclose()


async def test_librespeed_upload_posts_to_empty_php():
    clock, log = Clock(), Log()
    provider = librespeed(librespeed_server(clock, log), clock)
    result = await provider.upload(10_000_000, LIMIT)
    assert result.bytes == log.received == 10_000_000
    assert all(method == "POST" and path == "/speedtest/empty.php" for method, path in log.requests)
    await provider.aclose()


@pytest.mark.parametrize("cap", [10_000_000, 25_000_000, 100_000_000])
async def test_librespeed_download_never_reads_more_than_the_cap_from_a_server_that_sends_ten_times_more(cap):
    clock, log = Clock(), Log()
    provider = librespeed(librespeed_server(clock, log, overshoot=10), clock)
    result = await provider.download(cap, LIMIT)
    assert sum(log.asked) <= cap and result.bytes <= cap
    assert log.served <= cap + SERVED_CHUNK * len(log.asked)
    await provider.aclose()


async def test_librespeed_upload_never_sends_more_than_the_cap():
    clock, log = Clock(), Log()
    provider = librespeed(librespeed_server(clock, log), clock)
    result = await provider.upload(5_000_000, LIMIT)
    assert log.received <= 5_000_000 and result.bytes == log.received
    await provider.aclose()


async def test_librespeed_stops_at_the_time_limit():
    clock, log = Clock(), Log()
    provider = librespeed(librespeed_server(clock, log, rate=1e6), clock)
    result = await provider.download(250_000_000, LIMIT)
    assert LIMIT <= result.elapsed_s < LIMIT + 1.0 and result.bytes < 20_000_000
    await provider.aclose()


async def test_a_404_from_librespeed_says_where_the_backend_usually_is():
    clock, log = Clock(), Log()
    provider = LibreSpeedProvider("http://speed.example.com:8080/", clock=clock,
                                  transport=Streaming(librespeed_server(clock, log, status=404)))
    for call in (provider.download, provider.upload):
        with pytest.raises(ProviderError) as e:
            await call(5_000_000, LIMIT)
        assert e.value.reason == ("speed.example.com answered HTTP 404: is the address the folder that holds garbage.php? "
                                  "For the official Docker image it is http://speed.example.com:8080/backend")
    await provider.aclose()


async def test_librespeed_errors_are_failures():
    clock, log = Clock(), Log()
    provider = librespeed(librespeed_server(clock, log, status=429), clock)
    with pytest.raises(ProviderError, match="speed.example.com refused the test"):
        await provider.download(5_000_000, LIMIT)
    await provider.aclose()


# --- compressed and oversized answers ----------------------------------------------------------------------

class Junk(httpx.AsyncByteStream):
    """`pieces` pieces of `size` bytes each (gzip of zeros with `gzip=True`); `served` counts what the client took."""

    def __init__(self, pieces, size=64 * 1024, gzip=False):
        self.pieces, self.size, self.gzip, self.served = pieces, size, gzip, 0

    async def __aiter__(self):
        z = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
        for _ in range(self.pieces):
            piece = z.compress(bytes(self.size)) if self.gzip else b"x" * self.size
            self.served += len(piece)
            yield piece
            await asyncio.sleep(0)


@pytest.mark.parametrize("which", ["cloudflare", "librespeed"])
async def test_every_request_asks_for_no_compression_and_a_compressed_download_is_refused_not_counted(which):
    clock, log = Clock(), Log()

    async def handler(request):
        log.headers.append(dict(request.headers))
        if request.method == "GET":  # 1 MiB of gzip that would inflate to 1 GiB
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Junk(1024, 1 << 20, gzip=True))
        await take(request, clock, log, 10e6)
        return httpx.Response(200, content=b"")

    provider = cloudflare(handler, clock) if which == "cloudflare" else librespeed(handler, clock)
    with pytest.raises(ProviderError, match="compressed"):
        await provider.download(25_000_000, LIMIT)
    await provider.upload(2_000_000, LIMIT)
    assert log.headers and all(h["accept-encoding"] == "identity" for h in log.headers)
    await provider.aclose()


@pytest.mark.parametrize("which", ["cloudflare", "librespeed"])
async def test_an_upload_reads_only_the_start_of_an_answer_that_never_ends(which):
    clock, log = Clock(), Log()
    answers = []

    async def handler(request):
        await take(request, clock, log, 10e6)
        answers.append(Junk(320))  # 20 MiB
        return httpx.Response(200, stream=answers[-1])

    provider = cloudflare(handler, clock) if which == "cloudflare" else librespeed(handler, clock)
    result = await provider.upload(2_000_000, LIMIT)
    assert result.bytes == 2_000_000
    assert answers and all(a.served <= 128 * 1024 for a in answers)
    await provider.aclose()


# --- M-Lab (ndt7) ------------------------------------------------------------------------------------------------

MESSAGE = 64 * 1024


class NdtServer:
    """A loopback ndt7 server: /ndt/v7/download sends binary messages (and a text measurement now and then) until the
    client goes away, /ndt/v7/upload counts what it receives."""

    def __init__(self, clock=None, tick=0.0):
        self.clock, self.tick = clock, tick
        self.sent = 0
        self.received = 0
        self.messages = []
        self.subprotocols = []
        self.queries = []
        self.server = None
        self.port = None

    async def handle(self, connection):
        self.subprotocols.append(connection.subprotocol)
        path = connection.request.path
        self.queries.append(path)
        try:
            if "/download" in path:
                block = b"\1" * MESSAGE
                count = 0
                while True:
                    await connection.send(block)
                    self.sent += len(block)
                    count += 1
                    if count % 4 == 0:
                        await connection.send(json.dumps({"AppInfo": {"NumBytes": self.sent}}))
                    await asyncio.sleep(0)
            else:
                async for message in connection:
                    if isinstance(message, bytes):
                        self.received += len(message)
                        self.messages.append(len(message))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def __aenter__(self):
        self.server = await serve(self.handle, "127.0.0.1", 0, subprotocols=[SUBPROTOCOL])
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    def locate(self, seen=None, machine="mlab1-xyz01.mlab-oti.measurement-lab.org", status=200, urls=None):
        def handler(request):
            if seen is not None:
                seen.append(request)
            if status != 200:
                return httpx.Response(status)
            body = {"results": [{"machine": machine, "urls": urls or {
                "wss:///ndt/v7/download": f"ws://127.0.0.1:{self.port}/ndt/v7/download?access_token=tok",
                "wss:///ndt/v7/upload": f"ws://127.0.0.1:{self.port}/ndt/v7/upload?access_token=tok"}}]}
            return httpx.Response(200, json=body)
        return httpx.MockTransport(handler)


def mlab(server, clock=None, **kw):
    return MLabProvider(clock=clock or Clock(), transport=server.locate(kw.pop("seen", None), **kw), trusted_only=False)


async def test_mlab_download_counts_binary_messages_within_the_cap_and_names_the_machine():
    seen = []
    async with NdtServer() as server:
        provider = mlab(server, seen=seen)
        result = await provider.download(2_000_000, LIMIT)
        await provider.aclose()
    assert 2_000_000 <= result.bytes <= 2_000_000 + MESSAGE          # the slack: the message that crossed the cap
    assert result.server == "mlab1-xyz01.mlab-oti.measurement-lab.org"
    assert server.subprotocols == [SUBPROTOCOL] and "access_token=tok" in server.queries[0]
    query = parse_qs(seen[0].url.query.decode())
    assert query == {"client_name": ["pengumon"], "client_version": [__version__]}
    assert seen[0].url.host == "locate.measurementlab.net" or seen[0].url.path.endswith("/v2/nearest/ndt/ndt7")


async def test_mlab_download_never_counts_more_than_one_message_past_a_small_cap_of_a_server_that_never_stops():
    async with NdtServer() as server:
        provider = mlab(server)
        result = await provider.download(300_000, LIMIT)
        await provider.aclose()
    assert result.bytes <= 300_000 + MESSAGE
    assert server.sent < 300_000 * 10                                 # the server did not get to send ten times the cap


async def test_mlab_records_the_whole_last_message_but_measures_the_speed_on_the_cap():
    """The message that crosses the cap was received: it counts toward the data (the record and the monthly cap),
    while the speed is that of the bytes up to the cap."""
    async with NdtServer() as server:
        provider = mlab(server)
        result = await provider.download(100_000, LIMIT)
        assert provider.moved() == result.bytes
        await provider.aclose()
    assert result.bytes == 2 * MESSAGE                                # two whole messages, past the cap of 100 000
    assert provider.meter.total == 100_000 and provider.meter.over == 2 * MESSAGE - 100_000


async def test_mlab_keeps_at_most_two_messages_waiting():
    """websockets' default queue (16 messages of up to 16 MiB) let a server have 256 MiB buffered."""
    asked = []

    def connect(url, **kwargs):
        asked.append(kwargs)
        raise OSError("refused")

    async with NdtServer() as server:
        provider = MLabProvider(clock=Clock(), transport=server.locate(None), trusted_only=False, connect_fn=connect)
        with pytest.raises(ProviderError):
            await provider.download(1_000_000, LIMIT)
        await provider.aclose()
    assert asked[0]["max_queue"] == 2 and asked[0]["max_size"] == 1 << 24


async def test_mlab_upload_sends_binary_messages_never_beyond_the_cap():
    async with NdtServer() as server:
        provider = mlab(server)
        result = await provider.upload(1_000_000, LIMIT)
        await provider.aclose()
        await asyncio.sleep(0.05)
    assert result.bytes == 1_000_000 and server.received <= 1_000_000
    assert server.messages[0] == 1 << 13 and max(server.messages) <= 1 << 18 and min(server.messages) >= 1 << 10
    assert "upload" in server.queries[0] and server.subprotocols == [SUBPROTOCOL]


async def test_mlab_stops_at_the_time_limit():
    class Ticking(Clock):
        def __call__(self):
            self.now += 0.5
            return self.now
    async with NdtServer() as server:
        provider = mlab(server, clock=Ticking())
        result = await provider.download(10**9, 5.0)
        await provider.aclose()
    assert 0 < result.bytes < 10**9 and result.elapsed_s < 7.0


async def test_mlab_refuses_a_locate_answer_that_names_a_server_that_is_not_m_labs():
    evil = {"wss:///ndt/v7/download": "wss://evil.example.com/ndt/v7/download",
            "wss:///ndt/v7/upload": "wss://evil.example.com/ndt/v7/upload"}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"machine": "m", "urls": evil}]}))
    provider = MLabProvider(clock=Clock(), transport=transport)
    with pytest.raises(ProviderError, match="not M-Lab's"):
        await provider.download(1_000_000, LIMIT)
    plain = {k: v.replace("wss://evil.example.com", "ws://mlab1.measurement-lab.org") for k, v in evil.items()}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"machine": "m", "urls": plain}]}))
    with pytest.raises(ProviderError, match="not M-Lab's"):         # (not wss)
        await MLabProvider(clock=Clock(), transport=transport).download(1_000_000, LIMIT)
    ok = {k: v.replace("evil.example.com", "mlab1.measurement-lab.org") for k, v in evil.items()}
    provider = MLabProvider(clock=Clock(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"results": [{"machine": "m", "urls": ok}]})))
    assert await provider._locate() == ok


@pytest.mark.parametrize("status, text", [(429, "rate limit"), (500, "HTTP 500")])
async def test_mlab_locate_errors_are_failures(status, text):
    async with NdtServer() as server:
        with pytest.raises(ProviderError, match=text):
            await mlab(server, status=status).download(1_000_000, LIMIT)


async def test_mlab_does_not_follow_a_redirect_from_the_server_it_was_given():
    """The locate answer's servers are checked; a redirect from one of them could lead the test (and its token)
    anywhere, and `websockets` follows one by default."""
    async with NdtServer() as target:
        async def redirect(connection, request):
            response = connection.respond(302, "")
            response.headers["Location"] = f"ws://127.0.0.1:{target.port}{request.path}"
            return response
        redirector = await serve(lambda c: None, "127.0.0.1", 0, process_request=redirect)
        port = redirector.sockets[0].getsockname()[1]
        try:
            provider = MLabProvider(clock=Clock(), trusted_only=False, transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"results": [{"machine": "m", "urls": {
                    "wss:///ndt/v7/download": f"ws://127.0.0.1:{port}/ndt/v7/download?access_token=tok",
                    "wss:///ndt/v7/upload": f"ws://127.0.0.1:{port}/ndt/v7/upload?access_token=tok"}}]})))
            with pytest.raises(ProviderError, match="could not run the test at M-Lab"):
                await provider.download(1_000_000, LIMIT)
            await provider.aclose()
        finally:
            redirector.close()
            await redirector.wait_closed()
    assert target.queries == [] and target.sent == 0


async def test_mlab_access_token_stays_out_of_the_log_even_at_debug(monkeypatch):
    """`websockets` logs its handshake's request line (path and query: the access token) at DEBUG, and a debug log
    level is one Settings choice away, and the console log is kept (podman logs, journald). Its client logger stays at
    INFO by default."""
    import io
    import logging

    from pengumon.observability.logs import setup_logging

    for var in ("PENGUMON_LOG_LEVEL", "PENGUMON_LOG_LEVELS", "PENGUMON_LOG_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "websockets.client")
    named = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers), logging.getLogger(n).propagate)
             for n in names}
    stream = io.StringIO()
    try:
        setup_logging("debug", levels="", fmt="text", stream=stream)
        async with NdtServer() as server:
            provider = mlab(server)
            await provider.download(200_000, LIMIT)
            await provider.aclose()
        assert "access_token=tok" in server.queries[0]                     # (it was sent)
        # the test's own server logs it at debug (so debug was on); the client, which is Pengumon, does not
        leaks = [line for line in stream.getvalue().splitlines() if "access_token" in line]
        assert leaks and all("] websockets.server: " in line for line in leaks), leaks
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        for name, (level, handlers, propagate) in named.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate

async def test_mlab_connects_directly_whatever_proxy_the_environment_sets(monkeypatch):
    """The locate request and the websocket both ignore the environment's proxy (base.PROXY_NOTE: a test through a
    proxy measures the proxy). Here the proxy is a closed port: anything sent through it fails."""
    _clear_proxies(monkeypatch)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{closed.getsockname()[1]}"
    closed.close()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, dead)
    async with NdtServer() as server:
        located = []
        body = json.dumps({"results": [{"machine": "m", "urls": {
            "wss:///ndt/v7/download": f"ws://127.0.0.1:{server.port}/ndt/v7/download?access_token=tok",
            "wss:///ndt/v7/upload": f"ws://127.0.0.1:{server.port}/ndt/v7/upload?access_token=tok"}}]}).encode()

        async def locate(reader, writer):
            located.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n%s" % (len(body), body))
            await writer.drain()
            writer.close()
        listening = await asyncio.start_server(locate, "127.0.0.1", 0)
        try:
            provider = MLabProvider(clock=Clock(), trusted_only=False,       # the provider's own locate client
                                    locate_url=f"http://127.0.0.1:{listening.sockets[0].getsockname()[1]}/locate")
            result = await provider.download(500_000, LIMIT)
            await provider.aclose()
        finally:
            listening.close()
            await listening.wait_closed()
    assert len(located) == 1 and located[0].startswith(b"GET /locate?")   # (a proxied request names the full URL)
    assert result.bytes >= 500_000 and "access_token=tok" in server.queries[0]

async def test_mlab_with_no_server_in_the_answer_is_a_failure():
    provider = MLabProvider(clock=Clock(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"results": []})), trusted_only=False)
    with pytest.raises(ProviderError, match="found no server"):
        await provider.download(1_000_000, LIMIT)


@pytest.mark.parametrize("gzip", [False, True])
async def test_mlab_reads_no_more_than_64_kib_of_the_locate_answer(gzip):
    junk = Junk(64, 1 << 20, gzip=gzip)  # 64 MiB, or 64 KB of gzip that would inflate to it
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"} if gzip else {}, stream=junk)

    provider = MLabProvider(clock=Clock(), transport=httpx.MockTransport(handler), trusted_only=False)
    with pytest.raises(ProviderError, match="more than 64 KiB"):
        await provider.download(1_000_000, LIMIT)
    assert junk.served <= 2 << 20 and seen[0].headers["accept-encoding"] == "identity"
    await provider.aclose()


async def test_mlab_refused_connection_is_a_failure():
    async with NdtServer() as server:
        provider = mlab(server)
        port = server.port
    # the server is closed now: nothing listens on its port
    provider = MLabProvider(clock=Clock(), trusted_only=False, transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"results": [{"machine": "m", "urls": {
            "wss:///ndt/v7/download": f"ws://127.0.0.1:{port}/ndt/v7/download",
            "wss:///ndt/v7/upload": f"ws://127.0.0.1:{port}/ndt/v7/upload"}}]})))
    with pytest.raises(ProviderError, match="could not run the test at M-Lab"):
        await provider.download(1_000_000, LIMIT)


# --- over a real socket (the wire behavior the mock transports cannot show) ----------------------------------------

class RawServer:
    """A minimal HTTP/1.1 server on the loopback interface: GET /__down?bytes=N (and garbage.php) streams ten times what
    was asked for until the client hangs up; POST reads exactly Content-Length bytes (a chunked body is refused)."""

    def __init__(self):
        self.sent = 0
        self.received = 0
        self.asked = []
        self.upload_heads = []
        self.aborted = 0
        self.whole = 0          # ten-times bodies sent to their end (none, when the client hangs up)

    async def handle(self, reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, target, _ = lines[0].split(" ")
                headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
                if method == "GET":
                    asked = int(parse_qs(target.split("?", 1)[1]).get("bytes", parse_qs(target.split("?", 1)[1]).get("ckSize"))[0])
                    if "ckSize" in target:
                        asked *= MIB
                    self.asked.append(asked)
                    body = asked * 10
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {body}\r\ncf-meta-colo: AMS\r\n\r\n".encode())
                    left = body
                    while left > 0:
                        piece = min(SERVED_CHUNK, left)
                        writer.write(b"\0" * piece)
                        await writer.drain()                  # (raises when the client has closed)
                        await asyncio.sleep(0)                # the client shares this loop: let it read, and hang up
                        self.sent += piece
                        left -= piece
                    self.whole += 1
                else:
                    self.upload_heads.append(headers)
                    if "content-length" not in headers:
                        writer.write(b"HTTP/1.1 411 Length Required\r\nContent-Length: 0\r\n\r\n")
                        await writer.drain()
                        continue
                    size = int(headers["content-length"])
                    try:
                        got = await reader.readexactly(size)
                    except asyncio.IncompleteReadError as e:
                        self.received += len(e.partial)
                        self.aborted += 1
                        return
                    self.received += len(got)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\ncf-meta-colo: AMS\r\n\r\n")
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def raw_server():
    server = RawServer()
    listening = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    return server, listening, listening.sockets[0].getsockname()[1]


async def test_cloudflare_shaped_download_and_upload_over_a_real_socket_keep_the_cap():
    server, listening, port = await raw_server()
    provider = CloudflareProvider(base=f"http://127.0.0.1:{port}")
    try:
        down = await provider.download(2_000_000, LIMIT)
        up = await provider.upload(1_500_000, LIMIT)
    finally:
        await provider.aclose()
        listening.close()
        await listening.wait_closed()
    assert down.bytes == 2_000_000 and sum(server.asked) <= 2_000_000 and down.server == "AMS"
    # the ten-times bodies were hung up on, not read (what the server got into the socket buffers before it noticed
    # depends on their size: the client reads in batches)
    assert server.whole == 0 and server.sent < sum(server.asked) * 10
    assert up.bytes == server.received == 1_500_000
    assert all("content-length" in h and "transfer-encoding" not in h for h in server.upload_heads)
    assert down.mbps and up.mbps


async def test_librespeed_over_a_real_socket_asks_whole_mebibytes():
    server, listening, port = await raw_server()
    provider = LibreSpeedProvider(f"http://127.0.0.1:{port}")
    try:
        down = await provider.download(10_000_000, LIMIT)
    finally:
        await provider.aclose()
        listening.close()
        await listening.wait_closed()
    assert all(n % MIB == 0 for n in server.asked) and sum(server.asked) <= 10_000_000
    assert down.bytes == sum(server.asked)


def _clear_proxies(monkeypatch):
    import os
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_a_connection_failure_with_a_proxy_set_says_the_test_does_not_use_it(monkeypatch, error):
    _clear_proxies(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.example:3128")

    def refuse(request):
        raise error("refused", request=request)
    provider = CloudflareProvider(clock=Clock(), transport=httpx.MockTransport(refuse))
    with pytest.raises(ProviderError, match="could not connect to Cloudflare; a proxy is set for Pengumon") as e:
        await provider.download(5_000_000, LIMIT)
    assert "proxy.example" not in e.value.reason and "secret" not in e.value.reason
    await provider.aclose()


@pytest.mark.parametrize("env", [{}, {"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "speed.cloudflare.com"},
                                 {"HTTP_PROXY": "http://proxy.example:3128"}])
async def test_a_connection_failure_without_a_proxy_for_the_test_has_the_plain_reason(monkeypatch, env):
    _clear_proxies(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    provider = CloudflareProvider(clock=Clock(), transport=httpx.MockTransport(refuse))
    with pytest.raises(ProviderError) as e:
        await provider.download(5_000_000, LIMIT)
    assert e.value.reason.startswith("could not connect to Cloudflare") and "proxy" not in e.value.reason
    await provider.aclose()


def test_the_providers_time_with_the_high_resolution_clock():
    """On Windows time.monotonic advances in steps of about 15.6 ms: a phase over loopback measured 0 s and no speed (a
    CI run on windows-latest). perf_counter is the high-resolution clock on every platform."""
    import time
    from pengumon.speedtest.providers.base import SpeedProvider
    assert CloudflareProvider().clock is time.perf_counter
    assert LibreSpeedProvider("http://speed.example.com/backend").clock is time.perf_counter
    assert MLabProvider().clock is time.perf_counter
    assert SpeedProvider.__init__.__defaults__[0] is time.perf_counter
