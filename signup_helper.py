"""本地配置 + Astrwechat 消息推送 + Windows 表单操作。运行前阅读 README.md。"""
from datetime import datetime
from pathlib import Path
import ctypes
import json
import math
import os
import queue
import re
import threading
import time
import webbrowser

import cv2
import numpy as np
from PIL import ImageGrab, Image

from astr_client import AstrClient, CST, activity_key, validate_base
import native_input as native
from smart_ui import SmartUI, Target, normalize

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
EXAMPLE_CONFIG = ROOT / 'config.example.json'


def ensure_config(path=CONFIG, example=EXAMPLE_CONFIG):
    """首次从公开模板生成本地配置；已有个人配置始终保留。"""
    path = Path(path)
    if not path.exists():
        try:
            with path.open('xb') as output:
                output.write(Path(example).read_bytes())
        except FileExistsError:
            pass
    return path


def read_config(path=CONFIG):
    path = Path(path)
    if path == CONFIG:
        ensure_config()
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(cfg, dict):
        raise ValueError('配置必须是 JSON 对象。')
    validate_base(cfg['Astrwechat地址'])
    for name in ('AccessToken', '群名称', '群ID', '报名时间', '页面标题包含'):
        if not isinstance(cfg.get(name, ''), str):
            raise ValueError(name + ' 必须是字符串。')
    if not cfg.get('页面标题包含', '').strip():
        raise ValueError('页面标题包含不能为空，请填写报名窗口标题中的独特文字。')
    for name in ('允许二维码域名', '活动关键词', '成功提示文字'):
        values = cfg.get(name)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(name + ' 必须是字符串数组。')
    if not cfg['允许二维码域名'] or any(not re.fullmatch(r'[a-zA-Z0-9.-]+', h) or '.' not in h or h.startswith('.') for h in cfg['允许二维码域名']):
        raise ValueError('允许二维码域名请填实际主机名，不要写 https://、路径或通配符。')
    for name in ('智能识别', '自动读取报名时间', '仅预填'):
        if type(cfg.get(name)) is not bool:
            raise ValueError(name + ' 必须是 true 或 false。')
    for name, default, low, high in (
        ('图片等待秒', 20, 1, 120), ('页面超时秒', 20, 1, 300),
        ('页面检测间隔毫秒', 40, 10, 1000), ('输入焦点等待毫秒', 30, 10, 1000),
        ('图像匹配阈值', .92, .8, .999)):
        value = cfg.setdefault(name, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{name} 超出范围 {low}～{high}。')
    if cfg.get('报名时间'):
        datetime.strptime(cfg['报名时间'], '%Y-%m-%d %H:%M:%S')
    steps = cfg.get('步骤')
    if not isinstance(steps, list) or not steps:
        raise ValueError('至少配置一个步骤。')
    names, late = set(), False
    for step in steps:
        if not isinstance(step, dict) or step.get('动作') not in ('填写', '点击') or step.get('阶段') not in ('提前', '到点'):
            raise ValueError('步骤动作只能是填写/点击，阶段只能是提前/到点。')
        name = step.get('名称')
        if not isinstance(name, str) or not re.fullmatch(r'[\w\u4e00-\u9fff-]{1,40}', name) or name in names:
            raise ValueError('步骤名称必须唯一，只用中文、字母、数字、下划线或短横线。')
        names.add(name)
        if late and step['阶段'] == '提前':
            raise ValueError('提前步骤必须排在所有到点步骤之前。')
        late |= step['阶段'] == '到点'
        aliases = step.setdefault('识别文字', [name])
        if not isinstance(aliases, list) or not aliases or any(not isinstance(s, str) or not normalize(s) for s in aliases):
            raise ValueError(name + ' 的识别文字必须是非空字符串数组。')
        if step['动作'] == '填写' and (not isinstance(step.get('内容'), str) or not step['内容']):
            raise ValueError(name + ' 的内容必须是非空字符串。')
        delay = step.setdefault('操作后等待毫秒', 50)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 10000:
            raise ValueError(name + ' 的等待毫秒无效。')
    return cfg


class Stopped(Exception):
    pass


class AttemptStore:
    """提交前先占用活动，异常退出也不自动重复报名。只保存链接摘要。"""
    def __init__(self, path):
        self.path = Path(path)
        self.entries = json.loads(self.path.read_text('utf-8')) if self.path.exists() else []
        if not isinstance(self.entries, list) or any(not isinstance(s, str) for s in self.entries):
            raise ValueError('已尝试活动.json 损坏，请先人工核对报名状态。')

    def claim(self, url):
        key = activity_key(url)
        if key in self.entries:
            return False
        self.entries = (self.entries + [key])[-2000:]
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.entries), encoding='utf-8')
        os.replace(temp, self.path)
        return True


def match_template(frame, template, threshold):
    if frame.shape[0] < template.shape[0] or frame.shape[1] < template.shape[1] or template.std() < 3:
        return None
    scores = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
    _, peak, _, point = cv2.minMaxLoc(scores)
    if peak < threshold:
        return None
    # 同一匹配峰附近属于同一个控件，其他位置仍有高分则拒绝猜选。
    x, y = point
    h, w = template.shape[:2]
    scores[max(0, y-h//2):y+h//2+1, max(0, x-w//2):x+w//2+1] = -1
    if cv2.minMaxLoc(scores)[1] >= threshold:
        raise RuntimeError('标定图像匹配到多个位置，请重新标定带有独特文字的区域。')
    return point


class Runner:
    def __init__(self, cfg, stop, notify, root=ROOT):
        self.cfg, self.stop, self.notify, self.root = cfg, stop, notify, Path(root)
        self.client, self.ui = None, None
        self.buffers = {s['名称']: native.compile_text(s['内容']) for s in cfg['步骤'] if s['动作'] == '填写'}
        self.templates = {}
        for step in cfg['步骤']:
            path = self.root / 'calibration' / (step['名称'] + '.png')
            if path.exists():
                self.templates[step['名称']] = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
            if not cfg['智能识别'] and step['名称'] not in self.templates:
                raise ValueError(step['名称'] + ' 尚未标定。')
        self.hwnd = None

    def check(self):
        if self.stop.is_set() or all(native.user32.GetAsyncKeyState(k) & 0x8000 for k in (0x11, 0x12, 0x7B)):
            raise Stopped()

    def wait(self, seconds):
        if self.stop.wait(max(0, seconds)):
            raise Stopped()
        self.check()

    def find(self, step):
        self.check()
        hwnd, box = native.foreground_bounds()
        title = ctypes.create_unicode_buffer(1024)
        native.user32.GetWindowTextW(hwnd, title, len(title))
        if self.cfg['页面标题包含'] not in title.value:
            return None
        if self.hwnd is not None and hwnd != self.hwnd:
            raise RuntimeError('报名窗口失去前台，操作已停止。')
        target = self.ui.find(step, hwnd) if self.cfg['智能识别'] else None
        if target is not None:
            return target
        template = self.templates.get(step['名称'])
        if template is not None:
            frame = cv2.cvtColor(np.asarray(ImageGrab.grab(bbox=box)), cv2.COLOR_RGB2GRAY)
            point = match_template(frame, template, self.cfg['图像匹配阈值'])
            if point:
                return Target((box[0]+point[0]+template.shape[1]//2, box[1]+point[1]+template.shape[0]//2), hwnd)
        return None

    def wait_target(self, step, prepared=None):
        end = time.perf_counter() + self.cfg['页面超时秒']
        while True:
            self.check()
            target = self.ui.refresh(prepared) if prepared is not None and prepared.control is not None else None
            if target is None:
                target = self.find(step)
            if target is not None and target.enabled:
                # UIA 再读实时位置；等待开放期间不缓存按钮坐标。
                target = self.ui.refresh(target) if target.control is not None else target
                if target and target.enabled:
                    return target
            if time.perf_counter() >= end:
                raise RuntimeError(step['名称'] + ' 未能定位或未启用。检查识别文字，或先进行图像标定。')
            self.wait(self.cfg['页面检测间隔毫秒'] / 1000)

    def operate(self, step, prepared=None):
        target = self.wait_target(step, prepared)
        self.check()
        native.assert_target(target.hwnd, target.point)
        self.hwnd = target.hwnd
        native.click(target.point)
        if step['动作'] == '填写':
            self.wait(self.cfg['输入焦点等待毫秒'] / 1000)
            native.assert_target(target.hwnd, target.point)
            self.check()
            if target.control is not None and not target.control.HasKeyboardFocus:
                raise RuntimeError(step['名称'] + ' 未取得键盘焦点，已停止。')
            native.send(self.buffers[step['名称']])
            if target.control is not None:
                # 支持 ValuePattern 的控件必须读回一致；异步页面允许短暂更新。
                end = time.perf_counter() + 1
                while True:
                    self.check()
                    value = self.ui.value(target)
                    if value is None or value == step['内容']:
                        break
                    if time.perf_counter() >= end:
                        raise RuntimeError(step['名称'] + ' 输入后读回不一致，已停止。')
                    self.wait(.02)
        self.notify('已操作：' + step['名称'])
        self.wait(step['操作后等待毫秒'] / 1000)

    def deadline(self, stamp):
        if stamp is None or stamp <= time.time():
            return
        self.notify('已预填，等待报名开放：' + datetime.fromtimestamp(stamp, CST).strftime('%m-%d %H:%M:%S'))
        # 等待阶段随系统时钟校正；最后 20ms 换成单调时钟，最后 2ms 短暂自旋。
        while stamp - time.time() > .02:
            self.wait(min(.2, max(.001, stamp - time.time() - .02)))
        end = time.perf_counter() + max(0, stamp - time.time())
        while (left := end - time.perf_counter()) > 0:
            self.check()
            if left > .002:
                self.wait(left - .002)

    def execute(self, stamp=None):
        before = self.ui.texts(native.user32.GetForegroundWindow())
        if self.cfg.get('报名时间'):
            stamp = datetime.strptime(self.cfg['报名时间'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST).timestamp()
        waited = False
        for step in self.cfg['步骤']:
            if self.cfg['仅预填'] and step['动作'] != '填写':
                continue
            prepared = None
            if step['阶段'] == '到点' and not waited:
                # 已显示的到点按钮可提前解析；到点只刷新其位置和启用状态。
                if stamp is not None and stamp > time.time():
                    prepared = self.find(step)
                self.deadline(stamp)
                waited = True
            self.operate(step, prepared)
        if self.cfg['仅预填']:
            self.notify('预填结束；仅预填模式未执行任何点击步骤。')
            return False
        wanted = {normalize(s) for s in self.cfg['成功提示文字']}
        end = time.perf_counter() + min(10, self.cfg['页面超时秒'])
        while wanted and time.perf_counter() < end:
            self.check()
            if self.hwnd != native.user32.GetForegroundWindow():
                break
            if (self.ui.texts(self.hwnd) - before) & wanted:
                self.notify('已检测到配置的成功提示，请以报名平台记录为准。')
                return True
            self.wait(.1)
        self.notify('操作已发出，但未检测到新的成功提示；请人工核对，程序不会重复提交。')
        return False

    def run(self):
        if any('待填写' in s.get('内容', '') for s in self.cfg['步骤']):
            raise ValueError('请先在 config.json 填写个人信息。')
        self.ui = SmartUI(self.check)
        native.winmm.timeBeginPeriod(1)
        try:
            store = AttemptStore(self.root / '已尝试活动.json')
            self.client = AstrClient(self.cfg, self.stop, self.notify)
            for url, stamp in self.client.activities():
                self.check()
                if not store.claim(url):
                    self.notify('已跳过此前处理过的活动。')
                    continue
                self.notify('发现符合域名与关键词条件的报名链接，正在打开页面。')
                if not webbrowser.open(url, new=2):
                    raise RuntimeError('无法打开默认浏览器。')
                # 新页面加载完成由控件出现驱动；首次启动浏览器的转场留少量时间。
                self.wait(.6)
                self.execute(stamp)
                return  # 一次只报名一个活动，防止继续报名群里的其他活动。
        finally:
            if self.client:
                self.client.close()
            self.ui.close()
            native.winmm.timeEndPeriod(1)


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.stop, self.messages = threading.Event(), queue.Queue()
        self.thread, self.cfg = None, None
        root.title('抢课脚本 · Astrwechat')
        root.geometry('850x590')
        ttk.Label(root, text='先配置 Astrwechat 和本地个人信息，再检查连接、试填、监听。', padding=12).pack(anchor='w')
        bar = ttk.Frame(root, padding=8)
        bar.pack(fill='x')
        for title, command in [('编辑本地配置', lambda: os.startfile(ensure_config())), ('重载配置', self.reload),
                               ('检查连接', self.connection), ('识别当前页', self.inspect),
                               ('开始监听', self.start), ('停止', self.cancel)]:
            ttk.Button(bar, text=title, command=command).pack(side='left', padx=3)
        row = ttk.Frame(root, padding=8)
        row.pack(fill='x')
        self.selection = ttk.Combobox(row, state='readonly', width=22)
        self.selection.pack(side='left')
        ttk.Button(row, text='标定所选步骤（3 秒后）', command=self.calibrate).pack(side='left', padx=8)
        ttk.Label(root, text='标定：把鼠标停在目标中心。报名操作时请让页面保持前台；Ctrl+Alt+F12 停止。', padding=8).pack(anchor='w')
        self.logbox = tk.Text(root, wrap='word', state='disabled', font=('Microsoft YaHei UI', 10))
        self.logbox.pack(fill='both', expand=True, padx=12, pady=10)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.reload()
        root.after(100, self.pump)

    def log(self, value):
        self.messages.put(time.strftime('%H:%M:%S') + '  ' + value)

    def pump(self):
        while not self.messages.empty():
            self.logbox.configure(state='normal')
            self.logbox.insert('end', self.messages.get_nowait() + '\n')
            self.logbox.see('end')
            self.logbox.configure(state='disabled')
        self.root.after(100, self.pump)

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def reload(self):
        if self.busy():
            self.log('请先停止当前任务。')
            return
        try:
            self.cfg = read_config()
            names = [s['名称'] for s in self.cfg['步骤']]
            self.selection['values'] = names
            self.selection.current(0)
            self.log('配置已加载。' + ('当前为仅预填模式。' if self.cfg['仅预填'] else '当前为自动执行模式，命中活动后会执行配置的点击步骤。'))
        except Exception as exc:
            self.cfg = None
            self.log('配置错误：' + str(exc))

    def job(self, function):
        if self.busy():
            self.log('当前任务尚未结束，请先停止。')
            return
        self.reload()
        if self.cfg is None:
            return
        self.stop.clear()
        def work():
            try:
                function()
            except Stopped:
                self.log('已停止。')
            except Exception as exc:
                # 不记录 API URL、请求头、表单内容或完整聊天消息。
                from requests import RequestException
                self.log('任务停止：' + ('本地网络请求失败，请检查 Astrwechat。' if isinstance(exc, RequestException) else str(exc)))
            finally:
                self.log('本次任务结束。')
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def connection(self):
        def check():
            client = AstrClient(self.cfg, self.stop, self.log)
            try:
                group = client.check_connection()
                self.log('连接正常，已匹配群：' + group['displayName'] + '；ID：' + group['username'])
            finally:
                client.close()
        self.job(check)

    def start(self):
        self.job(lambda: Runner(self.cfg, self.stop, self.log).run())

    def inspect(self):
        def inspect_page():
            self.log('3 秒后只读识别当前前台窗口，请切换到报名页。')
            if self.stop.wait(3):
                raise Stopped()
            ui = SmartUI(lambda: None)
            try:
                rows = ui.scan(native.user32.GetForegroundWindow(), force=True)
                names = [name for name, kind, _ in rows if kind in ('EditControl', 'ButtonControl', 'HyperlinkControl')]
                self.log('可识别的输入框/按钮名称：' + ('、'.join(names[:50]) if names else '未找到，可使用图像标定。'))
            finally:
                ui.close()
        self.job(inspect_page)

    def calibrate(self):
        name = self.selection.get()
        if self.busy() or not name:
            return
        self.log('3 秒后采集鼠标中心 160×48 区域，请切换页面并把鼠标放在控件中心。')
        self.root.after(3000, lambda: self.capture(name))

    def capture(self, name):
        if self.busy():
            return
        try:
            point = native.W.POINT()
            native.user32.GetCursorPos(ctypes.byref(point))
            hwnd, bounds = native.foreground_bounds()
            box = (point.x-80, point.y-24, point.x+80, point.y+24)
            if box[0] < bounds[0] or box[1] < bounds[1] or box[2] > bounds[2] or box[3] > bounds[3]:
                raise ValueError('控件太接近窗口边缘，请移到中间后标定。')
            native.assert_target(hwnd, (point.x, point.y))
            image = ImageGrab.grab(bbox=box)
            if np.asarray(image.convert('L')).std() < 3:
                raise ValueError('标定区域缺少可区分的文字或边框，请重新选择。')
            directory = ROOT / 'calibration'
            directory.mkdir(exist_ok=True)
            image.save(directory / (name + '.png'))
            self.log('已保存标定：' + name + '。保持相同页面缩放和屏幕缩放。')
        except Exception as exc:
            self.log('标定失败：' + str(exc))

    def cancel(self):
        self.stop.set()
        self.log('已请求停止；正在进行的网络读取或系统控件查询可能稍后返回。')

    def close(self):
        self.stop.set()
        self.root.destroy()


def main():
    import tkinter as tk
    # 防止同一台电脑同时启动两个助手重复报名。
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, 'Local\\AstrActivitySignupHelper')
    if not mutex or ctypes.get_last_error() == 183:
        raise SystemExit('助手已在运行，请先关闭已有窗口。')
    try:
        native.user32.SetProcessDPIAware()
        root = tk.Tk()
        App(root)
        root.mainloop()
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
