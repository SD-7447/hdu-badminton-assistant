"""Venue HTTP protocol, scheduling configuration and real-minute candidates."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import http.client
import json
import math
from pathlib import Path
import re
import ssl
import threading
import time

from venue_session import Session

CHINA = timezone(timedelta(hours=8))
HOST = "sportmeta.hdu.edu.cn"
PRIORITY = [6, 7, 5, 8, 1, 2, 3, 4, 9, 10, 11, 12]


def minute(value: str):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("时间必须为 HH:MM")
    h, m = map(int, value.split(":"))
    if not 0 <= h <= 23 or not 0 <= m <= 59:
        raise ValueError("无效时间")
    return h * 60 + m


@dataclass
class Config:
    version: int = 2
    target_date: str = ""
    venue_name: str = "综合馆羽毛球"
    venue_type: str = "badminton"
    court_priority: list = field(default_factory=lambda: list(PRIORITY))
    earliest_start: str = "10:00"
    min_minutes: int = 60
    max_minutes: int = 120
    max_slots_per_day: int = 2
    free_only: bool = True
    release_days_before: int = 2
    release_time: str = "20:00"
    release_override: str = ""
    preflight_seconds: int = 120
    warmup_seconds: int = 60
    fast_window_seconds: float = 5.0
    normal_poll_seconds: float = 1.0
    fast_poll_seconds: float = 0.1
    late_poll_seconds: float = 0.5
    booking_window_seconds: float = 30.0
    request_timeout_seconds: float = 2.0
    auto_refresh: bool = True
    refresh_timeout_seconds: float = 15.0
    refresh_attempts: int = 2
    storage_path: str = ""

    def validate(self):
        if self.version != 2:
            raise ValueError("接口配置版本不兼容")
        for key in ("target_date", "venue_name", "venue_type", "earliest_start", "release_time", "release_override", "storage_path"):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"{key} 必须为字符串")
        date.fromisoformat(self.target_date)
        if self.venue_name != "综合馆羽毛球" or self.venue_type != "badminton":
            raise ValueError("当前适配器仅支持综合馆羽毛球")
        if not isinstance(self.court_priority, list) or not self.court_priority or any(type(n) is not int or not 1 <= n <= 12 for n in self.court_priority):
            raise ValueError("场地顺序必须包含 1～12 范围内的整数")
        if len(set(self.court_priority)) != len(self.court_priority):
            raise ValueError("场地顺序不能重复")
        if minute(self.earliest_start) < 600:
            raise ValueError("开始时间不能早于 10:00")
        minute(self.release_time)
        for key, low, high in [("min_minutes", 60, 120), ("max_minutes", 60, 120),
                               ("max_slots_per_day", 1, 2), ("release_days_before", 0, 30),
                               ("preflight_seconds", 60, 3600), ("warmup_seconds", 5, 600),
                               ("refresh_attempts", 1, 2)]:
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} 必须为 {low}～{high} 之间的整数")
        if self.min_minutes > self.max_minutes or self.preflight_seconds < self.warmup_seconds:
            raise ValueError("时长范围或预热顺序不正确")
        for key, low, high in [("fast_window_seconds", 1, 10), ("normal_poll_seconds", 1, 60),
                               ("fast_poll_seconds", 0.1, 10), ("late_poll_seconds", 0.5, 10),
                               ("booking_window_seconds", 1, 120), ("request_timeout_seconds", 0.2, 10),
                               ("refresh_timeout_seconds", 1, 30)]:
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} 必须在 {low}～{high} 秒之间")
        if type(self.free_only) is not bool or not self.free_only:
            raise ValueError("当前版本仅支持免费预约")
        if type(self.auto_refresh) is not bool:
            raise ValueError("auto_refresh 必须为布尔值")
        self.release_epoch()
        return self

    def release_epoch(self):
        if self.release_override:
            value = datetime.fromisoformat(self.release_override)
            if value.tzinfo is None:
                value = value.replace(tzinfo=CHINA)
        else:
            day = date.fromisoformat(self.target_date) - timedelta(days=self.release_days_before)
            m = minute(self.release_time)
            value = datetime.combine(day, dt_time(m // 60, m % 60), CHINA)
        return value.timestamp()

    @classmethod
    def for_today(cls, profile=None, now=None):
        today = (now or datetime.now(CHINA)).astimezone(CHINA).date()
        return replace(profile or cls(), target_date=(today + timedelta(days=2)).isoformat(),
                       release_days_before=2, release_override="")

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text(encoding="utf-8"))).validate()

    def save(self, path):
        self.validate()
        atomic_json(Path(path), asdict(self))


def atomic_json(path: Path, value):
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


class ApiError(RuntimeError):
    def __init__(self, kind, message, retry_after=0.0, status=0):
        super().__init__(message)
        self.kind, self.retry_after, self.status = kind, retry_after, status


def classify(message):
    lowered = message.lower()
    if any(n in lowered for n in ("token expired", "invalid token", "token has expired", "令牌过期", "登录已过期",
                                  "登录过期", "token过期", "token已过期", "未登录", "登录失效", "登录已失效",
                                  "请重新登录", "请先登录", "游客无法预约", "游客不可预约",
                                  "游客不能预约", "游客请先登录", "guest users cannot book")):
        return "AUTH"
    if lowered.strip(" 。.!！") in {"游客", "游客登录", "游客模式", "guest", "guest session"}:
        return "AUTH"
    if any(n in message for n in ("尚未开放", "暂未开放", "未到预约", "未到放号", "尚未开始", "未开始预约", "暂未开启", "不在可预约日期", "不在预约日期", "超出预约日期", "预约日期范围")):
        return "NOT_RELEASED"
    if any(n in message for n in ("上限", "额度", "超出限制", "次数限制", "最多预约", "已达", "黑名单", "权限")):
        return "LIMIT"
    if any(n in message for n in ("冲突", "已被预约", "已预约", "不可预约", "已占用")):
        return "CONFLICT"
    return "REJECTED"


@dataclass
class Response:
    status: int
    body: dict
    elapsed_ms: float = 0.0


class RateGate:
    def __init__(self):
        self.until = 0.0
        self.lock = threading.Lock()

    def remaining(self):
        with self.lock:
            return max(0.0, self.until - time.perf_counter())

    def defer(self, seconds):
        with self.lock:
            self.until = max(self.until, time.perf_counter() + seconds)


class HttpTransport:
    """One reusable connection per channel; never retry a request implicitly."""
    def __init__(self, session: Session, timeout=2.0, gate=None):
        self.session, self.timeout = session, timeout
        self.connection = None
        self.timings = []
        self.lock = threading.RLock()
        self.gate = gate or RateGate()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def request(self, endpoint, payload=None):
        with self.lock:
            delay = self.gate.remaining()
            if delay:
                raise ApiError("RATE", "接口退避中", delay)
            return self._request(endpoint, payload)

    def _request(self, endpoint, payload=None):
        started = time.perf_counter()
        try:
            if self.connection is None:
                self.connection = http.client.HTTPSConnection(HOST, timeout=self.timeout, context=ssl.create_default_context())
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.session.token,
                       "Accept": "application/json", "Origin": "https://" + HOST,
                       "Referer": "https://" + HOST + "/book/dingtalk/"}
            body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.connection.request("GET" if payload is None else "POST", "/book/client/" + endpoint, body, headers)
            result = self.connection.getresponse()
            raw = result.read(2 * 1024 * 1024 + 1)
            elapsed = (time.perf_counter() - started) * 1000
            self.timings.append(round(elapsed, 3))
            self.timings = self.timings[-200:]
            if result.status == 429:
                retry = result.getheader("Retry-After", "1")
                try:
                    delay = max(1.0, float(retry))
                except ValueError:
                    from email.utils import parsedate_to_datetime
                    try:
                        delay = max(1.0, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        delay = 1.0
                self.gate.defer(delay)
                raise ApiError("RATE", "接口限流，等待后重试", delay, result.status)
            if result.status == 401:
                raise ApiError("AUTH", "场馆令牌已失效", status=401)
            if result.status >= 500:
                raise ApiError("UNCERTAIN", "服务端异常，预约写入结果需核对", status=result.status)
            if len(raw) > 2 * 1024 * 1024:
                raise ApiError("UNCERTAIN", "接口返回过大")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ApiError("UNCERTAIN", "接口返回非 JSON 数据") from None
            if not isinstance(data, dict):
                raise ApiError("UNCERTAIN", "接口返回结构不兼容")
            message = str(data.get("message", data.get("msg", "请求被拒绝")))
            # Avoid exposing server responses or credentials through exception logs.
            kind = classify(message)
            if data.get("code") in (401, "401") or kind == "AUTH":
                raise ApiError("AUTH", "场馆令牌已失效", status=result.status)
            if result.status == 403:
                raise ApiError("LIMIT", "服务器拒绝当前账户权限", status=403)
            if not 200 <= result.status < 300 or result.status == 201:
                detail = message
                for secret in (self.session.token, self.session.openid, self.session.phone):
                    if secret:
                        detail = detail.replace(secret, "[已隐藏]")
                detail = re.sub(r"Bearer\s+\S+|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]+", "[已隐藏]", detail)[:200]
                prefix = {"NOT_RELEASED": "尚未放号", "LIMIT": "账户额度或权限限制", "CONFLICT": "场地已被占用"}.get(kind, "业务请求被拒绝")
                raise ApiError(kind, prefix + "：" + detail, status=result.status)
            return Response(result.status, data, elapsed)
        except ApiError as error:
            if error.kind == "UNCERTAIN":
                self.close()
            raise
        except (OSError, http.client.HTTPException, TimeoutError):
            self.close()
            raise ApiError("UNCERTAIN", "网络中断或超时，写入结果需核对") from None


class VenueApi:
    def __init__(self, reader, writer=None):
        self.reader, self.writer = reader, writer or reader

    @classmethod
    def connect(cls, session, config):
        gate = RateGate()
        return cls(HttpTransport(session, config.request_timeout_seconds, gate),
                   HttpTransport(session, config.request_timeout_seconds, gate))

    def close(self):
        self.reader.close()
        if self.writer is not self.reader:
            self.writer.close()

    def server_time(self, writer=False):
        result = (self.writer if writer else self.reader).request("post_server_time")
        value = result.body.get("data")
        if not isinstance(value, (float, int)) or value < 1e12:
            raise ApiError("SCHEMA", "服务器时间结构不兼容")
        return float(value) / 1000

    def metadata(self, config):
        return self.reader.request("post_venue_info", {"date": config.target_date, "venue_name": config.venue_name}).body

    def situation(self, config):
        return self.reader.request("post_site_situation", {"date": config.target_date, "venue_name": config.venue_name, "is_permanent": 0}).body

    def orders(self, session):
        body = self.reader.request("post_order_info", {"openid": session.openid, "is_invite": False}).body
        if not isinstance(body.get("order_info"), list) or any(not isinstance(o, dict) for o in body["order_info"]):
            raise ApiError("SCHEMA", "本人订单结构不兼容，停止写入")
        return body["order_info"]

    def prepare(self, payload):
        return self.writer.request("creat_book_info", payload).body

    def commit(self, payload):
        return self.writer.request("creat_order", payload).body

    def release_hold(self, session):
        body = self.writer.request("cancel_book", {"openid": session.openid}).body
        # Only an explicit application success is evidence of cleanup.
        message = str(body.get("message", ""))
        if body.get("success") is True or body.get("status") == 200 or "成功" in message:
            return
        raise ApiError("UNCERTAIN", "临时占用释放结果不明确，停止写入")


@dataclass(frozen=True)
class Candidate:
    site_id: int
    indices: tuple
    start: str
    end: str
    minutes: int
    price: Decimal

    @property
    def key(self):
        return self.site_id, self.indices

    def summary(self):
        return {"site_id": self.site_id, "indices": list(self.indices), "start": self.start,
                "end": self.end, "minutes": self.minutes, "price": str(self.price)}

    def payload(self, session, config):
        return {"orderData": {"openid": session.openid, "nickname": session.nickname, "phone": session.phone,
                              "date": config.target_date, "venue_name": config.venue_name, "venue_type": config.venue_type,
                              "site_id": self.site_id, "total_price": float(self.price), "time_list": list(self.indices),
                              "start_time": self.start, "end_time": self.end}}


def sort_key(candidate, config):
    return -candidate.minutes, config.court_priority.index(candidate.site_id), minute(candidate.start)


def _indices(values, count):
    if not isinstance(values, list) or any(type(v) is not int or not 0 <= v < count for v in values):
        raise ValueError("无效时段索引")
    return set(values)


def _price(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError("无效价格")
        return number
    except (InvalidOperation, TypeError):
        raise ValueError("无效价格") from None


class Snapshot:
    def __init__(self, metadata, situation):
        try:
            self.times = metadata["time_list"]
            if not isinstance(self.times, list) or len(self.times) < 2:
                raise ValueError("缺少时间边界")
            self.minutes = [minute(t) for t in self.times]
            if any(a >= b for a, b in zip(self.minutes, self.minutes[1:])):
                raise ValueError("时间边界无序")
            count = len(self.times) - 1
            self.rest = _indices(metadata["rest_list"], count)
            self.prices = [_price(metadata["base_price"])] * count
            extra = situation["extra_list"]
            if len(extra["extra_price_time"]) != len(extra["extra_price_list"]):
                raise ValueError("价格长度不一致")
            _indices(extra["extra_price_time"], count)
            for idx, price in zip(extra["extra_price_time"], extra["extra_price_list"]):
                self.prices[idx] = _price(price)
            self.sites = {}
            for site in situation["site_situation"]:
                site_id = site["site_id"]
                if type(site_id) is not int or site_id < 1 or site_id in self.sites:
                    raise ValueError("无效场地 ID")
                closed = self.rest ^ _indices(site["oci_time_list"], count)
                self.sites[site_id] = set(range(count)) - closed - _indices(site["book_time_list"], count)
            if not self.sites:
                raise ValueError("缺少场地")
        except (KeyError, TypeError, ValueError, IndexError):
            raise ApiError("SCHEMA", "场馆余量结构不兼容，停止写入") from None

    def candidates(self, config, remaining_slots=None, now_epoch=None, only_site=None, allowed=None):
        quota = config.max_slots_per_day if remaining_slots is None else remaining_slots
        result = []
        today_minutes = None
        if now_epoch is not None:
            now = datetime.fromtimestamp(now_epoch, CHINA)
            target = date.fromisoformat(config.target_date)
            if target < now.date():
                return []
            if target == now.date():
                today_minutes = now.hour * 60 + now.minute + now.second / 60
        for site_id, available in self.sites.items():
            if site_id not in config.court_priority or only_site is not None and site_id != only_site:
                continue
            if allowed is not None:
                available = available & set(allowed)
            for first in sorted(available):
                if self.minutes[first] < minute(config.earliest_start) or today_minutes is not None and self.minutes[first] <= today_minutes:
                    continue
                for last in range(first, min(first + quota, len(self.times) - 1)):
                    if last not in available:
                        break
                    duration = self.minutes[last + 1] - self.minutes[first]
                    if duration > config.max_minutes:
                        break
                    price = sum(self.prices[first:last + 1], Decimal(0))
                    if duration >= config.min_minutes and (not config.free_only or price == 0):
                        result.append(Candidate(site_id, tuple(range(first, last + 1)), self.times[first],
                                                self.times[last + 1], duration, price))
        return sorted(result, key=lambda c: sort_key(c, config))


class ServerClock:
    def __init__(self, monotonic=time.perf_counter):
        self.monotonic = monotonic
        self.anchor_mono = None
        self.anchor_epoch = None
        self.rtt_ms = None

    def calibrate(self, api, samples=5):
        measurements = []
        for _ in range(samples):
            before = self.monotonic()
            epoch = api.server_time()
            after = self.monotonic()
            measurements.append((after - before, (before + after) / 2, epoch))
        rtt, self.anchor_mono, self.anchor_epoch = min(measurements)
        self.rtt_ms = rtt * 1000
        api.server_time(writer=True)  # warm the independent write connection, GET only

    def now(self):
        if self.anchor_mono is None:
            raise RuntimeError("服务器时钟尚未校准")
        return self.anchor_epoch + self.monotonic() - self.anchor_mono

    def deadline(self, epoch):
        return self.monotonic() + epoch - self.now()
