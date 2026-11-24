"""Local, image-calibrated booking state machine. No network or account credentials."""
from __future__ import annotations

import base64
import io
import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image, ImageChops, ImageStat

CHINA = timezone(timedelta(hours=8))


class StopRun(RuntimeError):
    pass


def encode_image(image: Image.Image) -> str:
    stream = io.BytesIO()
    image.convert("RGB").save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def decode_image(value: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB")


def distance(a: Image.Image, b: Image.Image) -> float:
    a = a.convert("RGB").resize((48, 32), Image.Resampling.BILINEAR)
    b = b.convert("RGB").resize((48, 32), Image.Resampling.BILINEAR)
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / (3 * 255)


def exact_patch_matches(current: Image.Image, expected: Image.Image) -> bool:
    """Check changed pixels as well as MAE; a changed date digit must not disappear in white space."""
    if current.size != expected.size:
        return False
    diff = ImageChops.difference(current.convert("RGB"), expected.convert("RGB"))
    if sum(ImageStat.Stat(diff).mean) / 3 > 0.75:
        return False
    channels = diff.split()
    maximum = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    histogram = maximum.histogram()
    return sum(histogram[25:]) <= max(1, current.width * current.height * 0.001)


def rect_ok(box, size) -> bool:
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(type(n) is int for n in box)
            and 0 <= box[0] < box[2] <= size[0]
            and 0 <= box[1] < box[3] <= size[1])


def parse_trigger(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        raise ValueError("启动时间格式：2026-11-20 19:59:59.800；留空表示立即")
    return result.replace(tzinfo=CHINA) if result.tzinfo is None else result.astimezone(CHINA)


@dataclass
class Candidate:
    label: str
    box: list[int]


@dataclass
class Anchor:
    box: list[int]
    image: str


@dataclass
class Profile:
    version: int = 1
    target_date: str = ""
    calibrated_date: str = ""
    trigger: str = ""
    interval: float = 1.2
    settle: float = 0.45
    duration: float = 90
    max_refreshes: int = 90
    auto_submit: bool = False
    size: list[int] = field(default_factory=list)
    guards: dict[str, Anchor] = field(default_factory=dict)
    refresh: list[int] = field(default_factory=list)
    submit: list[int] = field(default_factory=list)
    free_sample: str = ""
    busy_sample: str = ""
    selected_sample: str = ""
    success: Anchor | None = None
    candidates: list[Candidate] = field(default_factory=list)

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        if data.get("version", 1) != 1:
            raise ValueError("不支持的配置版本")
        data["guards"] = {k: Anchor(**v) for k, v in data.get("guards", {}).items()}
        data["candidates"] = [Candidate(**v) for v in data.get("candidates", [])]
        data["success"] = Anchor(**data["success"]) if data.get("success") else None
        return cls(**data)

    def validate(self, now: datetime | None = None, require_samples=True, require_auto=None):
        now = now or datetime.now(CHINA)
        target = date.fromisoformat(self.target_date)
        if self.calibrated_date != self.target_date:
            raise ValueError("目标日期与校准日期不一致，请选中目标日期并重新框选日期区域")
        if target < now.astimezone(CHINA).date():
            raise ValueError("预约日期已经过去，请重新校准日期区域")
        trigger = parse_trigger(self.trigger)
        if trigger and trigger < now - timedelta(seconds=3):
            raise ValueError("启动时间已经过去；立即运行请清空启动时间")
        for name, value, low, high in [("刷新间隔", self.interval, 1, 60),
                                       ("加载等待", self.settle, 0.2, 10),
                                       ("监测时长", self.duration, 1, 3600)]:
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name}必须在 {low}–{high} 秒之间")
        if type(self.max_refreshes) is not int or not 1 <= self.max_refreshes <= 600:
            raise ValueError("刷新次数必须为 1–600")
        if len(self.size) != 2 or any(type(n) is not int or n < 200 for n in self.size):
            raise ValueError("先截取场馆速约窗口并校准")
        for required in ("title", "date", "rows", "button"):
            if required not in self.guards:
                raise ValueError(f"缺少校准区域：{required}")
        for anchor in list(self.guards.values()) + ([self.success] if self.success else []):
            if not rect_ok(anchor.box, self.size):
                raise ValueError("校准区域超出窗口")
            expected_size = (anchor.box[2] - anchor.box[0], anchor.box[3] - anchor.box[1])
            if decode_image(anchor.image).size != expected_size:
                raise ValueError("校准图片尺寸不匹配")
        if not self.candidates:
            raise ValueError("至少添加一个目标格子")
        boxes = set()
        for item in self.candidates:
            if not item.label.strip() or not rect_ok(item.box, self.size):
                raise ValueError("候选格子的名称或位置无效")
            if tuple(item.box) in boxes:
                raise ValueError("存在重复候选格子")
            boxes.add(tuple(item.box))
        use_auto = self.auto_submit if require_auto is None else require_auto
        for point in [self.refresh] + ([self.submit] if use_auto else []):
            if (len(point) != 2 or any(type(n) is not int for n in point)
                    or not 0 <= point[0] < self.size[0] or not 0 <= point[1] < self.size[1]):
                raise ValueError("缺少或无效的刷新/提交位置")
        if not self.free_sample or not self.busy_sample:
            if require_samples:
                raise ValueError("必须分别采集一个真实空闲格和一个不可用格；没有空闲样本时只能预览")
            return
        free, busy = decode_image(self.free_sample), decode_image(self.busy_sample)
        if distance(free, busy) < 0.035:
            raise ValueError("空闲与不可用样本太相似，请重新采样格子内部（排除边框）")
        if use_auto:
            if not self.selected_sample:
                raise ValueError("自动提交需要采集已选中格样本（采样后请手动取消选择）")
            selected = decode_image(self.selected_sample)
            if distance(selected, free) < 0.055 or distance(selected, busy) < 0.055:
                raise ValueError("选中状态与空闲/不可用状态区分不足")
            if self.success:
                for guard in self.guards.values():
                    if (self.success.box == guard.box
                            and exact_patch_matches(decode_image(self.success.image), decode_image(guard.image))):
                        raise ValueError("成功样本不能使用预约表中的常驻内容")


class Adapter(Protocol):
    def capture(self) -> Image.Image: ...
    def click(self, x: int, y: int): ...


class Runner:
    """At most one cell selection and one submission. Uncertain results never cause retries."""
    def __init__(self, profile: Profile, adapter: Adapter, stop: threading.Event,
                 log: Callable[[str], None], journal: Callable[[str, str], None],
                 wait: Callable[[float], bool] | None = None,
                 wall: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] | None = None):
        self.p, self.a, self.stop, self.log, self.journal = profile, adapter, stop, log, journal
        self.wait = wait or stop.wait
        self.wall = wall or (lambda: datetime.now(CHINA))
        self.monotonic = monotonic or time.monotonic
        self.guards = {k: decode_image(v.image) for k, v in profile.guards.items()}
        self.free = decode_image(profile.free_sample) if profile.free_sample else None
        self.busy = decode_image(profile.busy_sample) if profile.busy_sample else None

    def pause(self, seconds):
        if self.wait(max(0, seconds)) or self.stop.is_set():
            raise StopRun("已停止")

    def frame(self, check_button=True):
        if self.stop.is_set():
            raise StopRun("已停止")
        image = self.a.capture().convert("RGB")
        if list(image.size) != self.p.size:
            raise StopRun("窗口尺寸改变；重新校准后再运行")
        for key, anchor in self.p.guards.items():
            if key == "button" and not check_button:
                continue
            if not exact_patch_matches(image.crop(anchor.box), self.guards[key]):
                raise StopRun(f"界面校验失败（{key}）：日期、滚动位置、页面或选择状态发生改变")
        return image

    def is_free(self, image, item):
        patch = image.crop(item.box)
        positive, negative = distance(patch, self.free), distance(patch, self.busy)
        return positive <= 0.015 and negative - positive >= 0.02

    def click(self, point):
        if self.stop.is_set():
            raise StopRun("已停止")
        self.a.click(*point)

    def run(self, mode="preview"):
        self.p.validate(self.wall(), require_samples=mode != 'preview', require_auto=mode == 'submit')
        if mode not in ("preview", "select", "submit"):
            raise ValueError("未知运行模式")
        if mode == "submit" and not self.p.auto_submit:
            raise ValueError("自动提交未启用")
        if mode != 'preview':
            self.frame()
        trigger = parse_trigger(self.p.trigger)
        if mode != "preview" and trigger:
            self.log(f"等待北京时间 {trigger.isoformat(sep=' ', timespec='milliseconds')}")
            while (remaining := (trigger - self.wall()).total_seconds()) > 0:
                self.pause(min(0.25, remaining))
        if mode != "preview" and date.fromisoformat(self.p.target_date) < self.wall().astimezone(CHINA).date():
            raise StopRun("等待过程中预约日期已过期")
        start, refreshes = self.monotonic(), 0
        while self.monotonic() - start < self.p.duration:
            image = self.frame()
            if mode == 'preview' and (self.free is None or self.busy is None):
                self.log("预览：界面校验通过；缺少空闲/不可用样本，未判断余量、未执行任何点击")
                return "PREVIEW"
            candidate = next((item for item in self.p.candidates if self.is_free(image, item)), None)
            if mode == "preview":
                self.log("预览：发现空闲 " + candidate.label if candidate else "预览：候选格均未匹配空闲样本")
                return "PREVIEW"
            if candidate:
                self.pause(0.12)
                image = self.frame()
                if not self.is_free(image, candidate):
                    continue
                # Durable intent is written BEFORE the input. A capture/click error cannot permit an automatic retry.
                self.journal("SELECT_INTENT", candidate.label)
                self.click([(candidate.box[0] + candidate.box[2]) // 2,
                            (candidate.box[1] + candidate.box[3]) // 2])
                if mode == "select":
                    self.journal("SELECTED_UNVERIFIED", candidate.label)
                    self.log(f"已点击 {candidate.label}；请核对底部选择信息并手动提交，尚未确认预约")
                    return "SELECTED_UNVERIFIED"
                self.pause(self.p.settle)
                image = self.frame(check_button=False)
                if distance(image.crop(candidate.box), decode_image(self.p.selected_sample)) > 0.025:
                    raise StopRun("格子点击后未匹配已选中样本，停止；没有提交")
                if exact_patch_matches(image.crop(self.p.guards['button'].box), self.guards['button']):
                    raise StopRun("底部按钮仍为未选择状态，停止；没有提交")
                if self.p.success and exact_patch_matches(image.crop(self.p.success.box), decode_image(self.p.success.image)):
                    raise StopRun("成功样本在提交前已出现，校准无效；没有提交")
                self.journal("SUBMIT_INTENT", candidate.label)
                self.click(self.p.submit)
                self.journal("SUBMITTED_UNKNOWN", candidate.label)
                self.log(f"已执行一次提交：{candidate.label}。不重试；正在核对结果")
                if self.p.success:
                    deadline = self.monotonic() + 8
                    while self.monotonic() < deadline:
                        self.pause(0.35)
                        image = self.a.capture()
                        if list(image.size) != self.p.size:
                            break
                        if exact_patch_matches(image.crop(self.p.success.box), decode_image(self.p.success.image)):
                            self.pause(0.25)
                            image = self.a.capture()
                            if (list(image.size) == self.p.size and
                                    exact_patch_matches(image.crop(self.p.success.box), decode_image(self.p.success.image))):
                                self.journal("SUCCESS_MARKER", candidate.label)
                                self.log("检测到校准的成功标记。请在我的预约核对，并及时邀请同伴")
                                return "SUCCESS_MARKER"
                self.log("提交结果待核对：请查看弹窗或我的预约。不要直接再次提交")
                return "SUBMITTED_UNKNOWN"
            if refreshes >= self.p.max_refreshes:
                break
            # Refresh once per cycle. No request concurrency or private endpoint replay.
            self.frame()
            self.click(self.p.refresh)
            refreshes += 1
            self.log(f"第 {refreshes} 次刷新；未发现目标空闲格")
            self.pause(min(max(self.p.interval, self.p.settle),
                           max(0, self.p.duration - (self.monotonic() - start))))
        self.log(f"监测结束，共刷新 {refreshes} 次；没有执行预约提交")
        return "EXHAUSTED"


def save_profile(profile: Profile, path: Path):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_profile(path: Path) -> Profile:
    return Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
