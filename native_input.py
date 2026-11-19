"""Windows 原生批量输入。文字不经过剪贴板，不进行模拟逐字延时。"""
import ctypes as C
from ctypes import wintypes as W

user32 = C.WinDLL('user32', use_last_error=True)
winmm = C.WinDLL('winmm')

class MouseInput(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('mouseData', W.DWORD), ('flags', W.DWORD), ('time', W.DWORD), ('extra', C.c_size_t)]
class KeyboardInput(C.Structure):
    _fields_ = [('vk', W.WORD), ('scan', W.WORD), ('flags', W.DWORD), ('time', W.DWORD), ('extra', C.c_size_t)]
class InputUnion(C.Union):
    _fields_ = [('mi', MouseInput), ('ki', KeyboardInput)]
class Input(C.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', W.DWORD), ('data', InputUnion)]

user32.SendInput.argtypes = [W.UINT, C.POINTER(Input), C.c_int]
user32.SendInput.restype = W.UINT
user32.GetForegroundWindow.restype = W.HWND
user32.WindowFromPoint.argtypes = [W.POINT]
user32.WindowFromPoint.restype = W.HWND
user32.GetAncestor.argtypes = [W.HWND, W.UINT]
user32.GetAncestor.restype = W.HWND
user32.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user32.GetWindowTextW.restype = C.c_int
user32.GetAsyncKeyState.argtypes = [C.c_int]
user32.GetAsyncKeyState.restype = C.c_short

def key(vk=0, scan=0, flags=0):
    item = Input(type=1)
    item.ki = KeyboardInput(vk, scan, flags, 0, 0)
    return item

def compile_text(text):
    events = [key(0x11), key(0x41), key(0x41, flags=2), key(0x11, flags=2)]
    raw = text.encode('utf-16-le')
    for i in range(0, len(raw), 2):
        code = int.from_bytes(raw[i:i+2], 'little')
        events.extend((key(scan=code, flags=4), key(scan=code, flags=6)))
    return (Input * len(events))(*events)

def send(events):
    if user32.SendInput(len(events), events, C.sizeof(Input)) != len(events):
        raise RuntimeError('Windows 未接收全部输入，已停止；请让目标软件和助手使用相同的普通用户权限。')

def click(point):
    if not user32.SetCursorPos(*map(int, point)):
        raise RuntimeError('无法移动鼠标；桌面可能已锁定。')
    events = (Input * 2)()
    events[0].mi.flags, events[1].mi.flags = 2, 4
    send(events)

def foreground_bounds():
    hwnd, rect = user32.GetForegroundWindow(), W.RECT()
    if not user32.GetWindowRect(hwnd, C.byref(rect)):
        raise RuntimeError('无法读取当前窗口。')
    box = (max(0, rect.left), max(0, rect.top), min(user32.GetSystemMetrics(0), rect.right), min(user32.GetSystemMetrics(1), rect.bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise RuntimeError('请把报名窗口放到主屏幕。')
    return hwnd, box

def assert_target(hwnd, point):
    root = user32.GetAncestor(user32.WindowFromPoint(W.POINT(*map(int, point))), 2)
    if not root or root != hwnd or user32.GetForegroundWindow() != hwnd:
        raise RuntimeError('报名窗口已失去前台或被遮挡，已停止，避免资料填入其他窗口。')
    if any(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
        raise RuntimeError('请松开键盘修饰键，再重新运行。')
