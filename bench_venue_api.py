"""Measure local write-ahead dispatch cost. No HTTP or real credentials used."""
from decimal import Decimal
from pathlib import Path
import json
import statistics
import tempfile
import time

from venue_api import Candidate, Config
from venue_engine import BookingEngine, Journal
from venue_session import Session


def benchmark(rounds=100):
    samples = []
    cfg = Config(target_date="2099-01-04", release_override="2099-01-01 20:00:00")
    ses = Session("offline-token", "offline-id", "offline", "000")
    candidate = Candidate(6, (0, 1), "10:00", "12:00", 120, Decimal(0))

    class Clock:
        def now(self): return cfg.release_epoch()

    class OfflineApi:
        def prepare(self, payload):
            samples.append((time.perf_counter() - self.began) * 1000)
            return {"conflict_times": [], "available_times": [0, 1]}

        def commit(self, payload):
            return {"data": {"order_num": "offline", "order_date": cfg.target_date, "venue_name": cfg.venue_name,
                             "site_id": 6, "start_time": "10:00", "end_time": "12:00"}}

    with tempfile.TemporaryDirectory() as folder:
        journal = Journal(Path(folder) / "offline-journal.json")
        api = OfflineApi()
        engine = BookingEngine(cfg, ses, api, journal, clock=Clock())
        for _ in range(rounds):
            journal.bind(cfg, ses, [])
            api.began = time.perf_counter()
            engine.prepare(candidate)
    ordered = sorted(samples)
    return {"rounds": rounds, "median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(ordered[max(0, int(rounds * 0.95) - 1)], 3),
            "max_ms": round(max(samples), 3), "network_included": False,
            "measured": "prepare entry to dispatch, including atomic journal write and fsync"}


if __name__ == "__main__":
    print(json.dumps(benchmark(), ensure_ascii=False, indent=2))
