"""The probe loop: one round every `prober.interval_seconds` that probes, stores, classifies and publishes.

It never waits for HTTP or for a websocket client: the devices run in their own tasks (their latest snapshots are
simply read by the live parts) and `ws.publish` only stores the message for the broadcaster task. The live message
is the round itself (timestamp, diagnosis, targets) plus one field per registered live part (components.LivePart):
the devices' summaries, the line, the incidents version. It is only built while a dashboard is open.
"""

import asyncio
import time
from typing import Iterable, Sequence

from pengumon.api.ws import ws_manager
from pengumon.components import LivePart
from pengumon.live.message import LiveStreamMessage
from pengumon.observability import get_logger
from pengumon.observability.telemetry import probe_round_duration, record_probes

_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had

_ROUND_FIELDS = frozenset({"timestamp", "diagnosis", "targets"})


def check_live_parts(parts: Iterable[LivePart]) -> None:
    """Every part names a field of the live message that the round itself does not fill, and no two the same one;
    otherwise pydantic would drop it, or one would silently replace the other."""
    names = [p.name for p in parts]
    unknown = [n for n in names if n not in LiveStreamMessage.model_fields or n in _ROUND_FIELDS]
    if unknown:
        raise ValueError(f"live parts {', '.join(unknown)} are not fields a component may fill in LiveStreamMessage")
    if len(set(names)) != len(names):
        raise ValueError(f"a live-message field has two parts: {', '.join(sorted(names))}")


async def probe_background_worker(config, prober, classifier, repository, ws=ws_manager,
                                  live_parts: Sequence[LivePart] = ()):
    """Continuous background loop executing network probes and handing telemetry to the broadcaster."""
    _log.info(f"Starting continuous network prober (Interval: {config.prober.interval_seconds}s)...")

    interval = config.prober.interval_seconds
    while True:
        start_time = time.perf_counter()
        try:
            # 1. Execute probe round across all targets concurrently
            samples = await prober.execute_probe_round(
                targets=config.targets,
                dns_probes=config.dns_probes,
            )

            # 2. Persist samples to SQLite
            await repository.insert_samples(samples)
            record_probes(samples, {target_id: s.category for target_id, s in prober.stats.items()})

            # 3. Classify network health & detect incidents
            diagnosis = await classifier.evaluate(samples, prober.stats)

            # 4. Only build the live message when a dashboard is open.
            if ws.active_connections:
                ws.publish(LiveStreamMessage(
                    timestamp=time.time(),
                    diagnosis=diagnosis,
                    targets=list(prober.stats.values()),
                    **{part.name: part.value(diagnosis) for part in live_parts},
                ))

        except asyncio.CancelledError:
            _log.info("Probe background loop cancelled.")
            break
        except Exception as e:
            _log.error(f"Error during probe round: {e}", exc_info=True)

        # Precise timing compensation
        elapsed = time.perf_counter() - start_time
        probe_round_duration.record(elapsed)
        sleep_dur = max(0.1, interval - elapsed)
        await asyncio.sleep(sleep_dur)
