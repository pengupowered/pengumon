import asyncio, json, time

async def main():
    import websockets
    sizes = []
    async with websockets.connect("ws://127.0.0.1:8000/ws", max_size=None) as ws:
        t0 = time.time()
        for _ in range(4):
            msg = await ws.recv()
            sizes.append(len(msg))
            if len(sizes) == 1:
                d = json.loads(msg)
                parts = {k: len(json.dumps(v)) for k, v in d.items()}
                print("first message keys by bytes:", dict(sorted(parts.items(), key=lambda kv: -kv[1])))
                print("recent_incidents entries:", len(d.get("recent_incidents") or []))
        print("message sizes (bytes):", sizes, "| elapsed %.1fs for %d messages" % (time.time() - t0, len(sizes)))
        print("=> about %.0f KB/s per connected dashboard" % (sum(sizes) / (time.time() - t0) / 1000))

asyncio.run(main())

# established TCP connections to port 8000 (0x1F40) in this network namespace
n = 0
for line in open("/proc/net/tcp").read().splitlines()[1:]:
    f = line.split()
    if f[3] == "01" and f[1].endswith(":1F40"):
        n += 1
print("established connections to :8000 right now (includes this probe's own, now closed):", n)
