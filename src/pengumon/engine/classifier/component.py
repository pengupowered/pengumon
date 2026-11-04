"""The classifier component: the OutageClassifier, which turns each probe round into a diagnosis and opens and
resolves the network incidents (announced through the notifier, when there is one).

At shutdown its path traces end first (they write to the repository, which closes after this component has
stopped), then the incident still open is resolved: nothing watches it once the app has stopped.
"""

import time

from pengumon.components import Component, Context
from pengumon.engine.classifier import OutageClassifier
from pengumon.lines import wording
from pengumon.observability import get_logger

_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had


async def register(ctx: Context) -> Component:
    notifier, repository = ctx.notifier, ctx.state.repository
    classifier = OutageClassifier(
        config=ctx.config.classifier, repository=repository, known_targets=ctx.config.targets, load=ctx.line_load,
        on_incident_started=notifier.on_incident_started if notifier else None,
        on_incident_resolved=notifier.on_incident_resolved if notifier else None,
        on_incident_escalated=notifier.on_incident_escalated if notifier else None,
        line_technology=ctx.state.devices.line_technology(),
    )
    ctx.state.classifier = classifier
    hub = getattr(ctx, "settings", None)
    if hub is not None:
        @hub.on(events=["devices_rebuilt"], name="classifier.devices")
        def _devices_rebuilt(change) -> None:
            classifier.line = wording(ctx.state.devices.line_technology())  # the access line may be another technology

    async def stop() -> None:
        await classifier.cancel_background()
        if classifier.active_incident and classifier.active_incident.id:
            try:
                await repository.resolve_incident(classifier.active_incident.id, time.time())
            except Exception as e:
                _log.error(f"Error resolving active incident on shutdown: {e}")

    return Component("classifier", stop=stop)
