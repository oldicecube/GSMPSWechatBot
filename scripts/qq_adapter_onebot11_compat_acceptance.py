"""OneBot 11 兼容画像回归：任意标准实现都应能连上，而不是只认 napcat/snowluma。"""
import asyncio
import importlib
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_pkg = ModuleType("qq_adapter_compat")
_pkg.__path__ = [str(ROOT / "plugins/MaiBot-SnowLuma-Adapter")]
sys.modules[_pkg.__name__] = _pkg


def load(name):
    return importlib.import_module(_pkg.__name__ + "." + name)


profile = load("profile")
config = load("config")


class OneBot11Compat(unittest.TestCase):
    def test_named_implementations_keep_their_own_profile(self):
        self.assertEqual(profile.detect_client_type_from_app_name("NapCat"), "napcat")
        self.assertEqual(profile.detect_client_type_from_app_name("napcat-v4"), "napcat")
        self.assertEqual(profile.detect_client_type_from_app_name("SnowLuma"), "snowluma")

    def test_other_implementations_fall_back_to_generic_onebot11(self):
        for app_name in ("NapukettoQQ", "LLBot", "LuckyLillia", "Lagrange", "UnknownImpl", "  OneBot11 "):
            with self.subTest(app_name=app_name):
                self.assertEqual(profile.detect_client_type_from_app_name(app_name), "onebot11")
        self.assertIsNone(profile.detect_client_type_from_app_name(""))

    def test_generic_profile_uses_standard_targeted_actions(self):
        generic = profile.PROFILE_BY_TYPE["onebot11"]
        self.assertIs(generic, profile.ONEBOT11_PROFILE)
        self.assertEqual(generic.msg_action_style, profile.MSG_ACTION_STYLE_TARGETED)
        self.assertFalse(generic.supports_qzone)
        self.assertFalse(generic.transcode_silk_voice)
        self.assertEqual(generic.forward_param_key, profile.FORWARD_PARAM_KEY_MESSAGE)
        self.assertEqual(set(profile.PROFILE_BY_TYPE), {"napcat", "snowluma", "onebot11"})

    def test_probe_accepts_new_implementations(self):
        async def call(action, params):
            self.assertEqual(action, profile.PROBE_ACTION)
            return {"app_name": "NapukettoQQ", "app_version": "2.1.0"}

        result = asyncio.run(profile.probe_client_profile(call, _logger()))
        self.assertEqual(result, ("onebot11", "NapukettoQQ", "2.1.0"))

    def test_probe_without_app_name_still_connects(self):
        async def call(action, params):
            return {}

        result = asyncio.run(profile.probe_client_profile(call, _logger()))
        self.assertEqual(result[0], "onebot11")
        self.assertEqual(result[1], "")

    def test_probe_still_fails_when_the_peer_does_not_answer(self):
        async def call(action, params):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            asyncio.run(profile.probe_client_profile(call, _logger()))

    def test_config_accepts_explicit_onebot11(self):
        server = config.QQServerConfig(client_type="onebot11")
        self.assertEqual(server.client_type, "onebot11")
        self.assertIs(profile.PROFILE_BY_TYPE[server.client_type], profile.ONEBOT11_PROFILE)
        self.assertEqual(config.QQServerConfig(client_type="AUTO").client_type, "auto")
        self.assertEqual(config.QQServerConfig(client_type="bogus").client_type, "auto")


class _Logger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


def _logger():
    return _Logger()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(OneBot11Compat))
    report = dict(passed=result.wasSuccessful(), tests=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors),
                  qq_login=False, physical_messages=False)
    destination = ROOT / "docs/maibot-wechat-acceptance/qq-adapter-onebot11-compat-20261118.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
