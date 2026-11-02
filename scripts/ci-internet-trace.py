"""CI only (the internet-trace job): what probing the internet looks like from this runner. Prints the backend, an
echo to 1.1.1.1 and a trace to it, and one `::notice` annotation that sums it up. Never fails: hosted runners may drop
ICMP, and the job is there to learn what works, not to gate anything.

    python scripts/ci-internet-trace.py <label>
"""

import asyncio
import sys


async def main(label: str) -> str:
    from pengumon.engine.icmp_select import describe_resolution, open_backend
    from pengumon.engine.mtr import MtrEngine

    try:
        backend = open_backend()
    except OSError as exc:
        return f"{label}: no ICMP backend ({exc})"
    try:
        echo = await backend.probe("1.1.1.1", timeout=2.0)
        hops = await MtrEngine("1.1.1.1", max_hops=20, timeout=2.0, backend=backend).run_trace()
    finally:
        backend.close()
    for h in hops:
        print(f"  {h.hop_number:2}  {h.ip or '*':15}  {h.rtt_ms if h.rtt_ms is not None else '-'}")
    answered = sum(1 for h in hops if h.ip)
    reached = bool(hops) and hops[-1].ip == "1.1.1.1"
    return (f"{label}: {backend.name} ({describe_resolution(backend.resolution_ms)}); echo to 1.1.1.1: {echo.kind}"
            f"{f' in {echo.rtt_ms:.2f} ms' if echo.rtt_ms is not None else ''}; trace: {answered} of {len(hops)} hops "
            f"answered, target {'reached' if reached else 'not reached'}")


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else sys.platform
    try:
        summary = asyncio.run(main(label))
    except Exception as exc:  # noqa: BLE001 - report anything, fail nothing
        summary = f"{label}: the trace raised {type(exc).__name__}: {exc}"
    print(summary)
    print(f"::notice title=Internet trace ({label})::{summary}")
