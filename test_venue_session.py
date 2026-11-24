"""Synthetic LevelDB fixtures; no real credentials enter the test artifacts."""
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from venue_session import (ORIGIN, DingTalkSessionSource, Session, SessionError,
                           SessionRefresher, chromium_string, log_records, masked_crc, open_login,
                           read_origin, snappy, table_records, unwrap)


def vint(n):
    result = bytearray()
    while n >= 128:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def blob(value):
    return vint(len(value)) + value


def record(data, kind=1):
    return struct.pack("<IHB", masked_crc(bytes([kind]) + data), len(data), kind) + data


def key(name, origin=ORIGIN):
    return b"_" + origin.encode() + b"\0\1" + name.encode()


def batch(sequence, values):
    data = struct.pack("<QI", sequence, len(values))
    for k, value in values:
        data += bytes([0 if value is None else 1]) + blob(k)
        if value is not None:
            data += blob(b"\1" + value.encode())
    return record(data)


def block(entries):
    data = b""
    for k, v in entries:
        data += vint(0) + vint(len(k)) + vint(len(v)) + k + v
    return data + struct.pack("<II", 0, 1)


def table(entries):
    raw = block([(k + struct.pack("<Q", (seq << 8) | kind), v) for seq, kind, k, v in entries])
    data = raw + b"\0" + struct.pack("<I", masked_crc(raw + b"\0"))
    index = block([(b"last", vint(0) + vint(len(raw)))])
    index_offset = len(data)
    data += index + b"\0" + struct.pack("<I", masked_crc(index + b"\0"))
    footer = (vint(0) + vint(0) + vint(index_offset) + vint(len(index))).ljust(40, b"\0")
    return data + footer + bytes.fromhex("57fb808b247547db")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / "CURRENT").write_bytes(b"MANIFEST-000001\n")
        (self.path / "MANIFEST-000001").write_bytes(record(vint(2) + vint(2)))
        (self.path / "000002.log").write_bytes(b"")

    def test_latest_put_delete_origin_scope_and_numeric_string(self):
        data = batch(10, [(key("token"), '"old"'), (key("openid"), '{"type":"string","data":"00123"}'),
                          (key("phone"), '"000123"'), (key("nickname"), '"test"')])
        data += batch(20, [(key("token"), '"new"'), (key("token", "https://unrelated.example"), '"foreign"')])
        (self.path / "000002.log").write_bytes(data)
        self.assertEqual(read_origin(self.path)["token"], "new")
        self.assertEqual(read_origin(self.path)["openid"], "00123")
        self.assertEqual(read_origin(self.path)["phone"], "000123")
        (self.path / "000002.log").write_bytes(data + batch(30, [(key("token"), None)]))
        self.assertNotIn("token", read_origin(self.path))
        with self.assertRaises(SessionError):
            DingTalkSessionSource(str(self.path)).read()

    def test_ignores_obsolete_log_with_larger_sequence(self):
        (self.path / "000001.log").write_bytes(batch(999, [(key("token"), '"obsolete"')]))
        (self.path / "000002.log").write_bytes(batch(1, [(key("token"), '"active"')]))
        self.assertEqual(read_origin(self.path)["token"], "active")

    def test_table_decode_and_manifest_deleted_table_ignored(self):
        data = table([(10, 1, key("token"), b'\1"table-token"')])
        self.assertEqual(list(table_records(data))[0][3], b'\1"table-token"')
        (self.path / "000003.ldb").write_bytes(data)
        new_file = vint(7) + vint(0) + vint(3) + vint(len(data)) + blob(b"a") + blob(b"z")
        manifest = record(vint(2) + vint(2) + new_file)
        (self.path / "MANIFEST-000001").write_bytes(manifest)
        self.assertEqual(read_origin(self.path)["token"], "table-token")
        (self.path / "MANIFEST-000001").write_bytes(manifest + record(vint(6) + vint(0) + vint(3)))
        self.assertNotIn("token", read_origin(self.path))

    def test_wal_delete_wins_over_table(self):
        data = table([(10, 1, key("token"), b'\1"old"')])
        (self.path / "000003.ldb").write_bytes(data)
        new_file = vint(7) + vint(0) + vint(3) + vint(len(data)) + blob(b"a") + blob(b"z")
        (self.path / "MANIFEST-000001").write_bytes(record(vint(2) + vint(2) + new_file))
        (self.path / "000002.log").write_bytes(batch(11, [(key("token"), None)]))
        self.assertNotIn("token", read_origin(self.path))

    def test_truncated_tail_is_not_applied_and_corruption_fails_closed(self):
        good = batch(1, [(key("token"), '"complete"')])
        (self.path / "000002.log").write_bytes(good + batch(2, [(key("token"), '"partial"')])[:-3])
        self.assertEqual(read_origin(self.path)["token"], "complete")
        (self.path / "000002.log").write_bytes(good[:-1] + b"X")
        with self.assertRaises(SessionError):
            read_origin(self.path)

    def test_snappy_literals_and_overlapping_copy(self):
        self.assertEqual(snappy(b"\x06\x08abc\x0a\x03\x00"), b"abcabc")
        self.assertEqual(snappy(b"\x08\x00a\x1a\x01\x00"), b"aaaaaaaa")
        with self.assertRaises(ValueError):
            snappy(b"\x06\x0a\x00\x00")

    def test_chromium_unicode_and_opaque_token(self):
        self.assertEqual(chromium_string(b"\0" + "羽毛球".encode("utf-16-le")), "羽毛球")
        self.assertIsNone(Session("opaque", "id", "name", "phone").expires_at)
        self.assertEqual(unwrap('{"type":"string","data":"0123"}'), "0123")


class RenewalTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DingTalk protocol")
    def test_restart_exits_then_opens_client_and_venue_once(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "DingTalk.exe"
            exe.touch()
            steps = Mock()
            steps.close.return_value = str(exe)
            with patch("venue_session.close_dingtalk", steps.close), patch("venue_session.os.startfile", steps.open), \
                    patch("venue_session.time.sleep"), patch("venue_session.last_login_opened", None):
                open_login(restart=True)
                with self.assertRaises(SessionError):
                    open_login(restart=True)
            self.assertEqual([call[0] for call in steps.mock_calls], ["close", "open", "open"])
            self.assertEqual(steps.open.call_args_list[0].args[0], str(exe))
            self.assertTrue(steps.open.call_args_list[1].args[0].startswith("dingtalk://"))

    def test_refresh_reuses_logged_in_sso_and_requires_new_token(self):
        old = Session("old", "id", "n", "p")
        new = Session("new", "id", "n", "p")
        source, opener = Mock(), Mock()
        source.read.side_effect = [old, new]
        refresh = SessionRefresher(source, opener, sleep=lambda _: None)
        self.assertEqual(refresh.refresh(old, 100), new)
        self.assertEqual(refresh.refresh(old, 100), new)
        opener.assert_called_once()

    def test_refresh_account_change_is_rejected(self):
        source = Mock()
        source.read.return_value = Session("new", "other", "n", "p")
        with self.assertRaises(SessionError):
            SessionRefresher(source, Mock()).refresh(Session("old", "own", "n", "p"), 100)

    def test_logout_timeout_and_stop_are_bounded(self):
        class Timer:
            value = 0
            def now(self): return self.value
            def sleep(self, seconds): self.value += seconds
        timer = Timer()
        source, opener = Mock(), Mock()
        source.read.side_effect = SessionError("logged out")
        r = SessionRefresher(source, opener, timeout=1, monotonic=timer.now, sleep=timer.sleep)
        with self.assertRaises(SessionError):
            r.refresh(Session("old", "id", "n", "p"), 100)
        self.assertEqual(opener.call_count, 2)
        self.assertLessEqual(timer.value, 3.1)
        with self.assertRaises(SessionError):
            r.refresh(None, 100, stopped=lambda: True)
        self.assertEqual(opener.call_count, 2)

    def test_concurrent_expiry_only_opens_sso_once(self):
        old, new = Session("old", "id", "n", "p"), Session("new", "id", "n", "p")
        source, opener = Mock(), Mock()
        source.read.return_value = new
        refresher = SessionRefresher(source, opener)
        results = []
        threads = [threading.Thread(target=lambda: results.append(refresher.refresh(old, 100))) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(results), 4)
        opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
