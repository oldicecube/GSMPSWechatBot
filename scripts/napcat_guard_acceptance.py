"""Persistent NapCat login guard tests. No client launch, popup or login."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import napcat_login_watch as guard


class _Proc:
    def __init__(self):
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


class Guard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='napcat-guard-', dir=ROOT / 'temp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cfg = self.root / 'depends-data/napcat/config'
        cfg.mkdir(parents=True)
        image = self.root / 'depends-data/napcat/cache/qrcode.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'qr')
        self.client = Mock()
        self.client.credential = ''
        self.clock = 0

    def _now(self):
        return self.clock

    def _sleep(self, seconds):
        self.clock += seconds

    def test_guard_keeps_running_after_login_and_reopens_qr_on_relogin(self):
        states = [{'isLogin': True}, {'isLogin': True}, {'isLogin': False, 'loginPhase': 'waiting_qrcode'},
                  {'isLogin': False, 'loginPhase': 'waiting_qrcode'}, {'isLogin': True}]
        self.client.status.side_effect = lambda: states.pop(0) if states else {'isLogin': True}
        opened = []

        def _open(proc, image, uin, popen):
            opened.append(self.clock)
            return _Proc()

        with patch.object(guard, '_open_window', side_effect=_open), \
             patch.object(guard, '_close_window'), \
             patch.object(guard, '_webui_alive', return_value=True):
            result = guard.qr_window_loop(self.client, seconds=18, interval=3, now=self._now,
                                          sleep=self._sleep, popen=lambda *a, **k: _Proc(),
                                          root=self.root, port=6099)
        self.assertTrue(result['online'])
        self.assertEqual(result['qr_windows'], 1)
        events = [json.loads(line) for line in
                  (self.root / 'depends-data/napcat/config/login-events.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertEqual([item.get('online') for item in events], [True, False, True])

    def test_guard_relaunches_dead_client_after_grace_period(self):
        self.client.status.side_effect = RuntimeError('down')
        relaunched = []

        def _popen(args, **kwargs):
            relaunched.append(args)
            return _Proc()

        with patch.object(guard, '_webui_alive', return_value=False), \
             patch.object(guard, '_open_window', return_value=None), \
             patch.object(guard, '_close_window'):
            guard.qr_window_loop(self.client, seconds=30, interval=10, now=self._now,
                                 sleep=self._sleep, popen=_popen, root=self.root, port=6099)
        self.assertEqual(len(relaunched), 1)
        self.assertIn('/c', relaunched[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)

