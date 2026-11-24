"""Local desktop bridge. JSON lines over inherited pipes, with no listening port."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import sys
import threading
import time

from venue_api import CHINA, Config, atomic_json
from venue_engine import Journal, PENDING
from venue_session import open_login
from venue_notice import fetch_notice
from venue_supervisor import RunLock, Supervisor, worker_main

PROFILE = "api-profile.json"
JOURNAL = "run-api-journal.json"
FIELDS = {
    "target_date", "earliest_start", "min_minutes", "max_minutes", "court_priority",
    "release_days_before", "release_time", "release_override", "auto_refresh",
}


def state_directory():
    return Path(os.environ["LOCALAPPDATA"]) / "HDU Badminton Assistant"


def local_config(path, now=None):
    return Config.for_today(Config.load(path) if path.is_file() else None, now)


def migrate(source, target):
    """Copy recovery before preferences under the lock used by both engines."""
    with RunLock(target):
        target.mkdir(parents=True, exist_ok=True)
        moved = []
        source_profile, target_profile = source / PROFILE, target / PROFILE
        source_journal, target_journal = source / JOURNAL, target / JOURNAL
        profile = Config.load(source_profile) if source_profile.is_file() and not target_profile.exists() else None
        if source_journal.is_file():
            journal = Journal(source_journal).data
            if target_journal.exists():
                current = Journal(target_journal).data
                if journal.get("state") in PENDING and journal != current:
                    raise RuntimeError("新版已有不同运行记录，请先核对旧版待确认的预约")
            else:
                atomic_json(target_journal, journal)
                moved.append(JOURNAL)
        if profile:
            profile.save(target_profile)
            moved.append(PROFILE)
        return moved


class Bridge:
    def __init__(self, root):
        self.root = root
        self.task = None
        self.stop = None
        self.retry_login = None
        self.restart_login = None
        self.output_lock = threading.Lock()
        self.notice_path = root / "venue-notice.json"
        self.notice_lock = threading.Lock()
        self.display_config = None
        self.active_config = None
        self.clock_offset = 0

    def cached_notice(self):
        try:
            data = json.loads(self.notice_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("body"), str) and isinstance(data.get("synced"), str):
                datetime.fromisoformat(data["synced"])
                return data
        except (OSError, ValueError):
            pass
        return None

    def notice_delay(self):
        config = self.active_config or self.display_config
        if config:
            delta = time.time() + self.clock_offset - config.release_epoch()
            if -60 <= delta < config.booking_window_seconds:
                return int((config.booking_window_seconds - delta) * 1000) + 1
        return 0

    def refresh_notice(self):
        try:
            delay = self.notice_delay()
            if delay:
                event = {"deferMs": delay}
            else:
                data = fetch_notice()
                atomic_json(self.notice_path, data)
                event = {"notice": data}
        except (OSError, ValueError, http.client.HTTPException):
            event = {"failed": True}
        finally:
            self.notice_lock.release()
        self.send({"event": {"event": "venue_notice", **event}})

    def send(self, message):
        with self.output_lock:
            print(json.dumps(message, ensure_ascii=False), flush=True)

    def status(self, config):
        journal = Journal.for_date(self.root, config.target_date).data
        return {"config": {key: value for key, value in asdict(config).items() if key in FIELDS},
                "releaseEpoch": round(config.release_epoch() * 1000),
                "windowMs": round(config.booking_window_seconds * 1000),
                "today": datetime.now(CHINA).date().isoformat(),
                "recovery": {"state": journal.get("state"), "date": journal.get("target_date")}
                if journal.get("state") in PENDING else None}

    def run_task(self, config, mode):
        def emit(event):
            if "clock_offset_ms" in event:
                self.clock_offset = event["clock_offset_ms"] / 1000
            self.send({"event": event})
        try:
            result = Supervisor(config, self.root, emit, stop_event=self.stop, retry_login=self.retry_login,
                                restart_login=self.restart_login).run(mode)
        except Exception:
            result = "ERROR"
            emit({"event": "result", "state": result, "message": "后台意外退出，请核对运行记录"})
        try:
            recovery = self.status(config)["recovery"]
        except RuntimeError:
            recovery = {"state": "UNKNOWN", "date": config.target_date}
        # Reset the task after finalizing the supervisor and before accepting new commands.
        self.active_config = None
        self.task = None
        emit({"event": "finished", "state": result, "recovery": recovery})

    def dispatch(self, request):
        action = request.get("action")
        busy = self.task is not None
        if action == "restartDingTalk":
            active = self.active_config
            if not busy or active is None or self.restart_login is None:
                raise ValueError("当前没有正在等待的定时任务")
            if time.time() + self.clock_offset >= active.release_epoch() - 60:
                raise ValueError("已进入放号前 1 分钟，请在本轮结束后重启钉钉")
            self.restart_login.set()
            return {}, None
        if action == "openVenueLogin":
            active = self.active_config
            if active and time.time() + self.clock_offset >= active.release_epoch() - 60:
                raise ValueError("已进入放号前 1 分钟，请在本轮结束后重新打开场馆登录")
            open_login()
            return {}, None
        if action == "retryLogin":
            if not busy or self.retry_login is None:
                raise ValueError("当前没有等待登录的任务")
            self.retry_login.set()
            return {}, None
        if action == "stop":
            if busy:
                self.stop.set()
            return {}, None
        if action == "notice":
            delay = self.notice_delay()
            if delay or not self.notice_lock.acquire(blocking=False):
                return {"deferMs": delay}, None
            return {}, threading.Thread(target=self.refresh_notice, daemon=True).start
        config = local_config(self.root / PROFILE)
        if action == "load":
            self.display_config = config
            return {**self.status(config), "notice": self.cached_notice()}, None
        if action not in ("save", "start", "schedule") or busy:
            raise ValueError("请先停止当前任务" if busy else "未知操作")
        changes = request.get("config")
        if not isinstance(changes, dict) or set(changes) != FIELDS:
            raise ValueError("配置字段不完整")
        config = Config(**{**asdict(config), **changes}).validate()
        self.display_config = config
        if action == "schedule":
            return self.status(config), None
        if action == "save":
            config.save(self.root / PROFILE)
            return self.status(config), None
        mode = request.get("mode")
        if mode not in ("preview", "run", "refresh", "restart"):
            raise ValueError("未知任务类型")
        info = self.status(config)
        config.save(self.root / PROFILE)
        self.stop = threading.Event()
        self.retry_login = threading.Event()
        self.restart_login = threading.Event()
        self.clock_offset = 0
        self.active_config = config if mode == "run" else None
        self.task = threading.Thread(target=self.run_task, args=(config, mode))
        return info, self.task.start

    def serve(self):
        try:
            for line in sys.stdin:
                request = {}
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("无效请求")
                    result, after_reply = self.dispatch(request)
                    self.send({"id": request.get("id"), "ok": True, "data": result})
                    if after_reply:
                        after_reply()
                except (ValueError, OSError, TypeError, RuntimeError) as error:
                    self.send({"id": request.get("id") if isinstance(request, dict) else None,
                               "ok": False, "error": str(error)})
        finally:
            task = self.task
            if task:
                self.stop.set()
                task.join()


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bridge", action="store_true")
    mode.add_argument("--worker", action="store_true")
    mode.add_argument("--migrate", type=Path)
    parser.add_argument("--state-dir", type=Path, default=state_directory())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--mode", choices=("preview", "run"), default="run")
    args = parser.parse_args()
    if args.worker:
        return worker_main(Config.load(args.config), args.state_dir, args.mode)
    if args.migrate:
        print(json.dumps({"copied": migrate(args.migrate, args.state_dir)}))
    else:
        Bridge(args.state_dir).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
