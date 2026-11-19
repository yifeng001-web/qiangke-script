"""本地 HTTP 服务复现 Astrwechat 接口，不连接真实微信。"""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import io
import json
import threading
import time
import unittest

import cv2
import numpy as np
from PIL import Image
from astr_client import AstrClient, ApiError, CST, parse_sse, notification_time, allowed_url, exact_message


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.events = []
        self.requests = []
        self.media_reads = 0
        self.media_url = '/api/v1/media/image/test.png'
        self.url = 'https://m.baominggongju.com/?eid=test-activity'
        qr = cv2.QRCodeEncoder_create().encode(self.url)
        qr = np.pad(qr, 4, constant_values=255)
        out = io.BytesIO()
        Image.fromarray(qr).resize((400, 400), Image.Resampling.NEAREST).save(out, format='PNG')
        self.png = out.getvalue()
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                p = urlsplit(self.path)
                parent.requests.append((p.path, self.headers.get('Authorization'), self.headers.get('Last-Event-ID')))
                if self.headers.get('Authorization') != 'Bearer local-test-token':
                    self.send_response(401)
                    self.end_headers()
                    return
                if p.path == '/api/v1/push/messages':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.end_headers()
                    body = 'event: ready\ndata: {"success":true}\n\n: ping\n\n'
                    for number, event in enumerate(parent.events, 1):
                        body += f'id: {number}\nevent: message.new\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                    try:
                        for byte in body.encode('utf-8'):
                            self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    return
                if p.path.startswith('/api/v1/media/'):
                    payload, kind = parent.png, 'image/png'
                else:
                    kind = 'application/json'
                    if p.path == '/api/v1/sessions':
                        data = {'success': True, 'sessions': [{'username':'target@chatroom', 'displayName':'测试群'}, {'username':'private', 'displayName':'测试群'}]}
                    elif p.path == '/api/v1/messages':
                        query = parse_qs(p.query)
                        if query.get('talker') != ['target@chatroom'] or query.get('server_id') != ['42']:
                            self.send_error(400)
                            return
                        parent.media_reads += 1
                        data = {'success':True, 'messages': [
                            {'serverIdRaw':'wrong', 'localId':7, 'mediaUrl':'/api/v1/media/wrong.png'},
                            {'serverIdRaw':'42', 'localId':7, 'mediaUrl':parent.media_url if parent.media_reads > 1 else ''}]}
                    else:
                        self.send_error(404)
                        return
                    payload = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cfg = {'Astrwechat地址':f'http://127.0.0.1:{self.server.server_port}', 'AccessToken':'local-test-token',
                    '群名称':'测试群', '群ID':'', '允许二维码域名':['baominggongju.com'], '图片等待秒':2}
        self.client = AstrClient(self.cfg, self.stop)

    def tearDown(self):
        self.stop.set()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()

    def event(self, rawid='42', **kwargs):
        value = {'event':'message.new','sessionId':'target@chatroom','rawid':rawid,'localId':7,
                 'timestamp':time.time(), 'content':'[图片]', 'localType':3}
        value.update(kwargs)
        return value

    def test_real_sse_filter_delayed_image_and_qr(self):
        self.events = [self.event('old', timestamp=time.time()-30), self.event('other', sessionId='other@chatroom'), self.event(), self.event()]
        url, stamp = next(self.client.activities())
        self.assertEqual(url, self.url)
        self.assertIsNone(stamp)
        self.assertGreaterEqual(self.media_reads, 2)
        self.assertFalse(any(path.endswith('wrong.png') for path, _, _ in self.requests))
        self.assertTrue(all(token == 'Bearer local-test-token' for _, token, _ in self.requests))

    def test_reconnect_replay_dedup_and_new_event_after_sequence_reset(self):
        self.events = [self.event('first', content='第一条')]
        stream = self.client.events()
        self.assertEqual(next(stream)['rawid'], 'first')
        self.events.append(self.event('second', content='第二条'))
        self.assertEqual(next(stream)['rawid'], 'second')
        self.assertTrue(all(cursor == '0' for path, _, cursor in self.requests if path.endswith('/push/messages')))

    def test_foreign_media_origin_rejected(self):
        self.client.resolve_group()
        self.media_reads = 2
        self.media_url = 'https://example.com/api/v1/media/test.png'
        with self.assertRaises(ApiError):
            self.client.image_bytes(self.event())
        self.assertFalse(any(path.startswith('/api/v1/media') for path, _, _ in self.requests))

    def test_wrong_token(self):
        self.client.http.headers['Authorization'] = 'Bearer wrong'
        with self.assertRaises(ApiError):
            self.client.check_connection()

    def test_sse_multiline_and_url_boundary(self):
        rows = list(parse_sse([b': heartbeat', b'id: 5', b'event: message.new', b'data: {', b'data: "a":1}', b'']))
        self.assertEqual(rows, [('message.new','5','{\n"a":1}')])
        self.assertFalse(allowed_url('https://baominggongju.com.evil.test/', ['baominggongju.com']))
        self.assertFalse(allowed_url('https://user@baominggongju.com/', ['baominggongju.com']))
        self.assertIsNone(exact_message([{'serverId':'wrong','localId':7}], self.event()))

    def test_only_explicit_registration_time(self):
        now = datetime(2026, 11, 19, 12, tzinfo=CST)
        self.assertEqual(notification_time('报名时间：2026年11月19日晚上19:00', now), datetime(2026,11,19,19,tzinfo=CST).timestamp())
        self.assertIsNone(notification_time('活动时间：2026年11月19日19:00', now))
        self.assertIsNone(notification_time('报名时间：2026年11月4日晚上19:00', now))
        self.assertIsNone(notification_time('报名时间：2026年11月19日19:00\n报名时间：2026年11月19日20:00', now))


if __name__ == '__main__':
    unittest.main()
