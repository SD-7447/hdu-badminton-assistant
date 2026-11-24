"""State-machine checks use generated frames and a fake input adapter; never touch DingTalk."""
import copy
from datetime import datetime, timedelta
import threading
import unittest

from PIL import Image, ImageDraw

from venue_core import (Anchor, Candidate, CHINA, Profile, Runner, StopRun,
                        distance, encode_image, exact_patch_matches, parse_trigger)


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=CHINA)
FIRST = [10, 75, 58, 107]
SECOND = [75, 75, 123, 107]


def fixture():
    frame = Image.new('RGB', (320, 240), 'white')
    draw = ImageDraw.Draw(frame)
    for box, label in [([5, 5, 105, 24], 'BADMINTON'), ([115, 5, 165, 24], '11/20'),
                       ([5, 35, 65, 54], '18:00'), ([5, 180, 315, 219], 'NO SELECTION')]:
        draw.text((box[0] + 2, box[1] + 2), label, fill='black')
    busy = Image.new('RGB', (48, 32), '#ededed')
    draw_busy = ImageDraw.Draw(busy)
    draw_busy.ellipse((18, 4, 30, 16), fill='white')
    draw_busy.rectangle((14, 18, 34, 29), fill='white')
    free = Image.new('RGB', (48, 32), 'white')
    selected = Image.new('RGB', (48, 32), '#4a87ff')
    frame.paste(busy, FIRST[:2])
    frame.paste(busy, SECOND[:2])
    profile = Profile(target_date='2026-11-20', calibrated_date='2026-11-20', size=[320, 240],
                      duration=5, max_refreshes=2, interval=1, settle=.2,
                      refresh=[290, 25], submit=[150, 200],
                      free_sample=encode_image(free), busy_sample=encode_image(busy),
                      selected_sample=encode_image(selected),
                      candidates=[Candidate('18:00 court2', SECOND), Candidate('18:00 court1', FIRST)])
    for name, box in [('title', [5, 5, 105, 24]), ('date', [115, 5, 165, 24]),
                      ('rows', [5, 35, 65, 54]), ('button', [5, 180, 315, 219])]:
        profile.guards[name] = Anchor(box, encode_image(frame.crop(box)))
    return profile, frame, free, selected


class Fake:
    def __init__(self, frame, free, selected):
        self.frame = frame.copy()
        self.free, self.selected = free, selected
        self.clicks = []
        self.release_on_refresh = False
        self.select_works = True
        self.submit_raises = False
        self.success_image = None
        self.clock = 0
        self.stop = threading.Event()

    def capture(self):
        return self.frame.copy()

    def click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) == (290, 25):
            if self.release_on_refresh:
                self.frame.paste(self.free, SECOND[:2])
        elif y < 150:
            if self.select_works:
                box = FIRST if x < 60 else SECOND
                self.frame.paste(self.selected, box[:2])
                ImageDraw.Draw(self.frame).rectangle([5, 180, 315, 219], fill='#4a87ff')
        else:
            if self.submit_raises:
                raise OSError('simulated uncertain input')
            if self.success_image:
                self.frame.paste(self.success_image, (200, 110))

    def wait(self, seconds):
        self.clock += seconds
        return self.stop.is_set()


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.p, self.frame, self.free, self.selected = fixture()
        self.a = Fake(self.frame, self.free, self.selected)
        self.messages, self.journal = [], []

    def runner(self):
        return Runner(self.p, self.a, self.a.stop, self.messages.append,
                      lambda stage, label: self.journal.append((stage, label)),
                      wait=self.a.wait, wall=lambda: NOW, monotonic=lambda: self.a.clock)

    def test_busy_cells_never_selected_and_refresh_is_bounded(self):
        self.assertEqual(self.runner().run('select'), 'EXHAUSTED')
        self.assertEqual(self.a.clicks, [(290, 25), (290, 25)])
        self.assertEqual(self.journal, [])

    def test_priority_and_one_selection_after_refresh(self):
        self.a.release_on_refresh = True
        result = self.runner().run('select')
        self.assertEqual(result, 'SELECTED_UNVERIFIED')
        self.assertEqual(self.a.clicks, [(290, 25), (99, 91)])
        self.assertEqual(self.journal[0][0], 'SELECT_INTENT')

    def test_preview_does_not_click_even_if_free(self):
        self.a.frame.paste(self.free, SECOND[:2])
        self.assertEqual(self.runner().run(), 'PREVIEW')
        self.assertEqual(self.a.clicks, [])

    def test_preview_without_samples_checks_interface(self):
        self.p.free_sample = self.p.busy_sample = ''
        self.assertEqual(self.runner().run(), 'PREVIEW')
        self.assertEqual(self.a.clicks, [])

    def test_changed_date_stops_before_any_input(self):
        ImageDraw.Draw(self.a.frame).rectangle([115, 5, 165, 24], fill='black')
        with self.assertRaisesRegex(StopRun, 'date'):
            self.runner().run('select')
        self.assertEqual(self.a.clicks, [])

    def test_changed_scroll_position_stops_before_input(self):
        ImageDraw.Draw(self.a.frame).text((8, 37), '19:00', fill='red')
        with self.assertRaisesRegex(StopRun, 'rows'):
            self.runner().run('select')
        self.assertEqual(self.a.clicks, [])

    def test_window_resize_stops(self):
        self.a.frame = self.a.frame.resize((640, 480))
        with self.assertRaisesRegex(StopRun, '尺寸'):
            self.runner().run('select')
        self.assertEqual(self.a.clicks, [])

    def test_auto_submit_once_result_remains_unknown(self):
        self.p.auto_submit = True
        self.a.frame.paste(self.free, SECOND[:2])
        self.assertEqual(self.runner().run('submit'), 'SUBMITTED_UNKNOWN')
        self.assertEqual(self.a.clicks, [(99, 91), (150, 200)])
        self.assertEqual([x[0] for x in self.journal], ['SELECT_INTENT', 'SUBMIT_INTENT', 'SUBMITTED_UNKNOWN'])

    def test_failed_selection_cannot_submit(self):
        self.p.auto_submit = True
        self.a.select_works = False
        self.a.frame.paste(self.free, SECOND[:2])
        with self.assertRaisesRegex(StopRun, '未匹配已选中'):
            self.runner().run('submit')
        self.assertEqual(self.a.clicks, [(99, 91)])

    def test_success_requires_post_submit_marker(self):
        self.p.auto_submit = True
        marker = Image.new('RGB', (100, 30), '#12b771')
        ImageDraw.Draw(marker).text((2, 8), 'BOOKING SUCCESS', fill='white')
        self.p.success = Anchor([200, 110, 300, 140], encode_image(marker))
        self.a.success_image = marker
        self.a.frame.paste(self.free, SECOND[:2])
        self.assertEqual(self.runner().run('submit'), 'SUCCESS_MARKER')
        self.assertEqual(self.a.clicks.count((150, 200)), 1)
        self.assertEqual(self.journal[-1][0], 'SUCCESS_MARKER')

    def test_preexisting_success_marker_rejects_submission(self):
        self.p.auto_submit = True
        self.p.success = Anchor([200, 110, 300, 140], encode_image(self.frame.crop([200, 110, 300, 140])))
        self.a.frame.paste(self.free, SECOND[:2])
        with self.assertRaisesRegex(StopRun, '提交前已出现'):
            self.runner().run('submit')
        self.assertNotIn((150, 200), self.a.clicks)

    def test_uncertain_submission_is_not_retried(self):
        self.p.auto_submit = True
        self.a.submit_raises = True
        self.a.frame.paste(self.free, SECOND[:2])
        with self.assertRaises(OSError):
            self.runner().run('submit')
        self.assertEqual(self.a.clicks.count((150, 200)), 1)
        self.assertEqual(self.journal[-1][0], 'SUBMIT_INTENT')

    def test_journal_failure_prevents_click(self):
        self.a.frame.paste(self.free, SECOND[:2])
        runner = self.runner()
        runner.journal = lambda stage, label: (_ for _ in ()).throw(OSError('disk full'))
        with self.assertRaises(OSError):
            runner.run('select')
        self.assertEqual(self.a.clicks, [])

    def test_stop_before_start(self):
        self.a.stop.set()
        with self.assertRaises(StopRun):
            self.runner().run('select')
        self.assertEqual(self.a.clicks, [])

    def test_unconfirmed_date_change_rejected(self):
        self.p.target_date = '2026-11-21'
        with self.assertRaisesRegex(ValueError, '校准日期'):
            self.runner().run('select')

    def test_nan_and_unbounded_intervals_rejected(self):
        for value in [float('nan'), float('inf'), .1, 61]:
            self.p.interval = value
            with self.assertRaises(ValueError):
                self.runner().run('select')

    def test_equal_templates_rejected(self):
        self.p.free_sample = self.p.busy_sample
        with self.assertRaisesRegex(ValueError, '太相似'):
            self.runner().run('select')

    def test_missing_selected_sample_cannot_auto_submit(self):
        self.p.auto_submit, self.p.selected_sample = True, ''
        with self.assertRaisesRegex(ValueError, '选中格样本'):
            self.runner().run('submit')

    def test_no_trigger_runs_immediately_and_shanghai_timezone(self):
        self.assertIsNone(parse_trigger(''))
        self.assertEqual(parse_trigger('2026-11-20 20:00:00.500').utcoffset(), timedelta(hours=8))
        self.assertEqual(parse_trigger('2026-11-20T12:00:00Z').hour, 20)

    def test_future_schedule_waits_and_can_be_interrupted(self):
        self.p.trigger = '2026-11-19 20:00:00'
        self.a.wait = lambda seconds: self.a.stop.set() or True
        with self.assertRaises(StopRun):
            self.runner().run('select')
        self.assertEqual(self.a.clicks, [])

    def test_date_digit_difference_is_detected(self):
        expected = Image.new('RGB', (75, 55), 'white')
        current = expected.copy()
        ImageDraw.Draw(expected).text((5, 20), '11/20', fill='black')
        ImageDraw.Draw(current).text((5, 20), '11/21', fill='black')
        self.assertFalse(exact_patch_matches(current, expected))

    def test_profile_round_trip(self):
        self.assertEqual(Profile.from_dict(self.p.to_dict()).to_dict(), self.p.to_dict())


if __name__ == '__main__':
    unittest.main(verbosity=2)
