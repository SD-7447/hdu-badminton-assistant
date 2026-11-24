"""Keep the UI alive while workers restart after normal DingTalk SSO renewal."""
from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
import ctypes
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from venue_api import ApiError, CHINA, Config, VenueApi
from venue_engine import (BookingEngine, Journal, NeedAuth, PAGE_CHECK_CUTOFF_SECONDS, PAGE_CHECK_LEAD_SECONDS,
                          PENDING, Stopped, account_tag)
from venue_session import DingTalkSessionSource, SessionError, SessionRefresher, SessionRefreshTimeout, open_login

AUTH_EXIT = 75


class LoginWindowEnded(Exception):
    pass


class TimerResolution(AbstractContextManager):
    """Request 1 ms Windows waits for this worker; restore when it exits."""
    def __enter__(self):
        self.active = False
        if os.name == "nt":
            self.winmm = ctypes.WinDLL("winmm")
            self.active = self.winmm.timeBeginPeriod(1) == 0
        return self

    def __exit__(self, *args):
        if self.active:
            self.winmm.timeEndPeriod(1)


class RunLock(AbstractContextManager):
    """One supervisor per machine, including copies in other directories."""
    def __init__(self, root):
        self.root, self.handle = Path(root), None
        self.file = None

    def __enter__(self):
        if os.name == "nt":
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            ctypes.set_last_error(0)
            self.handle = self.kernel.CreateMutexW(None, False, "Local\\HDUVenueQuickBookApiV2")
            if not self.handle:
                raise RuntimeError("无法建立抢约管理锁")
            if ctypes.get_last_error() == 183:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
                raise RuntimeError("已有接口助手运行，请先停止另一个任务")
        else:
            import fcntl
            self.root.mkdir(parents=True, exist_ok=True)
            self.file = (self.root / ".venue-api.lock").open("a+")
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                self.file = None
                raise RuntimeError("已有接口助手运行") from None
        return self

    def __exit__(self, *args):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.file:
            self.file.close()


class Supervisor:
    def __init__(self, config, root, emit, stop_event=None, source=None, refresher=None, retry_login=None,
                 restart_login=None):
        self.config, self.root, self.publish = config, Path(root), emit
        self.stop_event = stop_event or threading.Event()
        self.retry_login = retry_login or threading.Event()
        self.restart_login = restart_login or threading.Event()
        self.source, self.refresher = source, refresher
        self.child = None
        self.clock_offset = 0
        self.login_attempted = False
        self.restart_attempted = False
        self.rejected_tokens = set()

    def emit(self, event):
        self.publish(event)
        if "clock_offset_ms" in event:
            self.clock_offset = event["clock_offset_ms"] / 1000
        # Only fixed protocol labels go to disk. Messages and response data may contain PII.
        record = {"event": event["event"]}
        allowed = {"phase": {"waiting", "ready", "booking", "renewing", "restarting", "verified", "unverified",
                             "await_login", "prepare", "commit", "cleanup", "manual_retry"},
                   "reason": {"AUTH", "ACCOUNT_CHANGED", "RATE", "UNCERTAIN", "SCHEMA", "LIMIT",
                              "NO_SESSION", "SESSION_CHANGED", "PAGE_CHECK", "MANUAL_RESTART"},
                   "state": {"SUCCESS", "UNKNOWN", "EXPIRED", "NO_AVAILABILITY", "STOPPED", "ERROR",
                             "VERIFIED"}}
        for key, labels in allowed.items():
            if event.get(key) in labels:
                record[key] = event[key]
        if len(record) == 1 and event["event"] != "auth_required":
            return
        record["time"] = datetime.now(CHINA).isoformat(timespec="seconds")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "login-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            if not getattr(self, "warned_log_failure", False):
                self.warned_log_failure = True
                self.publish({"event": "log", "message": "登录诊断文件写入失败，请查看窗口运行日志"})

    def read_session(self):
        try:
            self.source = self.source or DingTalkSessionSource(self.config.storage_path)
            return self.source.read()
        except SessionError:
            return None

    def verify_session(self, session):
        if self.stop_event.is_set():
            raise Stopped("已停止")
        api = VenueApi.connect(session, self.config)
        try:
            api.orders(session)
        except ApiError as error:
            if error.kind == "AUTH":
                self.rejected_tokens.add(session.token)
            raise
        finally:
            api.close()
        self.emit({"event": "login_status", "phase": "verified", "verified_at": time.time(),
                   "message": "场馆已确认登录"})

    def restart_due(self):
        remaining = self.config.release_epoch() - time.time() - self.clock_offset
        return (self.config.auto_refresh and not self.restart_attempted
                and PAGE_CHECK_CUTOFF_SECONDS < remaining <= PAGE_CHECK_LEAD_SECONDS)

    def wait_for_login(self, expected, reason, retry_after=0):
        end = self.config.release_epoch() + self.config.booking_window_seconds
        self.retry_login.clear()
        self.log("请在钉钉完成原账号登录并打开场馆速约。助手会检测新会话，也可点击「已登录，重新验证」。",
                 phase="await_login", reason=reason, notify=True)
        if retry_after:
            self.stop_event.wait(max(0, min(end - time.time() - self.clock_offset, retry_after)))
        while not self.stop_event.is_set():
            remaining = end - (time.time() + self.clock_offset)
            if remaining <= 0:
                raise LoginWindowEnded()
            manual_check = self.retry_login.is_set()
            if manual_check:
                self.retry_login.clear()
                self.log("正在验证用户刚完成的登录", phase="manual_retry")
            session = self.read_session()
            manual_restart = self.restart_login.is_set()
            self.restart_login.clear()
            matching_account = session is None or expected is None or account_tag(session) == expected
            if manual_restart or (matching_account and self.restart_due()):
                if remaining > self.config.booking_window_seconds + PAGE_CHECK_CUTOFF_SECONDS:
                    previous = session if matching_account else None
                    restart_reason = "MANUAL_RESTART" if manual_restart else "PAGE_CHECK"
                    try:
                        session = self.refresh(previous, time.time(), restart_reason)
                    except SessionError:
                        self.log("重启验证未完成，继续等待原账号登录", phase="await_login", reason=restart_reason)
            delay = 5
            if session is None:
                reason = "NO_SESSION"
            elif expected is not None and account_tag(session) != expected:
                reason = "ACCOUNT_CHANGED"
            elif manual_check or session.token not in self.rejected_tokens:
                try:
                    self.verify_session(session)
                except ApiError as error:
                    if error.kind not in ("AUTH", "RATE", "UNCERTAIN"):
                        raise
                    reason = error.kind
                    delay = max(delay, error.retry_after)
                else:
                    if time.time() + self.clock_offset >= end:
                        raise LoginWindowEnded()
                    self.log("原账号已通过场馆验证，正在核对原任务", phase="restarting")
                    return session
            if manual_check:
                message = "当前账号已变化，请切回原账号" if reason == "ACCOUNT_CHANGED" else "场馆尚未确认登录，请在钉钉检查场馆页面"
                self.log(message, phase="await_login", reason=reason)
            self.stop_event.wait(max(0, min(end - time.time() - self.clock_offset, delay)))
        raise Stopped("已停止")

    def log(self, message, **data):
        self.emit({"event": "log", "message": message, **data})

    def refresh(self, session, required_until, reason="AUTH"):
        if self.stop_event.is_set():
            raise Stopped("已停止")
        self.login_attempted = True
        restart = reason in ("PAGE_CHECK", "MANUAL_RESTART")
        if restart and (reason == "PAGE_CHECK" or self.config.release_epoch() - time.time() - self.clock_offset <= PAGE_CHECK_LEAD_SECONDS + 120):
            self.restart_attempted = True
        opener = partial(open_login, restart=True) if restart else open_login
        refresher = self.refresher or SessionRefresher(self.source, opener=opener,
                                                     timeout=self.config.refresh_timeout_seconds, attempts=1)
        message = "正在重启钉钉并打开场馆登录页面" if restart else "打开新的钉钉场馆页面验证登录"
        self.log(message, phase="renewing", reason=reason)
        return refresher.refresh(session, required_until, self.stop_event.is_set)

    def page_check_before_release(self, session, expected, required_until, reason="PAGE_CHECK"):
        remaining = self.config.release_epoch() - (time.time() + self.clock_offset)
        if remaining <= PAGE_CHECK_CUTOFF_SECONDS:
            if reason == "PAGE_CHECK":
                self.restart_attempted = True
            return session
        try:
            fresh = self.refresh(session, required_until, reason)
        except SessionError:
            self.log("重启登录未取得新会话，重新核对当前登录", phase="restarting", reason=reason)
            return self.restore_login(session, expected, required_until, reason)
        if account_tag(fresh) != expected:
            return self.wait_for_login(expected, "ACCOUNT_CHANGED")
        self.log("钉钉重启后已取得会话，正在核验预约接口", phase="restarting", reason=reason)
        return fresh

    def restore_login(self, session, expected, required_until, reason):
        if reason == "AUTH":
            self.rejected_tokens.add(session.token)
        if time.time() + self.clock_offset >= self.config.release_epoch() + self.config.booking_window_seconds:
            raise LoginWindowEnded()
        current = self.read_session()
        if current is not None and account_tag(current) != expected:
            return self.wait_for_login(expected, "ACCOUNT_CHANGED")
        if current is not None and current.token not in self.rejected_tokens:
            try:
                self.verify_session(current)
            except ApiError as error:
                if error.kind not in ("AUTH", "RATE", "UNCERTAIN"):
                    raise
                if error.kind != "AUTH":
                    return self.wait_for_login(expected, error.kind, max(5, error.retry_after))
                reason = error.kind
            else:
                self.log("已使用钉钉当前登录继续原任务", phase="restarting")
                return current
        if reason == "AUTH" and self.config.auto_refresh and (not self.login_attempted or self.restart_due()):
            refresh_reason = "PAGE_CHECK" if self.restart_due() else "AUTH"
            try:
                candidate = self.refresh(current or session, required_until, refresh_reason)
            except SessionError:
                pass
            else:
                if account_tag(candidate) == expected and candidate.token not in self.rejected_tokens:
                    self.log("已取得新会话，正在验证原任务", phase="restarting")
                    return candidate
        return self.wait_for_login(expected, reason)

    def check_login(self, session):
        if session is None:
            raise SessionError("请先在钉钉登录并打开场馆速约，再检查登录")
        try:
            self.verify_session(session)
        except ApiError as error:
            if error.kind == "AUTH":
                raise SessionError("场馆登录已失效，请在钉钉重新打开场馆速约") from None
            raise

    def run(self, mode="run"):
        try:
            self.config.validate()
            with RunLock(self.root):
                session = self.read_session()
                if mode in ("refresh", "restart"):
                    if mode == "restart":
                        original = session
                        try:
                            session = self.refresh(session, time.time(), "MANUAL_RESTART")
                        except SessionRefreshTimeout:
                            session = self.read_session()
                        if session is not None and original is not None and account_tag(session) != account_tag(original):
                            raise SessionError("钉钉账号已变化，请切回原账号")
                    self.check_login(session)
                    message = "钉钉已重启，场馆登录核验通过" if mode == "restart" else "当前场馆登录有效，可启动定时抢约"
                    self.emit({"event": "result", "state": "VERIFIED", "message": message})
                    return "VERIFIED"
                prior = Journal.for_date(self.root, self.config.target_date).data if mode == "run" else {}
                expected = prior.get("account") if prior.get("state") in PENDING else None
                if session is None:
                    if mode == "preview":
                        raise SessionError("请先在钉钉登录并打开场馆速约，再运行只读演练")
                    if self.config.auto_refresh and time.time() < self.config.release_epoch() - PAGE_CHECK_CUTOFF_SECONDS:
                        try:
                            reason = "PAGE_CHECK" if self.restart_due() else "NO_SESSION"
                            session = self.refresh(None, time.time(), reason)
                        except SessionError:
                            pass
                    if session is None:
                        session = self.wait_for_login(expected, "NO_SESSION")
                if expected and account_tag(session) != expected:
                    session = self.wait_for_login(expected, "ACCOUNT_CHANGED")
                expected = account_tag(session)
                # This is a non-secret immutable task snapshot shared with restarted workers.
                snapshot = self.root / "run-api-config.json"
                self.config.save(snapshot)
                crash_restarts = 0
                while not self.stop_event.is_set():
                    env = os.environ.copy()
                    env["PYTHONUTF8"] = "1"
                    env["HDU_VENUE_WORKER"] = "1"
                    env["HDU_VENUE_ACCOUNT"] = expected
                    env["HDU_VENUE_TOKEN_TAG"] = hashlib.sha256(session.token.encode()).hexdigest()
                    env["HDU_VENUE_PAGE_CHECK"] = "1" if self.config.auto_refresh and not self.restart_attempted else "0"
                    executable = Path(sys.executable)
                    if executable.name.lower() == "pythonw.exe":
                        executable = executable.with_name("python.exe")
                    command = [str(executable)]
                    if not getattr(sys, "frozen", False):
                        entry = Path(sys.argv[0]).name
                        if entry != "venue_desktop.py":
                            entry = "venue_assistant.py"
                        command.extend(["-u", str(Path(__file__).with_name(entry))])
                    command.extend(["--worker", "--config", str(snapshot), "--mode", mode,
                                    "--state-dir", str(self.root)])
                    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                  text=True, encoding="utf-8", env=env, creationflags=flags, bufsize=1)
                    messages = queue.Queue()

                    def collect(stream, kind):
                        try:
                            for line in stream:
                                messages.put((kind, line))
                        finally:
                            stream.close()

                    readers = [threading.Thread(target=collect, args=(self.child.stdout, "stdout"), daemon=True),
                               threading.Thread(target=collect, args=(self.child.stderr, "stderr"), daemon=True)]
                    for thread in readers:
                        thread.start()
                    required_until = time.time() + 180
                    last_result = None
                    auth_reason = "AUTH"
                    stop_sent = None
                    while True:
                        if self.restart_login.is_set() and not self.stop_event.is_set():
                            self.restart_login.clear()
                            remaining = self.config.release_epoch() - (time.time() + self.clock_offset)
                            if remaining > PAGE_CHECK_CUTOFF_SECONDS:
                                try:
                                    self.child.stdin.write("restart-login\n")
                                    self.child.stdin.flush()
                                except (OSError, ValueError):
                                    self.restart_login.set()
                        if self.stop_event.is_set() and stop_sent is None:
                            stop_sent = time.perf_counter()
                            try:
                                self.child.stdin.write("stop\n")
                                self.child.stdin.flush()
                            except (OSError, ValueError):
                                pass
                        if stop_sent is not None and self.child.poll() is None and time.perf_counter() - stop_sent > 10:
                            self.child.terminate()  # pending writes were journaled before dispatch
                        try:
                            kind, line = messages.get(timeout=0.1)
                        except queue.Empty:
                            if self.child.poll() is not None and all(not t.is_alive() for t in readers) and messages.empty():
                                break
                            continue
                        if kind == "stderr":
                            # Never forward raw tracebacks or library responses containing secrets.
                            self.log("执行进程报告内部异常，将依据运行记录核对结果")
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("event") == "auth_required":
                            required_until = float(event["required_until"])
                            auth_reason = event.get("reason", "AUTH")
                        if event.get("event") in ("result", "preview"):
                            last_result = event.get("state", "PREVIEW")
                        self.emit(event)
                    code = self.child.wait()
                    self.child.stdin.close()
                    self.child = None
                    if self.stop_event.is_set():
                        self.emit({"event": "result", "state": "STOPPED", "message": "已停止；如有待核对操作，记录已保留"})
                        return "STOPPED"
                    if code == AUTH_EXIT:
                        if mode == "preview":
                            raise SessionError("只读演练无法确认登录，请点击「检查登录」")
                        if auth_reason in ("PAGE_CHECK", "MANUAL_RESTART"):
                            session = self.page_check_before_release(session, expected, required_until, auth_reason)
                        else:
                            session = self.restore_login(session, expected, required_until, auth_reason)
                        continue
                    if code != 0 and last_result is None and mode == "run" and crash_restarts < 1:
                        crash_restarts += 1
                        self.log("执行进程意外退出，重启一次并优先核对运行记录")
                        continue
                    return last_result or ("ERROR" if code else "DONE")
        except LoginWindowEnded:
            self.emit({"event": "result", "state": "EXPIRED", "message": "等待登录期间放号窗口已结束，请核对已有预约"})
            return "EXPIRED"
        except (Stopped, KeyboardInterrupt):
            self.stop_event.set()
            self.emit({"event": "result", "state": "STOPPED", "message": "已停止，恢复记录已保留"})
            return "STOPPED"
        except (SessionError, RuntimeError, ValueError, OSError, ApiError) as error:
            # These are our own sanitized operational errors, never raw HTTP bodies.
            self.emit({"event": "result", "state": "ERROR", "message": str(error)})
            return "ERROR"
        finally:
            if self.child is not None:
                try:
                    self.child.stdin.close()
                    self.child.wait(timeout=10)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    self.child.terminate()
                    self.child.wait()
                self.child = None


def worker_main(config, root, mode):
    stop = threading.Event()
    restart_requested = threading.Event()

    def monitor_parent():
        try:
            for line in sys.stdin:
                if line.strip() == "stop":
                    stop.set()
                    return
                if line.strip() == "restart-login":
                    restart_requested.set()
        finally:
            stop.set()  # closing the supervisor must stop an orphaned worker

    threading.Thread(target=monitor_parent, daemon=True).start()

    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    api = engine = journal = None
    try:
        if os.environ.get("HDU_VENUE_WORKER") != "1":
            raise RuntimeError("执行进程只能由管理器启动")
        try:
            source = DingTalkSessionSource(config.storage_path)
            session = source.read()
        except SessionError:
            emit({"event": "auth_required", "reason": "NO_SESSION", "required_until": time.time() + 180,
                  "message": "场馆登录暂时不可用"})
            return AUTH_EXIT
        if account_tag(session) != os.environ.get("HDU_VENUE_ACCOUNT"):
            emit({"event": "auth_required", "reason": "ACCOUNT_CHANGED", "required_until": time.time() + 180,
                  "message": "执行进程检测到账号变化，已暂停预约"})
            return AUTH_EXIT
        if hashlib.sha256(session.token.encode()).hexdigest() != os.environ.get("HDU_VENUE_TOKEN_TAG"):
            emit({"event": "auth_required", "reason": "SESSION_CHANGED", "required_until": time.time() + 60,
                  "message": "检测到钉钉会话变化，正在重新验证"})
            return AUTH_EXIT
        api = VenueApi.connect(session, config)
        journal = Journal.for_date(root, config.target_date)
        engine = BookingEngine(config, session, api, journal, emit, stop, read_session=source.read,
                               page_check=os.environ.get("HDU_VENUE_PAGE_CHECK") == "1",
                               restart_requested=restart_requested)
        with TimerResolution():
            engine.run(dry=mode == "preview")
        return 0
    except NeedAuth as error:
        emit({"event": "auth_required", "reason": error.reason, "required_until": error.required_until,
              "message": "场馆登录需要重新验证"})
        return AUTH_EXIT
    except Stopped:
        emit({"event": "result", "state": "STOPPED", "message": "执行进程已停止，恢复记录已保留"})
        return 0
    except ApiError as error:
        if error.kind == "AUTH":
            emit({"event": "auth_required", "reason": "AUTH", "required_until": time.time() + 60, "message": "场馆登录验证失败"})
            return AUTH_EXIT
        if journal is not None and journal.data.get("state") in PENDING and engine is not None:
            try:
                engine.reconcile()
                return 0
            except NeedAuth as required:
                emit({"event": "auth_required", "reason": required.reason, "required_until": required.required_until})
                return AUTH_EXIT
        emit({"event": "result", "state": "ERROR", "message": str(error)})
        return 1
    except (SessionError, ValueError, RuntimeError) as error:
        emit({"event": "result", "state": "ERROR", "message": str(error)})
        return 1
    except Exception:
        emit({"event": "result", "state": "ERROR", "message": "执行进程内部异常；已保留运行记录，请先核对订单"})
        return 1
    finally:
        if engine is not None and engine.poller is not None:
            engine.poller.stop()
        if api is not None:
            api.close()
