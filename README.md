# Puzzle Solver Assessment Guide

This repository contains the completed Python client for the Puzzle Solver assessment. It collects and orders puzzle pieces, submits candidate solutions, and attempts to solve as many puzzles as possible within one bounded run.

## Quick path

### Requirements

- Python 3.10 or newer
- Docker with access to the challenge image

The solver has no third-party Python dependencies.

1. Start the challenge server:

   ```bash
   docker run -p 8080:8080 ifajardov/puzzle
   ```

2. In another terminal, run the solver from the repository directory:

   ```bash
   python3 puzzle_solver.py
   ```

3. Inspect the per-run execution record:

   ```bash
   less output.log
   ```

4. Run the test suite:

   ```bash
   python3 -m unittest -v
   ```

## Challenge goal

Each puzzle is an ordered sequence of words exposed as retrievable pieces. The client must collect the pieces, restore their order, submit the candidate sequence, and maximize the number of accepted puzzles before the 30-second window expires.

The original brief is preserved in [`Puzzle Solver.pdf`](Puzzle%20Solver.pdf).

## Contract and interpretation

The solution separates the written protocol from later clarification and from behavior observed while developing the client.

### Original PDF contract

- `GET /start` creates a puzzle and returns its `session_id`.
- `GET /get/{n}?session_id={session_id}` returns a piece with an `id` and `word`. The PDF defines `n` as a non-negative integer and says the same index is deterministic within a session.
- `POST /submit` receives the session ID and an ordered `words` array.
- `ok: true` solves and closes the session; `ok: false` leaves it open for more retrievals and submissions.
- The score is the number of correctly solved puzzles within 30 seconds.

### Assessor clarifications

- A piece's numeric `id`, not retrieval index or word value, defines identity and submission order.
- `/submit` is the authority on whether the collected sequence is complete; there is no separate completion signal.
- Retrieval indices must be non-negative, reaffirming the PDF's stated index domain.
- Concurrent requests and multiple simultaneous sessions are permitted, but neither is required.
- Dependencies and external data are allowed, although a simple solution is preferred.

### Observations and implementation assumptions

The API does not expose a piece count or valid upper index. During development, puzzle sizes and distributions varied, so those observations are not treated as protocol guarantees. The implementation therefore probes increasing indices and uses rejected submissions as evidence that collection should continue. This is a practical heuristic, not proof that every possible server distribution can be completed.

## Architecture

The implementation is intentionally small and uses only the Python standard library:

| Component | Responsibility |
|---|---|
| `HttpPuzzleApi` | Encodes HTTP requests with `urllib`, parses JSON, validates response shapes, and translates transport or protocol failures into `ApiError`. |
| `Solver` | Owns the monotonic deadline, session lifecycle, concurrent retrieval batches, piece collection, submission, and recovery behavior. |
| `ExecutionLog` | Creates the per-invocation log, records lifecycle events, and finalizes it across success and failure paths. |
| `RunStats` | Carries the final session, solution, scheduling, and error counters. |
| `main` | Parses CLI arguments, wires the components together, prints the final summary, and determines the process exit code. |

Standard-library-only code keeps setup reproducible and avoids adding dependency installation or framework behavior to a short-lived command-line client. The tradeoff is using blocking `urllib` calls and threads rather than an asynchronous networking stack.

## Collection strategy

The solver keeps one active session at a time, even though simultaneous sessions are permitted. For that session it:

1. Requests increasing non-negative indices, beginning at `0`.
2. Schedules each bounded batch in a `ThreadPoolExecutor`.
3. Waits for the batch, records retrieval failures, and retains successful pieces.
4. Deduplicates pieces by numeric ID, sorts IDs numerically, and builds the ordered word list.
5. Submits after each completed batch.
6. Continues the same session with the next index batch after rejection or submission failure; after acceptance, starts a replacement session while time remains.

This favors speed through bounded parallel retrieval while preserving correctness through ID-based deduplication, deterministic ordering, and `/submit` as the final completeness check.

## Deadline behavior

A single monotonic deadline covers startup, retrieval, and submission. The remaining run time caps each request timeout. Once the deadline is reached, the solver stops scheduling or submitting, cancels queued futures where possible, and ignores results that arrive too late.

Python cannot forcibly terminate an already-running blocking socket call or worker thread. Executor shutdown therefore does not wait for running requests; those calls may outlive solver control until their request timeout or operating-system networking behavior releases them. Late results are not submitted.

## CLI options

Run `python3 puzzle_solver.py --help` for the parser-generated reference.

| Option | Default | Meaning |
|---|---:|---|
| `--base-url` | `http://localhost:8080` | Challenge server base URL. |
| `--duration` | `30.0` seconds | Total solver run duration. |
| `--workers` | `16` | Maximum retrieval worker threads. |
| `--batch-size` | `128` | Maximum indices scheduled per batch. |
| `--request-timeout` | `2.0` seconds | Per-request timeout, capped by remaining run time. |

Numeric values should be positive. The current CLI parses their numeric types but does not enforce positivity; valid values are the caller's responsibility.

## `output.log`

Every CLI invocation creates or overwrites `output.log` in the current working directory before argument parsing. This includes `--help` and invalid-argument exits. The file is intentionally ignored by Git.

Depending on how far execution proceeds, the log contains:

- UTC ISO-8601 execution start and finish timestamps;
- the effective base URL, duration, worker count, batch size, and request timeout after successful argument parsing;
- started session IDs;
- top-level API, piece-retrieval, and submission errors;
- each solved puzzle's session ID;
- final counters and exit code for completed or handled runs.

The log is finalized on normal completion, handled API failures, parser `SystemExit` paths such as help or invalid arguments, and unexpected exception paths. Parser exits may contain only start and finish records because configuration and solver execution have not begun. Unexpected non-`SystemExit` exceptions add an `Execution failed` record, finish with `exit_code=unknown`, and still propagate to the caller.

Example shape, using opaque session IDs:

```text
Execution started: 2026-09-29T12:00:00Z
Configuration: base_url=http://localhost:8080 duration=30.0 workers=16 batch_size=128 request_timeout=2.0
Session started: session_id=<opaque-session-a>
Puzzle solved: session_id=<opaque-session-a>
Final: solved=1 sessions=1 scheduled_requests=128 errors=0
Execution finished: 2026-09-29T12:00:30Z exit_code=0
```

The example demonstrates format only; its timing and counters are not benchmark claims.

## Final counters and exit status

| Field | Meaning |
|---|---|
| `solved` | Sessions accepted by `/submit`. |
| `sessions` | Sessions successfully created by `/start`. |
| `scheduled_requests` | Retrieval tasks submitted to the executor. It counts executor submissions, including tasks later cancelled before reaching the server. |
| `errors` | Recoverable retrieval or submission errors plus fatal API errors handled by the solver. |

A run returns exit code `1` only when an API error prevents any session from starting. Once a session has started, recoverable API errors do not by themselves make the run fail. Standard argument parsing uses exit code `0` for help and `2` for invalid arguments; unexpected exceptions propagate.

## Tests

Run the merged suite with:

```bash
python3 -m unittest -v
```

The current suite contains 13 deterministic tests covering HTTP request construction and response validation, transport error translation, ID ordering and deduplication, batch continuation and session replacement, deadline handling, event reporting, exit semantics, and log finalization. It does not claim live-server integration or benchmark coverage.

## Tradeoffs and known limitations

- Completion discovery is heuristic because the protocol exposes no piece count; `/submit` is the only authority.
- Only one session is active, leaving permitted cross-session parallelism unused.
- Waiting for the whole retrieval batch creates head-of-line delay when one request is slow.
- A failed retrieval index is not retried because subsequent batches continue from the next index.
- Worker, batch, timeout, and duration defaults are not benchmark-proven.
- Blocking requests may outlive solver control even though their late results are ignored.
- Numeric CLI positivity is expected but not enforced.

## Challenge discussion

The central correctness challenge is that retrieval indices do not define order and the API provides no completion boundary. The client resolves ordering with piece IDs, preserves equal words when their IDs differ, and lets `/submit` decide completeness. The speed challenge is using enough parallel retrieval to discover pieces quickly without allowing unbounded work to overrun the deadline. Bounded thread batches and deadline-aware request timeouts provide that balance, while the limitations above make the remaining assumptions explicit.
