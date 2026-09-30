#!/usr/bin/env python3
"""Solve as many puzzle sessions as possible within one bounded run."""

from __future__ import annotations

import time
from datetime import datetime, timezone

PROCESS_STARTED = time.monotonic()
PROCESS_STARTED_AT = datetime.now(timezone.utc)

import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    pass


def strictly_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be greater than zero")
    return parsed


def finite_strictly_positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} must be finite and greater than zero"
        )
    return parsed


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionLog:
    def __init__(
        self,
        path: Path,
        now: Callable[[], datetime] | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self._file = path.open("w", encoding="utf-8")
        self._now = _utc_now if now is None else now
        started_at = self._now() if started_at is None else started_at
        self.record(f"Execution started: {self._format_timestamp(started_at)}")

    def record(self, message: str) -> None:
        print(message, file=self._file, flush=True)

    def finish(
        self, exit_code: int | None, failure: BaseException | None = None
    ) -> None:
        if failure is not None and not isinstance(failure, SystemExit):
            self.record(f"Execution failed: {type(failure).__name__}: {failure}")
        outcome = "unknown" if exit_code is None else str(exit_code)
        self.record(f"Execution finished: {self._timestamp()} exit_code={outcome}")
        self._file.close()

    def _timestamp(self) -> str:
        return self._format_timestamp(self._now())

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


class HttpPuzzleApi:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def start(self, timeout: float) -> str:
        data = self._request("GET", "/start", timeout)
        session_id = data.get("session_id")
        if not isinstance(session_id, str):
            raise ApiError("invalid /start response")
        return session_id

    def get_piece(self, session_id: str, index: int, timeout: float) -> tuple[int, str]:
        query = urlencode({"session_id": session_id})
        data = self._request("GET", f"/get/{index}?{query}", timeout)
        piece_id, word = data.get("id"), data.get("word")
        if isinstance(piece_id, bool) or not isinstance(piece_id, int) or not isinstance(word, str):
            raise ApiError("invalid /get response")
        return piece_id, word

    def submit(self, session_id: str, words: list[str], timeout: float) -> bool:
        data = self._request(
            "POST", "/submit", timeout, {"session_id": session_id, "words": words}
        )
        accepted = data.get("ok")
        if not isinstance(accepted, bool):
            raise ApiError("invalid /submit response")
        return accepted

    def _request(
        self,
        method: str,
        path: str,
        timeout: float,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except HTTPError as error:
            raise ApiError(f"HTTP {error.code} for {path}") from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise ApiError(f"request failed for {path}: {error}") from error
        if not isinstance(data, dict):
            raise ApiError(f"non-object JSON response for {path}")
        return data


@dataclass
class RunStats:
    sessions: int = 0
    solved: int = 0
    scheduled_requests: int = 0
    errors: int = 0


class Solver:
    def __init__(
        self,
        api: Any,
        duration: float = 30.0,
        workers: int = 16,
        batch_size: int = 128,
        request_timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        event_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.api = api
        self.duration = duration
        self.workers = workers
        self.batch_size = batch_size
        self.request_timeout = request_timeout
        self.clock = clock
        self.event_handler = event_handler

    def run(self, started_at: float | None = None) -> RunStats:
        deadline = (self.clock() if started_at is None else started_at) + self.duration
        stats = RunStats()
        while self.clock() < deadline:
            try:
                self._solve_one(deadline, stats)
            except ApiError as error:
                stats.errors += 1
                message = f"API error: {error}"
                print(message)
                self._emit(message)
                break
        return stats

    def _solve_one(self, deadline: float, stats: RunStats) -> bool:
        session_id = self.api.start(self._timeout(deadline))
        stats.sessions += 1
        self._emit(f"Session started: session_id={session_id}")
        pieces: dict[int, str] = {}
        next_index = 0

        executor = ThreadPoolExecutor(max_workers=self.workers)
        try:
            while self.clock() < deadline:
                futures = []
                for index in range(next_index, next_index + self.batch_size):
                    if self.clock() >= deadline:
                        break
                    futures.append(
                        executor.submit(self._fetch, session_id, index, deadline)
                    )
                    next_index += 1
                if not futures:
                    return False
                stats.scheduled_requests += len(futures)
                done, pending = wait(futures, timeout=max(0.0, deadline - self.clock()))
                if pending:
                    for future in pending:
                        future.cancel()
                    return False

                for future in done:
                    try:
                        piece_id, word = future.result()
                    except ApiError as error:
                        stats.errors += 1
                        self._emit(
                            f"Piece retrieval failed: session_id={session_id} error={error}"
                        )
                    else:
                        pieces[piece_id] = word

                if self.clock() >= deadline:
                    return False

                words = [pieces[piece_id] for piece_id in sorted(pieces)]
                try:
                    accepted = self.api.submit(
                        session_id, words, self._timeout(deadline)
                    )
                except ApiError as error:
                    stats.errors += 1
                    self._emit(
                        f"Submission failed: session_id={session_id} error={error}"
                    )
                    continue

                if accepted:
                    stats.solved += 1
                    self._emit(f"Puzzle solved: session_id={session_id}")
                    return True
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return False

    def _fetch(self, session_id: str, index: int, deadline: float) -> tuple[int, str]:
        return self.api.get_piece(session_id, index, self._timeout(deadline))

    def _timeout(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ApiError("run deadline reached")
        return min(self.request_timeout, remaining)

    def _emit(self, message: str) -> None:
        if self.event_handler is not None:
            self.event_handler(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--duration", type=finite_strictly_positive_float, default=30.0
    )
    parser.add_argument("--workers", type=strictly_positive_int, default=16)
    parser.add_argument("--batch-size", type=strictly_positive_int, default=128)
    parser.add_argument(
        "--request-timeout", type=finite_strictly_positive_float, default=2.0
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    started_at = PROCESS_STARTED if argv is None else time.monotonic()
    wall_started_at = PROCESS_STARTED_AT if argv is None else _utc_now()
    execution_log = ExecutionLog(
        Path.cwd() / "output.log", started_at=wall_started_at
    )
    exit_code: int | None = None
    failure: BaseException | None = None
    try:
        args = build_parser().parse_args(argv)
        execution_log.record(
            f"Configuration: base_url={args.base_url} duration={args.duration} "
            f"workers={args.workers} batch_size={args.batch_size} "
            f"request_timeout={args.request_timeout}"
        )
        solver = Solver(
            HttpPuzzleApi(args.base_url),
            duration=args.duration,
            workers=args.workers,
            batch_size=args.batch_size,
            request_timeout=args.request_timeout,
            event_handler=execution_log.record,
        )
        stats = solver.run(started_at)
        summary = (
            f"Final: solved={stats.solved} sessions={stats.sessions} "
            f"scheduled_requests={stats.scheduled_requests} errors={stats.errors}"
        )
        print(summary)
        execution_log.record(summary)
        exit_code = int(stats.sessions == 0 and stats.errors > 0)
        return exit_code
    except BaseException as error:
        failure = error
        if isinstance(error, SystemExit) and isinstance(error.code, int):
            exit_code = error.code
        raise
    finally:
        execution_log.finish(exit_code, failure)


if __name__ == "__main__":
    raise SystemExit(main())
