"""Isolated retirement, points, dual-platform blacklist and help-poster acceptance."""
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='plugin-cleanup-',ignore_cleanup_errors=True)
ISOLATED=Path(INSTANCE.name)
os.environ['MAIBOT_INSTANCE_DIR']=str(ISOLATED);os.environ['MAIBOT_INTERCEPT_SEND']='1'
for folder in ('config','plugins','auto','temp'):(ISOLATED/folder).mkdir()
for name in ('bot_config.toml','model_config.toml'):shutil.copy2(ROOT/'config'/name,ISOLATED/'config'/name)
for folder in ('plugins','auto'):
    for path in (ROOT/folder).glob('*.py'):shutil.copy2(path,ISOLATED/folder/path.name)
(ISOLATED/'config.json').write_text('{}',encoding='utf8')
for name in ('tieba_share','mai-repost-shit'):
    (ISOLATED/'plugins'/name).mkdir()
    shutil.copy2(ROOT/'plugins'/name/'config.toml',ISOLATED/'plugins'/name/'config.toml')
from plugins import ban,help as help_plugin,player,sign
from utils import user_blacklist as blacklist
from utils.help_poster import parse_help,render_help
from core import maibot_shell_registry as registry
from PIL import Image

RETIRED=('guide','home','lottery','notice','prompt','redeem')


class RetiredAndPoints(unittest.TestCase):
    def test_retired_modules_no_files_commands_or_management_entries(self):
        rows=registry.catalog()
        self.assertFalse(set(RETIRED)&{row['module'] for row in rows})
        from core.dispatcher import Dispatcher
        dispatcher=Dispatcher();dispatcher.load_plugins()
        for name in RETIRED:
            self.assertFalse((ROOT/'plugins'/(name+'.py')).exists())
            self.assertNotIn('/'+name,dispatcher.plugins)
        self.assertNotIn('/彩票',dispatcher.plugins)
        self.assertFalse(registry.allowed('gsmps.command.lottery'))

    def test_fortune_has_no_point_award_but_keeps_daily_limit(self):
        fixture={'Player':{'bind_user':'wxid_fixture','last_fortune_at':None}}
        with patch.object(player,'player_stats_all',return_value=fixture), \
             patch('utils.account_identity.canonical_user',side_effect=lambda uid:uid), \
             patch.object(player,'build_fortune_text',return_value='今日运势：平安'), \
             patch.object(player,'player_stats_update') as update,patch('utils.points_manager.add_points') as credit:
            self.assertEqual(player.handle('fortune',{'wxid':'wxid_fixture'}),'今日运势：平安')
            credit.assert_not_called();update.assert_called_once()
            fixture['Player']['last_fortune_at']=datetime.now().isoformat()
            self.assertIn('已抽取',player.handle('fortune',{'wxid':'wxid_fixture'}));credit.assert_not_called()

    def test_sign_and_point_rules_no_fortune_credit_advertisement(self):
        with patch('utils.points_manager.binding_required',return_value=None), \
             patch.object(sign,'has_signed_today',return_value=False), \
             patch('utils.points_manager.claim_sign',return_value=(True,20)):
            text=sign.handle('',{'wxid':'wxid_fixture'})
            self.assertIn('签到成功',text);self.assertNotIn('fortune',text);self.assertNotIn('运势',text)
        from plugins import points
        with patch('utils.points_manager.binding_required',return_value=None):
            self.assertNotIn('今日运势',points.handle('help',{'wxid':'wxid_fixture'}))


class BanBothPlatforms(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_bans_persist_and_block_router_and_native_core(self):
        from src.config.config import global_config
        from core.router import Router
        from src.chat.message_receive.bot import ChatBot
        with patch.object(ban,'ADMIN_WXIDS',[]), \
             patch.object(global_config.plugin,'permission',['wechat:wxid_admin','qq:123456']), \
             patch.object(global_config.plugin,'command_permissions',{}):
            for platform,actor,target,key in (('wechat','wxid_admin','wxid_target','wxid_target'),('qq','123456','789012','qq:789012')):
                context={'platform':platform,'user_id':actor,'wxid':actor if platform=='wechat' else 'qq:'+actor}
                self.assertIn('加入',ban.handle(target,context))
                self.assertTrue(blacklist.is_banned(platform,target));self.assertFalse(blacklist.is_banned(platform,actor))
                router=Router([],['fixture'],prefix_mode='mixed')
                self.assertIsNone(router.parse({'platform':platform,'wxid':key,'group':'fixture','sessionId':'fixture','content':'/help'}))
                message=NS(received_at=datetime.now(),platform=platform,message_info=NS(
                    group_info=None,user_info=NS(user_id=target)))
                # Banned input exits before media/hooks/plugins/LLM admission.
                self.assertEqual(await ChatBot.receive_message(ChatBot.__new__(ChatBot),message),'user_blacklisted')
            self.assertFalse(blacklist.is_banned('wechat','789012'))
            self.assertFalse(blacklist.is_banned('qq','wxid_target'))
            from utils.sqlite_store import load_document
            self.assertTrue(load_document('data/blacklist.json')['qq:789012'])

    def test_nonadmin_and_bound_identity_cannot_gain_admin_rights(self):
        with patch.object(ban,'ADMIN_WXIDS',[]),patch('src.core.local_operator.has_command_permission',return_value=False):
            self.assertIn('没有权限',ban.handle('888888',{'platform':'qq','user_id':'999999','wxid':'qq:999999'}))
        with patch.object(ban,'ADMIN_WXIDS',['wxid_admin']):
            self.assertFalse(ban.is_admin({'platform':'qq','user_id':'wxid_admin'}))

    def test_cross_platform_explicit_id_and_reject_names(self):
        with patch.object(ban,'is_admin',return_value=True):
            self.assertIn('加入',ban.handle('qq:654321',{'platform':'wechat'}))
            self.assertTrue(blacklist.is_banned('qq','654321'))
            self.assertIn('用法',ban.handle('相似昵称',{'platform':'qq'}))
            self.assertIn('用法',ban.handle('-12345',{'platform':'qq'}))

    def test_legacy_json_is_migrated_but_sqlite_is_authoritative(self):
        path=ISOLATED/'data'/'legacy-blacklist.json';path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'wxid_legacy':True}),encoding='utf8')
        self.assertTrue(blacklist.is_banned('wechat','wxid_legacy',path=str(path)))
        blacklist.add('qq:345678',path=str(path))
        self.assertTrue(blacklist.is_banned('qq','345678',path=str(path)))
        self.assertNotIn('qq:345678',json.loads(path.read_text(encoding='utf8')))


class HelpPoster(unittest.TestCase):
    def test_active_catalog_filters_retired_disabled_wrong_platform_and_subsource(self):
        controls={f'gsmps.command.{name}':{'enabled':False} for name in ('fx','persona','otto')}
        with patch.object(registry,'state',return_value=controls),patch.object(registry,'get_dispatcher',return_value=None), \
             patch.object(registry,'allowed',return_value=True),patch.object(ban,'is_admin',return_value=False):
            for platform in ('wechat','qq'):
                visible=help_plugin.visible_commands({'platform':platform})
                self.assertFalse(visible('/fx help'));self.assertFalse(visible('/otto text'));self.assertFalse(visible('/ban id'))
                self.assertFalse(visible('/home'));self.assertFalse(visible('/lottery'))
                self.assertEqual(visible('/qq hello'),platform=='wechat')
                self.assertEqual(visible('/wx hello'),platform=='qq')
                self.assertTrue(visible('/sign'));self.assertTrue(visible('/对称 随机 5'))

    def test_real_text_layout_both_brightness_modes_and_bounds(self):
        text=(ROOT/'plugins/help.txt').read_text(encoding='utf8')
        self.assertFalse(any('/'+name in text for name in RETIRED))
        self.assertNotIn('/persona',text);self.assertNotIn('/fx ',text)
        parsed=parse_help(text)
        for label,color in (('light',(250,245,230)),('dark',(10,20,30))):
            bg=ISOLATED/('bg-'+label+'.png');Image.new('RGB',(800,1200),color).save(bg)
            output=ROOT/'temp'/('help-preview-'+label+'.jpg')
            report=render_help(parsed,bg,output)
            self.assertEqual(report['theme'],label)
            with Image.open(output) as image:
                self.assertEqual(image.width,1440);self.assertLess(image.height,6501)
        with self.assertRaises(ValueError):parse_help('a'*24001)

    def test_file_reread_each_request_background_fallback_send_and_cleanup(self):
        source=ISOLATED/'help.txt';outputs=[];captured=[]
        def render(parsed,bg,path):
            captured.append(parsed);Image.new('RGB',(40,40)).save(path);outputs.append(Path(path))
        def send(**kwargs):
            self.assertTrue(Path(kwargs['file_path']).is_file());return True,''
        with patch.object(help_plugin,'HELP_FILE',source),patch.object(help_plugin,'TEMP_DIR',ISOLATED/'temp'), \
             patch.object(help_plugin,'visible_commands',return_value=lambda label:True), \
             patch('plugins.acgpic.fetch_random_image',side_effect=RuntimeError('offline')), \
             patch('utils.help_poster.render_help',side_effect=render),patch.object(help_plugin,'send',side_effect=send):
            for platform,title in (('qq','第一版'),('wechat','第二版')):
                source.write_text(title+'\n[工具]\n/sign：每日签到',encoding='utf8')
                self.assertIsNone(help_plugin.handle('',{'platform':platform,'group':'fixture'}))
                self.assertEqual(captured[-1][0],title)
            self.assertTrue(all(not path.exists() for path in outputs))

    def test_successful_random_source_is_used_once_and_cleaned_after_send(self):
        bg=ISOLATED/'fixture-bg.png';Image.new('RGB',(600,900),(200,180,240)).save(bg)
        with patch.object(help_plugin,'HELP_FILE',ROOT/'plugins/help.txt'), \
             patch.object(help_plugin,'TEMP_DIR',ISOLATED/'temp'), \
             patch.object(help_plugin,'visible_commands',return_value=lambda label:True), \
             patch('plugins.acgpic.fetch_random_image',return_value=str(bg)) as fetch, \
             patch.object(help_plugin,'send',return_value=(True,'')) as send:
            self.assertIsNone(help_plugin.handle('',{'platform':'wechat','group':'fixture'}))
            fetch.assert_called_once_with(portrait=True);send.assert_called_once()
            self.assertFalse(bg.exists());self.assertFalse(Path(send.call_args.kwargs['file_path']).exists())

if __name__=='__main__':unittest.main(verbosity=2)
