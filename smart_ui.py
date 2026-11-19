"""按可访问名称找控件；无法准确识别时交给原有图像标定，不猜填表顺序。"""
import os
import re
import time
from dataclasses import dataclass


def normalize(text):
    text = re.sub(r"[\s*＊:：]", "", text or "")
    text = re.sub(r"[（(]必填[)）]$", "", text)
    return re.sub(r"^(请输入|请填写)", "", text)


@dataclass
class Target:
    point: tuple
    hwnd: int
    control: object = None
    enabled: bool = True


class SmartUI:
    def __init__(self, stop_check):
        import uiautomation as auto
        self.auto, self.check = auto, stop_check
        self.initializer = auto.UIAutomationInitializerInThread()
        self.rows, self.hwnd, self.updated = [], None, 0

    def close(self):
        self.rows.clear()
        self.initializer.Uninitialize()

    def scan(self, hwnd, force=False):
        if hwnd == self.hwnd and not force and time.monotonic() - self.updated < .1:
            return self.rows
        rows = []
        root = self.auto.ControlFromHandle(hwnd)
        if root is None or root.ProcessId == os.getpid():
            return []
        started = time.monotonic()
        for control, depth in self.auto.WalkControl(root, maxDepth=20):
            self.check()
            if len(rows) > 800 or time.monotonic() - started > .7:
                # An incomplete tree cannot establish uniqueness safely.
                return []
            try:
                if control.IsOffscreen:
                    continue
                name = normalize(control.Name)
                rect = control.BoundingRectangle
                if not name or rect.width() <= 0 or rect.height() <= 0:
                    continue
                rows.append((name, control.ControlTypeName, control))
            except self.auto.comtypes.COMError:
                continue
        self.hwnd, self.rows, self.updated = hwnd, rows, time.monotonic()
        return rows

    def find(self, step, hwnd):
        names = {normalize(n) for n in step.get("识别文字", [step["名称"]])}
        kinds = ("EditControl",) if step["动作"] == "填写" else ("ButtonControl", "MenuItemControl", "HyperlinkControl")
        found = [control for name, kind, control in self.scan(hwnd) if name in names and kind in kinds]
        if len(found) > 1:
            raise RuntimeError(f"{step['名称']} 匹配到多个控件，请缩小识别文字范围或使用图像标定。")
        return self.refresh(Target((0, 0), hwnd, found[0])) if found else None

    def refresh(self, target):
        try:
            control = target.control
            if control.IsOffscreen:
                return None
            rect = control.BoundingRectangle
            if rect.width() <= 0 or rect.height() <= 0:
                return None
            return Target((round((rect.left+rect.right)/2), round((rect.top+rect.bottom)/2)),
                          target.hwnd, control, bool(control.IsEnabled))
        except self.auto.comtypes.COMError:
            self.updated = 0
            return None

    def value(self, target):
        pattern = target.control.GetPattern(self.auto.PatternId.ValuePattern)
        return pattern.Value if pattern is not None else None

    def texts(self, hwnd):
        return {name for name, kind, _ in self.scan(hwnd, force=True) if kind == "TextControl"}
