"""The preview component: builds the job store for the outage-rules preview (settings/preview.py) and kills a running
replay at shutdown."""

from pengumon.components import Component, Context
from pengumon.settings.preview import PreviewJobs


async def register(ctx: Context) -> Component:
    jobs = PreviewJobs()
    ctx.state.preview = jobs
    return Component("settings.preview", stop=jobs.aclose)
