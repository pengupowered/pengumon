"""The TCP connect probe (engine/probe_tcp.py) against real listeners on loopback inside the test container."""

import asyncio
import socket
import struct
import sys

import pytest

from pengumon.engine import probe_tcp
from pengumon.engine.probe_tcp import (SocketAnswer, connect_socket, kernel_rtt_ms, rtt_from_tcp_info, tcp_connect)


@pytest.fixture
async def listener():
    accepted = []

    async def handle(reader, writer):
        accepted.append(1)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    server.accepted = accepted
    yield server, server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_a_listening_port_answers_with_the_handshake_time(listener):
    _, port = listener
    a = await tcp_connect("127.0.0.1", 4, port, 1.0)
    assert a.kind == "reply" and a.error is None and 0 < a.rtt_ms < 50


async def test_the_kernels_figure_is_used_when_it_has_one_and_the_apps_when_not(listener):
    _, port = listener
    assert (await tcp_connect("127.0.0.1", 4, port, 1.0, kernel_rtt=lambda s: 0.123)).rtt_ms == 0.123
    a = await tcp_connect("127.0.0.1", 4, port, 1.0, kernel_rtt=lambda s: None)
    assert a.kind == "reply" and 0 < a.rtt_ms < 50 and a.rtt_ms != 0.123


async def test_a_closed_port_is_refused_with_its_number():
    port = free_port()
    a = await tcp_connect("127.0.0.1", 4, port, 5.0)  # Windows reports a refusal after ~2 s; Linux at once
    assert a == SocketAnswer("refused", error=f"Connection refused on port {port}")


async def test_no_answer_in_time_is_a_timeout_and_the_socket_is_closed(monkeypatch):
    made = []
    real_socket = socket.socket

    class Recording(real_socket):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(probe_tcp.socket, "socket", Recording)
    loop = asyncio.get_running_loop()

    async def never(sock, address):
        await asyncio.sleep(10)

    monkeypatch.setattr(loop, "sock_connect", never)
    started = asyncio.get_running_loop().time()
    a = await tcp_connect("127.0.0.1", 4, 9999, 0.2)
    assert a == SocketAnswer("timeout", error="No answer in time (TCP handshake)")
    assert loop.time() - started < 1.0
    assert len(made) == 1 and made[0].fileno() == -1


async def test_a_cancelled_connect_closes_its_socket_and_an_other_oserror_is_raised(monkeypatch):
    made = []
    real_socket = socket.socket

    class Recording(real_socket):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(probe_tcp.socket, "socket", Recording)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "sock_connect", lambda sock, address: asyncio.sleep(10))
    task = asyncio.ensure_future(connect_socket("127.0.0.1", 4, 9999))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made[0].fileno() == -1

    async def unreachable(sock, address):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(loop, "sock_connect", unreachable)
    with pytest.raises(OSError) as e:
        await tcp_connect("127.0.0.1", 4, 9999, 1.0)
    assert e.value.errno == 101 and made[1].fileno() == -1


def test_the_kernels_round_trip_is_read_from_the_tcp_info_struct():
    buf = bytearray(104)
    struct.pack_into("=I", buf, 68, 1234)
    assert rtt_from_tcp_info(bytes(buf)) == 1.234
    struct.pack_into("=I", buf, 68, 0)
    assert rtt_from_tcp_info(bytes(buf)) is None
    assert rtt_from_tcp_info(bytes(60)) is None


@pytest.mark.skipif(not probe_tcp.KERNEL_TIMING, reason="TCP_INFO is Linux's")
async def test_the_real_tcp_info_of_a_connected_socket_reads_as_a_figure_or_none(listener):
    _, port = listener
    sock, ms = await connect_socket("127.0.0.1", 4, port)
    try:
        value = kernel_rtt_ms(sock)
        assert value is None or 0 < value < 50
        assert ms > 0
    finally:
        sock.close()
    assert sys.platform.startswith("linux")
