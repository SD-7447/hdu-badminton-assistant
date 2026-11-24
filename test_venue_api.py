"""Offline protocol/booking failure tests. Never contact the real venue service."""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from venue_api import (ApiError, CHINA, Config, HttpTransport, PRIORITY, RateGate,
                       Response, ServerClock, Snapshot, VenueApi, minute)
from venue_engine import BookingEngine, Journal, NeedAuth, Stopped, account_tag
from venue_session import MissingSession, Session, SessionError, SessionRefresher
from venue_supervisor import Supervisor

EPOCH = datetime(2026, 11, 19, 20, tzinfo=CHINA).timestamp()


def session(exp=EPOCH + 3600, identity="own-test-account", suffix="a"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return Session("header." + payload + "." + suffix, identity, "test", "00000000000")


def config(**kwargs):
    values = dict(target_date="2026-11-22", release_override="2026-11-19 20:00:00")
    values.update(kwargs)
    return Config(**values).validate()


def grid(sites=(6, 7), times=None, closed=(), used=None, exceptions=None, base=0):
    times = times or ["10:00", "11:00", "12:00"]
    meta = {"time_list": times, "rest_list": list(closed), "base_price": base, "days_can_appoint": 2}
    situation = {"site_situation": [{"site_id": n, "book_time_list": list((used or {}).get(n, [])),
                                      "oci_time_list": list((exceptions or {}).get(n, []))} for n in sites],
                 "extra_list": {"extra_price_time": [], "extra_price_list": []}}
    return meta, situation


class FakeClock:
    rtt_ms = 5.0

    def __init__(self, epoch=EPOCH + 0.01):
        self.epoch = epoch

    def calibrate(self, api):
        pass

    def now(self):
        return self.epoch

    def deadline(self, epoch):
        return time.perf_counter() + epoch - self.epoch


class FakePoller:
    def __init__(self, api, config, metadata, clock, initial):
        self.latest = initial
        self.error = None

    def start(self):
        pass

    def stop(self):
        pass

    def view(self):
        return self.latest, self.error, 1, time.perf_counter()


class FakeApi:
    def __init__(self, metadata=None, situation=None):
        self.meta, self.sit = grid() if metadata is None else (metadata, situation)
        self.calls = []
        self.order_list = []
        self.prepare_actions = []
        self.commit_actions = []
        self.cleanup_actions = []
        self.held = None
        self.journal = None

    def metadata(self, cfg):
        self.calls.append("metadata")
        return self.meta

    def situation(self, cfg):
        self.calls.append("situation")
        return self.sit

    def orders(self, ses):
        self.calls.append("orders")
        return list(self.order_list)

    def prepare(self, payload):
        self.calls.append(("prepare", payload))
        assert self.journal.data["state"] == "PREPARE_SENT"
        if self.prepare_actions:
            action = self.prepare_actions.pop(0)
            if isinstance(action, Exception):
                raise action
            self.held = payload["orderData"]
            return action
        self.held = payload["orderData"]
        return {"conflict_times": [], "available_times": payload["orderData"]["time_list"]}

    def commit(self, payload):
        self.calls.append(("commit", payload))
        assert self.held is not None
        assert self.journal.data["state"] == "COMMIT_SENT"
        if self.commit_actions:
            action = self.commit_actions.pop(0)
            if isinstance(action, Exception):
                raise action
            if callable(action):
                return action(payload)
        data = payload["orderData"]
        order = {k: data[k] for k in ("site_id", "start_time", "end_time", "venue_name")}
        order.update(order_date=data["date"], order_num="fake-order-1", with_friends=False)
        self.order_list.append(order)
        return {"data": order}

    def release_hold(self, ses):
        self.calls.append("cleanup")
        assert self.journal.data["state"] == "CLEANUP_SENT"
        if self.cleanup_actions:
            raise self.cleanup_actions.pop(0)
        self.held = None


class CandidateTests(unittest.TestCase):
    def test_default_date_offset_cross_year(self):
        c = Config(target_date="2027-01-02").validate()
        self.assertEqual(datetime.fromtimestamp(c.release_epoch(), CHINA).isoformat(), "2026-12-31T20:00:00+08:00")

    def test_exact_court_priority_and_duration_first(self):
        s = Snapshot(*grid(sites=tuple(range(1, 13))))
        candidates = s.candidates(config())
        self.assertEqual([c.site_id for c in candidates if c.minutes == 120], PRIORITY)
        s = Snapshot(*grid(used={6: [1]}))
        self.assertEqual((s.candidates(config())[0].site_id, s.candidates(config())[0].minutes), (7, 120))

    def test_real_minutes_and_start_boundary(self):
        times = ["08:00", "09:00", "10:00", "11:00", "11:40", "13:20", "14:00", "15:00"]
        candidates = Snapshot(*grid(times=times)).candidates(config())
        self.assertTrue(all(minute(c.start) >= 600 and 60 <= c.minutes <= 120 for c in candidates))
        self.assertTrue(any(c.start == "10:00" for c in candidates))
        self.assertTrue(any(c.start == "11:40" and c.minutes == 100 for c in candidates))
        self.assertFalse(any(c.start == "11:00" and c.end == "11:40" for c in candidates))

    def test_xor_opening_and_actual_site_id(self):
        s = Snapshot(*grid(sites=(8,), closed=(0, 1), exceptions={8: [1]}))
        self.assertEqual([(c.site_id, c.start) for c in s.candidates(config())], [(8, "11:00")])

    def test_no_gap_or_more_than_account_slot_limit(self):
        s = Snapshot(*grid(times=["10:00", "10:30", "11:00", "11:30", "12:00"], used={6: [1], 7: [1]}))
        self.assertTrue(all(c.indices == (2, 3) and c.minutes == 60 for c in s.candidates(config())))
        self.assertFalse(s.candidates(config(), remaining_slots=1))

    def test_free_only_and_extra_price(self):
        meta, sit = grid()
        sit["extra_list"] = {"extra_price_time": [1], "extra_price_list": [10]}
        candidates = Snapshot(meta, sit).candidates(config())
        self.assertTrue(all(c.indices == (0,) and c.price == 0 for c in candidates))

    def test_expired_times_and_deterministic_tie(self):
        c = config(target_date="2026-11-19")
        now = datetime(2026, 11, 19, 10, 1, tzinfo=CHINA).timestamp()
        candidates = Snapshot(*grid()).candidates(c, now_epoch=now)
        self.assertTrue(all(x.start == "11:00" for x in candidates))

    def test_bad_schema_and_unsafe_config_stop(self):
        meta, sit = grid()
        sit["site_situation"][0]["site_id"] = "6"
        with self.assertRaises(ApiError):
            Snapshot(meta, sit)
        for changes in ({"earliest_start": "09:00"}, {"court_priority": [6, 6]}, {"min_minutes": 40},
                        {"fast_poll_seconds": 0.01}, {"max_slots_per_day": 3}, {"free_only": False}):
            with self.assertRaises(ValueError):
                config(**changes)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "journal.json"
        self.c = config()
        self.s = session()
        self.events = []
        self.api = FakeApi()
        self.poller_patch = patch("venue_engine.Poller", FakePoller)
        self.poller_patch.start()
        self.addCleanup(self.poller_patch.stop)

    def engine(self, journal=None):
        j = journal or Journal(self.path)
        self.api.journal = j
        engine = BookingEngine(self.c, self.s, self.api, j, self.events.append, clock=FakeClock())
        engine.wait = lambda seconds: setattr(engine.clock, "epoch", engine.clock.epoch + max(0, seconds))
        return engine

    def writes(self, name):
        return [call for call in self.api.calls if isinstance(call, tuple) and call[0] == name]

    def test_dry_run_is_strictly_read_only(self):
        self.assertEqual(self.engine().run(dry=True), "PREVIEW")
        self.assertFalse(self.writes("prepare") or self.writes("commit"))
        self.assertNotIn("cleanup", self.api.calls)
        self.assertFalse(self.path.exists())

    def test_success_one_order_and_write_ahead_journal(self):
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual((len(self.writes("prepare")), len(self.writes("commit"))), (1, 1))
        self.assertEqual(self.writes("commit")[0][1]["orderData"]["site_id"], 6)
        self.assertEqual(Journal(self.path).data["state"], "SUCCESS")
        self.assertNotIn(self.s.token, self.path.read_text())
        self.assertNotIn(self.s.openid, self.path.read_text())

    def test_existing_booking_blocks_any_new_order(self):
        self.api.order_list = [{"order_date": self.c.target_date, "venue_name": self.c.venue_name, "order_num": "existing"}]
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertFalse(self.writes("prepare"))

    def test_partial_keeps_one_hour_when_no_better_candidate(self):
        self.api.meta, self.api.sit = grid(sites=(6,))
        self.api.prepare_actions = [{"conflict_times": [0], "available_times": [1]}]
        self.assertEqual(self.engine().run(), "SUCCESS")
        data = self.writes("commit")[0][1]["orderData"]
        self.assertEqual((data["start_time"], data["end_time"]), ("11:00", "12:00"))
        self.assertNotIn("cleanup", self.api.calls)

    def test_partial_releases_before_better_two_hours(self):
        self.api.prepare_actions = [{"conflict_times": [0], "available_times": [1]}]
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(self.api.calls.count("cleanup"), 1)
        self.assertEqual(self.writes("commit")[0][1]["orderData"]["site_id"], 7)

    def test_partial_under_one_hour_never_commits(self):
        self.api.meta, self.api.sit = grid(sites=(6,), times=["10:00", "10:40", "11:40"])
        self.api.prepare_actions = [{"conflict_times": [1], "available_times": [0]}]
        e = self.engine()
        e.snapshot = Snapshot(self.api.meta, self.api.sit)
        e.journal.bind(self.c, self.s, [])
        candidate = e.snapshot.candidates(self.c)[0]
        self.assertIsNone(e.prepare(candidate))
        self.assertNotIn("commit", [c[0] for c in self.api.calls if isinstance(c, tuple)])
        self.assertIn("cleanup", self.api.calls)

    def test_conflict_moves_to_next_candidate(self):
        self.api.prepare_actions = [{"conflict_times": [0, 1], "available_times": []}]
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(self.writes("commit")[0][1]["orderData"]["site_id"], 7)

    def test_no_release_then_opens(self):
        self.api.prepare_actions = [ApiError("NOT_RELEASED", "not yet")]
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(len(self.writes("prepare")), 2)

    def test_rate_limit_prepare_and_commit_retain_phase(self):
        self.api.prepare_actions = [ApiError("RATE", "rate", 1)]
        self.api.commit_actions = [ApiError("RATE", "rate", 1)]
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual((len(self.writes("prepare")), len(self.writes("commit"))), (2, 2))

    def test_cleanup_rate_never_starts_new_prepare_before_release(self):
        self.api.prepare_actions = [{"conflict_times": [0], "available_times": [1]}]
        self.api.cleanup_actions = [ApiError("RATE", "rate", 1)]
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(self.api.calls.count("cleanup"), 2)

    def test_prepare_auth_rejected_is_safe_to_resume_after_renewal(self):
        self.api.prepare_actions = [ApiError("AUTH", "expired")]
        with self.assertRaises(NeedAuth):
            self.engine().run()
        self.assertEqual(Journal(self.path).data["state"], "READY")
        self.assertEqual(self.engine().run(), "SUCCESS")

    def test_auth_between_phases_resumes_commit_without_reprepare(self):
        self.api.commit_actions = [ApiError("AUTH", "expired")]
        with self.assertRaises(NeedAuth):
            self.engine().run()
        self.assertEqual(Journal(self.path).data["state"], "PREPARED")
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(len(self.writes("prepare")), 1)
        self.assertEqual(len(self.writes("commit")), 2)

    def test_lost_commit_response_reconciles_without_repeating(self):
        def lost(payload):
            data = payload["orderData"]
            self.api.order_list.append({"order_date": data["date"], "venue_name": data["venue_name"], "order_num": "saved"})
            raise ApiError("UNCERTAIN", "connection lost")
        self.api.commit_actions = [lost]
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(len(self.writes("commit")), 1)
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(len(self.writes("commit")), 1)

    def test_unknown_prepare_persists_and_restart_never_resends(self):
        self.api.prepare_actions = [ApiError("UNCERTAIN", "timeout")]
        with patch("venue_engine.time.sleep"):
            self.assertEqual(self.engine().run(), "UNKNOWN")
            self.assertEqual(self.engine().run(), "UNKNOWN")
        self.assertEqual(len(self.writes("prepare")), 1)
        self.assertFalse(self.writes("commit"))

    def test_pending_task_cannot_change_date_policy_or_account(self):
        e = self.engine()
        e.journal.bind(self.c, self.s, [])
        e.journal.save("UNKNOWN")
        with self.assertRaises(RuntimeError):
            Journal(self.path).bind(replace(self.c, target_date="2026-11-23"), self.s, [])
        with self.assertRaises(RuntimeError):
            Journal(self.path).bind(self.c, session(identity="other"), [])
        with self.assertRaises(RuntimeError):
            Journal(self.path).bind(replace(self.c, court_priority=[7, 6]), self.s, [])

    def test_expiry_hint_does_not_replace_successful_server_check(self):
        for exp in (EPOCH - 1, EPOCH + 30):
            self.s = session(exp)
            self.engine().verify_login()
        self.assertEqual([event["phase"] for event in self.events], ["verified"] * 2)

    def test_deadline_does_not_book_late(self):
        e = self.engine()
        e.clock.epoch = EPOCH + 31
        self.assertEqual(e.run(), "EXPIRED")
        self.assertFalse(self.writes("prepare"))

    def test_stop_before_write_and_other_sport_overlap(self):
        e = self.engine()
        e.stop_event.set()
        with self.assertRaises(Stopped):
            e.run()
        self.assertFalse(self.writes("prepare"))
        e = self.engine()
        e.own_orders = [{"order_date": self.c.target_date, "venue_name": "other", "start_time": "10:30", "end_time": "11:30"}]
        self.assertFalse(e.candidates(Snapshot(*grid())))

    def test_read_preflight_rate_limit_is_retried_after_wait(self):
        e = self.engine()
        original = self.api.metadata
        self.api.metadata = Mock(side_effect=[ApiError("RATE", "rate", 1), original(self.c)])
        self.assertEqual(e.run(), "SUCCESS")
        self.assertEqual(self.api.metadata.call_count, 2)
        self.assertGreaterEqual(e.clock.epoch, EPOCH + 1)

    def test_long_wait_uses_low_frequency_checks_and_early_preflight(self):
        e = self.engine()
        e.clock.epoch = EPOCH - 4 * 3600
        e.wait_until = lambda t: setattr(e.clock, "epoch", t)
        e.run()
        observed = [v["verified_at"] - EPOCH for v in self.events if v.get("phase") == "verified"]
        self.assertEqual(observed, [*range(-4 * 3600, 0, 15 * 60), -600, -180, -120, -60])

    def test_expiring_session_is_rechecked_without_forcing_a_login(self):
        self.s = session(exp=EPOCH - 480)
        e = self.engine()
        e.clock.epoch = EPOCH - 720
        e.wait_until = lambda t: setattr(e.clock, "epoch", t)
        self.assertEqual(e.run(), "SUCCESS")
        observed = [v["verified_at"] - EPOCH for v in self.events if v.get("phase") == "verified"]
        self.assertEqual(observed, [-720, -600, -480, -180, -120, -60])

    def test_page_check_triggers_at_preflight_without_sending_a_booking(self):
        e = self.engine()
        e.page_check = True
        e.clock.epoch = EPOCH - 720
        e.wait_until = lambda t: setattr(e.clock, "epoch", t)
        with self.assertRaises(NeedAuth) as error:
            e.run()
        self.assertEqual((error.exception.reason, e.clock.epoch), ("PAGE_CHECK", EPOCH - 180))
        self.assertFalse(self.writes("prepare"))

    def test_page_check_is_skipped_in_final_minute_or_preview(self):
        for epoch, dry, outcome in [(EPOCH - 60, False, "SUCCESS"), (EPOCH - 180, True, "PREVIEW")]:
            e = self.engine()
            e.page_check = True
            e.clock.epoch = epoch
            e.wait_until = lambda t: setattr(e.clock, "epoch", t)
            self.assertEqual(e.run(dry=dry), outcome)

    def test_manual_restart_interrupts_the_wait_without_dispatching_booking(self):
        e = self.engine()
        e.clock.epoch = EPOCH - 500
        e.restart_requested = threading.Event()
        e.restart_requested.set()
        with self.assertRaises(NeedAuth) as error:
            e.wait_until(EPOCH - 180)
        self.assertEqual(error.exception.reason, "MANUAL_RESTART")
        self.assertFalse(self.writes("prepare"))

    def test_local_session_changes_pause_before_using_stale_login(self):
        for result, reason in [(session(suffix="new"), "SESSION_CHANGED"), (session(identity="other"), "ACCOUNT_CHANGED"),
                               (MissingSession(), "NO_SESSION")]:
            e = self.engine()
            e.read_session = Mock(side_effect=[result])
            with self.assertRaises(NeedAuth) as error:
                e.verify_login()
            self.assertEqual(error.exception.reason, reason)
        self.assertNotIn("orders", self.api.calls)

    def test_transient_local_storage_rotation_falls_back_to_server_check(self):
        e = self.engine()
        e.read_session = Mock(side_effect=SessionError("storage busy"))
        e.verify_login()
        self.assertEqual(self.api.calls, ["orders"])

    def test_server_revokes_valid_session_while_waiting(self):
        e = self.engine()
        e.clock.epoch = EPOCH - 180
        e.wait_until = lambda t: setattr(e.clock, "epoch", t)
        self.api.orders = Mock(side_effect=[[], ApiError("AUTH", "expired")])
        with self.assertRaises(NeedAuth):
            e.run()
        self.assertEqual(e.clock.epoch, EPOCH - 120)
        self.assertFalse(self.writes("prepare"))

    def test_valid_session_with_unknown_expiry_reaches_release(self):
        self.s = session(exp=None)
        e = self.engine()
        e.clock.epoch = EPOCH - 180
        e.wait_until = lambda t: setattr(e.clock, "epoch", t)
        self.assertEqual(e.run(), "SUCCESS")
        self.assertEqual(len(self.writes("prepare")), 1)

    def test_network_loss_does_not_trigger_auth_recovery(self):
        self.api.orders = Mock(side_effect=[ApiError("UNCERTAIN", "offline")] * 3 + [[]])
        e = self.engine()
        e.verify_login()
        self.assertEqual([v["phase"] for v in self.events], ["unverified", "verified"])

    def test_unknown_prepare_reconciles_without_replay_after_login(self):
        e = self.engine()
        e.journal.bind(e.config, e.session, [])
        e.journal.save("UNKNOWN", Snapshot(*grid()).candidates(self.c)[0], resume="prepare")
        self.assertEqual(e.run(), "UNKNOWN")
        self.assertFalse(self.writes("prepare") or self.writes("commit"))

    def test_no_availability_stops_at_deadline_without_writes(self):
        self.api.meta, self.api.sit = grid(closed=(0, 1))
        e = self.engine()
        self.assertEqual(e.run(), "NO_AVAILABILITY")
        self.assertFalse(self.writes("prepare"))

    def test_cleanup_auth_resumes_cleanup_before_new_booking(self):
        self.api.prepare_actions = [{"conflict_times": [0], "available_times": [1]}]
        self.api.cleanup_actions = [ApiError("AUTH", "expired")]
        with self.assertRaises(NeedAuth):
            self.engine().run()
        pending = Journal(self.path)
        self.assertEqual((pending.data["state"], pending.data["resume"]), ("PREPARED", "cleanup"))
        self.assertEqual(self.engine().run(), "SUCCESS")
        self.assertEqual(self.api.calls.count("cleanup"), 2)

    def test_journal_write_failure_prevents_http_dispatch(self):
        e = self.engine()
        e.snapshot = Snapshot(*grid())
        candidate = e.snapshot.candidates(self.c)[0]
        with patch("venue_engine.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                e.prepare(candidate)
        self.assertFalse(self.writes("prepare"))

    def test_lost_cleanup_ack_does_not_switch_to_another_court(self):
        self.api.prepare_actions = [{"conflict_times": [0], "available_times": [1]}]
        self.api.cleanup_actions = [ApiError("UNCERTAIN", "lost acknowledgement")]
        with self.assertRaises(ApiError):
            self.engine().run()
        self.assertEqual(Journal(self.path).data["state"], "UNKNOWN")
        self.assertEqual(len(self.writes("prepare")), 1)
        self.assertFalse(self.writes("commit"))

    def test_no_prepare_dispatched_if_journaling_crosses_deadline(self):
        e = self.engine()
        e.journal.bind(self.c, self.s, [])
        save = e.journal.save
        def delayed_save(state, *args, **kwargs):
            save(state, *args, **kwargs)
            if state == "PREPARE_SENT":
                e.clock.epoch = EPOCH + 31
        e.journal.save = delayed_save
        candidate = Snapshot(*grid()).candidates(self.c)[0]
        self.assertIsNone(e.prepare(candidate))
        self.assertFalse(self.writes("prepare"))
        self.assertEqual(Journal(self.path).data["state"], "READY")

    def test_confirmed_hold_finishes_when_prepare_response_crosses_deadline(self):
        e = self.engine()
        e.clock.epoch = EPOCH + 29.99
        prepare = self.api.prepare
        def slow_response(payload):
            result = prepare(payload)
            e.clock.epoch = EPOCH + 31
            return result
        self.api.prepare = slow_response
        self.assertEqual(e.run(), "SUCCESS")
        self.assertEqual(len(self.writes("prepare")), 1)
        self.assertEqual(len(self.writes("commit")), 1)


class HttpTests(unittest.TestCase):
    def transport(self, status=200, body=None, headers=None, gate=None):
        connection = Mock()
        reply = Mock(status=status)
        reply.read.return_value = json.dumps(body or {}).encode()
        reply.getheader.side_effect = lambda name, default=None: (headers or {}).get(name, default)
        connection.getresponse.return_value = reply
        transport = HttpTransport(session(), gate=gate)
        transport.connection = connection
        return transport, connection

    def test_two_phase_payload_normal_auth_and_no_implicit_retries(self):
        transport, connection = self.transport()
        payload = Snapshot(*grid()).candidates(config())[0].payload(session(), config())
        transport.request("creat_book_info", payload)
        args = connection.request.call_args.args
        self.assertEqual(args[0:2], ("POST", "/book/client/creat_book_info"))
        self.assertEqual(json.loads(args[2])["orderData"]["time_list"], [0, 1])
        self.assertEqual(args[3]["Authorization"], "Bearer " + session().token)
        connection.request.side_effect = OSError("closed")
        with self.assertRaises(ApiError) as ctx:
            transport.request("creat_order", payload)
        self.assertEqual(ctx.exception.kind, "UNCERTAIN")
        self.assertEqual(connection.request.call_count, 2)

    def test_http_auth_permission_limit_and_business_201(self):
        for status, body, kind in [(401, {}, "AUTH"), (403, {}, "LIMIT"),
                                   (200, {"message": "token已过期"}, "AUTH"),
                                   (200, {"message": "游客无法预约"}, "AUTH"),
                                   (201, {"message": "尚未开放"}, "NOT_RELEASED"),
                                   (201, {"message": "超过预约上限"}, "LIMIT")]:
            t, _ = self.transport(status, body)
            with self.assertRaises(ApiError) as ctx:
                t.request("anything", {})
            self.assertEqual(ctx.exception.kind, kind)

    def test_429_shared_gate_stops_other_channel(self):
        gate = RateGate()
        reader, _ = self.transport(429, headers={"Retry-After": "2"}, gate=gate)
        writer, connection = self.transport(gate=gate)
        with self.assertRaises(ApiError):
            reader.request("post_site_situation", {})
        with self.assertRaises(ApiError) as ctx:
            writer.request("creat_book_info", {})
        self.assertEqual(ctx.exception.kind, "RATE")
        connection.request.assert_not_called()

    def test_server_clock_uses_lowest_rtt_and_monotonic(self):
        ticks = iter([1, 1.1, 2, 2.01, 3, 3.04, 4])
        clock = ServerClock(monotonic=lambda: next(ticks))
        api = Mock()
        api.server_time.side_effect = [101.05, 102.005, 103.02, 104]
        clock.calibrate(api, samples=3)
        self.assertAlmostEqual(clock.rtt_ms, 10)
        self.assertAlmostEqual(clock.now(), 104)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        isolated_lock = patch("venue_supervisor.RunLock")
        isolated_lock.start()
        self.addCleanup(isolated_lock.stop)

    def test_wait_for_login_suppresses_repeated_probes_of_rejected_token(self):
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH), \
                patch("venue_supervisor.VenueApi.connect") as connect:
            probe = connect.return_value
            probe.orders.side_effect = [ApiError("AUTH", "test-sensitive-response"), []]
            events = []
            s, new = session(), session(suffix="new")
            source = Mock()
            source.read.side_effect = [s, s, new]
            refresher = Mock()
            manager = Supervisor(config(), folder, events.append, source=source, refresher=refresher)
            manager.stop_event.wait = Mock(return_value=False)
            self.assertIs(manager.wait_for_login(account_tag(s), "AUTH"), new)
            self.assertEqual(manager.stop_event.wait.call_count, 2)
            self.assertEqual(sum(bool(e.get("notify")) for e in events), 1)
            self.assertEqual(probe.close.call_count, 2)
            self.assertEqual([call.args[0] for call in probe.orders.call_args_list], [s, new])
            refresher.refresh.assert_not_called()
            log = (Path(folder) / "login-events.jsonl").read_text("utf-8")
            for secret in (s.token, s.openid, s.phone, "test-sensitive-response"):
                self.assertNotIn(secret, log)

    def test_changed_account_never_resumes_and_wait_can_stop(self):
        from venue_engine import Stopped
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH), \
                patch("venue_supervisor.VenueApi.connect") as connect:
            source = Mock()
            source.read.return_value = session(identity="other")
            manager = Supervisor(config(), folder, lambda event: None, source=source)
            manager.stop_event.wait = lambda seconds: manager.stop_event.set()
            with self.assertRaises(Stopped):
                manager.wait_for_login(account_tag(session()), "ACCOUNT_CHANGED")
            connect.assert_not_called()

    def test_manual_confirmation_during_wait_rechecks_account_before_resume(self):
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH), \
                patch("venue_supervisor.VenueApi.connect") as connect:
            original, refreshed = session(), session(suffix="new")
            source = Mock()
            source.read.side_effect = [session(identity="another"), refreshed]
            refresher = Mock()
            manager = Supervisor(config(), folder, lambda event: None, source=source, refresher=refresher)
            manager.rejected_tokens.add(refreshed.token)
            manager.stop_event.wait = Mock(side_effect=lambda seconds: manager.retry_login.set())
            def observe(event):
                if event.get("reason") == "ACCOUNT_CHANGED":
                    manager.retry_login.set()
            manager.publish = observe
            self.assertIs(manager.wait_for_login(account_tag(original), "ACCOUNT_CHANGED"), refreshed)
            refresher.refresh.assert_not_called()
            connect.assert_called_once_with(refreshed, manager.config)
            connect.return_value.orders.assert_called_once_with(refreshed)

    def test_expired_wait_never_dispatches_a_worker(self):
        from venue_supervisor import LoginWindowEnded
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH + 31), \
                patch("venue_supervisor.VenueApi.connect") as connect:
            manager = Supervisor(config(), folder, lambda event: None)
            with self.assertRaises(LoginWindowEnded):
                manager.wait_for_login(account_tag(session()), "AUTH")
            connect.assert_not_called()

    def test_auth_exit_refreshes_then_restarts_with_same_task(self):
        old, fresh = session(suffix="old"), session(suffix="fresh")
        class Source:
            def read(self):
                return old
        class Process:
            count = 0
            commands = []
            def __init__(self, command, **kwargs):
                self.commands.append(command)
                self.code = 75 if Process.count == 0 else 0
                Process.count += 1
                event = {"event": "auth_required", "required_until": EPOCH + 100} if self.code else {"event": "result", "state": "SUCCESS"}
                self.stdout, self.stderr, self.stdin = io.StringIO(json.dumps(event) + "\n"), io.StringIO(), io.StringIO()
            def poll(self): return self.code
            def wait(self, **kwargs): return self.code
        refresher = Mock()
        refresher.refresh.return_value = fresh
        events = []
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.subprocess.Popen", Process), \
                patch("venue_supervisor.time.time", return_value=EPOCH):
            result = Supervisor(config(), folder, events.append, source=Source(), refresher=refresher).run()
        self.assertEqual(result, "SUCCESS")
        self.assertEqual(Process.count, 2)
        self.assertEqual(Process.commands[0], Process.commands[1])
        refresher.refresh.assert_called_once()
        self.assertNotIn(old.token, json.dumps(events))

    def test_valid_manual_login_check_never_reopens_dingtalk(self):
        old = session()
        source, refresher = Mock(), Mock()
        source.read.return_value = old
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.VenueApi.connect") as connect:
            for _ in range(2):
                manager = Supervisor(config(), folder, lambda event: None, source=source, refresher=refresher)
                self.assertEqual(manager.run("refresh"), "VERIFIED")
            refresher.refresh.assert_not_called()
            self.assertEqual(connect.return_value.orders.call_count, 2)
            connect.return_value.orders.side_effect = ApiError("AUTH", "expired")
            manager = Supervisor(config(auto_refresh=True), folder, lambda event: None,
                                 source=source, refresher=refresher)
            self.assertEqual(manager.run("refresh"), "ERROR")
            refresher.refresh.assert_not_called()

    def test_second_auth_failure_waits_for_login_without_reopening_again(self):
        old, fresh, manual = session(), session(suffix="new"), session(suffix="manual")
        source, refresher = Mock(), Mock()
        source.read.side_effect = [old, fresh]
        refresher.refresh.return_value = fresh
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH):
            manager = Supervisor(config(), folder, lambda event: None, source=source, refresher=refresher)
            manager.wait_for_login = Mock(return_value=manual)
            self.assertIs(manager.restore_login(old, account_tag(old), EPOCH + 30, "AUTH"), fresh)
            self.assertIs(manager.restore_login(fresh, account_tag(old), EPOCH + 30, "AUTH"), manual)
            refresher.refresh.assert_called_once()
            manager.wait_for_login.assert_called_once_with(account_tag(old), "AUTH")

    def test_auth_failure_after_planned_restart_does_not_reopen_again(self):
        old, fresh, manual = session(), session(suffix="page"), session(suffix="manual")
        source, refresher = Mock(), Mock()
        source.read.return_value = fresh
        refresher.refresh.return_value = fresh
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH - 600):
            manager = Supervisor(config(), folder, lambda event: None, source=source, refresher=refresher)
            manager.wait_for_login = Mock(return_value=manual)
            self.assertIs(manager.page_check_before_release(old, account_tag(old), EPOCH + 90), fresh)
            self.assertIs(manager.restore_login(fresh, account_tag(old), EPOCH + 90, "AUTH"), manual)
            refresher.refresh.assert_called_once()
            manager.wait_for_login.assert_called_once_with(account_tag(old), "AUTH")

    def test_page_without_new_token_can_keep_a_verified_same_account(self):
        old = session()
        source, refresher = Mock(), Mock()
        source.read.return_value = old
        refresher.refresh.side_effect = SessionError("no new token")
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH - 600), \
                patch("venue_supervisor.VenueApi.connect") as connect:
            manager = Supervisor(config(), folder, lambda event: None, source=source, refresher=refresher)
            self.assertIs(manager.page_check_before_release(old, account_tag(old), EPOCH + 90), old)
            refresher.refresh.assert_called_once()
            connect.return_value.orders.assert_called_once_with(old)

    def test_page_check_after_cutoff_keeps_current_session(self):
        old, refresher = session(), Mock()
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH - 60):
            manager = Supervisor(config(), folder, lambda event: None, refresher=refresher)
            self.assertIs(manager.page_check_before_release(old, account_tag(old), EPOCH + 90), old)
            refresher.refresh.assert_not_called()

    def test_missing_session_still_attempts_scheduled_restart_at_three_minutes(self):
        original, fresh = session(), session(suffix="new")
        source, refresher = Mock(), Mock()
        source.read.side_effect = SessionError("no session")
        refresher.refresh.return_value = fresh
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH - 180), \
                patch("venue_supervisor.VenueApi.connect") as connect:
            manager = Supervisor(config(), folder, lambda event: None, source=source, refresher=refresher)
            manager.login_attempted = True
            self.assertIs(manager.wait_for_login(account_tag(original), "NO_SESSION"), fresh)
            self.assertTrue(manager.restart_attempted)
            refresher.refresh.assert_called_once()
            connect.return_value.orders.assert_called_once_with(fresh)

    def test_external_token_rotation_reuses_cached_login_without_reopening(self):
        old, fresh = session(), session(suffix="external")
        source, refresher = Mock(), Mock()
        source.read.return_value = fresh
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.VenueApi.connect") as connect, \
                patch("venue_supervisor.time.time", return_value=EPOCH):
            manager = Supervisor(config(), folder, lambda event: None, source=source, refresher=refresher)
            self.assertIs(manager.restore_login(old, account_tag(old), EPOCH + 30, "AUTH"), fresh)
            connect.return_value.orders.assert_called_once_with(fresh)
            refresher.refresh.assert_not_called()

    def test_recovery_opens_sso_once_even_with_legacy_two_attempt_config(self):
        old, fresh = session(), session(suffix="new")
        source = Mock()
        source.read.side_effect = [old, fresh]
        with tempfile.TemporaryDirectory() as folder, patch("venue_supervisor.time.time", return_value=EPOCH), \
                patch("venue_supervisor.SessionRefresher") as refresher:
            refresher.return_value.refresh.return_value = fresh
            manager = Supervisor(config(refresh_attempts=2), folder, lambda event: None, source=source)
            self.assertIs(manager.restore_login(old, account_tag(old), EPOCH + 30, "AUTH"), fresh)
            self.assertEqual(refresher.call_args.kwargs["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
