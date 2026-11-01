"""Only copied into isolated acceptance plugins; never installed in production."""
CLIP=b'isolated-repost-video-fixture'


def install(client):
    async def search(self,**kwargs):
        index = getattr(self, '_fixture_downloaded', 0) + 1
        return [dict(bvid=f'BVfixture{index:03}',aid=123,title='repost fixture @LLM',author='fixture',
                     mid=1,play=100000,like=1000,favorites=100,duration='0:20',typename='鬼畜',tag='整活')]
    async def detail(self,bvid):
        return dict(cid=456,aid=123,duration=20,title='repost fixture @LLM',owner={'name':'fixture'},
                    stat={'view':100000,'like':1000,'favorite':100},tname='鬼畜')
    async def comments(self,aid,pages):
        return ['@friend 来看']*40+['comment']*10
    async def playurl(self,bvid,cid,quality_preference):
        return {'quality':64,'segments':[{'size':len(CLIP),'url':'https://fixture.invalid/video'}]}
    async def download(self,url,dest,max_bytes):
        dest.write_bytes(CLIP)
        self._fixture_downloaded = getattr(self, '_fixture_downloaded', 0) + 1
        return len(CLIP)
    client.search_videos=search
    client.video_detail=detail
    client.fetch_comments=comments
    client.playurl=playurl
    client.download=download
