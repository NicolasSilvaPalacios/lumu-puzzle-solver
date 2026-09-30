#!/usr/bin/env python3
"""Solve as many puzzle sessions as possible within one bounded run."""

from __future__ import annotations

import time

PROCESS_STARTED = time.monotonic()

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    pass


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
    ) -> None:
        self.api = api
        self.duration = duration
        self.workers = workers
        self.batch_size = batch_size
        self.request_timeout = request_timeout
        self.clock = clock

    def run(self, started_at: float | None = None) -> RunStats:
        deadline = (self.clock() if started_at is None else started_at) + self.duration
        stats = RunStats()
        while self.clock() < deadline:
            try:
                self._solve_one(deadline, stats)
            except ApiError as error:
                stats.errors += 1
                print(f"API error: {error}")
                break
        return stats

    def _solve_one(self, deadline: float, stats: RunStats) -> bool:
        session_id = self.api.start(self._timeout(deadline))
        stats.sessions += 1
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
                    except ApiError:
                        stats.errors += 1
                    else:
                        pieces[piece_id] = word

                if self.clock() >= deadline:
                    return False

                words = [pieces[piece_id] for piece_id in sorted(pieces)]
                try:
                    accepted = self.api.submit(
                        session_id, words, self._timeout(deadline)
                    )
                except ApiError:
                    stats.errors += 1
                    continue

                if accepted:
                    stats.solved += 1
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    started_at = PROCESS_STARTED if argv is None else time.monotonic()
    args = build_parser().parse_args(argv)
    solver = Solver(
        HttpPuzzleApi(args.base_url),
        duration=args.duration,
        workers=args.workers,
        batch_size=args.batch_size,
        request_timeout=args.request_timeout,
    )
    stats = solver.run(started_at)
    print(
        f"Final: solved={stats.solved} sessions={stats.sessions} "
        f"scheduled_requests={stats.scheduled_requests} errors={stats.errors}"
    )
    return int(stats.sessions == 0 and stats.errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())
