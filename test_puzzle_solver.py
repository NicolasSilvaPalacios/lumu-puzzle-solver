import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from unittest.mock import patch

from puzzle_solver import Solver


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.value

    def advance(self, amount: float) -> None:
        with self.lock:
            self.value += amount


class ScriptedApi:
    def __init__(self, pieces, accepted_after=1, clock=None) -> None:
        self.pieces = pieces
        self.accepted_after = accepted_after
        self.clock = clock
        self.starts = 0
        self.requested = []
        self.submissions = []

    def start(self, timeout):
        self.starts += 1
        return f"session-{self.starts}"

    def get_piece(self, session_id, index, timeout):
        self.requested.append(index)
        result = self.pieces[index]
        if isinstance(result, Exception):
            raise result
        return result

    def submit(self, session_id, words, timeout):
        self.submissions.append((session_id, words))
        if self.clock is not None:
            self.clock.advance(1.0)
        return len(self.submissions) % self.accepted_after == 0


class SolverTests(unittest.TestCase):
    def make_solver(self, api, clock, **overrides):
        options = {
            "duration": 30.0,
            "workers": 1,
            "batch_size": 2,
            "request_timeout": 2.0,
            "clock": clock,
        }
        options.update(overrides)
        return Solver(api, **options)

    def test_orders_by_numeric_id_and_deduplicates_only_by_id(self):
        clock = Clock()
        api = ScriptedApi(
            {0: (2, "same"), 1: (0, "same"), 2: (2, "same"), 3: (1, "middle")},
            clock=clock,
        )
        solver = self.make_solver(api, clock, duration=1.0, batch_size=4)

        stats = solver.run()

        self.assertEqual(["same", "middle", "same"], api.submissions[0][1])
        self.assertEqual(1, stats.solved)

    def test_incomplete_submission_continues_with_next_index_batch(self):
        clock = Clock()
        api = ScriptedApi(
            {0: (1, "b"), 1: (0, "a"), 2: (2, "c"), 3: (2, "c")},
            accepted_after=2,
            clock=clock,
        )
        solver = self.make_solver(api, clock, duration=2.0)

        stats = solver.run()

        self.assertEqual([0, 1, 2, 3], api.requested)
        self.assertEqual(["a", "b"], api.submissions[0][1])
        self.assertEqual(["a", "b", "c"], api.submissions[1][1])
        self.assertEqual(1, stats.solved)

    def test_success_starts_replacement_sessions_while_time_remains(self):
        clock = Clock()
        api = ScriptedApi({0: (0, "a"), 1: (0, "a")}, clock=clock)
        solver = self.make_solver(api, clock, duration=2.0)

        stats = solver.run()

        self.assertEqual(2, api.starts)
        self.assertEqual(2, stats.solved)

    def test_deadline_stops_scheduling_and_ignores_late_work(self):
        clock = Clock()
        started = threading.Event()
        release = threading.Event()
        scheduled = []

        class BlockingApi:
            def __init__(self):
                self.requested = []
                self.submissions = 0

            def start(self, timeout):
                return "session"

            def get_piece(self, session_id, index, timeout):
                self.requested.append(index)
                started.set()
                release.wait(timeout=1.0)
                return 0, "late"

            def submit(self, session_id, words, timeout):
                self.submissions += 1
                return True

        class DeadlineExecutor:
            def __init__(self, max_workers):
                self.executor = RealThreadPoolExecutor(max_workers=max_workers)

            def submit(self, function, *args):
                scheduled.append(args[1])
                future = self.executor.submit(function, *args)
                clock.advance(1.0)
                return future

            def shutdown(self, **kwargs):
                self.executor.shutdown(**kwargs)

        api = BlockingApi()
        solver = Solver(
            api,
            duration=1.0,
            workers=1,
            batch_size=100,
            request_timeout=1.0,
            clock=clock,
        )

        before = time.monotonic()
        try:
            with patch("puzzle_solver.ThreadPoolExecutor", DeadlineExecutor):
                solver.run()
            elapsed = time.monotonic() - before

            self.assertTrue(started.is_set())
            self.assertLess(elapsed, 0.25)
            self.assertEqual([0], scheduled)
            self.assertEqual(0, api.submissions)
        finally:
            release.set()
        time.sleep(0.01)
        self.assertEqual(0, api.submissions)

if __name__ == "__main__":
    unittest.main()
