"""Web-facing Organizer facade.

Production Web process keeps the full API object shape but does not start
long-running lanes; those belong to the separate worker process. Tests opt in
with STRIMKEEP_INLINE_WORKER=1 to preserve the previous end-to-end semantics.
"""
import asyncio
import os

from .organizer import Organizer


class WebOrganizer(Organizer):
    def __init__(self, store, cloud, http, strm_root, probe_fn=None):
        super().__init__(store, cloud, http, strm_root, probe_fn=probe_fn)
        self.web_only = os.environ.get('STRIMKEEP_INLINE_WORKER') != '1'

    def start(self):
        if not self.web_only:
            return super().start()
        return None

    async def close(self):
        if not self.web_only:
            return await super().close()
        # Web owns API-facing clients only. No lanes/timers were started here.
        await self.channels.close()
        await self.bot.close()
        if self.notice_tasks:
            for task in list(self.notice_tasks):
                task.cancel()
            await asyncio.gather(*list(self.notice_tasks), return_exceptions=True)
