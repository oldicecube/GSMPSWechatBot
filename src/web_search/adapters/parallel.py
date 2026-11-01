"""Parallel's documented MCP tools through MaiBot's shared native connection."""
import hashlib

from .stepsearch import StepSearchAdapter
from ..models import PageResult, SearchHit, SearchResult, WebSearchError


class ParallelMCPAdapter(StepSearchAdapter):
    def session_arguments(self):
        # Stable per real conversation; never expose platform/group identifiers.
        session = (self.context.session_id if self.context else '') or self.invocation.session_id
        if not session:
            raise WebSearchError('联网工具需要真实聊天 session_id')
        return {'session_id': hashlib.sha256(('maibot:' + session).encode()).hexdigest()}

    async def search(self, query: str, max_results: int) -> SearchResult:
        data = await self.call('web_search', {
            'objective': query, 'search_queries': [query], **self.session_arguments()})
        return SearchResult(query=query, results=[SearchHit(
            title=item.get('title') or item['url'], url=item['url'],
            snippet='\n'.join(item.get('excerpts') or []), published_at=item.get('publish_date'))
            for item in data['results'][:max_results]])

    async def fetch(self, url: str) -> PageResult:
        data = await self.call('web_fetch', {'urls': [url], 'full_content': False, **self.session_arguments()})
        rows = data.get('results') or []
        if not rows:
            raise WebSearchError('Parallel 未返回网页正文；可能受访问限制')
        page = rows[0]
        return PageResult(url=page['url'], title=page.get('title') or page['url'],
            content=page.get('full_content') or '\n'.join(page.get('excerpts') or []),
            content_kind='page_excerpts')
