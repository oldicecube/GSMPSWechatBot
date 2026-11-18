"""/anima gi2（远端 GPT Image 网关）离线契约测试：绝不发真实生图请求。"""
import base64
import io
from unittest.mock import Mock

import pytest
from PIL import Image

from anima import gpt_image
from anima.commands import GPT_IMAGE_MODES, parse_request
from anima.gpt_image import GPTImageClient, GPTImageError


def png_bytes(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(out, 'PNG')
    return out.getvalue()


def fake_response(status=200, payload=None, content=b'', text=''):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else {}
    response.content = content
    response.text = text
    response.raise_for_status = Mock()
    return response


def client(tmp_path, **kwargs):
    kwargs.setdefault('base', 'https://gw.example.com')
    kwargs.setdefault('key', 'test-key')
    kwargs.setdefault('output_dir', tmp_path)
    return GPTImageClient(**kwargs)


def test_api_base_gets_v1_suffix(tmp_path):
    assert client(tmp_path).api_base == 'https://gw.example.com/v1'
    assert client(tmp_path, base='https://gw.example.com/v1/').api_base == 'https://gw.example.com/v1'


def test_check_requires_key(tmp_path):
    ready, reason = client(tmp_path, key='').check()
    assert ready is False and 'Key' in reason


def test_check_reports_gateway_status(tmp_path, monkeypatch):
    monkeypatch.setattr(gpt_image.requests, 'get', lambda *a, **k: fake_response(200))
    assert client(tmp_path).check() == (True, '')
    monkeypatch.setattr(gpt_image.requests, 'get', lambda *a, **k: fake_response(401))
    ready, reason = client(tmp_path).check()
    assert ready is False and '401' in reason


def test_generate_saves_b64_payload(tmp_path, monkeypatch):
    raw = png_bytes()
    payload = {'data': [{'b64_json': base64.b64encode(raw).decode(), 'mime_type': 'image/png'}]}
    monkeypatch.setattr(gpt_image.requests, 'post', lambda *a, **k: fake_response(200, payload))
    path = client(tmp_path).generate('一只猫')
    assert path.suffix == '.png' and path.read_bytes() == raw


def test_generate_accepts_data_url_prefix(tmp_path, monkeypatch):
    raw = png_bytes()
    encoded = 'data:image/png;base64,' + base64.b64encode(raw).decode()
    monkeypatch.setattr(
        gpt_image.requests, 'post',
        lambda *a, **k: fake_response(200, {'data': [{'b64_json': encoded}]}),
    )
    assert client(tmp_path).generate('cat').read_bytes() == raw


def test_generate_uses_edits_endpoint_with_reference(tmp_path, monkeypatch):
    calls = {}

    def fake_post(url, **kwargs):
        calls['url'] = url
        calls['files'] = kwargs.get('files')
        calls['data'] = kwargs.get('data')
        raw = png_bytes('blue')
        return fake_response(200, {'data': [{'b64_json': base64.b64encode(raw).decode()}]})

    monkeypatch.setattr(gpt_image.requests, 'post', fake_post)
    path = client(tmp_path).generate('改成蓝色', reference_png=png_bytes())
    assert calls['url'].endswith('/images/edits')
    assert set(calls['files']) == {'image'}
    assert calls['data']['model'] == 'gpt-image-2.6'
    assert path.read_bytes() == png_bytes('blue')


def test_generate_uses_generations_endpoint_without_reference(tmp_path, monkeypatch):
    urls = []

    def fake_post(url, **kwargs):
        urls.append(url)
        return fake_response(200, {'data': [{'b64_json': base64.b64encode(png_bytes()).decode()}]})

    monkeypatch.setattr(gpt_image.requests, 'post', fake_post)
    client(tmp_path).generate('cat')
    assert urls == ['https://gw.example.com/v1/images/generations']


def test_generate_downloads_url_payload(tmp_path, monkeypatch):
    raw = png_bytes('green')
    monkeypatch.setattr(
        gpt_image.requests, 'post',
        lambda *a, **k: fake_response(200, {'data': [{'url': 'https://cdn.example.com/a.png', 'mime_type': 'image/png'}]}),
    )
    monkeypatch.setattr(gpt_image.requests, 'get', lambda url, **k: fake_response(200, content=raw))
    assert client(tmp_path).generate('cat').read_bytes() == raw


def test_generate_surfaces_gateway_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gpt_image.requests, 'post',
        lambda *a, **k: fake_response(429, {'error': {'message': 'quota exceeded'}}, text='quota'),
    )
    with pytest.raises(GPTImageError) as excinfo:
        client(tmp_path).generate('cat')
    assert 'HTTP 429' in str(excinfo.value) and 'quota exceeded' in str(excinfo.value)


def test_generate_rejects_empty_data(tmp_path, monkeypatch):
    monkeypatch.setattr(gpt_image.requests, 'post', lambda *a, **k: fake_response(200, {'data': []}))
    with pytest.raises(GPTImageError):
        client(tmp_path).generate('cat')


def test_generate_rejects_oversized_image(tmp_path, monkeypatch):
    monkeypatch.setattr(gpt_image, 'MAX_IMAGE_BYTES', 8)
    monkeypatch.setattr(
        gpt_image.requests, 'post',
        lambda *a, **k: fake_response(200, {'data': [{'b64_json': base64.b64encode(png_bytes()).decode()}]}),
    )
    with pytest.raises(GPTImageError, match='过大'):
        client(tmp_path).generate('cat')


def test_generate_rejects_blank_prompt(tmp_path):
    with pytest.raises(GPTImageError):
        client(tmp_path).generate('   ')


@pytest.mark.parametrize(
    'text,mode',
    [
        ('一只猫\ngi2', 'gi2'),
        ('一只猫\ngptimage', 'gi2'),
        ('一只猫\n/gpt-image', 'gi2'),
        ('一只猫\n图生图 gi2', 'gi2_img2img'),
        ('一只猫\ngi2 图生图', 'gi2_img2img'),
        ('一只猫\ngi2img', 'gi2_img2img'),
    ],
)
def test_gi2_selectors_parse(text, mode):
    assert parse_request(text).mode == mode
    assert mode in GPT_IMAGE_MODES


class FakeService:
    def __init__(self, cache):
        self.output_cache = cache
        self.deleted = []
        self.done = False
        self.anima_config = {}

    def configure_connection(self, url, generation_timeout=None):
        pass

    def can_start_generate(self, uid):
        return True, ''

    def mark_generating(self, uid):
        return True

    def mark_done(self):
        self.done = True

    def delete_image(self, path):
        self.deleted.append(path)
        return True

    def check_connection(self):
        raise AssertionError('gi2 不应探测 ComfyUI')

    def preflight(self, spec):
        raise AssertionError('gi2 不应做 ComfyUI 预检')


class FakeClient:
    model = 'gpt-image-2.6'

    def __init__(self, target):
        self.target = target

    def check(self):
        return True, ''

    def generate(self, prompt, reference_png=None):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(png_bytes())
        return self.target


ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]


def _load_anima_plugin():
    """按文件路径加载仓库根的 plugins/anima.py（pytest 里 plugins 会解析到 src/plugins）。"""

    import importlib.util

    spec = importlib.util.spec_from_file_location('anima_plugin_under_test', ROOT / 'plugins' / 'anima.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root_core_shim(monkeypatch):
    """让 plugins.anima 的 core.* 导入指向仓库根 core/（与 main.py 运行时一致）。

    pytest 的 pythonpath 含 src，src/core 是常规包，会抢在根目录命名空间包之前，
    导致 core.shell_plugin_config 找不到。
    """

    import sys
    import types

    package = types.ModuleType('core')
    package.__path__ = [str(ROOT / 'core')]
    monkeypatch.setitem(sys.modules, 'core', package)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    _root_core_shim(monkeypatch)
    import core.shell_plugin_config as shell_config
    import utils.account_identity as identity
    import utils.points_manager as points
    module = _load_anima_plugin()
    service = FakeService(tmp_path / 'cache')
    monkeypatch.setattr(module, '_anima_service', service)
    monkeypatch.setattr(module, 'get_points', lambda uid: 100.0)
    monkeypatch.setattr(module, 'try_spend_points', lambda uid, cost: (True, 92.0))
    monkeypatch.setattr(points, 'binding_required', lambda ctx: None)
    monkeypatch.setattr(identity, 'canonical_user', lambda uid: uid)
    monkeypatch.setattr(
        shell_config, 'settings',
        lambda plugin_id: {
            'comfy_url': 'http://127.0.0.1:8188', 'generation_timeout': 180,
            'output_dir': None, 'reference_wait_seconds': 120,
            'gi2_api_base': 'https://gw.example.com/v1', 'gi2_api_key': 'k',
            'gi2_model': 'gpt-image-2.6', 'gi2_size': '1024x1024', 'gi2_timeout': 300,
        },
    )
    delivered = []
    target = tmp_path / 'cache' / 'gi2_fixture.png'
    monkeypatch.setattr(
        module, 'build_gpt_image_client', lambda current: FakeClient(target)
    )
    monkeypatch.setattr(
        module, 'send',
        lambda group, text=None, **kwargs: (delivered.append((group, text, kwargs)), (True, 'msg'))[1],
    )
    return module, service, delivered, target


CONTEXT = {'wxid': 'wxid_test', 'user': '测试用户', 'group': '测试群', 'sessionId': 's1'}


def test_execute_request_routes_gi2_to_gateway(plugin):
    module, service, delivered, target = plugin
    reply = module.execute_request('一只猫', CONTEXT, spec=parse_request('一只猫\ngi2'))
    assert '生成完成' in reply and 'GPT Image' in reply
    assert delivered[0][1].endswith('处理中...')
    assert delivered[1][2]['file_path'] == target
    assert service.deleted == [target]
    assert service.done is True


def test_execute_request_requires_reference_for_gi2_img2img(plugin):
    module, service, delivered, _ = plugin
    reply = module.execute_request('一只猫', CONTEXT, spec=parse_request('一只猫\ngi2 图生图'))
    assert '参考图片' in reply and '未扣积分' in reply
    assert service.deleted == []


def test_execute_request_passes_reference_bytes_to_gateway(plugin, monkeypatch):
    module, service, delivered, target = plugin
    seen = {}

    class Recorder(FakeClient):
        def generate(self, prompt, reference_png=None):
            seen['reference'] = reference_png
            return super().generate(prompt, reference_png)

    monkeypatch.setattr(module, 'build_gpt_image_client', lambda current: Recorder(target))
    reference = {'png': b'ref-bytes', 'width': 8, 'height': 8}
    reply = module.execute_request('一只猫', CONTEXT, spec=parse_request('一只猫\ngi2 图生图'), reference=reference)
    assert '生成完成' in reply
    assert seen['reference'] == b'ref-bytes'


def test_handle_reserves_gi2_img2img_without_comfyui(plugin, monkeypatch):
    module, service, delivered, _ = plugin
    import core.followup as followup

    registered = {}
    monkeypatch.setattr(
        followup, 'register',
        lambda session, target, **kw: registered.update({'session': session, 'target': target, **kw}),
    )
    monkeypatch.setattr(followup, 'clear', lambda *a, **k: None)

    reply = module.handle('一只猫\n图生图 gi2', CONTEXT)
    assert '请在' in reply and '参考图片' in reply
    assert registered['target'] == 'anima_media'
    assert registered['payload']['request']['mode'] == 'gi2_img2img'
