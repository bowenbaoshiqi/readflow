# Reliable Reading Log TDD Evidence

Date: 2026-07-23
Branch: `v2.5`
Source plan: `docs/superpowers/plans/2026-07-23-reliable-reading-log.md`

## User journeys

- As a mobile reader, I want active reading to be saved before the browser
  discards the page, so that the daily card job has source material.
- As a reader on an unreliable network, I want retries to be idempotent, so
  that one reading segment is not stored twice.
- As a card-job operator, I want explicit input and outcome counts, so that I
  can distinguish missing reading input from generation failure.

## RED and GREEN evidence

| Area | RED evidence | GREEN evidence | Guarantee |
|---|---|---|---|
| Database migration, spine extraction, API | `pytest tests/test_reading_log.py -q`: 8 failed, 8 passed | Same command: 16 passed | Existing databases migrate idempotently; explicit spine ranges produce text; duplicate segment keys create one row; legacy requests remain accepted |
| Browser session state | `node --test tests/js/reading-session.test.mjs`: module not found | Same command: 5 passed | Movement is segmented, retried with the same key, flushed after five minutes and marked for lifecycle sends |
| Reader lifecycle integration | `pytest tests/test_api.py::TestReaderSessionPost -q`: 1 failed | Combined reader/API target: 17 passed | Reader binds periodic state, `visibilitychange`, `pagehide`, `beforeunload`, beacon fallback, and return-button flush |
| In-flight movement | Node suite: 1 failed, 5 passed; second segment was absent | Node suite: 6 passed | Reading that continues while a prior request is in flight remains pending and is sent as the next segment |
| Daily card diagnostics | `pytest tests/test_jobs.py::TestDailyJobFullFlow -q`: 2 failed, 1 passed | Same command: 3 passed | Job logs recent input counts and created/skipped outcomes |
| Foliate chapter identity | Node suite failed because `normalizeRelocation` was missing | Node suite: 8 passed | Foliate's `section.current` is persisted as the real spine index instead of silently using chapter zero |
| Backward reading | Target API test returned 422 | Reading-log and typography target: 40 passed | Reading toward an earlier chapter is accepted and text is extracted across the covered range |
| EPUB-owned body font size | Typography target failed because only `html` received the override | Typography target: 23 passed | A book-level `body { font-size: ... }` rule cannot override the user's selected font size |
| NAS HTTP compatibility | Node suite failed because `createSessionId` was missing | Node suite: 9 passed | Reading sessions receive a valid UUID even when `crypto.randomUUID()` is unavailable on an HTTP IP origin |

## Full verification

- `.venv/bin/pytest -q` outside the socket-restricted sandbox:
  `233 passed, 12 skipped, 1 warning`.
- `node --test tests/js/reading-session.test.mjs`:
  `9 passed, 0 failed`.
- `uv run pytest --cov=app --cov-report=term -q`:
  `233 passed, 12 skipped`; total Python coverage `87%`.
- `node --check app/static/reading-session.js`: passed.
- `node --check app/static/reader.js`: passed.
- `python -m compileall -q app`: passed.
- `git diff --check`: passed.
- A copy of the supplied NAS `readflow.db` was migrated in `/tmp`: all four
  segment columns and `idx_reading_log_session_segment` were created without
  modifying the supplied database.

## Coverage and known gaps

The Python application exceeds the required 80% threshold at 87%. Browser
session behavior is covered separately by nine deterministic Node tests, while
the existing Playwright typography/reader suite passed in the full regression.

The 12 skipped tests depend on optional sample EPUB fixtures already absent
before this change. The existing test suite emits a Starlette/httpx deprecation
warning; the coverage run also exposes pre-existing SQLite `ResourceWarning`
messages. Neither warning is caused by the reading-log implementation, and
neither affects the passing result.

NAS deployment and a real mobile backgrounding smoke test remain operational
post-merge checks because this workspace does not have access to the NAS
container or its mounted `/data` volume.
