import io
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from puzzle_solver import (
    ApiError,
    ExecutionLog,
    HttpPuzzleApi,
    RunStats,
    Solver,
    build_parser,
    main,
)


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


class HttpPuzzleApiTests(unittest.TestCase):
    def setUp(self):
        self.api = HttpPuzzleApi("http://example.test/")

    def response(self, data):
        return io.BytesIO(json.dumps(data).encode())

    def test_start_builds_get_request_and_returns_session_id(self):
        with patch(
            "puzzle_solver.urlopen",
            return_value=self.response({"session_id": "session-1"}),
        ) as mocked_urlopen:
            session_id = self.api.start(1.5)

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual("session-1", session_id)
        self.assertEqual("http://example.test/start", request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertIsNone(request.data)
        self.assertEqual(1.5, mocked_urlopen.call_args.kwargs["timeout"])

    def test_get_piece_builds_encoded_get_request(self):
        with patch(
            "puzzle_solver.urlopen", return_value=self.response({"id": 4, "word": "four"})
        ) as mocked_urlopen:
            result = self.api.get_piece("session /?", 7, 1.5)

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual((4, "four"), result)
        self.assertEqual(
            "http://example.test/get/7?session_id=session+%2F%3F", request.full_url
        )
        self.assertEqual("GET", request.get_method())
        self.assertIsNone(request.data)
        self.assertEqual(1.5, mocked_urlopen.call_args.kwargs["timeout"])

    def test_submit_serializes_json_request(self):
        with patch(
            "puzzle_solver.urlopen", return_value=self.response({"ok": True})
        ) as mocked_urlopen:
            accepted = self.api.submit("session-1", ["one", "two"], 2.0)

        request = mocked_urlopen.call_args.args[0]
        self.assertTrue(accepted)
        self.assertEqual("http://example.test/submit", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual(
            {"session_id": "session-1", "words": ["one", "two"]},
            json.loads(request.data),
        )
        self.assertEqual("application/json", request.get_header("Content-type"))

    def test_validates_response_shapes(self):
        cases = [
            ("start", {"session_id": 1}, "invalid /start response"),
            ("get_piece", {"id": True, "word": "word"}, "invalid /get response"),
            ("submit", {"ok": "yes"}, "invalid /submit response"),
            ("start", [], "non-object JSON response for /start"),
        ]

        for method, response, message in cases:
            with self.subTest(method=method, response=response):
                with patch("puzzle_solver.urlopen", return_value=self.response(response)):
                    with self.assertRaisesRegex(ApiError, message):
                        if method == "start":
                            self.api.start(1.0)
                        elif method == "get_piece":
                            self.api.get_piece("session", 0, 1.0)
                        else:
                            self.api.submit("session", [], 1.0)

    def test_translates_transport_errors(self):
        errors = [
            (
                HTTPError(
                    "http://example.test/start", 503, "unavailable", None, None
                ),
                "HTTP 503 for /start",
            ),
            (URLError("connection refused"), "request failed for /start"),
        ]

        for error, message in errors:
            with self.subTest(error=type(error).__name__):
                with patch("puzzle_solver.urlopen", side_effect=error):
                    with self.assertRaisesRegex(ApiError, message) as raised:
                        self.api.start(1.0)
                self.assertIs(error, raised.exception.__cause__)
                raised.exception.__cause__ = None
                error.__traceback__ = None
                if isinstance(error, HTTPError):
                    error.close()

        with patch("puzzle_solver.urlopen", return_value=io.BytesIO(b"not JSON")):
            with self.assertRaisesRegex(ApiError, "request failed for /start"):
                self.api.start(1.0)


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
        self.assertEqual(4, stats.scheduled_requests)
        self.assertEqual(1, stats.solved)

    def test_success_starts_replacement_sessions_while_time_remains(self):
        clock = Clock()
        api = ScriptedApi({0: (0, "a"), 1: (0, "a")}, clock=clock)
        solver = self.make_solver(api, clock, duration=2.0)

        stats = solver.run()

        self.assertEqual(2, api.starts)
        self.assertEqual(2, stats.solved)

    def test_reports_started_and_solved_session_ids(self):
        clock = Clock()
        events = []
        api = ScriptedApi({0: (0, "a"), 1: (0, "a")}, clock=clock)
        solver = self.make_solver(
            api, clock, duration=1.0, event_handler=events.append
        )

        solver.run()

        self.assertEqual(
            [
                "Session started: session_id=session-1",
                "Puzzle solved: session_id=session-1",
            ],
            events,
        )

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


class ArgumentParserTests(unittest.TestCase):
    def assert_parse_error(self, option, value):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                build_parser().parse_args([option, value])
        self.assertEqual(2, raised.exception.code)

    def test_float_options_reject_non_positive_non_finite_and_lexical_values(self):
        for option in ("--duration", "--request-timeout"):
            for value in ("0", "-0.001", "nan", "inf", "-inf", "invalid"):
                with self.subTest(option=option, value=value):
                    self.assert_parse_error(option, value)

    def test_integer_options_reject_non_positive_and_lexical_values(self):
        for option in ("--workers", "--batch-size"):
            for value in ("0", "-1", "invalid"):
                with self.subTest(option=option, value=value):
                    self.assert_parse_error(option, value)

    def test_minimum_positive_examples_parse_successfully(self):
        args = build_parser().parse_args(
            [
                "--duration",
                "0.001",
                "--request-timeout",
                "0.001",
                "--workers",
                "1",
                "--batch-size",
                "1",
            ]
        )

        self.assertEqual(0.001, args.duration)
        self.assertEqual(0.001, args.request_timeout)
        self.assertEqual(1, args.workers)
        self.assertEqual(1, args.batch_size)


class MainTests(unittest.TestCase):
    def run_main(self, stats):
        timestamps = iter(
            [
                datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 29, 12, 0, 1, tzinfo=timezone.utc),
            ]
        )
        with TemporaryDirectory() as directory:
            with (
                patch("puzzle_solver.Solver") as solver_class,
                patch("puzzle_solver.Path.cwd", return_value=Path(directory)),
                patch("puzzle_solver._utc_now", side_effect=timestamps),
            ):
                solver_class.return_value.run.return_value = stats
                with redirect_stdout(io.StringIO()) as output:
                    exit_code = main([])
            log = (Path(directory) / "output.log").read_text(encoding="utf-8")
        return exit_code, output.getvalue(), log

    def test_returns_failure_when_api_error_prevents_first_session(self):
        exit_code, output, log = self.run_main(RunStats(errors=1))

        self.assertEqual(1, exit_code)
        self.assertIn("scheduled_requests=0", output)
        self.assertEqual(
            "Execution started: 2026-09-29T12:00:00Z\n"
            "Configuration: base_url=http://localhost:8080 duration=30.0 "
            "workers=16 batch_size=128 request_timeout=2.0\n"
            "Final: solved=0 sessions=0 scheduled_requests=0 errors=1\n"
            "Execution finished: 2026-09-29T12:00:01Z exit_code=1\n",
            log,
        )

    def test_returns_success_after_session_starts_despite_recoverable_errors(self):
        exit_code, _, _ = self.run_main(
            RunStats(sessions=1, scheduled_requests=2, errors=2)
        )

        self.assertEqual(0, exit_code)

    def test_finalizes_log_when_argument_parsing_exits(self):
        for argv in (["--workers", "invalid"], ["--duration", "0"]):
            with self.subTest(argv=argv):
                timestamps = iter(
                    [
                        datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
                        datetime(2026, 9, 29, 12, 0, 1, tzinfo=timezone.utc),
                    ]
                )
                with TemporaryDirectory() as directory:
                    with (
                        patch("puzzle_solver.Path.cwd", return_value=Path(directory)),
                        patch("puzzle_solver._utc_now", side_effect=timestamps),
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(SystemExit) as raised:
                            main(argv)
                    log = (Path(directory) / "output.log").read_text(
                        encoding="utf-8"
                    )

                self.assertEqual(2, raised.exception.code)
                self.assertEqual(
                    "Execution started: 2026-09-29T12:00:00Z\n"
                    "Execution finished: 2026-09-29T12:00:01Z exit_code=2\n",
                    log,
                )


class ExecutionLogTests(unittest.TestCase):
    def test_finalizes_and_preserves_an_unhandled_failure(self):
        timestamps = iter(
            [
                datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 29, 12, 0, 2, tzinfo=timezone.utc),
            ]
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "output.log"
            execution_log = ExecutionLog(path, now=lambda: next(timestamps))
            error = RuntimeError("boom")

            execution_log.finish(None, error)

            self.assertEqual(
                "Execution started: 2026-09-29T12:00:00Z\n"
                "Execution failed: RuntimeError: boom\n"
                "Execution finished: 2026-09-29T12:00:02Z exit_code=unknown\n",
                path.read_text(encoding="utf-8"),
            )

if __name__ == "__main__":
    unittest.main()
