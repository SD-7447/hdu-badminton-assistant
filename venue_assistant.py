"""杭电羽毛球接口助手：configuration UI and supervised command line."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from venue_api import CHINA, Config
from venue_supervisor import Supervisor, worker_main
from venue_desktop import local_config, state_directory

ROOT = Path(__file__).resolve().parent
if os.environ.get("LOCALAPPDATA") and (state_directory() / "api-profile.json").is_file():
    ROOT = state_directory()
PROFILE = ROOT / "api-profile.json"


def default_config():
    return Config.for_today()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("杭电羽毛球速约 · 接口版")
        self.geometry("1000x770")
        self.minsize(870, 650)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.closing = False
        self.config_data = local_config(PROFILE)
        self.calendar_day = datetime.now(CHINA).date()
        self.manual_date = False
        self.variables = {}
        self.controls = []
        self.status = tk.StringVar(value="就绪：先保存日期并运行只读演练")
        self._build()
        for key in ("target_date", "release_days_before", "release_override"):
            self.variables[key].trace_add("write", self._mark_manual)
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="羽毛球接口抢约", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="连续 60～120 分钟 · 10:00 起 · 时长优先，其次场地 6 / 7 / 5 / 8 / 其余").pack(anchor="w", pady=(4, 14))
        form = ttk.LabelFrame(frame, text="预约设置", padding=12)
        form.pack(fill="x")
        fields = [("target_date", "预约日期 YYYY-MM-DD"), ("release_days_before", "提前几天放号"),
                  ("release_time", "放号时间 HH:MM"), ("earliest_start", "最早开始 HH:MM"),
                  ("min_minutes", "最短连续分钟"), ("max_minutes", "最长连续分钟")]
        for i, (key, label) in enumerate(fields):
            row, column = divmod(i, 3)
            ttk.Label(form, text=label).grid(row=row * 2, column=column, sticky="w", padx=5)
            var = tk.StringVar(value=str(getattr(self.config_data, key)))
            self.variables[key] = var
            entry = ttk.Entry(form, textvariable=var, width=25)
            entry.grid(row=row * 2 + 1, column=column, sticky="ew", padx=5, pady=(3, 10))
            self.controls.append(entry)
        for c in range(3):
            form.columnconfigure(c, weight=1)
        ttk.Label(form, text="场地优先顺序（英文逗号分隔；列出的场地参与抢约）").grid(row=4, column=0, columnspan=3, sticky="w", padx=5)
        self.variables["court_priority"] = tk.StringVar(value=",".join(map(str, self.config_data.court_priority)))
        entry = ttk.Entry(form, textvariable=self.variables["court_priority"])
        entry.grid(row=5, column=0, columnspan=3, sticky="ew", padx=5, pady=(3, 10))
        self.controls.append(entry)
        ttk.Label(form, text="可选：覆盖放号时间 YYYY-MM-DD HH:MM:SS（补抢时使用；留空按日期计算）").grid(row=6, column=0, columnspan=3, sticky="w", padx=5)
        self.variables["release_override"] = tk.StringVar(value=self.config_data.release_override)
        entry = ttk.Entry(form, textvariable=self.variables["release_override"])
        entry.grid(row=7, column=0, columnspan=3, sticky="ew", padx=5, pady=(3, 10))
        self.controls.append(entry)
        self.variables["auto_refresh"] = tk.BooleanVar(value=self.config_data.auto_refresh)
        check = ttk.Checkbutton(form, text="放号前 3 分钟重启钉钉并验证登录", variable=self.variables["auto_refresh"])
        check.grid(row=8, column=0, columnspan=3, sticky="w", padx=5)
        self.controls.append(check)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=12)
        for label, callback in [("保存配置", self.save), ("只读演练", lambda: self.start("preview")),
                                ("启动定时抢约", lambda: self.start("run")), ("检查登录", lambda: self.start("refresh"))]:
            button = ttk.Button(actions, text=label, command=callback)
            button.pack(side="left", padx=(0, 8))
            self.controls.append(button)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        self.retry_button = ttk.Button(actions, text="已登录，重新验证", command=lambda: self.retry_login.set())
        self.retry_button.pack(side="right")
        self.retry_button.configure(state="disabled")
        ttk.Label(frame, textvariable=self.status, wraplength=930).pack(fill="x", pady=(0, 10))
        self.table = ttk.Treeview(frame, columns=("rank", "court", "time", "minutes"), show="headings", height=5)
        for key, label, width in [("rank", "排序", 60), ("court", "场地", 80), ("time", "连续时段", 250), ("minutes", "分钟", 100)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="center")
        self.table.pack(fill="x")
        self.output = scrolledtext.ScrolledText(frame, height=8, font=("Microsoft YaHei UI", 10), state="disabled", wrap="word")
        self.output.pack(fill="both", expand=True, pady=(10, 0))

    def _mark_manual(self, *args):
        self.manual_date = True

    def refresh_day(self):
        today = datetime.now(CHINA).date()
        if today != self.calendar_day and not self.manual_date and not (self.thread and self.thread.is_alive()):
            self.variables["target_date"].set(Config.for_today().target_date)
            self.manual_date = False
            self.calendar_day = today

    def collect_config(self):
        self.refresh_day()
        data = asdict(self.config_data)
        for key, var in self.variables.items():
            value = var.get()
            if key in ("release_days_before", "min_minutes", "max_minutes"):
                value = int(value)
            elif key == "court_priority":
                value = [int(n.strip()) for n in value.replace("，", ",").split(",") if n.strip()]
            elif isinstance(value, str):
                value = value.strip()
            data[key] = value
        return Config(**data).validate()

    def save(self):
        try:
            self.config_data = self.collect_config()
            self.config_data.save(PROFILE)
            release = datetime.fromtimestamp(self.config_data.release_epoch(), CHINA).strftime("%Y-%m-%d %H:%M:%S")
            self.status.set("配置已保存；北京时间 " + release + " 放号")
            return True
        except (ValueError, OSError) as error:
            messagebox.showerror("配置错误", str(error), parent=self)
            return False

    def start(self, mode):
        if self.thread and self.thread.is_alive() or not self.save():
            return
        self.stop_event = threading.Event()
        self.status.set({"preview": "正在读取会话并演练，不提交预约", "refresh": "正在验证场馆登录", "run": "定时任务已启动，正在检查会话"}[mode])
        self.retry_login = threading.Event()
        for control in self.controls:
            control.configure(state="disabled")
        self.stop_button.configure(state="normal")

        def run():
            supervisor = Supervisor(self.config_data, ROOT, self.events.put, self.stop_event, retry_login=self.retry_login)
            supervisor.run(mode)
            self.events.put({"event": "finished"})

        self.thread = threading.Thread(target=run, daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.status.set("正在停止并保留运行记录…")

    def _append(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", datetime.now(CHINA).strftime("%H:%M:%S") + "  " + text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _drain(self):
        self.refresh_day()
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("event")
            if event.get("phase") == "await_login":
                self.retry_button.configure(state="normal")
                if event.get("notify"):
                    self.bell()
            elif kind == "finished" or event.get("phase") in ("restarting", "manual_retry", "verified"):
                self.retry_button.configure(state="disabled")
            if kind == "finished":
                for control in self.controls:
                    control.configure(state="normal")
                self.stop_button.configure(state="disabled")
                if self.closing:
                    self.destroy()
                    return
                continue
            message = event.get("message", "")
            if kind == "log" and "rtt_ms" in event:
                message += f"；最佳往返 {event['rtt_ms']:.2f} ms，服务器时差 {event['clock_offset_ms']:.0f} ms"
            if kind == "preview":
                for item in self.table.get_children():
                    self.table.delete(item)
                for i, candidate in enumerate(event["candidates"], 1):
                    self.table.insert("", "end", values=(i, candidate["site_id"], candidate["start"] + "–" + candidate["end"], candidate["minutes"]))
                message += f"；符合条件的候选 {event['count']} 个"
                if event.get("existing_booking"):
                    message += "；该日已有本场馆预约，正式任务会直接停止"
            if kind == "result" and event.get("order"):
                order = event["order"]
                message += f"；{order.get('order_date', '')} 场地 {order.get('site_id', '')} {order.get('start_time', '')}–{order.get('end_time', '')}"
                if event.get("state") == "SUCCESS":
                    self.bell()
            if message:
                self.status.set(message)
                self._append(message)
        self.after(100, self._drain)

    def close(self):
        if self.thread and self.thread.is_alive():
            self.closing = True
            self.stop()
        else:
            self.destroy()


def main():
    parser = argparse.ArgumentParser(description="杭电羽毛球接口抢约助手")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", "--dry-run", dest="preview", action="store_true", help="只读联网演练，绝不提交预约")
    modes.add_argument("--run", action="store_true", help="按配置定时执行真实预约")
    modes.add_argument("--refresh-session", action="store_true", help="只读检查场馆登录")
    modes.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--ui-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=("run", "preview"), default="run", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.ui_check:
            app = App()
            app.withdraw()
            app.update_idletasks()
            print(json.dumps({"ui": "OK", "fields": list(app.variables), "title": app.title()}, ensure_ascii=False))
            app.destroy()
            return 0
        if args.worker or args.preview or args.run or args.refresh_session:
            if args.config is None:
                config = local_config(PROFILE)
            elif args.config.exists():
                config = Config.load(args.config)
            elif args.preview or args.refresh_session:
                config = default_config()
            else:
                raise ValueError("请先在图形界面保存 api-profile.json，再启动正式预约")
            if args.worker:
                return worker_main(config, args.state_dir, args.mode)
            def emit(event):
                print(json.dumps(event, ensure_ascii=False), flush=True)
            mode = "preview" if args.preview else "refresh" if args.refresh_session else "run"
            result = Supervisor(config, args.state_dir, emit).run(mode)
            return 1 if result in ("ERROR", "UNKNOWN") else 0
        App().mainloop()
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
