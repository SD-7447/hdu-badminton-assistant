"""Read only the venue origin's live Chromium Local Storage; renew via DingTalk SSO.

No browser database is opened for writing and no passwords are read. The small
LevelDB reader follows Google's log/table/version-edit formats and honors the
manifest and deletion records, so deleted credentials cannot be resurrected.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import quote

ORIGIN = "https://sportmeta.hdu.edu.cn"
BOOT_URL = ORIGIN + "/book/dingtalk/#/pages/boot/boot"


class SessionError(RuntimeError):
    pass


class MissingSession(SessionError):
    """The current storage snapshot has no complete venue session."""


class SessionRefreshTimeout(SessionError):
    """The login entry opened without issuing a new session before the deadline."""


login_open_lock = threading.Lock()
last_login_opened = None
last_login_restarted = False

# Run inside the desktop user's Windows session. The registered protocol handler
# identifies the installation root even if DingTalk was installed on another drive.
DINGTALK_EXIT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$command = (Get-Item -LiteralPath 'Registry::HKEY_CLASSES_ROOT\dingtalk\shell\open\command').GetValue('')
if ($command -notmatch '^\s*"([^"]+\.exe)"|^\s*([^\s"]+\.exe)') { exit 2 }
$handler = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
$handler = [Environment]::ExpandEnvironmentVariables($handler)
if (!(Test-Path -LiteralPath $handler -PathType Leaf)) { exit 2 }
$root = (Split-Path -Parent $handler).TrimEnd('\') + '\'
$desktop = (Get-Process -Id $PID).SessionId
function Get-LocalDingTalk {
    Get-Process -Name DingTalk -ErrorAction SilentlyContinue | Where-Object {
        $_.SessionId -eq $desktop -and $_.Path -and
        $_.Path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
    }
}
$running = @(Get-LocalDingTalk)
if (!$running.Count) { exit 0 }
$client = ($running | Sort-Object StartTime | Select-Object -First 1).Path
foreach ($process in $running) {
    if (!$process.HasExited -and $process.MainWindowHandle -ne 0) {
        [void]$process.CloseMainWindow()
    }
}
$deadline = [DateTime]::UtcNow.AddSeconds(4)
do {
    Start-Sleep -Milliseconds 200
    $remaining = @(Get-LocalDingTalk)
} while ($remaining.Count -and [DateTime]::UtcNow -lt $deadline)
if ($remaining.Count) {
    $remaining | Stop-Process -ErrorAction SilentlyContinue
    $deadline = [DateTime]::UtcNow.AddSeconds(4)
    do {
        Start-Sleep -Milliseconds 200
        $remaining = @(Get-LocalDingTalk)
    } while ($remaining.Count -and [DateTime]::UtcNow -lt $deadline)
}
if ($remaining.Count) { exit 3 }
$client
"""


def varint(data: bytes, pos: int = 0):
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("truncated varint")
        value = data[pos]
        pos += 1
        result |= (value & 127) << shift
        if value < 128:
            return result, pos
    raise ValueError("invalid varint")


def blob(data: bytes, pos: int):
    length, pos = varint(data, pos)
    end = pos + length
    if end > len(data):
        raise ValueError("truncated string")
    return data[pos:end], end


def _crc_table():
    table = []
    for n in range(256):
        for _ in range(8):
            n = (n >> 1) ^ (0x82F63B78 if n & 1 else 0)
        table.append(n)
    return table


_CRC = _crc_table()


def masked_crc(data: bytes):
    value = 0xFFFFFFFF
    for n in data:
        value = _CRC[(value ^ n) & 255] ^ (value >> 8)
    value ^= 0xFFFFFFFF
    return (((value >> 15) | (value << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def log_records(data: bytes, strict=False):
    pos, fragments = 0, None
    while pos + 7 <= len(data):
        remaining = 32768 - pos % 32768
        if remaining < 7:
            pos += remaining
            continue
        checksum, size, kind = struct.unpack_from("<IHB", data, pos)
        if size == kind == 0:
            pos += remaining
            continue
        end = pos + 7 + size
        if end > len(data):
            if strict:
                raise ValueError("incomplete manifest")
            break  # A writer may be in the middle of appending the last record.
        if size + 7 > remaining:
            raise ValueError("invalid log fragment")
        payload = data[pos + 7:end]
        if checksum != masked_crc(bytes([kind]) + payload):
            raise ValueError("log checksum mismatch")
        pos = end
        if kind == 1:
            fragments = None
            yield payload
        elif kind == 2:
            fragments = bytearray(payload)
        elif kind in (3, 4) and fragments is not None:
            fragments.extend(payload)
            if kind == 4:
                yield bytes(fragments)
                fragments = None
        else:
            raise ValueError("invalid log sequence")
    if strict and fragments is not None:
        raise ValueError("incomplete manifest record")


def snappy(data: bytes):
    expected, pos = varint(data)
    if expected > 64 * 1024 * 1024:
        raise ValueError("oversized snappy block")
    output = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            count = tag >> 2
            if count >= 60:
                width = count - 59
                if pos + width > len(data):
                    raise ValueError("truncated literal")
                count = int.from_bytes(data[pos:pos + width], "little")
                pos += width
            count += 1
            if pos + count > len(data):
                raise ValueError("truncated literal")
            output.extend(data[pos:pos + count])
            pos += count
        else:
            width = {1: 1, 2: 2, 3: 4}[kind]
            if pos + width > len(data):
                raise ValueError("truncated copy")
            offset = int.from_bytes(data[pos:pos + width], "little")
            pos += width
            if kind == 1:
                offset |= (tag & 0xE0) << 3
                count = 4 + ((tag >> 2) & 7)
            else:
                count = 1 + (tag >> 2)
            if not 0 < offset <= len(output):
                raise ValueError("invalid copy offset")
            for _ in range(count):
                output.append(output[-offset])
        if len(output) > expected:
            raise ValueError("snappy length mismatch")
    if len(output) != expected:
        raise ValueError("snappy length mismatch")
    return bytes(output)


def block_entries(data: bytes):
    if len(data) < 4:
        raise ValueError("invalid block")
    count = struct.unpack_from("<I", data, len(data) - 4)[0]
    limit = len(data) - 4 * (count + 1)
    if limit < 0:
        raise ValueError("invalid restarts")
    pos, previous = 0, b""
    while pos < limit:
        shared, pos = varint(data, pos)
        unique, pos = varint(data, pos)
        size, pos = varint(data, pos)
        if shared > len(previous) or pos + unique + size > limit:
            raise ValueError("invalid block entry")
        key = previous[:shared] + data[pos:pos + unique]
        pos += unique
        value = data[pos:pos + size]
        pos += size
        previous = key
        yield key, value


def table_records(data: bytes):
    if len(data) < 48 or data[-8:] != bytes.fromhex("57fb808b247547db"):
        raise ValueError("invalid table footer")
    pos = len(data) - 48
    _, pos = varint(data, pos)  # meta-index handle
    _, pos = varint(data, pos)
    offset, pos = varint(data, pos)
    size, _ = varint(data, pos)

    def block(start, length):
        end = start + length
        if end + 5 > len(data):
            raise ValueError("truncated table")
        raw, kind = data[start:end], data[end]
        checksum = struct.unpack_from("<I", data, end + 1)[0]
        if checksum != masked_crc(raw + bytes([kind])):
            raise ValueError("table checksum mismatch")
        if kind == 0:
            return raw
        if kind == 1:
            return snappy(raw)
        raise ValueError("unsupported table compression")

    for _, handle in block_entries(block(offset, size)):
        offset, pos = varint(handle)
        size, _ = varint(handle, pos)
        for key, value in block_entries(block(offset, size)):
            if len(key) < 8:
                raise ValueError("invalid internal key")
            packed = int.from_bytes(key[-8:], "little")
            yield packed >> 8, packed & 255, key[:-8], value


def manifest_state(data: bytes):
    files, log_number, previous_log = set(), 0, 0
    for record in log_records(data, strict=True):
        pos = 0
        while pos < len(record):
            tag, pos = varint(record, pos)
            if tag == 1:
                _, pos = blob(record, pos)
            elif tag in (2, 3, 4, 9):
                value, pos = varint(record, pos)
                if tag == 2:
                    log_number = value
                elif tag == 9:
                    previous_log = value
            elif tag == 5:
                _, pos = varint(record, pos)
                _, pos = blob(record, pos)
            elif tag in (6, 7):
                _, pos = varint(record, pos)  # level
                number, pos = varint(record, pos)
                if tag == 6:
                    files.discard(number)
                else:
                    files.add(number)
                    _, pos = varint(record, pos)  # file size
                    _, pos = blob(record, pos)
                    _, pos = blob(record, pos)
            else:
                raise ValueError("unsupported manifest version edit")
    return files, {n for n in (log_number, previous_log) if n}


def batch_records(data: bytes):
    if len(data) < 12:
        raise ValueError("truncated write batch")
    sequence, count = struct.unpack_from("<QI", data)
    pos = 12
    for i in range(count):
        if pos >= len(data):
            raise ValueError("truncated write batch")
        kind = data[pos]
        pos += 1
        key, pos = blob(data, pos)
        value = b""
        if kind == 1:
            value, pos = blob(data, pos)
        elif kind != 0:
            raise ValueError("unsupported write batch record")
        yield sequence + i, kind, key, value
    if pos != len(data):
        raise ValueError("invalid write batch length")


def chromium_string(value: bytes):
    if not value:
        return ""
    if value[0] == 0:
        return value[1:].decode("utf-16-le")
    if value[0] == 1:
        return value[1:].decode("latin-1")
    raise ValueError("unsupported Chromium string")


def unwrap(value):
    for _ in range(4):
        if isinstance(value, dict) and "data" in value:
            value = value["data"]
        elif isinstance(value, str) and value[:1] in ('"', '{', '['):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                break
            if decoded == value:
                break
            value = decoded
        else:
            break
    return value


def read_origin(path: Path):
    """Take a consistent read-only snapshot; retry if manifest/WAL rotates."""
    prefix = b"_" + ORIGIN.encode("ascii") + b"\x00"
    fields = {"token", "openid", "nickname", "phone"}
    for _ in range(3):
        try:
            current = (path / "CURRENT").read_bytes()
            name = current.decode("ascii").strip()
            if not re.fullmatch(r"MANIFEST-\d+", name):
                raise ValueError("invalid CURRENT")
            manifest = (path / name).read_bytes()
            tables, logs = manifest_state(manifest)
            selected = [next((p for p in (path / f"{n:06d}.ldb", path / f"{n:06d}.sst")
                              if p.exists()), path / f"{n:06d}.ldb") for n in tables]
            selected += [path / f"{n:06d}.log" for n in logs]
            before = [(p.stat().st_size, p.stat().st_mtime_ns) for p in selected]
            latest = {}
            for p in selected:
                data = p.read_bytes()
                records = (item for record in log_records(data) for item in batch_records(record)) \
                    if p.suffix == ".log" else table_records(data)
                for seq, kind, key, value in records:
                    if not key.startswith(prefix):
                        continue
                    field_name = chromium_string(key[len(prefix):])
                    if field_name.startswith("UNIAPP_STORAGE_"):
                        field_name = field_name[len("UNIAPP_STORAGE_"):]
                    if field_name in fields and (field_name not in latest or seq > latest[field_name][0]):
                        latest[field_name] = (seq, kind, value)
            after = [(p.stat().st_size, p.stat().st_mtime_ns) for p in selected]
            if before != after or current != (path / "CURRENT").read_bytes() or manifest != (path / name).read_bytes():
                continue
            return {key: unwrap(chromium_string(value)) for key, (_, kind, value) in latest.items() if kind == 1}
        except (OSError, ValueError, UnicodeError):
            continue
    raise SessionError("钉钉本地会话正在变化或格式不兼容，请重新打开场馆速约后重试")


@dataclass(frozen=True)
class Session:
    token: str = field(repr=False)
    openid: str = field(repr=False)
    nickname: str = field(repr=False)
    phone: str = field(repr=False)

    @property
    def expires_at(self):
        try:
            part = self.token.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            result = payload.get("exp")
            return float(result) if isinstance(result, (int, float)) else None
        except (IndexError, ValueError, TypeError, UnicodeError):
            return None

    def covers(self, epoch: float):
        # exp is only a scheduling hint. It never substitutes for server auth.
        expiry = self.expires_at
        return expiry is not None and expiry > epoch


class DingTalkSessionSource:
    def __init__(self, storage_path=""):
        self.path = Path(storage_path) if storage_path else None

    @staticmethod
    def discover():
        root = Path(os.environ.get("LOCALAPPDATA", ""))
        versions = sorted(root.glob("DingTalk_*"),
                          key=lambda p: int(p.name.split("_")[-1]) if p.name.split("_")[-1].isdigit() else -1,
                          reverse=True)
        for folder in versions:
            choices = [p for p in (folder / "Default/Local Storage/leveldb", folder / "Local Storage/leveldb")
                       if (p / "CURRENT").is_file()]
            if choices:
                return choices[0]
        raise SessionError("未找到钉钉浏览器会话；请先在钉钉打开场馆速约")

    def read(self):
        if self.path is None:
            self.path = self.discover()
        data = read_origin(self.path)
        if not all(isinstance(data.get(k), str) and data[k] for k in ("token", "openid", "nickname", "phone")):
            raise MissingSession("场馆登录信息不完整，请在钉钉重新打开场馆速约")
        return Session(**{k: data[k] for k in ("token", "openid", "nickname", "phone")})


def close_dingtalk():
    script = base64.b64encode(DINGTALK_EXIT_SCRIPT.encode("utf-16-le")).decode("ascii")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise SessionError("结束钉钉客户端时超时或失败，请检查钉钉窗口") from None
    if result.returncode:
        raise SessionError("未能确认钉钉已完全退出，请检查钉钉窗口")
    return result.stdout.strip()


def open_login(restart=False):
    global last_login_opened, last_login_restarted
    if os.name != "nt":
        raise SessionError("自动免登仅支持 Windows 钉钉客户端")
    # Unique URL forces a fresh entry even when DingTalk reuses an existing panel.
    target = ORIGIN + "/book/dingtalk/?venue_reauth=" + str(time.time_ns()) + "#/pages/boot/boot"
    uri = "dingtalk://dingtalkclient/page/link?pc_slide=true&url=" + quote(target, safe="")
    with login_open_lock:
        now = time.monotonic()
        if last_login_opened is not None and now - last_login_opened < 60 and (not restart or last_login_restarted):
            raise SessionError("已打开场馆登录入口，请等待钉钉完成登录")
        try:
            client = close_dingtalk() if restart else ""
            if client and Path(client).is_file() and Path(client).name.lower() == "dingtalk.exe":
                os.startfile(client)
                time.sleep(1)
            os.startfile(uri)
        except OSError:
            raise SessionError("无法唤起钉钉免登，请确认已安装并登录钉钉") from None
        last_login_opened = time.monotonic()
        last_login_restarted = restart


class SessionRefresher:
    def __init__(self, source, opener: Callable = open_login, timeout=15.0, attempts=2,
                 monotonic=time.perf_counter, sleep=time.sleep):
        self.source, self.opener = source, opener
        self.timeout, self.attempts = timeout, attempts
        self.monotonic, self.sleep = monotonic, sleep
        self.lock = threading.Lock()
        self.last = None

    def refresh(self, previous: Session | None, required_until: float,
                stopped: Callable = lambda: False):
        with self.lock:
            if self.last is not None and previous is not None and self.last.openid != previous.openid:
                raise SessionError("续期账户不一致，已停止")
            if self.last is not None and (previous is None or self.last.token != previous.token):
                if self.last.expires_at is None or self.last.covers(required_until):
                    return self.last
            for attempt in range(self.attempts):
                if stopped():
                    raise SessionError("续期已停止")
                self.opener()
                deadline = self.monotonic() + self.timeout
                while self.monotonic() < deadline:
                    if stopped():
                        raise SessionError("续期已停止")
                    try:
                        fresh = self.source.read()
                    except SessionError:
                        fresh = None
                    if fresh is not None:
                        if previous is not None and fresh.openid != previous.openid:
                            raise SessionError("续期后账户发生变化，已停止抢约")
                        changed = previous is None or fresh.token != previous.token
                        if changed and (fresh.expires_at is None or fresh.covers(required_until)):
                            self.last = fresh
                            return fresh
                    self.sleep(0.25)
                if attempt + 1 < self.attempts:
                    self.sleep(1.0)
            raise SessionRefreshTimeout("钉钉免登未取得新会话；请检查钉钉登录、验证码或安全提示")
