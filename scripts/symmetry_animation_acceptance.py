"""Synthetic image/sticker regression; no live media fetches or group sends."""
import base64
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch

from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='symmetry-',ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR']=INSTANCE.name;os.environ['MAIBOT_INTERCEPT_SEND']='1'
(Path(INSTANCE.name)/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,Path(INSTANCE.name)/'config'/name)
from utils import symmetry_image as codec
from core import weflow_media as media
from auto import picture
from core import followup

MODES=('left_right','right_left','up_down','down_up')

def animated(disposal=2,loop=2):
    frames=[]
    for index in range(3):
        frame=Image.new('RGBA',(9,7),(0,0,0,0))
        draw=ImageDraw.Draw(frame)
        draw.rectangle((index,1,index+1,3),fill=(255,0,0,255))
        draw.point((3,index+3),fill=(0,0,0,255))
        draw.point((7-index,5),fill=(0,0,255,255))
        draw.point((index+1,6),fill=(0,255,0,255))
        frames.append(codec._gif_palette(frame))
    output=BytesIO();kw={'loop':loop} if loop is not None else {}
    frames[0].save(output,'GIF',save_all=True,append_images=frames[1:],
        duration=[40,90,130],transparency=0,background=0,disposal=disposal,optimize=True,**kw)
    return output.getvalue()


class Pixels(unittest.TestCase):
    def test_four_directions_preserve_odd_center_and_exact_rgba(self):
        for size in ((5,7),(6,8),(1,7),(7,1)):
            image=Image.new('RGBA',size)
            for y in range(size[1]):
                for x in range(size[0]):image.putpixel((x,y),(x*20,y*20,10,(x*33+y*17)%256))
            for mode in MODES:
                result=codec.symmetry_frame(image,mode)
                w,h=size
                for y in range(h):
                    for x in range(w):
                        sx=(min(x,w-1-x) if mode=='left_right' else max(x,w-1-x)) if mode in MODES[:2] else x
                        sy=(min(y,h-1-y) if mode=='up_down' else max(y,h-1-y)) if mode in MODES[2:] else y
                        self.assertEqual(result.getpixel((x,y)),image.getpixel((sx,sy)),(size,mode,x,y))

    def test_static_transparent_sticker_is_png_with_alpha_unchanged(self):
        image=Image.new('RGBA',(6,4),(20,40,60,128));buf=BytesIO();image.save(buf,'PNG')
        path=codec.save_symmetry(buf.getvalue(),'left_right',INSTANCE.name)
        with Image.open(path) as out:
            self.assertEqual(out.format,'PNG');self.assertEqual(out.convert('RGBA').getpixel((5,3)),(20,40,60,128))

    def test_gif_disposal_transparency_opaque_black_timing_and_loop(self):
        for disposal in (1,2,3):
            data=animated(disposal)
            for mode in MODES:
                path=codec.save_symmetry(data,mode,INSTANCE.name)
                self.assertTrue(path.endswith('.gif'))
                with Image.open(BytesIO(data)) as source,Image.open(path) as output:
                    self.assertEqual(output.n_frames,source.n_frames);self.assertEqual(output.info['loop'],2)
                    for index in range(source.n_frames):
                        source.seek(index);output.seek(index)
                        self.assertEqual(output.info['duration'],source.info['duration'])
                        expected=codec.symmetry_frame(source,mode);actual=output.convert('RGBA')
                        for xy in ((x,y) for x in range(9) for y in range(7)):
                            a,b=actual.getpixel(xy),expected.getpixel(xy)
                            self.assertEqual(a[3],b[3],(disposal,mode,index,xy))
                            if b[3]:self.assertEqual(a,b,(disposal,mode,index,xy))

    def test_finite_no_loop_animation_does_not_become_infinite(self):
        path=codec.save_symmetry(animated(loop=None),'left_right',INSTANCE.name)
        with Image.open(path) as out:self.assertNotIn('loop',out.info)

    def test_bounds_reject_input_frames_and_decoded_pixels(self):
        for flag,value in (('MAX_INPUT_BYTES',1),('MAX_FRAMES',2),('MAX_TOTAL_PIXELS',100),('MAX_FRAME_PIXELS',10)):
            with patch.object(codec,flag,value),self.assertRaises(ValueError):
                codec.save_symmetry(animated(),'left_right',INSTANCE.name)
        with self.assertRaises(Exception):codec.save_symmetry(b'not-an-image','left_right',INSTANCE.name)


class InputAndWorkflow(unittest.TestCase):
    def test_wechat_exact_original_and_nested_emoji_flags(self):
        context={'sessionId':'g@chatroom','serverId':'999','localId':23,'wxid':'wxid_user',
                 'raw':{'_raw':{'localType':47}}}
        self.assertTrue(picture._is_image(context))
        with patch('core.weflow_client.WeFlowClient') as client,patch.object(media,'download_media',return_value=animated()) as download:
            client.return_value.get_original_message.return_value={'ok':True,'message':{'serverIdRaw':'999','mediaType':'emoji','mediaUrl':'/api/v1/media/original.gif'}}
            self.assertTrue(media.load_visual_bytes(context).startswith(b'GIF'))
            client.return_value.get_original_message.assert_called_once_with('g@chatroom',23,'999',timeout=25,media=True)
            download.assert_called_once_with('/api/v1/media/original.gif',max_bytes=20*1024*1024)
            client.return_value.get_original_message.return_value={'ok':True,'message':{'serverIdRaw':'998','mediaUrl':'/wrong.png'}}
            with self.assertRaises(ValueError):media.load_visual_bytes(context)
            self.assertEqual(download.call_count,1)

    def test_qq_never_falls_back_to_wechat_and_supports_native_sticker_and_file(self):
        from core.shell_platform_bridge import native_router_message
        from src.plugin_runtime.host.message_utils import PluginMessageUtils as U
        from src.common.data_models.message_component_data_model import EmojiComponent,FileComponent
        data=animated()
        payload=dict(platform='qq',account_id='123',scope='fixture',message_id='sticker',
            message_info=dict(user_info=dict(user_id='456',user_nickname='fixture'),group_info=dict(group_id='789',group_name='fixture')),
            raw_message=[])
        message=U._build_session_message_from_dict(payload)
        for component in (EmojiComponent(binary_data=data,binary_hash='fixture'),
                          FileComponent(name='sticker.gif',base64_data=base64.b64encode(data).decode())):
            message.raw_message.components=[component]
            context=native_router_message(message)
            self.assertTrue(picture._is_image(context))
            self.assertEqual(media.load_visual_bytes(context),data)
        with patch.object(media,'download_media') as download,self.assertRaises(ValueError):
            media.load_visual_bytes({'platform':'qq','sessionId':'qq:fixture'})
        download.assert_not_called()

    def test_same_sender_next_sticker_processed_once_and_file_lives_through_send(self):
        for platform in ('wechat','qq'):
            session='g@chatroom' if platform=='wechat' else 'qq:123:fixture:group:789'
            base={'sessionId':session,'group':session,'wxid':'wxid_user','platform':platform,'content':''}
            data=animated();sent=[]
            def send(**kwargs):
                p=Path(kwargs['file_path']);self.assertTrue(p.exists());self.assertEqual(p.suffix,'.gif')
                with Image.open(p) as out:self.assertEqual(out.n_frames,3)
                sent.append(p);return True,''
            with patch.object(picture,'settings',return_value={'followup_ttl_seconds':60}), \
                 patch.object(picture,'SAVE_DIR',INSTANCE.name),patch.object(picture,'send',side_effect=send), \
                 patch.object(picture,'load_visual_bytes',wraps=media.load_visual_bytes) as load:
                picture.handle_auto({**base,'command':'/对称','args':'左右'})
                picture.handle_auto({**base,'wxid':'another','is_emoji':True,'_image_binary':data})
                self.assertFalse(sent);self.assertIsNotNone(followup.peek(session,wxid='wxid_user'))
                incoming={**base,'is_emoji':True,'_image_binary':data}
                picture.handle_auto(incoming);picture.handle_auto(incoming)
                self.assertEqual(len(sent),1);load.assert_called_once()
                self.assertFalse(sent[0].exists())

    def test_other_followup_is_not_consumed_and_text_cancels_symmetry(self):
        context={'sessionId':'test-followup','wxid':'user','content':'ordinary text'}
        followup.register('test-followup','another',wxid='user')
        picture.handle_auto(context);self.assertEqual(followup.peek('test-followup',wxid='user')['target'],'another')
        followup.register('test-followup','picture',wxid='user',payload={'mode':'left_right'})
        picture.handle_auto(context);self.assertIsNone(followup.peek('test-followup',wxid='user'))

    def test_streaming_download_limits_and_no_token_on_external_files(self):
        response=Mock(headers={},iter_content=Mock(return_value=[b'abc',b'def']))
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch.object(media,'TOKEN','fixture-secret'),patch.object(media.requests,'get',return_value=response) as get:
            with self.assertRaises(ValueError):media.download_media('https://example.invalid/image.gif',max_bytes=4)
            self.assertEqual(get.call_args.kwargs['headers'],{})

class NativeOutput(unittest.IsolatedAsyncioTestCase):
    async def test_qq_send_keeps_gif_bytes_in_native_emoji_component(self):
        from core.shell_platform_bridge import send_output
        from src.chat.message_receive.chat_manager import chat_manager
        from src.chat.heart_flow.heartflow_manager import heartflow_manager
        from src.common.data_models.message_component_data_model import EmojiComponent
        data=animated();path=codec.save_symmetry(data,'left_right',INSTANCE.name)
        context={'platform':'qq','account_id':'123','scope':'fixture','native_session_id':'test-native',
                 'sessionId':'qq:123:fixture:group:789','group':'qq:123:fixture:group:789','group_id':'789'}
        session=NS(platform='qq',account_id='123',scope='fixture',session_id='test-native')
        sender=AsyncMock(return_value=NS())
        with patch.object(chat_manager,'get_session_by_session_id',return_value=session), \
             patch.dict(heartflow_manager.heartflow_chat_list,{},clear=True), \
             patch('src.services.send_service.custom_reply_set_to_stream_with_message',sender):
            ok,error=await send_output(context,target=context['group'],file_path=path,mode='wechat_file')
        self.assertTrue(ok,error)
        component=sender.call_args.args[0].components[0]
        self.assertIsInstance(component,EmojiComponent)
        self.assertEqual(component.binary_data,Path(path).read_bytes())
        with Image.open(BytesIO(component.binary_data)) as out:self.assertEqual(out.n_frames,3)

if __name__=='__main__':unittest.main(verbosity=2)
