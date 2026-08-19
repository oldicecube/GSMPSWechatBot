import unittest
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.parse import urlparse

from services.music.models import ResolvedAudio, SongInfo
from services.music.resolver import (
    MusicConfigError,
    MusicResolutionError,
    MusicResolver,
)
from services.music.script_discovery import discover_scripts
from services.music.source import MusicSourceError
from services.music.sources import build_sources
from services.music.sources.exclusive import ExclusiveSource
from services.music.sources.flower import FlowerSource
from services.music.sources.grass import GrassSource
from services.music.sources.lx_daemon_client import LxDaemonError
from services.music.sources.lx_script import LxScriptSource, _build_platform_attempts
from services.music.sources.netease import NeteaseSource
from core.wechat_sender import file_down

STUB_SOURCE_ORDER = ("flower", "exclusive", "grass", "netease")


class StubSource:
    def __init__(self, source_id, result=None, error=None):
        self.source_id = source_id
        self.name = source_id
        self.result = result
        self.error = error
        self.calls = []

    def resolve(self, song, quality, timeout):
        self.calls.append((song, quality, timeout))
        if self.error:
            raise self.error
        return self.result


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class MusicResolverTests(unittest.TestCase):
    def setUp(self):
        self.song = SongInfo(
            song_id="123",
            name="晴天",
            artist="周杰伦",
            album="叶惠美",
            duration=269.0,
        )

    def test_legacy_config_uses_safe_default_chain(self):
        sources, order = build_sources({"script_dir": "music-source"})
        stub_sources = {
            source_id: StubSource(source_id)
            for source_id in order
        }
        resolver = MusicResolver.from_config(
            {},
            sources=stub_sources,
            source_order=order,
        )

        self.assertEqual(tuple(order), resolver.source_order)
        self.assertEqual(resolver.quality, "128k")
        self.assertTrue(resolver.allow_netease_fallback)

    def test_resolution_continues_after_source_failure(self):
        expected = ResolvedAudio(
            url="https://cdn.example.test/song.mp3",
            source_id="exclusive",
            source_name="独家音源",
        )
        sources = {
            "flower": StubSource(
                "flower",
                error=MusicSourceError("flower", "服务不可用"),
            ),
            "exclusive": StubSource("exclusive", result=expected),
            "grass": StubSource("grass"),
            "netease": StubSource("netease"),
        }
        resolver = MusicResolver.from_config(
            {"music": {"resolve_timeout_seconds": 7}},
            sources=sources,
            source_order=STUB_SOURCE_ORDER,
        )

        result = resolver.resolve(self.song)

        self.assertEqual(result, expected)
        self.assertEqual(sources["flower"].calls[0][1:], ("128k", 7.0))
        self.assertEqual(len(sources["grass"].calls), 0)
        self.assertEqual(len(sources["netease"].calls), 0)

    def test_netease_is_last_fallback(self):
        expected = ResolvedAudio(
            url="https://music.example.test/fallback.mp3",
            source_id="netease",
            source_name="网易云音乐",
        )
        sources = {
            source_id: StubSource(
                source_id,
                result=expected if source_id == "netease" else None,
                error=None if source_id == "netease" else MusicSourceError(
                    source_id,
                    "解析失败",
                ),
            )
            for source_id in STUB_SOURCE_ORDER
        }

        result = MusicResolver.from_config(
            {},
            sources=sources,
            source_order=STUB_SOURCE_ORDER,
        ).resolve(self.song)

        self.assertEqual(result, expected)
        self.assertEqual(len(sources["netease"].calls), 1)

    def test_invalid_source_order_is_rejected(self):
        with self.assertRaises(MusicConfigError):
            MusicResolver(
                source_order=["flower", "flower"],
                sources={"flower": StubSource("flower")},
            )

        with self.assertRaises(MusicConfigError):
            MusicResolver(
                source_order=["unknown"],
                sources={},
            )

    def test_invalid_quality_is_rejected(self):
        with self.assertRaises(MusicConfigError):
            MusicResolver(
                source_order=STUB_SOURCE_ORDER,
                quality="192k",
                sources={source_id: StubSource(source_id) for source_id in STUB_SOURCE_ORDER},
            )

    def test_resolution_error_contains_attempted_sources(self):
        sources = {
            source_id: StubSource(
                source_id,
                error=MusicSourceError(source_id, "解析失败"),
            )
            for source_id in STUB_SOURCE_ORDER
        }
        resolver = MusicResolver(
            source_order=STUB_SOURCE_ORDER,
            sources=sources,
        )

        with self.assertRaises(MusicResolutionError) as context:
            resolver.resolve(self.song)

        self.assertEqual(
            context.exception.attempted_sources,
            list(STUB_SOURCE_ORDER),
        )

    def test_resolve_candidates_keeps_download_fallbacks(self):
        candidates = [
            ResolvedAudio(
                url="https://flower.example.test/song.mp3",
                source_id="flower",
                source_name="野花",
            ),
            ResolvedAudio(
                url="https://netease.example.test/song.mp3",
                source_id="netease",
                source_name="网易云音乐",
            ),
        ]
        sources = {
            source_id: StubSource(source_id)
            for source_id in STUB_SOURCE_ORDER
        }
        sources["flower"].result = candidates[0]
        sources["netease"].result = candidates[1]

        result = MusicResolver(
            source_order=STUB_SOURCE_ORDER,
            sources=sources,
        ).resolve_candidates(self.song)

        self.assertEqual(result, candidates[:1] + [candidates[1]])

    def test_iter_candidates_resolves_lazily_in_priority_order(self):
        expected = ResolvedAudio(
            url="https://flower.example.test/song.mp3",
            source_id="flower",
            source_name="野花",
        )
        sources = {
            source_id: StubSource(source_id)
            for source_id in STUB_SOURCE_ORDER
        }
        sources["flower"].result = expected
        resolver = MusicResolver(
            source_order=STUB_SOURCE_ORDER,
            sources=sources,
        )

        first = next(resolver.iter_candidates(self.song))

        self.assertEqual(first, expected)
        self.assertEqual(len(sources["exclusive"].calls), 0)
        self.assertEqual(len(sources["grass"].calls), 0)
        self.assertEqual(len(sources["netease"].calls), 0)


class MusicAdapterTests(unittest.TestCase):
    def setUp(self):
        self.song = SongInfo(
            song_id="123",
            name="晴天",
            artist="周杰伦",
            album="叶惠美",
            duration=269.0,
        )

    def test_flower_builds_lx_request_and_returns_data_url(self):
        requests = []

        def request_get(url, headers, timeout):
            requests.append((url, headers, timeout))
            return FakeResponse({"code": 0, "data": "https://cdn.example.test/flower.mp3"})

        result = FlowerSource(
            base_url="https://flower.example.test/flower/v1",
            request_get=request_get,
        ).resolve(self.song, "128k", timeout=7)

        self.assertEqual(result.url, "https://cdn.example.test/flower.mp3")
        self.assertEqual(result.source_id, "flower")
        self.assertEqual(urlparse(requests[0][0]).path, "/flower/v1/url/wy/123/128k")
        self.assertEqual(requests[0][1]["User-Agent"], "lx-music/desktop")
        self.assertEqual(requests[0][1]["ver"], "2.0.0")
        self.assertEqual(requests[0][1]["source-ver"], "1")
        self.assertTrue(requests[0][1]["tag"])
        self.assertEqual(requests[0][2], 7)

    def test_grass_rejects_nonzero_source_code(self):
        def request_get(url, headers, timeout):
            return FakeResponse({"code": 1, "msg": "not found"})

        with self.assertRaises(MusicSourceError) as context:
            GrassSource(
                base_url="https://grass.example.test/grass/v1",
                request_get=request_get,
            ).resolve(self.song, "128k", timeout=5)

        self.assertIn("not found", context.exception.reason)

    def test_exclusive_uses_configured_request_key_and_songmid(self):
        requests = []

        def request_get(url, headers, timeout):
            requests.append((url, headers, timeout))
            return FakeResponse({"code": 0, "data": "https://cdn.example.test/exclusive.mp3"})

        result = ExclusiveSource(
            base_url="https://exclusive.example.test/lxmusicv4",
            api_key="test-key",
            request_get=request_get,
        ).resolve(self.song, "320k", timeout=9)

        self.assertEqual(result.url, "https://cdn.example.test/exclusive.mp3")
        self.assertIn("/url/wy/123/320k", requests[0][0])
        self.assertEqual(requests[0][1]["X-Request-Key"], "test-key")
        self.assertEqual(requests[0][1]["follow_max"], "5")
        self.assertEqual(requests[0][2], 9)

    def test_build_sources_uses_configured_endpoints(self):
        sources, _order = build_sources(
            {
                "mode": "python",
                "sources": {
                    "flower": {"base_url": "https://flower.test"},
                    "exclusive": {
                        "base_url": "https://exclusive.test",
                        "api_key": "key",
                    },
                    "grass": {"base_url": "https://grass.test"},
                },
            }
        )

        self.assertIsInstance(sources["flower"], FlowerSource)
        self.assertIsInstance(sources["exclusive"], ExclusiveSource)
        self.assertIsInstance(sources["grass"], GrassSource)

    def test_netease_search_normalizes_first_song(self):
        def request_get(url, params, headers, timeout):
            self.assertIn("music.163.com", url)
            self.assertEqual(params["limit"], 1)
            return FakeResponse(
                {
                    "result": {
                        "songs": [
                            {
                                "id": 123,
                                "name": "晴天",
                                "duration": 269000,
                                "artists": [{"name": "周杰伦"}],
                                "album": {"name": "叶惠美"},
                            }
                        ]
                    }
                }
            )

        song = NeteaseSource(request_get=request_get).search("晴天", timeout=11)

        self.assertEqual(song.song_id, "123")
        self.assertEqual(song.artist, "周杰伦")
        self.assertEqual(song.duration, 269.0)

    def test_netease_resolve_uses_redirect_location(self):
        requests = []

        class RedirectResponse:
            headers = {"Location": "https://cdn.example.test/netease.mp3"}

            def raise_for_status(self):
                return None

        def request_get(url, headers, allow_redirects, timeout):
            requests.append((url, headers, allow_redirects, timeout))
            return RedirectResponse()

        result = NeteaseSource(request_get=request_get).resolve(
            self.song,
            "128k",
            timeout=13,
        )

        self.assertEqual(result.url, "https://cdn.example.test/netease.mp3")
        self.assertEqual(result.download_headers["Referer"], "https://music.163.com/")
        self.assertFalse(requests[0][2])
        self.assertEqual(requests[0][3], 13)

    def test_netease_missing_redirect_location_is_rejected(self):
        class EmptyRedirectResponse:
            headers = {}

            def raise_for_status(self):
                return None

        def request_get(url, headers, allow_redirects, timeout):
            return EmptyRedirectResponse()

        with self.assertRaises(MusicSourceError):
            NeteaseSource(request_get=request_get).resolve(
                self.song,
                "128k",
                timeout=5,
            )

    def test_netease_invalid_redirect_location_is_rejected(self):
        class InvalidRedirectResponse:
            headers = {"Location": "file:///tmp/song.mp3"}

            def raise_for_status(self):
                return None

        def request_get(url, headers, allow_redirects, timeout):
            return InvalidRedirectResponse()

        with self.assertRaises(MusicSourceError):
            NeteaseSource(request_get=request_get).resolve(
                self.song,
                "128k",
                timeout=5,
            )

    def test_netease_404_redirect_location_is_rejected(self):
        class NotFoundRedirectResponse:
            headers = {"Location": "http://music.163.com/404"}

            def raise_for_status(self):
                return None

        def request_get(url, headers, allow_redirects, timeout):
            return NotFoundRedirectResponse()

        with self.assertRaises(MusicSourceError) as ctx:
            NeteaseSource(request_get=request_get).resolve(
                self.song,
                "128k",
                timeout=5,
            )
        self.assertIn("无可用音源", str(ctx.exception.reason))


class LxScriptSourceTests(unittest.TestCase):
    def setUp(self):
        self.song = SongInfo(
            song_id="123",
            name="晴天",
            artist="周杰伦",
            album="叶惠美",
            duration=269.0,
        )

    def test_build_sources_defaults_to_lx_scripts(self):
        sources, order = build_sources({"script_dir": "music-source"})
        lx_sources = [item for item in sources.values() if isinstance(item, LxScriptSource)]
        self.assertGreaterEqual(len(lx_sources), 1)
        self.assertIsInstance(sources["netease"], NeteaseSource)
        self.assertIn("netease", order)

    def test_discover_scripts_reads_music_source_directory(self):
        discovered = discover_scripts("music-source")
        names = {item.script_path.name for item in discovered}
        self.assertIn("野花音源.js", names)
        self.assertIn("野草音源.js", names)

    def test_sort_discovered_uses_source_order_file(self):
        import tempfile

        from services.music.script_discovery import (
            DiscoveredScript,
            sort_discovered,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = Path(temp_dir)
            (script_dir / "source_order.txt").write_text(
                "b.js\na.js\n",
                encoding="utf-8",
            )
            scripts = [
                DiscoveredScript("a", script_dir / "a.js", "A"),
                DiscoveredScript("b", script_dir / "b.js", "B"),
            ]
            ordered = sort_discovered(scripts, script_dir)
            self.assertEqual([item.source_id for item in ordered], ["b", "a"])

    def test_build_platform_attempts_respects_supported_platforms(self):
        attempts = _build_platform_attempts(
            ("wy", "kg", "kw"),
            ["wy", "kg"],
            "wy",
        )
        self.assertEqual(attempts, ["wy", "kg"])

    def test_build_sources_python_mode_when_base_url_configured(self):
        sources, _order = build_sources(
            {
                "mode": "python",
                "sources": {
                    "flower": {"base_url": "https://flower.example.test/v1"},
                },
            }
        )
        self.assertIsInstance(sources["flower"], FlowerSource)

    def test_lx_script_source_uses_daemon_pool(self):
        class FakeDaemon:
            def __init__(self):
                self.calls = []

            def get_supported_platforms(self):
                return ["wy", "kg"]

            def music_url(self, platform, quality, music_info, timeout):
                self.calls.append((platform, quality, music_info, timeout))
                if platform == "wy":
                    raise LxDaemonError("flower", "wy failed")
                return "https://cdn.example.test/lx.mp3"

        class FakePool:
            def __init__(self):
                self.daemon = FakeDaemon()

            def get_daemon(self, source_id, script_path):
                return self.daemon

        pool = FakePool()
        source = LxScriptSource(
            "flower",
            Path(__file__).resolve(),
            name="野花",
            daemon_pool=pool,
            platform_order=("wy", "kg"),
        )
        result = source.resolve(self.song, "128k", timeout=10)
        self.assertEqual(result.url, "https://cdn.example.test/lx.mp3")
        self.assertEqual([call[0] for call in pool.daemon.calls], ["wy", "kg"])

    def test_lx_script_source_raises_on_daemon_error(self):
        class FakeDaemon:
            def get_supported_platforms(self):
                return ["wy"]

            def music_url(self, platform, quality, music_info, timeout):
                raise LxDaemonError("flower", "not found")

        class FakePool:
            def get_daemon(self, source_id, script_path):
                return FakeDaemon()

        source = LxScriptSource(
            "flower",
            Path(__file__).resolve(),
            name="野花",
            daemon_pool=FakePool(),
            platform_order=("wy",),
        )
        with self.assertRaises(MusicSourceError):
            source.resolve(self.song, "128k", timeout=5)

    def test_song_info_to_lx_music_info_shape(self):
        info = self.song.to_lx_music_info()
        self.assertEqual(info["id"], "123")
        self.assertEqual(info["meta"]["songId"], "123")
        self.assertEqual(info["interval"], "04:29")


class DownloadAndPluginTests(unittest.TestCase):
    def test_download_voice_file_accepts_source_headers_and_timeout(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.side_effect = [b"audio", b""]
        response.status = 200
        response.headers = {
            "Content-Type": "audio/mpeg",
            "Content-Length": "5",
        }
        response.geturl.return_value = "https://cdn.example.test/song.mp3"

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(file_down, "VOICE_TEMP_DIR", temp_dir):
                with patch.object(
                    file_down.urllib.request,
                    "urlopen",
                    return_value=response,
                ) as urlopen, patch.object(
                    file_down,
                    "validate_audio_file",
                    return_value={"frames": 10},
                ):
                    with self.assertLogs(file_down.logger, level="INFO") as logs:
                        path = file_down.download_voice_file(
                            "https://cdn.example.test/song.mp3",
                            prefix="song_123",
                            headers={"Referer": "https://source.example.test/"},
                            timeout=4,
                            source_id="test",
                        )
                    contents = Path(path).read_bytes()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Referer"), "https://source.example.test/")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 4)
        self.assertEqual(contents, b"audio")
        self.assertTrue(any("source=test" in message for message in logs.output))

    def test_invalid_audio_is_removed_and_raises_download_error(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.side_effect = [b"not audio", b""]
        response.status = 200
        response.headers = {"Content-Type": "audio/mpeg"}
        response.geturl.return_value = "https://cdn.example.test/bad.mp3"

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(file_down, "VOICE_TEMP_DIR", temp_dir):
                with patch.object(
                    file_down.urllib.request,
                    "urlopen",
                    return_value=response,
                ), patch.object(
                    file_down,
                    "validate_audio_file",
                    side_effect=ValueError("invalid audio"),
                ):
                    with self.assertRaises(ValueError):
                        file_down.download_voice_file(
                            "https://cdn.example.test/bad.mp3",
                            source_id="flower",
                        )

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_validate_audio_file_rejects_non_audio_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.mp3"
            path.write_bytes(b"not an audio stream")

            with self.assertRaises(ValueError):
                file_down.validate_audio_file(path)

    def test_song_plugin_retries_download_with_next_source(self):
        fake_mcrcon = types.ModuleType("mcrcon")
        fake_mcrcon.MCRcon = object
        with patch.dict(sys.modules, {"mcrcon": fake_mcrcon}):
            import plugins.song as song_plugin

        song = SongInfo(
            song_id="123",
            name="晴天",
            artist="周杰伦",
            album="叶惠美",
            duration=269.0,
        )
        candidates = [
            ResolvedAudio(
                url="https://flower.example.test/song.mp3",
                source_id="flower",
                source_name="野花",
                download_headers={"X-Source": "flower"},
            ),
            ResolvedAudio(
                url="https://netease.example.test/song.mp3",
                source_id="netease",
                source_name="网易云音乐",
                download_headers={"Referer": "https://music.163.com/"},
            ),
        ]
        resolver = Mock(
            resolve_timeout_seconds=13,
            download_timeout_seconds=41,
        )
        resolver.iter_candidates.return_value = iter(candidates)
        netease = Mock()
        netease.search.return_value = song

        with patch.object(song_plugin, "_music_resolver", resolver), patch.object(
            song_plugin, "_netease_source", netease
        ), patch.object(
            song_plugin,
            "download_voice_file",
            side_effect=[RuntimeError("first source failed"), "voice.mp3"],
        ) as download, patch.object(
            song_plugin,
            "send",
            return_value=(True, None),
        ) as send:
            result = song_plugin.handle(
                "晴天\n30 40",
                {"group": "测试群"},
            )

        self.assertIsNone(result)
        self.assertEqual(
            download.call_args_list,
            [
                call(
                    "https://flower.example.test/song.mp3",
                    prefix="song_123",
                    headers={"X-Source": "flower"},
                    timeout=41,
                    source_id="flower",
                ),
                call(
                    "https://netease.example.test/song.mp3",
                    prefix="song_123",
                    headers={"Referer": "https://music.163.com/"},
                    timeout=41,
                    source_id="netease",
                ),
            ],
        )
        send.assert_called_once_with(
            target="测试群",
            file_path="voice.mp3",
            mode="wechat_voice",
            duration=10.0,
            voice_start=30.0,
        )

    def test_song_plugin_reports_music_config_error_separately(self):
        fake_mcrcon = types.ModuleType("mcrcon")
        fake_mcrcon.MCRcon = object
        with patch.dict(sys.modules, {"mcrcon": fake_mcrcon}):
            import plugins.song as song_plugin

        with patch.object(song_plugin, "_music_resolver", None), patch.object(
            song_plugin, "_netease_source", None
        ), patch.object(
            song_plugin,
            "_music_init_error",
            MusicConfigError("source_order 无效"),
        ):
            result = song_plugin.handle("晴天", {"group": "测试群"})

        self.assertEqual(result, "音乐源配置错误：source_order 无效")


if __name__ == "__main__":
    unittest.main()
