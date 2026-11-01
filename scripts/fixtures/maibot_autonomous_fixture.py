"""Copied into an isolated plugin only. Never install in production plugins."""
from maibot_sdk import API


def install(cls):
    @API('autonomous_life.acceptance_cycle', public=True, timeout_ms=180000)
    async def acceptance_cycle(self, **kwargs):
        self.config.life.wake = '00:00'
        self.config.life.sleep = '00:00'
        await self.consider()
        sid, _ = await self.context('acceptance@chatroom')
        return self.store.snapshot(sid)
    cls.acceptance_cycle = acceptance_cycle
    @API('autonomous_life.acceptance_flush_progress', public=True, timeout_ms=120000)
    async def acceptance_flush_progress(self, release_cooldown: bool = False, **kwargs):
        if release_cooldown:
            with self.store.db() as db:
                db.execute('UPDATE shares SET stamp=stamp-260000')
        sid, rows = await self.context('acceptance@chatroom')
        sent = await self.flush_progress(sid, rows)
        return dict(success=True, sent=sent, pending=len(self.store.pending_progress(sid, self.now().timestamp())))
    cls.acceptance_flush_progress = acceptance_flush_progress
    @API('autonomous_life.acceptance_repost', public=True, timeout_ms=240000)
    async def acceptance_repost(self, **kwargs):
        # Accelerate only the isolated test clock's prior share cooldown.
        with self.store.db() as db:
            db.execute('UPDATE shares SET stamp=stamp-7500')
        return await self.repost_opportunity(group_id='acceptance@chatroom')
    cls.acceptance_repost = acceptance_repost
    @API('autonomous_life.acceptance_project_permission', public=True, timeout_ms=120000)
    async def acceptance_project_permission(self, setup: bool = False, **kwargs):
        # This API exists only in the isolated copied plugin, never in production.
        group = 'acceptance@chatroom'
        if setup:
            self.config.project.enabled = True
            self.config.project.request_id = 'acceptance-project-once'
            self.config.project.owner_user_id = 'wxid_acceptance_user'
            self.config.project.objective = 'Build a bounded prototype'
            self.projects.seed()
            with self.store.db() as db:
                db.execute('UPDATE shares SET stamp=stamp-260000')
            self.projects.block(group, dict(project=dict(title='native permission prototype')),
                dict(capability='packages', reason='missing a dependency', alternative='standard library prototype'))
        sid, _ = await self.context(group)
        sent = await self.projects.notify(group, sid)
        job = self.projects.get(group)
        return dict(success=True, sent=sent, state=job['state'], request_states=[r['state'] for r in job['permissions']])
    cls.acceptance_project_permission = acceptance_project_permission
