# Puzzle Solver Assessment

This repository hosts a Python technical assessment: build a client that solves as many word-sequence puzzles as possible within a 30-second window. The initial `main` baseline contains the challenge context only; the solution will be introduced through later reviewable pull requests.

## Challenge goal

Each puzzle is an ordered sequence of words exposed as retrievable pieces. A client must collect the pieces, restore their order, submit the candidate sequence, and maximize the number of accepted puzzles before time expires.

## API flow

The local challenge server runs at `http://localhost:8080`:

```bash
docker run -p 8080:8080 ifajardov/puzzle
```

1. `GET /start` creates a puzzle and returns its `session_id`.
2. `GET /get/{n}?session_id={session_id}` returns a piece containing `id` and `word`.
3. `POST /submit` accepts the `session_id` and an ordered `words` array, then returns `ok`.
4. After `ok: false`, the session remains open for more retrievals and submissions. After `ok: true`, the session closes and another puzzle may be started.

## Confirmed contract

- Retrieval indices are non-negative integers; the same index is deterministic within a session.
- A piece's numeric `id` defines its identity and submission order. Equal words may belong to different IDs.
- `POST /submit` is the authoritative completeness check; clients may continue collecting after rejection.
- Concurrent requests and multiple simultaneous sessions are permitted but not required.
- The API exposes no piece count or separate completion signal.
- Observed puzzle distributions and sizes are not protocol guarantees.

## Expected deliverable

The assessment requires a Python client published in a GitHub repository. Its final documentation must explain how to run it, how it balances speed and correctness, and which challenge constraints shaped the solution. The repository link is then sent to the challenge provider.

The original brief is preserved in [`Puzzle Solver.pdf`](Puzzle%20Solver.pdf).

## Roadmap

1. **Baseline:** establish the assessment contract and repository purpose on `main`.
2. **Implementation PR:** add the solver and its tests as one reviewable work unit.
3. **Review-fixes PR:** apply independently reviewable corrections identified during implementation review.
