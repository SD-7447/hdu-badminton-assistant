"""杭电场馆速约：可校准的 Windows 桌面辅助脚本。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from PIL import ImageGrab, ImageTk

from venue_core import (Anchor, Candidate, CHINA, Profile, Runner, StopRun,
                        decode_image, encode_image, load_profile, save_profile)
from datetime import datetime

ROOT = Path(__file__).resolve().parent


class DingTalkWindow:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("仅支持 Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = W.HWND
        self.user.GetAncestor.argtypes = [W.HWND, W.UINT]
        self.user.GetAncestor.restype = W.HWND
        self.user.IsWindowVisible.argtypes = [W.HWND]
        self.user.IsIconic.argtypes = [W.HWND]
        self.user.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        self.user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        self.user.WindowFromPoint.argtypes = [W.POINT]
        self.user.WindowFromPoint.restype = W.HWND
        self.user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.kernel.OpenProcess.restype = W.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, W.BOOL, W.LPCWSTR]
        self.kernel.CreateMutexW.restype = W.HANDLE
        self.last = None
        self.enum_callback = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.user.EnumWindows.argtypes = [self.enum_callback, W.LPARAM]

    def owner(self, hwnd):
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return pid.value, ""
        try:
            size, name = W.DWORD(32768), ctypes.create_unicode_buffer(32768)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                return pid.value, ""
            return pid.value, name.value
        finally:
            self.kernel.CloseHandle(handle)

    def locate(self, require_foreground=True):
        found = []

        @self.enum_callback
        def collect(hwnd, _):
            name = ctypes.create_unicode_buffer(512)
            self.user.GetWindowTextW(hwnd, name, 512)
            if name.value == "场馆速约" and self.user.IsWindowVisible(hwnd):
                pid, executable = self.owner(hwnd)
                if Path(executable).name.lower() == "dingtalk.exe":
                    found.append((hwnd, pid))
            return True

        self.user.EnumWindows(collect, 0)
        if len(found) != 1:
            raise StopRun(f"需要唯一一个钉钉“场馆速约”窗口，当前找到 {len(found)} 个")
        hwnd, pid = found[0]
        if self.user.IsIconic(hwnd):
            raise StopRun("场馆速约窗口已最小化，请手动恢复")
        if require_foreground and self.user.GetAncestor(self.user.GetForegroundWindow(), 2) != hwnd:
            raise StopRun("场馆速约没有处于前台；请切回该窗口再运行")
        rect = W.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise StopRun("无法获取场馆窗口位置")
        return hwnd, pid, (rect.left, rect.top, rect.right, rect.bottom)

    def capture(self):
        before = self.locate()
        frame = ImageGrab.grab(bbox=before[2], all_screens=True).convert("RGB")
        after = self.locate()
        if before != after:
            raise StopRun("截图期间窗口位置发生变化")
        self.last = after
        return frame

    def click(self, x, y):
        now = self.locate()
        if self.last is None or now != self.last:
            raise StopRun("输入前窗口发生变化，停止")
        left, top, right, bottom = now[2]
        if not 0 <= x < right - left or not 0 <= y < bottom - top:
            raise StopRun("点击位置超出场馆窗口")
        screen_x, screen_y = left + x, top + y
        at = self.user.WindowFromPoint(W.POINT(screen_x, screen_y))
        if self.user.GetAncestor(at, 2) != now[0]:
            raise StopRun("目标位置被其他窗口遮挡")
        if self.user.GetAsyncKeyState(0x1B) & 0x8000:
            raise StopRun("Escape 已按下")
        # One physical click; do not silently fall back to PostMessage on errors.
        ulong_ptr = W.WPARAM

        class Mouse(ctypes.Structure):
            _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                        ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ulong_ptr)]

        class Key(ctypes.Structure):
            _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                        ("time", W.DWORD), ("dwExtraInfo", ulong_ptr)]

        class Hardware(ctypes.Structure):
            _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]

        class Payload(ctypes.Union):
            _fields_ = [("mi", Mouse), ("ki", Key), ("hi", Hardware)]

        class Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", W.DWORD), ("data", Payload)]

        self.user.SendInput.argtypes = [W.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = W.UINT
        if not self.user.SetCursorPos(screen_x, screen_y):
            raise StopRun("无法移动到目标位置")
        if self.locate() != now:
            raise StopRun("点击前窗口发生变化")
        events = (Input * 2)()
        events[0].type = events[1].type = 0
        events[0].mi.dwFlags, events[1].mi.dwFlags = 0x0002, 0x0004
        if self.user.SendInput(2, events, ctypes.sizeof(Input)) != 2:
            # Ensure a partial injection cannot leave the left mouse button held down.
            release = Input()
            release.mi.dwFlags = 0x0004
            self.user.SendInput(1, ctypes.byref(release), ctypes.sizeof(Input))
            raise StopRun("点击结果未知；停止并人工核对")

    def mutex(self):
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, "Local\\HDUVenueQuickBook")
        if not handle:
            raise StopRun("无法建立运行锁")
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(handle)
            raise StopRun("另一个脚本正在运行")
        return handle


class ScreenshotPicker(tk.Toplevel):
    def __init__(self, parent, screenshot, text, point, callback):
        super().__init__(parent)
        self.title(text + " · Esc 取消")
        self.source, self.point, self.callback = screenshot, point, callback
        self.factor = min(1, 1180 / screenshot.width, 720 / screenshot.height)
        width, height = round(screenshot.width * self.factor), round(screenshot.height * self.factor)
        self.photo = ImageTk.PhotoImage(screenshot.resize((width, height)))
        ttk.Label(self, text=text + ("：单击目标" if point else "：拖动框选，避开边框、鼠标和动态内容")).pack(padx=12, pady=10)
        self.canvas = tk.Canvas(self, width=width, height=height, cursor="crosshair", highlightthickness=0)
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<ButtonPress-1>", self.down)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.up)
        self.bind("<Escape>", lambda e: self.destroy())
        self.start = None
        self.shape = None
        self.transient(parent)
        self.grab_set()

    def down(self, event):
        self.start = (event.x, event.y)
        if self.shape:
            self.canvas.delete(self.shape)
        self.shape = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#ff3d54", width=2)

    def move(self, event):
        if self.start:
            self.canvas.coords(self.shape, *self.start, event.x, event.y)

    def up(self, event):
        if self.start is None:
            return
        sx, sy = self.start
        point = (max(0, min(self.source.width - 1, round(event.x / self.factor))),
                 max(0, min(self.source.height - 1, round(event.y / self.factor))))
        if self.point:
            answer = list(point)
        else:
            answer = [max(0, round(min(sx, event.x) / self.factor)),
                      max(0, round(min(sy, event.y) / self.factor)),
                      min(self.source.width, round(max(sx, event.x) / self.factor)),
                      min(self.source.height, round(max(sy, event.y) / self.factor))]
            if answer[2] - answer[0] < 8 or answer[3] - answer[1] < 8:
                return
        self.destroy()
        self.callback(answer)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("杭电羽毛球速约助手 · 本地校准版")
        self.geometry("1050x830")
        self.minsize(900, 740)
        self.profile_path = ROOT / "profile.json"
        self.journal_path = ROOT / "run-journal.json"
        self.p = Profile(target_date=datetime.now(CHINA).date().isoformat())
        self.image = None
        self.busy = False
        self.stop = threading.Event()
        self.messages = queue.Queue()
        self.pending_capture = False
        self.close_after_stop = False
        self.native = DingTalkWindow()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        if self.profile_path.exists():
            try:
                self.p = load_profile(self.profile_path)
                self.load_fields()
            except Exception as error:
                self.log(f"配置读取失败：{error}；未启动任何操作")
        self.update_status()
        self.after(100, self.pump)

    def build(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="杭电 · 羽毛球速约助手", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="沿用钉钉登录。定时监测 → 按优先顺序选场 → 至多提交一次。按住 Esc 可停止。",
                  foreground="#536477").pack(anchor="w", pady=(5, 12))
        settings = ttk.LabelFrame(outer, text="预约设置（北京时间）", padding=10)
        settings.pack(fill="x")
        self.fields = {}
        definitions = [("target_date", "预约日期", self.p.target_date), ("trigger", "启动时间（留空立即）", ""),
                       ("interval", "刷新间隔/秒", "1.2"), ("duration", "最多监测/秒", "90"),
                       ("max_refreshes", "最多刷新次数", "90"), ("settle", "选中后等待/秒", "0.45")]
        for index, (key, label, value) in enumerate(definitions):
            row, column = divmod(index, 3)
            ttk.Label(settings, text=label).grid(row=row * 2, column=column, sticky="w", padx=6)
            self.fields[key] = tk.StringVar(value=value)
            ttk.Entry(settings, textvariable=self.fields[key], width=30 if key == "trigger" else 25).grid(
                row=row * 2 + 1, column=column, sticky="ew", padx=6, pady=(2, 8))
            settings.columnconfigure(column, weight=1)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text="启用一次自动提交（需选中状态样本；默认仅选场）", variable=self.auto).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=6)
        ttk.Label(settings, text="启动时间示例：2026-11-20 19:59:59.800。放号时间需自行核实，程序未预设。",
                  foreground="#6b7280").grid(row=5, column=0, columnspan=3, sticky="w", padx=6, pady=(5, 0))

        calibration = ttk.LabelFrame(outer, text="界面校准（先选好日期并滚到目标时段，保持窗口大小和滚动位置）", padding=10)
        calibration.pack(fill="x", pady=10)
        line = ttk.Frame(calibration)
        line.pack(fill="x")
        ttk.Button(line, text="① 3 秒后截取场馆窗口", command=self.snapshot).pack(side="left", padx=(0, 10))
        ttk.Button(line, text="查看截图与候选框", command=self.show_image).pack(side="left")
        ttk.Button(line, text="保存配置", command=self.save).pack(side="right")
        anchors = ttk.Frame(calibration)
        anchors.pack(fill="x", pady=8)
        for label, key in [("② 标题区域", "title"), ("③ 已选日期区域", "date"),
                           ("④ 左侧时段标签", "rows"), ("⑤ 未选择时的按钮", "button")]:
            ttk.Button(anchors, text=label, command=lambda k=key: self.pick_anchor(k)).pack(side="left", padx=(0, 6))
        samples = ttk.Frame(calibration)
        samples.pack(fill="x")
        for label, key in [("⑥ 空闲格样本", "free_sample"), ("⑦ 不可用格样本", "busy_sample"),
                           ("选中格样本（自动模式）", "selected_sample")]:
            ttk.Button(samples, text=label, command=lambda k=key: self.pick_sample(k)).pack(side="left", padx=(0, 6))
        positions = ttk.Frame(calibration)
        positions.pack(fill="x", pady=(8, 0))
        for label, key in [("⑧ 刷新位置", "refresh"), ("提交按钮位置", "submit")]:
            ttk.Button(positions, text=label, command=lambda k=key: self.pick_point(k)).pack(side="left", padx=(0, 6))
        ttk.Button(positions, text="可选：成功标记区域", command=lambda: self.pick_anchor("success")).pack(side="left", padx=(0, 6))
        ttk.Button(positions, text="清空校准", command=self.reset_calibration).pack(side="right")
        self.status = ttk.Label(calibration, text="", foreground="#536477", wraplength=940)
        self.status.pack(fill="x", pady=(8, 0))

        candidates = ttk.LabelFrame(outer, text="候选格子（从上到下优先；先添加格子，再填写准确时段和场地号）", padding=8)
        candidates.pack(fill="x")
        self.listbox = tk.Listbox(candidates, height=4, font=("Microsoft YaHei UI", 10), exportselection=False)
        self.listbox.pack(side="left", fill="x", expand=True)
        tools = ttk.Frame(candidates)
        tools.pack(side="left", padx=(10, 0))
        for text, command in [("添加单格", self.add_candidate), ("整行 12 场", self.add_row),
                              ("上移", lambda: self.reorder(-1)), ("移除", self.remove_candidate)]:
            ttk.Button(tools, text=text, command=command).pack(fill="x", pady=1)
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=10)
        self.buttons = []
        for label, mode in [("只读预览", "preview"), ("定时监测并选场", "select"), ("定时监测并提交", "submit")]:
            button = ttk.Button(actions, text=label, command=lambda m=mode: self.start(m))
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)
        ttk.Button(actions, text="停止 / Esc", command=self.stop.set).pack(side="left")
        ttk.Button(actions, text="已核对预约结果，解除重试保护", command=self.resolve).pack(side="right")
        self.console = tk.Text(outer, height=8, state="disabled", wrap="word", font=("Microsoft YaHei UI", 9))
        self.console.pack(fill="both", expand=True)
        self.log("先截图并完成校准。灰色人形格已观察为不可选；没有真实空闲样本时不会执行抢约。")

    def log(self, text):
        line = datetime.now(CHINA).strftime("%H:%M:%S") + "  " + text
        self.console.configure(state="normal")
        self.console.insert("end", line + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def editable(self):
        if self.busy or self.pending_capture:
            messagebox.showinfo("运行中", "请先停止当前操作", parent=self)
            return False
        return True

    def read_fields(self):
        self.p.target_date = self.fields['target_date'].get().strip()
        self.p.trigger = self.fields['trigger'].get().strip()
        for key in ('interval', 'duration', 'settle'):
            setattr(self.p, key, float(self.fields[key].get()))
        self.p.max_refreshes = int(self.fields['max_refreshes'].get())
        self.p.auto_submit = self.auto.get()

    def load_fields(self):
        for key, variable in self.fields.items():
            variable.set(str(getattr(self.p, key)))
        self.auto.set(self.p.auto_submit)

    def update_status(self):
        flags = [f"{key}✓" for key in self.p.guards]
        flags += [text for key, text in [('free_sample', '空闲✓'), ('busy_sample', '不可用✓'),
                                        ('selected_sample', '选中✓'), ('refresh', '刷新✓'), ('submit', '提交✓')]
                  if getattr(self.p, key)]
        self.status.configure(text=f"窗口：{self.p.size or '未截取'}  |  校准：{' / '.join(flags) or '未完成'}  |  日期：{self.p.calibrated_date or '未校准'}")
        self.listbox.delete(0, "end")
        for index, item in enumerate(self.p.candidates):
            self.listbox.insert("end", f"{index + 1}. {item.label}   {item.box}")

    def snapshot(self):
        if not self.editable():
            return
        self.pending_capture = True
        self.log("3 秒后截图；请切到已打开的场馆速约，移开鼠标，保持目标日期和时段可见")
        self.iconify()
        self.after(3000, self.finish_snapshot)

    def finish_snapshot(self):
        try:
            image = self.native.capture()
            if self.p.size and self.p.size != list(image.size):
                raise ValueError("窗口尺寸与已校准尺寸不同，请恢复原尺寸或清空校准")
            self.image = image
            self.p.size = list(image.size)
            self.log(f"已截取 {image.width}×{image.height}；截图仅用于本地校准")
        except Exception as error:
            self.log(f"截图失败：{error}")
        finally:
            self.pending_capture = False
            self.deiconify()
            self.update_status()

    def picker(self, text, point, callback):
        if not self.editable():
            return
        if self.image is None:
            messagebox.showinfo("先截图", "请先使用“3 秒后截取场馆窗口”", parent=self)
            return
        ScreenshotPicker(self, self.image, text, point, callback)

    def pick_anchor(self, key):
        labels = {'title': '框选羽毛球场馆标题（包括综合馆羽毛球文字）', 'date': '紧贴框选当前已选中的目标日期卡片',
                  'rows': '紧贴框选左侧可见时段标签，避开表格与底部按钮',
                  'button': '框选底部完整“未选择预订时段”按钮', 'success': '仅框选预约成功专属标记；不得选常驻内容'}

        def done(box):
            if key == 'date':
                target = self.fields['target_date'].get().strip()
                try:
                    from datetime import date
                    date.fromisoformat(target)
                except ValueError:
                    messagebox.showerror("日期无效", "先填写正确的预约日期", parent=self)
                    return
                if not messagebox.askyesno("核对预约日期", f"框选区域中的已选日期确定是 {target} 吗？\n程序不做文字识别，需要你核对这一步。", parent=self):
                    return
                self.p.calibrated_date = target
            anchor = Anchor(box, encode_image(self.image.crop(box)))
            if key == 'success':
                self.p.success = anchor
            else:
                self.p.guards[key] = anchor
            self.update_status()
        self.picker(labels[key], False, done)

    def pick_sample(self, key):
        label = {'free_sample': '框选真实空闲格的内部；不要用灰色人形格', 'busy_sample': '框选不可用格的内部',
                 'selected_sample': '框选已选中格的内部；采样后请手动取消选择并重新截图'}[key]

        def done(box):
            setattr(self.p, key, encode_image(self.image.crop(box)))
            self.update_status()
        self.picker(label, False, done)

    def pick_point(self, key):
        def done(point):
            setattr(self.p, key, point)
            self.update_status()
        self.picker('点击页面内“刷新”文字中心' if key == 'refresh' else '点击底部提交按钮中心', True, done)

    def add_candidate(self):
        def done(box):
            label = simpledialog.askstring("目标格", "准确时段、场地号，例如：18:00–19:00 场地3", parent=self)
            if label:
                self.p.candidates.append(Candidate(label.strip(), box))
                self.update_status()
        self.picker('框选目标格内部，范围与空闲样本保持一致', False, done)

    def add_row(self):
        def done(box):
            label = simpledialog.askstring("目标时段", "这一行的准确时段，例如：18:00–19:00", parent=self)
            if not label:
                return
            step = simpledialog.askinteger("列间距", "相邻场地中心的像素距离（1360宽实测为65；缩放后需重测）", initialvalue=65, minvalue=10, maxvalue=300, parent=self)
            if step is None:
                return
            order = simpledialog.askstring("场地优先顺序", "用逗号分隔场地号；需从场地1的格子开始框选", initialvalue="1,2,3,4,5,6,7,8,9,10,11,12", parent=self)
            try:
                numbers = [int(n.strip()) for n in (order or '').split(',')]
                if not numbers or len(set(numbers)) != len(numbers) or any(n < 1 or n > 12 for n in numbers):
                    raise ValueError()
                generated = [Candidate(f"{label} 场地{n}", [box[0] + step * (n - 1), box[1],
                                                          box[2] + step * (n - 1), box[3]]) for n in numbers]
                if any(c.box[2] > self.image.width for c in generated):
                    raise ValueError()
                self.p.candidates.extend(generated)
                self.update_status()
                self.show_image()
            except ValueError:
                messagebox.showerror("设置无效", "检查场地顺序、列间距和可见范围", parent=self)
        self.picker('框选目标时段的场地1格子内部；随后生成同一行候选框', False, done)

    def show_image(self):
        if self.image is None:
            messagebox.showinfo("先截图", "尚无截图", parent=self)
            return
        from PIL import ImageDraw
        image = self.image.copy()
        draw = ImageDraw.Draw(image)
        for index, item in enumerate(self.p.candidates):
            draw.rectangle(item.box, outline="#ff354f", width=2)
            draw.text((item.box[0] + 1, item.box[1] + 1), str(index + 1), fill="#dd142e")
        ScreenshotPicker(self, image, "检查每个红框是否对应准确时段和场地；按 Esc 关闭", True, lambda _: None)

    def remove_candidate(self):
        if self.editable() and self.listbox.curselection():
            del self.p.candidates[self.listbox.curselection()[0]]
            self.update_status()

    def reorder(self, delta):
        if self.editable() and self.listbox.curselection():
            index = self.listbox.curselection()[0]
            other = index + delta
            if 0 <= other < len(self.p.candidates):
                self.p.candidates[index], self.p.candidates[other] = self.p.candidates[other], self.p.candidates[index]
                self.update_status()
                self.listbox.selection_set(other)

    def reset_calibration(self):
        if self.editable() and messagebox.askyesno("清空校准", "清空位置、样本和候选框，重新校准？", parent=self):
            self.p.size, self.p.guards, self.p.candidates = [], {}, []
            self.p.free_sample = self.p.busy_sample = self.p.selected_sample = self.p.calibrated_date = ''
            self.p.refresh, self.p.submit, self.p.success = [], [], None
            self.image = None
            self.update_status()

    def save(self):
        if not self.editable():
            return
        try:
            self.read_fields()
            save_profile(self.p, self.profile_path)
            self.log("已保存本地配置；尚未启动预约")
        except Exception as error:
            messagebox.showerror("保存失败", str(error), parent=self)

    def read_journal(self):
        if not self.journal_path.exists():
            return {}
        data = json.loads(self.journal_path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or type(data.get('pending')) is not bool:
            raise ValueError("运行记录损坏；请人工核对预约状态后检查 run-journal.json")
        return data

    def write_journal(self, stage, label, target_date):
        record = {'pending': True, 'stage': stage, 'candidate': label, 'date': target_date,
                  'at': datetime.now(CHINA).isoformat()}
        temporary = self.journal_path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.journal_path)
        self.messages.put(('log', f"运行记录：{stage} · {label}"))

    def resolve(self):
        if not self.editable():
            return
        try:
            record = self.read_journal()
            if not record.get('pending'):
                self.log("没有待核对的运行")
                return
            if not messagebox.askyesno("核对后解除保护", f"上次：{record.get('date')} {record.get('candidate')}\n状态：{record.get('stage')}\n\n确定已经检查“我的预约”和页面选择状态，并确认下一次操作不会重复预约？", parent=self):
                return
            record['pending'] = False
            record['reviewed_at'] = datetime.now(CHINA).isoformat()
            temporary = self.journal_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.journal_path)
            self.log("已记录人工核对；下次运行仍将重新校验界面")
        except Exception as error:
            messagebox.showerror("记录异常", str(error), parent=self)

    def start(self, mode):
        if not self.editable():
            return
        try:
            self.read_fields()
            self.p.validate(require_samples=mode != 'preview', require_auto=mode == 'submit')
            if mode == 'submit' and not self.p.auto_submit:
                raise ValueError("请先采集选中状态样本，并勾选启用一次自动提交")
            if mode != 'preview' and self.read_journal().get('pending'):
                raise ValueError("上次已有选场或提交操作；请在我的预约核对后使用“解除重试保护”")
            save_profile(self.p, self.profile_path)
            profile = Profile.from_dict(self.p.to_dict())
        except Exception as error:
            messagebox.showerror("尚不能运行", str(error), parent=self)
            return
        if mode == 'submit' and not messagebox.askyesno("本次自动提交", f"预约日期：{profile.target_date}\n优先候选：{profile.candidates[0].label}\n启动：{profile.trigger or '立即'}\n\n仅提交一次。确认弹窗或付款页面将交给你操作。是否启动？", parent=self):
            return
        self.busy = True
        self.stop.clear()
        for button in self.buttons:
            button.configure(state='disabled')
        self.log("5 秒后开始；请切回场馆速约，移开鼠标。运行时不要滚动、缩放或更改日期")
        self.iconify()
        threading.Thread(target=self.worker, args=(profile, mode), daemon=True).start()

    def worker(self, profile, mode):
        handle = None
        try:
            handle = self.native.mutex()
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if self.stop.wait(0.05):
                    raise StopRun("已停止")
            def interruptible_wait(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if self.native.user.GetAsyncKeyState(0x1B) & 0x8000:
                        self.stop.set()
                    if self.stop.wait(min(0.05, max(0, end - time.monotonic()))):
                        return True
                return self.stop.is_set()
            runner = Runner(profile, self.native, self.stop,
                            lambda text: self.messages.put(('log', text)),
                            lambda stage, label: self.write_journal(stage, label, profile.target_date),
                            wait=interruptible_wait)
            result = runner.run(mode)
            self.messages.put(('log', f"本次结束：{result}"))
        except Exception as error:
            self.messages.put(('log', f"已停止：{error}；如已点选/提交，请核对我的预约"))
        finally:
            if handle:
                self.native.kernel.CloseHandle(handle)
            self.messages.put(('done', ''))

    def pump(self):
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.log(value)
            elif kind == 'done':
                self.busy = False
                for button in self.buttons:
                    button.configure(state='normal')
                if self.close_after_stop:
                    self.destroy()
                    return
                self.deiconify()
                self.bell()
        self.after(100, self.pump)

    def close(self):
        if self.busy:
            self.close_after_stop = True
            self.stop.set()
            self.log("停止中；等待当前步骤结束后关闭")
        else:
            self.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只检查环境与窗口元数据；不截图、不点击')
    parser.add_argument('--smoke-ui', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('仅支持 Windows')
    user = ctypes.WinDLL('user32')
    try:
        user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        user.SetProcessDPIAware()
    if args.check:
        try:
            window = DingTalkWindow().locate(require_foreground=False)
            print(json.dumps({'ok': True, 'hwnd': window[0], 'pid': window[1], 'rect': window[2]}, ensure_ascii=False))
        except StopRun as error:
            print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
            return 1
        return 0
    app = App()
    if args.smoke_ui:
        app.after(800, app.destroy)
    app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
