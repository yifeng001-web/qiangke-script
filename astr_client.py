"""AstrWeChat 1.5.x 本地 HTTP/SSE 客户端；只读消息，不启用机器人回复。"""
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import html
import io
import json
import re
import queue
import threading
import time
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

CST = timezone(timedelta(hours=8))


class ApiError(RuntimeError):
    pass


def validate_base(value):
    p = urlsplit(value)
    if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost", "::1") or p.username or p.password or p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("Astrwechat 地址必须是本机 HTTP 地址，例如 http://127.0.0.1:5031。")
    return value.rstrip("/")


def allowed_url(value, hosts):
    try:
        p = urlsplit(value)
        return p.scheme in ("http", "https") and not p.username and bool(p.hostname) and any(
            p.hostname.lower() == h.lower() or p.hostname.lower().endswith("." + h.lower()) for h in hosts)
    except ValueError:
        return False


def activity_key(url):
    p = urlsplit(url)
    eid = parse_qs(p.query).get("eid")
    key = "baominggongju:" + eid[0] if eid and allowed_url(url, ["baominggongju.com"]) else url
    return hashlib.sha256(key.encode()).hexdigest()


def event_key(event):
    return str(event.get("sessionId", "")) + ":" + str(event.get("messageKey") or event.get("serverId") or event.get("rawid") or event.get("localId") or "")


def exact_message(messages, event):
    """原消息 ID 精确匹配；不按时间接近猜图片，避免拿错同一秒的其他图片。"""
    server = str(event.get("serverId") or event.get("rawid") or "")
    local = str(event.get("localId") or "")
    for msg in messages:
        other = str(msg.get("serverIdRaw") or msg.get("serverId") or msg.get("rawid") or "")
        if server not in ("", "0") and other not in ("", "0"):
            if server == other:
                return msg
        elif local not in ("", "0") and local == str(msg.get("localId", "")):
            return msg
    return None


def parse_sse(lines):
    """支持注释心跳、CRLF、多行 data、UTF-8 和 id；不在每个网络分块上猜 JSON。"""
    name, data, cursor, size = "message", [], "", 0
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.rstrip("\r\n")
        if not line:
            if data:
                yield name, cursor, "\n".join(data)
            name, data, size = "message", [], 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        size += len(line)
        if size > 1_000_000:
            raise ApiError("推送事件过大，已断开连接。")
        if field == "data":
            data.append(value)
        elif field == "event":
            name = value
        elif field == "id" and "\0" not in value:
            cursor = value


def notification_time(text, now=None):
    """只读明确写出的报名时间，绝不把活动举行时间当成报名时间。"""
    now = now or datetime.now(CST)
    matches = re.findall(r"报名(?:开始|开放)?时间\s*[:：]?\s*((?:20\d{2}[年/-])?\d{1,2}[月/-]\d{1,2}日?[^\n，,；;]{0,12}?\d{1,2}[:：]\d{2}(?:[:：]\d{2})?)", text)
    times = set()
    for value in matches:
        m = re.fullmatch(r"(?:(20\d{2})[年/-])?(\d{1,2})[月/-](\d{1,2})日?\s*(上午|下午|晚上|晚间|中午)?\s*(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?", value)
        if not m:
            continue
        year, month, day, period, hour, minute, second = m.groups()
        hour = int(hour)
        if period in ("下午", "晚上", "晚间") and hour < 12:
            hour += 12
        try:
            when = datetime(int(year or now.year), int(month), int(day), hour, int(minute), int(second or 0), tzinfo=CST)
        except ValueError:
            continue
        if now - timedelta(minutes=2) <= when <= now + timedelta(days=31):
            times.add(when.timestamp())
    return times.pop() if len(times) == 1 else None


class AstrClient:
    def __init__(self, cfg, stop, notify=lambda _: None):
        self.cfg, self.stop, self.notify = cfg, stop, notify
        self.base = validate_base(cfg["Astrwechat地址"])
        self.http = requests.Session()
        self.http.trust_env = False  # 本地服务不经过系统代理，Token 不进入 URL。
        if cfg.get("AccessToken"):
            self.http.headers["Authorization"] = "Bearer " + cfg["AccessToken"]
        self.http.headers["Accept"] = "application/json"
        self.group_id = ""
        self.cursor = ""
        self.seen, self.order = set(), deque()
        self.started = int(time.time())
        self.context = deque(maxlen=12)
        self.closed = threading.Event()

    def close(self):
        self.closed.set()
        self.http.close()

    def get(self, path, **kwargs):
        response = self.http.get(self.base + path, timeout=kwargs.pop("timeout", (3, 10)), allow_redirects=False, **kwargs)
        if response.status_code in (401, 403):
            response.close()
            raise ApiError("Astrwechat 拒绝访问：检查 AccessToken，并在 Astrwechat 开启 API 和主动推送。")
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            raise ApiError(f"Astrwechat 返回 HTTP {status}，请检查数据库连接和 API 状态。")
        return response

    def json(self, path, params=None):
        with self.get(path, params=params) as response:
            value = response.json()
        if not isinstance(value, dict) or value.get("success") is False:
            raise ApiError("Astrwechat API 返回失败，请在其界面检查数据库连接。")
        return value

    def resolve_group(self):
        # 用户在config.json 填「群ID」或「群名称」。只匹配唯一目标；同名群不会猜选。
        wanted_id = self.cfg.get("群ID", "").strip()
        wanted_name = self.cfg.get("群名称", "").strip()
        if not wanted_id and (not wanted_name or "待填写" in wanted_name):
            raise ValueError("请在 config.json 填写群名称或群ID。优先用以 @chatroom 结尾的群ID。")
        rows = self.json("/api/v1/sessions", {"keyword": wanted_id or wanted_name, "limit": 10000}).get("sessions", [])
        matches = [r for r in rows if str(r.get("username", "")).endswith("@chatroom") and
                   (r.get("username") == wanted_id if wanted_id else r.get("displayName") == wanted_name)]
        if len(matches) != 1:
            raise ValueError("未找到唯一目标群。检查群名是否完全一致；同名群请在配置中填写准确群ID。")
        self.group_id = matches[0]["username"]
        return matches[0]

    def check_connection(self):
        group = self.resolve_group()
        with self.get("/api/v1/push/messages", stream=True, timeout=(3, 5)) as response:
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise ApiError("接口未返回 SSE 推送，请检查 Astrwechat 版本和主动推送设置。")
        return group

    def remember(self, key):
        if key in self.seen:
            return False
        self.seen.add(key)
        self.order.append(key)
        if len(self.order) > 4000:
            self.seen.discard(self.order.popleft())
        return True

    def _stream(self):
        if not self.group_id:
            self.resolve_group()
        delay = .5
        while not self.stop.is_set() and not self.closed.is_set():
            try:
                # 服务重启会把事件序号归零；重连回放有限缓存，再按原消息 ID 去重。
                # 只沿用旧序号会漏掉服务重启后较小序号的新消息。
                headers = {"Accept": "text/event-stream", "Last-Event-ID": "0"}
                with self.get("/api/v1/push/messages", headers=headers, stream=True, timeout=(3, 35)) as response:
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        raise ApiError("不是 SSE 接口。")
                    self.notify("已接通后台消息推送；无需显示微信群窗口。")
                    delay = .5
                    # chunk_size=1 避免 requests 默认缓冲等到下一批消息才交出当前事件。
                    for name, cursor, raw in parse_sse(response.iter_lines(chunk_size=1)):
                        if self.stop.is_set() or self.closed.is_set():
                            return
                        if cursor:
                            self.cursor = cursor
                        if name not in ("message.new", "message"):
                            continue
                        try:
                            event = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(event, dict) or event.get("event", name) != "message.new" or event.get("sessionId") != self.group_id:
                            continue
                        try:
                            timestamp = float(event.get("timestamp", 0))
                        except (TypeError, ValueError):
                            continue
                        identity = event_key(event)
                        if timestamp < self.started or identity.endswith(":") or not self.remember(identity):
                            continue
                        yield event
            except requests.RequestException:
                self.notify("本地推送连接中断，正在自动重连；若持续失败请检查 Astrwechat。")
            if not self.stop.is_set():
                self.stop.wait(delay)
                delay = min(delay * 2, 5)

    def events(self):
        # 独立接收线程等待 25 秒 SSE 心跳；UI 停止按钮无需等待网络读超时。
        inbox = queue.Queue(maxsize=256)
        failed = []
        def receive():
            try:
                for event in self._stream():
                    inbox.put_nowait(event)
            except queue.Full:
                failed.append(ApiError("消息队列已满，已停止，避免漏处理后继续报名。"))
            except Exception as exc:
                failed.append(exc)
        threading.Thread(target=receive, daemon=True).start()
        while not self.stop.is_set() and not self.closed.is_set():
            if failed:
                raise failed[0]
            try:
                yield inbox.get(timeout=.1)
            except queue.Empty:
                continue

    def image_bytes(self, event):
        server = str(event.get("serverId") or event.get("rawid") or "")
        local = event.get("localId")
        if server in ("", "0") and not local:
            raise ApiError("图片事件缺少原消息 ID，无法安全定位图片。")
        params = {"talker": self.group_id, "limit": 100, "media": 1, "image": 1,
                  "voice": 0, "video": 0, "emoji": 0, "file": 0}
        if server not in ("", "0"):
            params["server_id"] = server
        if local:
            params["local_id"] = local
        end = time.monotonic() + self.cfg.get("图片等待秒", 20)
        while not self.stop.is_set():
            messages = self.json("/api/v1/messages", params).get("messages", [])
            match = exact_message(messages, event)
            if match and match.get("mediaUrl"):
                url = urljoin(self.base + "/", match["mediaUrl"])
                base, parsed = urlsplit(self.base), urlsplit(url)
                if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or not parsed.path.startswith("/api/v1/media/"):
                    raise ApiError("媒体地址不属于本地 Astrwechat，已拒绝附带 Token 访问。")
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                with self.get(path, stream=True) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_content(16384):
                        if self.stop.is_set():
                            return None
                        size += len(chunk)
                        if size > 20 * 1024 * 1024:
                            raise ApiError("图片超过 20 MB。")
                        chunks.append(chunk)
                    return b"".join(chunks)
            if time.monotonic() >= end:
                raise ApiError("图片尚未导出：检查 Astrwechat 图片密钥、微信图片下载状态。")
            self.stop.wait(.25)
        return None

    def activities(self):
        import cv2
        import numpy as np
        from PIL import Image
        detector = cv2.QRCodeDetector()
        for event in self.events():
            content = str(event.get("content", ""))
            stamp = float(event.get("timestamp", 0))
            self.context.append((stamp, content[:8000]))
            context = "\n".join(text for when, text in self.context if stamp - when <= 600)
            keywords = self.cfg.get("活动关键词", [])
            if keywords and not any(word in context for word in keywords):
                continue
            urls = {u.rstrip("。。，,；;！!）)") for u in re.findall(r'https?://[^\s<>"\u3000]+', html.unescape(content))}
            urls = {url for url in urls if allowed_url(url, self.cfg["允许二维码域名"])}
            if not urls and (event.get("localType") == 3 or "[图片]" in content or "<img" in content):
                try:
                    data = self.image_bytes(event)
                    if data is None:
                        return
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width * image.height > 25_000_000:
                            raise ValueError("图片分辨率超过限制。")
                    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
                    if frame is None:
                        raise ValueError("无法解码图片。")
                    ok, values, _, _ = detector.detectAndDecodeMulti(frame)
                    if not ok or not any(values):
                        value, _, _ = detector.detectAndDecode(frame)
                        values = [value]
                    urls = {url for url in values if allowed_url(url, self.cfg["允许二维码域名"])}
                except (ApiError, requests.RequestException, ValueError, OSError, cv2.error) as exc:
                    self.notify("目标群图片未能处理：" + (str(exc) if not isinstance(exc, requests.RequestException) else "本地媒体请求失败"))
                    continue
            if len(urls) > 1:
                self.notify("同一条消息含多个报名链接，无法判断目标，已跳过。")
                continue
            if urls:
                yield urls.pop(), notification_time(context) if self.cfg.get("自动读取报名时间", True) else None
