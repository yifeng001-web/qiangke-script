from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import ctypes
import json
import threading
import unittest

import numpy as np
from signup_helper import read_config, ensure_config, EXAMPLE_CONFIG, AttemptStore, Runner, match_template
from smart_ui import SmartUI, Target
import native_input as native


class SignupTest(unittest.TestCase):
    def test_local_config_creation_preserves_existing_values(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            ensure_config(path, EXAMPLE_CONFIG)
            self.assertEqual(read_config(path)['AccessToken'], '')
            cfg = read_config(path)
            cfg['AccessToken'] = 'local-only-test-value'
            path.write_text(json.dumps(cfg), encoding='utf-8')
            ensure_config(path, EXAMPLE_CONFIG)
            self.assertEqual(read_config(path)['AccessToken'], 'local-only-test-value')

    def test_config_validation_and_order(self):
        cfg = read_config(EXAMPLE_CONFIG)
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'config.json'
            for mutate in (lambda c:c.update(页面标题包含=''), lambda c:c.update(页面检测间隔毫秒=0),
                           lambda c:c['步骤'].append(deepcopy(c['步骤'][0])),
                           lambda c:c.update(允许二维码域名=['https://bad/'])):
                bad = deepcopy(cfg)
                mutate(bad)
                p.write_text(json.dumps(bad), encoding='utf8')
                with self.assertRaises(ValueError):
                    read_config(p)

    def test_persistent_dedup_share_params(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'attempts.json'
            self.assertTrue(AttemptStore(p).claim('https://m.baominggongju.com/?eid=123&_referer=one'))
            self.assertFalse(AttemptStore(p).claim('http://m.baominggongju.com/?eid=123&_referer=two'))
            self.assertTrue(AttemptStore(p).claim('https://m.baominggongju.com/?eid=456'))
            self.assertNotIn('baominggongju', p.read_text())
            p.write_text('broken')
            with self.assertRaises(ValueError):
                AttemptStore(p)

    def test_unicode_input_buffer_including_surrogate_pair(self):
        text = '张三0123😀'
        events = native.compile_text(text)
        self.assertEqual(ctypes.sizeof(native.Input), 40)
        codes = [int.from_bytes(text.encode('utf-16-le')[n:n+2], 'little') for n in range(0,len(text.encode('utf-16-le')),2)]
        self.assertEqual([e.ki.scan for e in events[4::2]], codes)
        self.assertTrue(all(e.ki.flags == 6 for e in events[5::2]))

    def test_visual_relocation_and_duplicate_refusal(self):
        template = np.random.default_rng(1).integers(0, 256, (30, 50), dtype=np.uint8)
        frame = np.zeros((250, 300), np.uint8)
        frame[80:110, 130:180] = template
        self.assertEqual(match_template(frame, template, .92), (130, 80))
        frame[170:200, 200:250] = template
        with self.assertRaises(RuntimeError):
            match_template(frame, template, .92)

    def test_prepared_button_waits_enabled_and_refreshes_position(self):
        cfg = read_config(EXAMPLE_CONFIG)
        with TemporaryDirectory() as folder:
            runner = Runner(cfg, threading.Event(), lambda _:None, folder)
            control = object()
            prepared = Target((10,10), 123, control, False)
            class UI:
                def __init__(self):
                    self.calls = 0
                def refresh(self, target):
                    self.calls += 1
                    return Target((900,700),123,control,self.calls > 2)
            runner.ui = UI()
            runner.check = lambda:None
            runner.wait = lambda _:None
            self.assertEqual(runner.wait_target(cfg['步骤'][-1], prepared).point, (900,700))
            self.assertGreaterEqual(runner.ui.calls, 3)

    def test_stop_before_input(self):
        cfg = read_config(EXAMPLE_CONFIG)
        stop = threading.Event()
        stop.set()
        with TemporaryDirectory() as folder, patch.object(native,'click') as click:
            runner = Runner(cfg, stop, lambda _:None, folder)
            from signup_helper import Stopped
            with self.assertRaises(Stopped):
                runner.operate(cfg['步骤'][0])
            click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
