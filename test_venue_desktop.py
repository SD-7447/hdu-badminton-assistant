from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from venue_api import CHINA, Config, atomic_json
from venue_desktop import Bridge, local_config
from venue_engine import Journal
from venue_notice import fetch_notice


class DesktopTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.bridge = Bridge(self.root)
        self.bridge.send = Mock()

    def test_old_preferences_before_during_and_after_window(self):
        profile = self.root / "api-profile.json"
        Config(target_date="2026-11-24", release_days_before=3, release_override="2026-11-21 19:00:00",
               court_priority=[5], auto_refresh=False).save(profile)
        for hour, minute, second in [(0, 0, 0), (19, 59, 59), (20, 0, 15), (20, 0, 30), (23, 59, 59)]:
            now = datetime(2026, 11, 22, hour, minute, second, tzinfo=CHINA)
            config = local_config(profile, now)
            self.assertEqual(config.target_date, "2026-11-24")
            self.assertEqual(datetime.fromtimestamp(config.release_epoch(), CHINA), now.replace(hour=20, minute=0, second=0))
            self.assertEqual((config.court_priority, config.auto_refresh, config.release_override), ([5], False, ""))
        self.assertEqual(Config.load(profile).release_days_before, 3)

    def test_beijing_midnight_month_and_year_boundaries(self):
        for utc, expected in [("2026-11-22T15:59:59", "2026-11-24"), ("2026-11-22T16:00:00", "2026-11-25"),
                              ("2026-11-29T16:00:00", "2026-12-02"), ("2026-12-30T16:00:00", "2027-01-02")]:
            self.assertEqual(Config.for_today(now=datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)).target_date, expected)

    def test_dated_record_preserves_legacy_unknown(self):
        legacy = {"version": 2, "target_date": "2026-11-22", "state": "UNKNOWN"}
        path = self.root / "run-api-journal.json"
        atomic_json(path, legacy)
        before = path.read_bytes()
        other = Journal.for_date(self.root, "2026-11-24")
        self.assertFalse(other.data)
        same = Journal.for_date(self.root, "2026-11-22")
        self.assertEqual(same.data, legacy)
        same.save("SUCCESS")
        self.assertEqual(Journal.for_date(self.root, "2026-11-22").data["state"], "SUCCESS")
        self.assertIsNone(self.bridge.status(Config(target_date="2026-11-24"))["recovery"])
        self.assertEqual(path.read_bytes(), before)

    def test_notice_failure_and_withdrawal(self):
        original = {"body": "第一行\n第二行", "synced": datetime.now(CHINA).isoformat(), "published": ""}
        empty = {**original, "body": ""}
        with patch("venue_desktop.fetch_notice", side_effect=[original, TimeoutError(), empty]):
            for expected in [original, original, empty]:
                self.bridge.notice_lock.acquire()
                self.bridge.refresh_notice()
                self.assertEqual(self.bridge.cached_notice(), expected)
        events = [c.args[0]["event"] for c in self.bridge.send.call_args_list]
        self.assertTrue(events[1]["failed"])
        self.assertEqual(events[2]["notice"]["body"], "")

    def test_notice_deferred_until_release_window_ends(self):
        config = Config(target_date="2026-11-24")
        self.bridge.display_config = config
        for offset, deferred in [(-61, False), (-60, True), (0, True), (29.99, True), (30, False)]:
            with patch("venue_desktop.time.time", return_value=config.release_epoch() + offset):
                self.assertEqual(self.bridge.notice_delay() > 0, deferred)
                if deferred:
                    reply, callback = self.bridge.dispatch({"action": "notice"})
                    self.assertGreater(reply["deferMs"], 0)
                    self.assertIsNone(callback)

    def test_open_login_leaves_waiting_task_armed_and_respects_release_window(self):
        config = Config(target_date="2026-11-24")
        self.bridge.active_config = config
        self.bridge.task = Mock()
        self.bridge.stop = Mock()
        with patch("venue_desktop.open_login") as open_entry:
            with patch("venue_desktop.time.time", return_value=config.release_epoch() - 61):
                self.bridge.dispatch({"action": "openVenueLogin"})
            for offset in (-60, 0, 30):
                with patch("venue_desktop.time.time", return_value=config.release_epoch() + offset):
                    with self.assertRaises(ValueError):
                        self.bridge.dispatch({"action": "openVenueLogin"})
            open_entry.assert_called_once()
        self.bridge.stop.set.assert_not_called()

    def test_manual_restart_is_queued_on_active_task_only_before_cutoff(self):
        config = Config(target_date="2026-11-24")
        self.bridge.active_config = config
        self.bridge.task = Mock()
        self.bridge.stop = Mock()
        self.bridge.restart_login = threading.Event()
        with patch("venue_desktop.time.time", return_value=config.release_epoch() - 61):
            self.bridge.dispatch({"action": "restartDingTalk"})
        self.assertTrue(self.bridge.restart_login.is_set())
        self.bridge.restart_login.clear()
        with patch("venue_desktop.time.time", return_value=config.release_epoch() - 60):
            with self.assertRaises(ValueError):
                self.bridge.dispatch({"action": "restartDingTalk"})
        self.assertFalse(self.bridge.restart_login.is_set())
        self.bridge.stop.set.assert_not_called()

    def test_notice_rejects_auth_or_invalid_response_and_keeps_exact_text(self):
        with patch("venue_notice.http.client.HTTPSConnection") as factory:
            response = factory.return_value.getresponse.return_value
            for status, raw in [(401, b'{}'), (200, b'{"code":401,"message":"expired"}'), (200, b'{}')]:
                response.status = status
                response.read.return_value = raw
                with self.assertRaises(ValueError):
                    fetch_notice()
            response.read.return_value = b'{"message":"First\\nSecond", "published_at":"2026-11-22"}'
            data = fetch_notice()
            self.assertEqual((data["body"], data["published"]), ("First\nSecond", "2026-11-22"))

