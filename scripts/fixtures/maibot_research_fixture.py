"""Network fixture copied into an isolated Runner only; never into live plugins."""
def install():
    from . import research

    def explore(query='', url='', **kwargs):
        if url:
            return dict(url=url, text='verified_page_evidence_852', title='Primary documentation',
                        total_chars=26, next_offset=None, truncated=False)
        return dict(query=query, results=[dict(title='verified_search_evidence_741',
                    url='https://example.com/research-acceptance', excerpt='A technical primary source')])

    research.explore = explore
