"""Exercise the shipped plugin HTML in an isolated headless browser, no live settings writes."""
import asyncio
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
HTML=ROOT/'plugins/mai-repost-shit/webui/story.html'
PRESET=json.loads((ROOT/'plugins/mai-repost-shit/core/story_tags.json').read_text(encoding='utf8'))


async def main():
    browser_path=next((p for p in (Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
        Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')) if p.exists()),None)
    if browser_path is None:raise RuntimeError('No prepared browser')
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(executable_path=str(browser_path),headless=True)
        try:
            page=await browser.new_page(viewport={'width':1280,'height':1000})
            setup='''<script>
              window.calls=[];window.state={success:true,tags:TAGS,authorized:false,uid:'',use_account:false,rules:'每批10条，分阶段标签匹配'};
              window.maibot={ready:Promise.resolve(),query:async()=>state,action:async(name,args)=>{
                calls.push({name,args});if(name==='edit'){
                  state.tags=state.tags.filter(x=>x.tag!==args.tag);if(!args.delete)state.tags.push({tag:args.tag,weight:args.weight});return {success:true};}
                if(name==='importTags')return {success:true,imported:2};
                if(name==='auth'&&args.action==='start')return {success:true,image:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlqVq0AAAAASUVORK5CYII=',expires_in:180};
                if(name==='auth'){state.authorized=true;state.uid='123';return {success:true,state:'authorized',uid:'123'};}
                if(name==='revoke'){state.authorized=false;return {success:true};}return {success:true};}};
            </script>'''.replace('TAGS',json.dumps([{'tag':k,'weight':v} for k,v in PRESET.items()],ensure_ascii=False))
            style='''<style>:root{--maibot-background:#f8f9fb;--maibot-foreground:#1e293b;--maibot-muted:#64748b;--maibot-border:#dbe2eb;--maibot-primary:#6366f1;--maibot-danger:#dc2626}
            .maibot-page{font-family:Arial,'Microsoft Yahei';color:var(--maibot-foreground);background:var(--maibot-background)}.maibot-card{padding:20px;border:1px solid var(--maibot-border);border-radius:12px;background:white}.maibot-button,.maibot-input,.maibot-select{padding:8px 12px;border:1px solid var(--maibot-border);border-radius:6px;background:transparent;color:inherit}.maibot-button-primary{background:var(--maibot-primary);color:white}.maibot-table{width:100%;border-collapse:collapse}.maibot-table td,.maibot-table th{text-align:left;padding:9px;border-bottom:1px solid var(--maibot-border)}.maibot-muted{color:var(--maibot-muted)}.maibot-card-header{padding-bottom:12px;border-bottom:1px solid var(--maibot-border)}</style>'''
            await page.set_content('<!doctype html>'+setup+style+HTML.read_text(encoding='utf8'))
            await page.wait_for_function("document.getElementById('count').textContent.includes('333')")
            assert await page.locator('#tags tr').count()==40
            await page.locator('#search').fill('抽象');assert await page.locator('#tags tr').count()==1
            await page.locator('#tags input').fill('77');await page.locator('#tags button').first.click()
            await page.wait_for_function("state.tags.some(x=>x.tag==='抽象'&&x.weight===77)")
            await page.locator('#file').set_input_files({'name':'tags.txt','mimeType':'text/plain','buffer':'排名\t标签\t出现视频数\n1\t新标签\t5'.encode()})
            await page.locator('#import').click();assert '请选择' in await page.locator('#error').inner_text() or '请先选择' in await page.locator('#error').inner_text()
            await page.locator('#mode').select_option('merge');await page.locator('#import').click()
            await page.wait_for_function("calls.some(x=>x.name==='importTags'&&x.args.mode==='merge')")
            await page.locator('summary').click()
            await page.locator('#tokens').fill('{"accessToken":"fixture-not-a-real-secret"}')
            await page.locator('#importAuth').click()
            await page.wait_for_function("document.getElementById('tokens').value===''")
            await page.locator('#login').click();await page.locator('#qr').wait_for(state='visible')
            await page.locator('#verify').click();await page.wait_for_function("document.getElementById('auth').textContent.includes('123')")
            await page.locator('#search').fill('');await page.screenshot(path=str(ROOT/'temp/story-webui-light.png'),full_page=True)
            await page.add_style_tag(content=':root{--maibot-background:#111827;--maibot-foreground:#e5e7eb;--maibot-border:#374151;--maibot-muted:#9ca3af}.maibot-card{background:#1f2937}')
            await page.screenshot(path=str(ROOT/'temp/story-webui-dark.png'),full_page=True)
            print('PASS: 333 tags, pagination, filtering, edit, import mode selection, import payload, masked token clearing, QR display, auth status, light/dark layout')
        finally:await browser.close()


if __name__=='__main__':asyncio.run(main())
