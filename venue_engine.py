"""Crash-safe two-phase booking worker. No desktop clicks are used."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import threading
import time

from venue_api import ApiError, Candidate, CHINA, Config, ServerClock, Snapshot, atomic_json, minute, sort_key
from venue_session import MissingSession, SessionError

PENDING = {"PREPARE_SENT", "PREPARED", "COMMIT_SENT", "CLEANUP_SENT", "UNKNOWN"}
LOGIN_CHECK_INTERVAL = 15 * 60
PAGE_CHECK_LEAD_SECONDS = 3 * 60
PAGE_CHECK_CUTOFF_SECONDS = 60


class Stopped(RuntimeError):
    pass


class NeedAuth(RuntimeError):
    def __init__(self, required_until, reason="AUTH"):
        super().__init__("需要钉钉免登续期")
        self.required_until = required_until
        self.reason = reason


def account_tag(session):
    return hashlib.sha256(session.openid.encode()).hexdigest()


def policy_tag(config):
    settings = asdict(config)
    for key in ("storage_path", "auto_refresh", "refresh_attempts", "refresh_timeout_seconds"):
        settings.pop(key, None)
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def order_summary(order):
    fields = ("order_num", "order_date", "venue_name", "site_id", "start_time", "end_time", "with_friends")
    return {key: order[key] for key in fields if key in order}


def same_day_venue(order, config):
    return str(order.get("order_date", order.get("date", "")))[:10] == config.target_date and order.get("venue_name") == config.venue_name


def candidate_from(data):
    return Candidate(int(data["site_id"]), tuple(data["indices"]), data["start"], data["end"],
                     int(data["minutes"]), Decimal(data["price"]))


class Journal:
    @classmethod
    def for_date(cls, root, target_date):
        day = date.fromisoformat(target_date).isoformat()
        path = Path(root) / "journals" / (day + ".json")
        journal = cls(path)
        if not path.exists():
            legacy = cls(Path(root) / "run-api-journal.json")
            if legacy.data.get("state") in PENDING and not legacy.data.get("target_date"):
                raise RuntimeError("旧预约记录缺少日期，请先核对订单")
            if legacy.data.get("target_date") == day:
                journal.data = legacy.data
        if journal.data and journal.data.get("target_date") != day:
            raise RuntimeError("恢复记录与所选日期不符，请核对订单")
        return journal

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if self.data.get("version") != 2 or not isinstance(self.data.get("state"), str):
                    raise ValueError()
            except (OSError, ValueError, TypeError, AttributeError):
                raise RuntimeError("运行记录损坏；请核对我的预约并保留记录，不能自动重试") from None

    def bind(self, config, session, baseline):
        same_task = (self.data.get("target_date") == config.target_date and self.data.get("venue_name") == config.venue_name
                     and self.data.get("account") == account_tag(session))
        if self.data.get("state") in PENDING:
            if not same_task or self.data.get("policy") != policy_tag(config):
                raise RuntimeError("仍有未核对的预约操作，不能切换账户、日期或配置后继续写入")
            return
        if same_task and self.data.get("state") == "SUCCESS":
            return
        self.data = {"version": 2, "state": "READY", "target_date": config.target_date,
                     "venue_name": config.venue_name, "account": account_tag(session), "policy": policy_tag(config),
                     "baseline": [str(o["order_num"]) for o in baseline if "order_num" in o]}
        self.save("READY")

    def save(self, state, candidate=None, **extra):
        self.data["state"] = state
        self.data["updated_at"] = datetime.now(CHINA).isoformat()
        if candidate is not None:
            self.data["candidate"] = candidate.summary()
        self.data.update(extra)
        atomic_json(self.path, self.data)


class Poller:
    def __init__(self, api, config, metadata, clock, initial):
        self.api, self.config, self.metadata, self.clock = api, config, metadata, clock
        self.latest = initial
        self.error = None
        self.revision = 0
        self.updated = time.perf_counter()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="venue-availability")

    def start(self):
        self.thread.start()

    def view(self):
        with self.lock:
            return self.latest, self.error, self.revision, self.updated

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(self.config.request_timeout_seconds + 1)

    def _run(self):
        while not self.stop_event.is_set():
            began = time.perf_counter()
            delta = self.clock.now() - self.config.release_epoch()
            interval = (self.config.fast_poll_seconds if -self.config.fast_window_seconds <= delta < 3
                        else self.config.normal_poll_seconds if delta < 0 else self.config.late_poll_seconds)
            try:
                current = Snapshot(self.metadata, self.api.situation(self.config))
                with self.lock:
                    self.latest, self.error = current, None
                    self.revision += 1
                    self.updated = time.perf_counter()
            except ApiError as error:
                with self.lock:
                    self.error = error
                if error.kind in ("AUTH", "LIMIT", "SCHEMA"):
                    return
                interval = max(interval, error.retry_after if error.kind == "RATE" else 0.5)
            self.stop_event.wait(max(0.001, interval - (time.perf_counter() - began)))


class BookingEngine:
    def __init__(self, config, session, api, journal, emit=lambda event: None,
                 stop_event=None, clock=None, read_session=None, page_check=False, restart_requested=None):
        self.config, self.session, self.api, self.journal = config, session, api, journal
        self.emit = emit
        self.stop_event = stop_event or threading.Event()
        self.clock = clock or ServerClock()
        self.last_verified = None
        self.read_session = read_session
        self.page_check = page_check
        self.restart_requested = restart_requested
        self.poller = None
        self.snapshot = None
        self.blocked = {}
        self.own_orders = []

    def log(self, message, **extra):
        self.emit({"event": "log", "message": message, **extra})

    def check_stop(self):
        if self.stop_event.is_set():
            raise Stopped("已停止")

    def wait(self, seconds):
        if self.stop_event.wait(max(0, seconds)):
            raise Stopped("已停止")

    def wait_until(self, epoch):
        deadline = self.clock.deadline(epoch)
        while True:
            self.check_stop()
            if self.restart_requested is not None and self.restart_requested.is_set():
                self.restart_requested.clear()
                if self.config.release_epoch() - self.clock.now() > PAGE_CHECK_CUTOFF_SECONDS:
                    raise NeedAuth(self.end_epoch + 60, "MANUAL_RESTART")
            left = deadline - time.perf_counter()
            if left <= 0:
                return
            if left < 0.002:
                # Only the final 2 ms spin; Windows Python 3.9 sleep can otherwise
                # overshoot the release tick. perf_counter is monotonic and high-res.
                continue
            self.wait(min(max(0.0001, left - 0.002), 0.05 if left < 1 else 0.5))

    def auth_requirement(self):
        return max(self.clock.now() + 60, min(self.config.release_epoch(), self.clock.now() + 120)
                   + self.config.booking_window_seconds + 60)

    def check_connection(self, action, dry, now):
        while True:
            self.check_stop()
            try:
                return action()
            except ApiError as error:
                if error.kind not in ("RATE", "UNCERTAIN"):
                    raise
                self.emit({"event": "login_status", "phase": "unverified", "reason": error.kind,
                           "message": "暂时无法确认登录状态，等待网络恢复"})
                remaining = self.end_epoch - now()
                if dry or remaining <= 0:
                    raise
                self.wait(min(remaining, max(5, error.retry_after)))

    def verify_login(self, dry=False):
        if self.read_session is not None:
            try:
                current = self.read_session()
            except MissingSession:
                raise NeedAuth(self.auth_requirement(), "NO_SESSION") from None
            except SessionError:
                # A LevelDB rotation can briefly make the local snapshot unreadable.
                # The server check below can still verify the current session.
                current = None
            if current is not None:
                if current.openid != self.session.openid:
                    raise NeedAuth(self.auth_requirement(), "ACCOUNT_CHANGED")
                if current.token != self.session.token:
                    raise NeedAuth(self.auth_requirement(), "SESSION_CHANGED")
        self.own_orders = self.check_connection(self.orders, dry, self.clock.now)
        self.last_verified = self.clock.now()
        self.emit({"event": "login_status", "phase": "verified", "verified_at": self.last_verified,
                   "message": "场馆已确认登录"})

    def next_login_check(self, until):
        release = self.config.release_epoch()
        checkpoints = (release - 600, release - PAGE_CHECK_LEAD_SECONDS, release - 120, release - 60,
                       release - self.config.preflight_seconds, until)
        checkpoint = min(t for t in checkpoints if self.last_verified < t <= until)
        periodic = self.last_verified + LOGIN_CHECK_INTERVAL
        expiry = self.session.expires_at
        # Verify when the advertised expiry arrives, without treating it as server rejection.
        if expiry is not None and expiry > self.last_verified:
            periodic = min(periodic, expiry)
        return periodic if periodic < checkpoint - 30 else checkpoint

    def request_page_check(self):
        remaining = self.config.release_epoch() - self.clock.now()
        if self.page_check and PAGE_CHECK_CUTOFF_SECONDS < remaining <= PAGE_CHECK_LEAD_SECONDS:
            raise NeedAuth(self.end_epoch + 60, "PAGE_CHECK")

    def monitor_login_until_warmup(self):
        warmup = self.config.release_epoch() - self.config.warmup_seconds
        while self.last_verified < warmup:
            self.wait_until(self.next_login_check(warmup))
            self.verify_login()
            self.request_page_check()

    def orders(self):
        try:
            return self.read_retry(lambda: self.api.orders(self.session))
        except ApiError as error:
            if error.kind == "AUTH":
                raise NeedAuth(self.auth_requirement()) from None
            raise

    def read_retry(self, action):
        for attempt in range(3):
            self.check_stop()
            try:
                return action()
            except ApiError as error:
                if error.kind not in ("RATE", "UNCERTAIN") or attempt == 2:
                    raise
                self.wait(error.retry_after if error.kind == "RATE" else 0.2 * (attempt + 1))

    def success(self, order, existing=False):
        detail = order_summary(order)
        self.journal.save("SUCCESS", order=detail)
        self.emit({"event": "result", "state": "SUCCESS", "existing": existing, "order": detail,
                   "message": "已有该日场馆预约，已停止" if existing else "预约成功；请立即在钉钉完成同行邀请，页面规则为 10 分钟内至少邀请 1 人"})
        return "SUCCESS"

    def reconcile(self):
        # Read-only retries are safe; an empty list never proves a write failed.
        if self.poller:
            self.poller.stop()
        for delay in (0, 0.2, 0.5):
            if delay:
                time.sleep(delay)  # finish result reconciliation even after Stop
            try:
                orders = self.orders()
            except ApiError:
                continue
            matches = [o for o in orders if same_day_venue(o, self.config)]
            if matches:
                return self.success(matches[0])
        self.journal.save("UNKNOWN")
        self.emit({"event": "result", "state": "UNKNOWN", "message": "写入结果仍不明确；已保留记录并停止重试，请核对钉钉我的预约"})
        return "UNKNOWN"

    def recover(self, orders):
        state = self.journal.data["state"]
        matching = [o for o in orders if same_day_venue(o, self.config)]
        if matching:
            return self.success(matching[0], existing=state not in PENDING)
        if state == "SUCCESS":
            self.emit({"event": "result", "state": "SUCCESS", "order": self.journal.data.get("order", {}),
                       "message": "本任务已有成功记录，保留防重复保护；请在钉钉核对"})
            return "SUCCESS"
        if state in PENDING and state != "PREPARED":
            return self.reconcile()
        if state == "PREPARED":
            candidate = candidate_from(self.journal.data["candidate"])
            self.log("恢复已确认的临时占用；先核对订单，再继续原阶段")
            if self.journal.data.get("resume") == "cleanup" or self.clock.now() >= self.finalize_deadline:
                self.cleanup(candidate)
                return None
            return self.commit(candidate)
        return None

    @property
    def end_epoch(self):
        return self.config.release_epoch() + self.config.booking_window_seconds

    @property
    def finalize_deadline(self):
        # Stop initiating bookings at T+30, but finish an already confirmed hold.
        # This is a local recovery bound, not a claim about the server's hold TTL.
        return self.journal.data.get("finalize_deadline", self.end_epoch)

    def mark_prepared(self, candidate, resume):
        grace = (self.config.refresh_timeout_seconds * self.config.refresh_attempts
                 + max(0, self.config.refresh_attempts - 1) + self.config.request_timeout_seconds * 3 + 5)
        self.journal.save("PREPARED", candidate, resume=resume, finalize_deadline=self.clock.now() + grace)

    def _mark_rejection(self, error, stage, candidate):
        if stage == "prepare":
            self.journal.save("READY", resume="")
        else:
            self.journal.save("PREPARED", candidate, resume="cleanup" if stage == "cleanup" else "commit")
        if error.kind == "AUTH":
            raise NeedAuth(self.auth_requirement()) from None

    def cleanup(self, candidate):
        while True:
            self.check_stop()
            self.journal.save("CLEANUP_SENT", candidate, resume="cleanup")
            try:
                self.api.release_hold(self.session)
                break
            except ApiError as error:
                if error.kind in ("AUTH", "RATE"):
                    self._mark_rejection(error, "cleanup", candidate)
                    self.wait(error.retry_after)
                    continue
                self.journal.save("UNKNOWN")
                self.log("释放临时占用的结果待核对", phase="cleanup", reason=error.kind)
                raise ApiError("UNCERTAIN", "临时占用释放结果未确认") from None
        self.journal.save("READY", resume="")

    def commit(self, candidate):
        while True:
            self.check_stop()
            if self.clock.now() >= self.finalize_deadline:
                self.cleanup(candidate)
                return None
            self.journal.save("COMMIT_SENT", candidate, resume="commit")
            try:
                body = self.api.commit(candidate.payload(self.session, self.config))
            except ApiError as error:
                if error.kind in ("AUTH", "RATE"):
                    self._mark_rejection(error, "commit", candidate)
                    if error.kind == "RATE":
                        self.wait(error.retry_after)
                        continue
                elif error.kind in ("CONFLICT", "REJECTED", "LIMIT", "NOT_RELEASED"):
                    self.journal.save("PREPARED", candidate, resume="cleanup")
                    self.cleanup(candidate)
                    self.blocked[candidate.key] = time.perf_counter() + 1
                    if error.kind in ("REJECTED", "LIMIT"):
                        raise
                    return None
                else:
                    self.journal.save("UNKNOWN")
                    self.log("提交订单的结果待核对", phase="commit", reason=error.kind)
                    return self.reconcile()
            else:
                order = body.get("data")
                if isinstance(order, dict) and order.get("order_num") and same_day_venue(order, self.config) \
                        and str(order.get("site_id")) == str(candidate.site_id) \
                        and order.get("start_time") == candidate.start and order.get("end_time") == candidate.end:
                    return self.success(order)
                self.journal.save("UNKNOWN")
                self.log("订单回执与预期不符，核对我的预约", phase="commit", reason="SCHEMA")
                return self.reconcile()

    def prepare(self, candidate):
        self.check_stop()
        if self.clock.now() >= self.end_epoch:
            return None
        self.journal.save("PREPARE_SENT", candidate, resume="prepare")
        if self.stop_event.is_set() or self.clock.now() >= self.end_epoch:
            # No request has been dispatched yet; the outcome is still known.
            self.journal.save("READY", resume="")
            self.check_stop()
            return None
        try:
            body = self.api.prepare(candidate.payload(self.session, self.config))
        except ApiError as error:
            if error.kind in ("AUTH", "RATE", "CONFLICT", "NOT_RELEASED", "LIMIT", "REJECTED"):
                self._mark_rejection(error, "prepare", candidate)
                if error.kind == "CONFLICT":
                    self.blocked[candidate.key] = time.perf_counter() + 1
                    return None
                raise
            self.journal.save("UNKNOWN")
            self.log("预提交结果待核对", phase="prepare", reason=error.kind)
            return self.reconcile()
        conflicts, available = body.get("conflict_times"), body.get("available_times")
        if not isinstance(conflicts, list) or not isinstance(available, list) \
                or any(type(n) is not int or n not in candidate.indices for n in conflicts + available) \
                or set(conflicts) & set(available):
            self.journal.save("UNKNOWN")
            self.log("预提交回执与预期不符，核对我的预约", phase="prepare", reason="SCHEMA")
            return self.reconcile()
        if not conflicts:
            self.mark_prepared(candidate, resume="commit")
            return self.commit(candidate)
        self.blocked[candidate.key] = time.perf_counter() + 1
        if not available:
            self.journal.save("READY", resume="")
            return None
        self.mark_prepared(candidate, resume="cleanup")
        remaining = self.snapshot.candidates(self.config, only_site=candidate.site_id, allowed=available,
                                             now_epoch=self.clock.now())
        if not remaining:
            self.cleanup(candidate)
            return None
        best = remaining[0]
        latest, _, _, updated = self.poller.view() if self.poller else (None, None, 0, 0)
        if latest is not None and time.perf_counter() - updated < 0.5:
            alternatives = [c for c in self.candidates(latest)
                            if not (c.site_id == candidate.site_id and set(c.indices) & set(candidate.indices))]
            if alternatives and sort_key(alternatives[0], self.config) < sort_key(best, self.config):
                self.cleanup(candidate)
                self.log("部分时段冲突，已释放临时占用并切换更高优先级候选")
                return None
        self.log(f"部分时段冲突，保留场地 {best.site_id} 的连续 {best.minutes} 分钟")
        self.journal.save("PREPARED", best, resume="commit")
        return self.commit(best)

    def candidates(self, snapshot):
        result = snapshot.candidates(self.config, now_epoch=self.clock.now())
        # Any existing booking at the same time (including another sport) blocks it.
        occupied = []
        for order in self.own_orders:
            if str(order.get("order_date", ""))[:10] == self.config.target_date:
                try:
                    occupied.append((minute(order["start_time"]), minute(order["end_time"])))
                except (KeyError, ValueError):
                    raise ApiError("SCHEMA", "本人订单时段格式不兼容") from None
        now = time.perf_counter()
        return [c for c in result if self.blocked.get(c.key, 0) <= now
                and not any(minute(c.start) < end and start < minute(c.end) for start, end in occupied)]

    def run(self, dry=False):
        self.config.validate()
        calibrate = lambda: self.read_retry(lambda: self.clock.calibrate(self.api))
        self.check_connection(calibrate, dry, time.time)
        self.log("服务器时间已校准", rtt_ms=round(self.clock.rtt_ms, 3),
                 clock_offset_ms=round((self.clock.now() - time.time()) * 1000, 1))
        self.verify_login(dry)
        if dry:
            self.snapshot = Snapshot(self.read_retry(lambda: self.api.metadata(self.config)),
                                     self.read_retry(lambda: self.api.situation(self.config)))
            candidates = self.candidates(self.snapshot)
            self.emit({"event": "preview", "count": len(candidates), "candidates": [c.summary() for c in candidates[:30]],
                       "existing_booking": any(same_day_venue(o, self.config) for o in self.own_orders),
                       "release": datetime.fromtimestamp(self.config.release_epoch(), CHINA).isoformat(),
                       "rtt_ms": round(self.clock.rtt_ms, 3), "message": "只读演练完成，未调用预提交、创建或取消接口"})
            return "PREVIEW"
        previous = self.journal.data
        if (previous.get("state") in PENDING - {"PREPARED"}
                and previous.get("target_date") == self.config.target_date
                and previous.get("account") == account_tag(self.session)
                and previous.get("venue_name") == self.config.venue_name):
            return self.recover(self.own_orders)
        self.journal.bind(self.config, self.session, self.own_orders)
        recovery = self.recover(self.own_orders)
        if recovery:
            return recovery
        if self.clock.now() >= self.end_epoch:
            self.emit({"event": "result", "state": "EXPIRED", "message": "放号抢约窗口已结束；请调整日期或放号时间"})
            return "EXPIRED"
        self.log("等待放号前会话检查", phase="waiting",
                 release=datetime.fromtimestamp(self.config.release_epoch(), CHINA).isoformat())
        self.request_page_check()
        self.monitor_login_until_warmup()
        existing = [o for o in self.own_orders if same_day_venue(o, self.config)]
        if existing:
            return self.success(existing[0], existing=True)
        self.check_connection(calibrate, dry, self.clock.now)
        metadata = self.read_retry(lambda: self.api.metadata(self.config))
        if metadata.get("days_can_appoint") != self.config.release_days_before:
            self.log("接口可见天数与配置的放号偏移不同，继续按配置定时，以服务端实际开放结果为准",
                     days_can_appoint=metadata.get("days_can_appoint"), release_days_before=self.config.release_days_before)
        self.snapshot = Snapshot(metadata, self.read_retry(lambda: self.api.situation(self.config)))
        self.poller = Poller(self.api, self.config, metadata, self.clock, self.snapshot)
        self.poller.start()
        self.log("连接已预热，开始余量监测；整点发送预提交", phase="ready")
        try:
            # Surface a rejected session during warmup, while there is time to sign in.
            while self.clock.now() < self.config.release_epoch():
                _, error, _, _ = self.poller.view()
                if error and error.kind == "AUTH":
                    raise NeedAuth(self.auth_requirement())
                release = self.config.release_epoch()
                if self.last_verified < release - 60:
                    due = self.next_login_check(release - 60)
                    if self.clock.now() >= due:
                        self.verify_login()
                        existing = next((o for o in self.own_orders if same_day_venue(o, self.config)), None)
                        if existing:
                            return self.success(existing, existing=True)
                        self.request_page_check()
                        continue
                else:
                    due = release
                self.wait_until(min(due, self.clock.now() + 1))
            not_released_logged = False
            while self.clock.now() < self.end_epoch:
                self.check_stop()
                latest, error, _, _ = self.poller.view()
                self.snapshot = latest
                if error and error.kind in ("AUTH", "SCHEMA", "LIMIT"):
                    if error.kind == "AUTH":
                        raise NeedAuth(self.auth_requirement())
                    raise error
                if error and error.kind == "RATE":
                    self.wait(min(max(error.retry_after, 0.1), max(0, self.end_epoch - self.clock.now())))
                    continue
                choices = self.candidates(latest)
                if not choices:
                    self.wait(0.05)
                    continue
                chosen = choices[0]
                self.log(f"尝试场地 {chosen.site_id}，{chosen.start}–{chosen.end}，{chosen.minutes} 分钟", phase="booking")
                try:
                    result = self.prepare(chosen)
                except ApiError as error:
                    if error.kind == "NOT_RELEASED":
                        if not not_released_logged:
                            self.log("服务器尚未放号，按短轮询等待开放")
                            not_released_logged = True
                        interval = self.config.fast_poll_seconds if self.clock.now() - self.config.release_epoch() < 3 else self.config.late_poll_seconds
                        self.wait(interval)
                        continue
                    if error.kind == "RATE":
                        self.wait(error.retry_after)
                        continue
                    raise
                if result:
                    return result
            if self.journal.data.get("state") == "PREPARED":
                self.cleanup(candidate_from(self.journal.data["candidate"]))
            self.emit({"event": "result", "state": "NO_AVAILABILITY", "message": "本轮窗口结束，未取得预约"})
            return "NO_AVAILABILITY"
        finally:
            self.poller.stop()
