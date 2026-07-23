# Reliable Reading Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist actual reading segments approximately every five minutes and on lifecycle boundaries, without duplicates, while extracting text from the correct EPUB spine range.

**Architecture:** A focused browser-side `ReadingSession` module owns pending segment state, periodic flushing, lifecycle flushing, and idempotency keys. The existing FastAPI endpoint validates the supplied Foliate spine indices, extracts the corresponding EPUB chapters, and inserts through a partial unique index on `(session_id, segment_no)`. The daily job keeps its existing input contract but emits explicit input and outcome counts.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite WAL, EbookLib, browser ES modules, Node built-in test runner, pytest.

## Global Constraints

- Work only on branch `v2.5`.
- Submit only when the reading position has actually changed.
- Periodic persistence threshold is exactly 5 minutes; the timer may poll once per minute.
- Flush immediately on spine change, return-to-library, `visibilitychange` to hidden, and `pagehide`.
- Keep `beforeunload` only as a final fallback.
- Use `session_id + segment_no` as the stable idempotency key.
- Continue accepting legacy requests that omit `session_id`, `segment_no`, and spine indices during rolling deployment.
- Do not change daily card counts, prompts, or scheduling.
- Do not add runtime dependencies.
- Do not modify or commit the existing unrelated changes in `pyproject.toml`, `uv.lock`, `docs/articles/`, or `readflow.db`.

## File Structure

- Modify `app/db.py`: add the idempotent `reading_log` column/index migration.
- Modify `app/epub_text.py`: expose spine-index-based chapter extraction; retain a compatibility wrapper only where existing callers/tests need it.
- Modify `app/routes/reader.py`: validate the new request fields, call the new extractor, and return created/duplicate/skipped statuses.
- Create `app/static/reading-session.js`: isolated browser session state machine with no DOM rendering responsibility.
- Modify `app/static/reader.js`: feed Foliate relocate events into the state machine and bind lifecycle/navigation triggers.
- Modify `app/jobs.py`: log daily input counts and generation outcomes.
- Modify `tests/test_reading_log.py`: migration, extractor, endpoint, compatibility, and idempotency tests.
- Create `tests/js/reading-session.test.mjs`: deterministic state-machine tests using Node's built-in test runner.
- Modify `tests/test_jobs.py`: observable no-input/success/failure job tests.

---

### Task 1: Idempotent reading-log schema migration

**Files:**
- Modify: `app/db.py:96-130`
- Test: `tests/test_reading_log.py`

**Interfaces:**
- Consumes: existing `db.init_db()` and SQLite connection helpers.
- Produces: `_migrate_reading_log_columns(conn) -> None` and the columns `session_id`, `segment_no`, `start_spine_index`, `end_spine_index`.

- [ ] **Step 1: Write failing migration tests**

Add these tests to `TestReadingLogTable`:

```python
def test_reading_log_session_columns_and_unique_index(self):
    with db.get_conn() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(reading_log)")
        }
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(reading_log)")
        }
    assert {
        "session_id", "segment_no", "start_spine_index", "end_spine_index"
    } <= columns
    assert "idx_reading_log_session_segment" in indexes

def test_reading_log_migration_is_idempotent(self):
    db.init_db()
    db.init_db()
    with db.get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('reading_log') "
            "WHERE name IN ('session_id','segment_no',"
            "'start_spine_index','end_spine_index')"
        ).fetchone()[0]
    assert count == 4
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/test_reading_log.py::TestReadingLogTable::test_reading_log_session_columns_and_unique_index tests/test_reading_log.py::TestReadingLogTable::test_reading_log_migration_is_idempotent -v
```

Expected: the first test fails because the four columns and index do not exist.

- [ ] **Step 3: Implement the migration**

Call `_migrate_reading_log_columns(conn)` from `init_db()` after the metadata migration, and add:

```python
def _migrate_reading_log_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(reading_log)")
    }
    new_cols = [
        ("session_id", "TEXT"),
        ("segment_no", "INTEGER"),
        ("start_spine_index", "INTEGER"),
        ("end_spine_index", "INTEGER"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE reading_log ADD COLUMN {col} {typ}")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_reading_log_session_segment
           ON reading_log(session_id, segment_no)
           WHERE session_id IS NOT NULL AND segment_no IS NOT NULL"""
    )
```

- [ ] **Step 4: Run migration tests and the DB test module**

Run:

```bash
uv run pytest tests/test_reading_log.py::TestReadingLogTable -v
```

Expected: all `TestReadingLogTable` tests pass.

- [ ] **Step 5: Commit only this task**

```bash
git add app/db.py tests/test_reading_log.py
git commit -m "feat: add idempotent reading log segments"
```

---

### Task 2: Extract text using explicit spine indices

**Files:**
- Modify: `app/epub_text.py`
- Test: `tests/test_reading_log.py`

**Interfaces:**
- Consumes: EbookLib `epub.read_epub()` and zero-based Foliate renderer indices.
- Produces: `extract_spine_text(epub_path: str, start_index: int, end_index: int) -> str`.

- [ ] **Step 1: Replace CFI-assumption tests with spine-range tests**

Add/import `ebooklib.epub` and write:

```python
def _readable_spine_indices(path):
    from ebooklib import epub
    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    return [
        i for i, (item_id, linear) in enumerate(book.spine)
        if linear != "no" and book.get_item_with_id(item_id) is not None
    ]

def test_extract_text_from_explicit_spine_index():
    from app.epub_text import extract_spine_text
    path = _first_sample()
    index = _readable_spine_indices(path)[0]
    text = extract_spine_text(str(path), index, index)
    assert text.strip()

def test_extract_text_across_explicit_spine_range():
    from app.epub_text import extract_spine_text
    path = _first_sample()
    indices = _readable_spine_indices(path)
    text = extract_spine_text(str(path), indices[0], indices[1])
    first = extract_spine_text(str(path), indices[0], indices[0])
    second = extract_spine_text(str(path), indices[1], indices[1])
    assert first in text
    assert second in text

def test_extract_text_rejects_bad_spine_range():
    from app.epub_text import InvalidSpineRange, extract_spine_text
    with pytest.raises(InvalidSpineRange):
        extract_spine_text(str(_first_sample()), -1, 0)
```

- [ ] **Step 2: Run extractor tests and verify RED**

Run:

```bash
uv run pytest tests/test_reading_log.py::TestEpubTextExtraction -v
```

Expected: import failures for `extract_spine_text` and `InvalidSpineRange`.

- [ ] **Step 3: Implement explicit spine extraction**

Replace regex-based index inference with:

```python
class EpubTextError(Exception):
    pass


class InvalidSpineRange(EpubTextError):
    pass


def _html_to_text(content: bytes) -> str:
    html = content.decode("utf-8", errors="ignore")
    body = re.search(r"<body[^>]*>(.*?)</body>", html, re.S | re.I)
    inner = body.group(1) if body else html
    inner = re.sub(r"<script[^>]*>.*?</script>", "", inner, flags=re.S | re.I)
    inner = re.sub(r"<style[^>]*>.*?</style>", "", inner, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "", inner)
    return re.sub(r"\s+", " ", text).strip()


def extract_spine_text(
    epub_path: str, start_index: int, end_index: int
) -> str:
    try:
        book = epub.read_epub(epub_path, options={"ignore_ncx": True})
    except Exception as exc:
        raise EpubTextError(f"cannot read epub: {exc}") from exc

    spine = book.spine
    if (
        start_index < 0
        or end_index < 0
        or start_index > end_index
        or end_index >= len(spine)
    ):
        raise InvalidSpineRange(
            f"invalid spine range {start_index}..{end_index} "
            f"for {len(spine)} items"
        )

    parts = []
    for item_id, linear in spine[start_index:end_index + 1]:
        if linear == "no":
            continue
        item = book.get_item_with_id(item_id)
        if item is None:
            continue
        text = _html_to_text(item.get_content())
        if text:
            parts.append(text)
    return "\n".join(parts)
```

Keep the old `extract_text()` temporarily as a compatibility wrapper for legacy endpoint requests, but mark it deprecated and do not use it for new requests.

- [ ] **Step 4: Run extractor and full reading-log tests**

Run:

```bash
uv run pytest tests/test_reading_log.py -v
```

Expected: all tests pass, including legacy extractor tests retained for rolling compatibility.

- [ ] **Step 5: Commit only this task**

```bash
git add app/epub_text.py tests/test_reading_log.py
git commit -m "fix: extract reading text from explicit spine range"
```

---

### Task 3: Make the reading-session API idempotent and observable

**Files:**
- Modify: `app/routes/reader.py:417-461`
- Test: `tests/test_reading_log.py`

**Interfaces:**
- Consumes: `extract_spine_text(path, start_index, end_index)` from Task 2 and migrated columns from Task 1.
- Produces: compatible `ReadingSessionIn` and responses with `status` equal to `created`, `duplicate`, or `skipped`.

- [ ] **Step 1: Write failing endpoint tests**

Add:

```python
def _segment_payload(**overrides):
    payload = {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "segment_no": 1,
        "start_cfi": "epubcfi(/6/2!/4)",
        "end_cfi": "epubcfi(/6/4!/4)",
        "start_spine_index": 0,
        "end_spine_index": 0,
        "percent_from": 0.01,
        "percent_to": 0.02,
    }
    payload.update(overrides)
    return payload

def test_reading_session_is_idempotent(self, client):
    bid = ingest.ingest_file(_first_sample())
    payload = _segment_payload()
    first = client.post(f"/api/books/{bid}/reading-session", json=payload)
    second = client.post(f"/api/books/{bid}/reading-session", json=payload)
    assert first.status_code == 200
    assert first.json()["status"] == "created"
    assert second.json() == {
        "ok": True, "status": "duplicate", "id": first.json()["id"]
    }
    with db.get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM reading_log WHERE session_id=? AND segment_no=?",
            (payload["session_id"], payload["segment_no"]),
        ).fetchone()[0]
    assert count == 1

def test_reading_session_rejects_invalid_spine_range(self, client):
    bid = ingest.ingest_file(_first_sample())
    response = client.post(
        f"/api/books/{bid}/reading-session",
        json=_segment_payload(start_spine_index=-1),
    )
    assert response.status_code == 422

def test_legacy_reading_session_request_remains_supported(self, client):
    bid = ingest.ingest_file(_first_sample())
    response = client.post(
        f"/api/books/{bid}/reading-session",
        json={
            "start_cfi": "epubcfi(/6/4!/4/2/4)",
            "end_cfi": "epubcfi(/6/4!/4/16/2)",
            "percent_from": 0.30,
            "percent_to": 0.45,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] in {"created", "skipped"}
```

- [ ] **Step 2: Run endpoint tests and verify RED**

Run:

```bash
uv run pytest tests/test_reading_log.py::TestReadingSessionAPI -v
```

Expected: new payload fields are ignored and the second request creates a duplicate; `status` is absent.

- [ ] **Step 3: Implement request validation and idempotent insertion**

Define:

```python
class ReadingSessionIn(BaseModel):
    session_id: str | None = None
    segment_no: int | None = Field(default=None, ge=1)
    start_cfi: str
    end_cfi: str
    start_spine_index: int | None = Field(default=None, ge=0)
    end_spine_index: int | None = Field(default=None, ge=0)
    percent_from: float
    percent_to: float


def _existing_segment(body: ReadingSessionIn):
    if body.session_id is None or body.segment_no is None:
        return None
    with db.get_conn() as conn:
        return conn.execute(
            """SELECT id FROM reading_log
               WHERE session_id=? AND segment_no=?""",
            (body.session_id, body.segment_no),
        ).fetchone()
```

Import `Field` from Pydantic and `sqlite3` plus the module logger dependencies
at the top of `reader.py`.

In `create_reading_session()`:

1. Return `status="skipped"` for equal CFIs.
2. Return the existing row as `status="duplicate"` before EPUB extraction.
3. Use `extract_spine_text()` when both spine indices are present.
4. Use the legacy `extract_text()` only when both are absent.
5. Reject a half-specified pair with HTTP 422.
6. Convert `InvalidSpineRange` to HTTP 422 and `EpubTextError` to HTTP 500 after logging the exception.
7. Insert all ten fields.
8. Catch `sqlite3.IntegrityError`, re-read the idempotent row, and return `duplicate` to cover concurrent retries.

The insert must be:

```python
cur = conn.execute(
    """INSERT INTO reading_log(
           book_id, start_cfi, end_cfi, text, percent_from, percent_to,
           session_id, segment_no, start_spine_index, end_spine_index
       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
    (
        book_id, body.start_cfi, body.end_cfi, text,
        body.percent_from, body.percent_to,
        body.session_id, body.segment_no,
        body.start_spine_index, body.end_spine_index,
    ),
)
```

- [ ] **Step 4: Run API tests and all reader API tests**

Run:

```bash
uv run pytest tests/test_reading_log.py tests/test_api.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit only this task**

```bash
git add app/routes/reader.py tests/test_reading_log.py
git commit -m "feat: make reading session API idempotent"
```

---

### Task 4: Build and test the browser reading-session state machine

**Files:**
- Create: `app/static/reading-session.js`
- Create: `tests/js/reading-session.test.mjs`

**Interfaces:**
- Consumes: location objects shaped as `{cfi, fraction, index}` and an injected `send(payload, lifecycle) -> Promise<boolean>`.
- Produces: `ReadingSession` with `relocate(location)`, `flush({lifecycle})`, `start()`, and `stop()`.

- [ ] **Step 1: Write failing Node state-machine tests**

Create `tests/js/reading-session.test.mjs`:

```javascript
import test from 'node:test'
import assert from 'node:assert/strict'
import { ReadingSession } from '../../app/static/reading-session.js'

const loc = (cfi, fraction, index = 1) => ({ cfi, fraction, index })

test('does not submit without movement', async () => {
  const sent = []
  const session = new ReadingSession({
    sessionId: 's1', send: async p => (sent.push(p), true),
  })
  session.relocate(loc('a', 0.1))
  await session.flush()
  assert.equal(sent.length, 0)
})

test('success advances the segment from the prior endpoint', async () => {
  const sent = []
  const session = new ReadingSession({
    sessionId: 's1', send: async p => (sent.push(p), true),
  })
  session.relocate(loc('a', 0.1, 1))
  session.relocate(loc('b', 0.2, 1))
  await session.flush()
  session.relocate(loc('c', 0.3, 2))
  await session.flush()
  assert.deepEqual(sent.map(x => [x.segment_no, x.start_cfi, x.end_cfi]), [
    [1, 'a', 'b'],
    [2, 'b', 'c'],
  ])
})

test('failed send retries the same idempotency key', async () => {
  const sent = []
  let succeeds = false
  const session = new ReadingSession({
    sessionId: 's1',
    send: async p => (sent.push(p), succeeds),
  })
  session.relocate(loc('a', 0.1))
  session.relocate(loc('b', 0.2))
  await session.flush()
  succeeds = true
  await session.flush()
  assert.deepEqual(sent.map(x => x.segment_no), [1, 1])
})

test('spine change requests an immediate flush', async () => {
  const sent = []
  const session = new ReadingSession({
    sessionId: 's1', send: async p => (sent.push(p), true),
  })
  session.relocate(loc('a', 0.1, 1))
  await session.relocate(loc('b', 0.2, 2))
  assert.equal(sent.length, 1)
})
```

- [ ] **Step 2: Run Node tests and verify RED**

Run:

```bash
node --test tests/js/reading-session.test.mjs
```

Expected: module-not-found failure for `app/static/reading-session.js`.

- [ ] **Step 3: Implement the isolated state machine**

Create `app/static/reading-session.js` with this public shape:

```javascript
export class ReadingSession {
  constructor({
    sessionId = crypto.randomUUID(),
    send,
    now = () => Date.now(),
    setIntervalFn = setInterval,
    clearIntervalFn = clearInterval,
    flushAfterMs = 5 * 60 * 1000,
    pollMs = 60 * 1000,
  }) {
    Object.assign(this, {
      sessionId, send, now, setIntervalFn, clearIntervalFn,
      flushAfterMs, pollMs,
    })
    this.segmentNo = 1
    this.startLocation = null
    this.endLocation = null
    this.dirty = false
    this.dirtySince = null
    this.inFlight = null
    this.timer = null
  }

  async relocate(location) {
    if (!location?.cfi) return
    if (!this.startLocation) {
      this.startLocation = { ...location }
      this.endLocation = { ...location }
      return
    }
    const oldIndex = this.endLocation.index
    if (location.cfi === this.endLocation.cfi) return
    this.endLocation = { ...location }
    this.dirty = this.startLocation.cfi !== this.endLocation.cfi
    this.dirtySince ??= this.now()
    if (oldIndex !== location.index) await this.flush()
  }

  payload() {
    return {
      session_id: this.sessionId,
      segment_no: this.segmentNo,
      start_cfi: this.startLocation.cfi,
      end_cfi: this.endLocation.cfi,
      start_spine_index: this.startLocation.index,
      end_spine_index: this.endLocation.index,
      percent_from: this.startLocation.fraction ?? 0,
      percent_to: this.endLocation.fraction ?? 0,
    }
  }

  async flush({ lifecycle = false } = {}) {
    if (!this.dirty || this.inFlight) return this.inFlight
    const payload = this.payload()
    this.inFlight = Promise.resolve(this.send(payload, lifecycle))
    try {
      const ok = await this.inFlight
      if (ok) {
        this.startLocation = { ...this.endLocation }
        this.segmentNo += 1
        this.dirty = false
        this.dirtySince = null
      }
      return ok
    } finally {
      this.inFlight = null
    }
  }

  start() {
    if (this.timer) return
    this.timer = this.setIntervalFn(() => {
      if (
        this.dirty
        && this.dirtySince !== null
        && this.now() - this.dirtySince >= this.flushAfterMs
      ) this.flush()
    }, this.pollMs)
  }

  stop() {
    if (this.timer) this.clearIntervalFn(this.timer)
    this.timer = null
  }
}
```

The spine-change payload intentionally spans the old and new spine indices. This
captures the boundary crossed by the reader and lets the backend concatenate both
chapters; after success, the next segment begins at the new-spine endpoint.

- [ ] **Step 4: Run Node tests**

Run:

```bash
node --test tests/js/reading-session.test.mjs
```

Expected: all four tests pass.

- [ ] **Step 5: Commit only this task**

```bash
git add app/static/reading-session.js tests/js/reading-session.test.mjs
git commit -m "feat: add reliable browser reading session state"
```

---

### Task 5: Integrate periodic and lifecycle flushing into the reader

**Files:**
- Modify: `app/static/reader.js:5-79,167,216-232`
- Modify: `tests/js/reading-session.test.mjs`
- Modify: `tests/test_api.py:586-602`

**Interfaces:**
- Consumes: `ReadingSession` from Task 4 and `/api/books/{id}/reading-session` from Task 3.
- Produces: normal and lifecycle send adapters plus event bindings.

- [ ] **Step 1: Add failing lifecycle and source-contract tests**

Extend the Node test with:

```javascript
test('five minute timer flushes only dirty state', async () => {
  const sent = []
  let now = 0
  let tick
  const session = new ReadingSession({
    sessionId: 's1',
    send: async p => (sent.push(p), true),
    now: () => now,
    setIntervalFn: fn => (tick = fn, 1),
    clearIntervalFn: () => {},
  })
  session.start()
  session.relocate(loc('a', 0.1))
  session.relocate(loc('b', 0.2))
  now = 5 * 60 * 1000
  tick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(sent.length, 1)
})
```

Replace the weak Python source-contract assertions with:

```python
def test_reader_js_binds_reliable_session_events(self):
    from pathlib import Path
    js = (
        Path(__file__).resolve().parent.parent / "app" / "static" / "reader.js"
    ).read_text()
    assert "ReadingSession" in js
    assert "visibilitychange" in js
    assert "pagehide" in js
    assert "beforeunload" in js
    assert "navigator.sendBeacon" in js
    assert "readingSession.flush" in js
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
node --test tests/js/reading-session.test.mjs
uv run pytest tests/test_api.py::TestReaderSessionPost -v
```

Expected: Python contract test fails because the reader lacks `visibilitychange`, `pagehide`, `sendBeacon`, and the new state machine.

- [ ] **Step 3: Wire the state machine into `reader.js`**

Import and instantiate:

```javascript
import { ReadingSession } from './reading-session.js'

async function sendReadingSegment(payload, lifecycle = false) {
  const url = `${BASE}/api/books/${BOOK_ID}/reading-session`
  const body = JSON.stringify(payload)
  if (lifecycle && navigator.sendBeacon) {
    return navigator.sendBeacon(
      url, new Blob([body], { type: 'application/json' }),
    )
  }
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: lifecycle,
    })
    if (!response.ok) {
      console.warn('[readflow] reading session failed', response.status)
      return false
    }
    return true
  } catch (error) {
    console.warn('[readflow] reading session failed', error)
    return false
  }
}

const readingSession = new ReadingSession({ send: sendReadingSegment })
readingSession.start()
```

In the `relocate` handler replace the four old session scalar assignments with:

```javascript
readingSession.relocate({ cfi, fraction: fraction || 0, index: index ?? 0 })
```

Replace direct return navigation with:

```javascript
document.getElementById('back').onclick = async () => {
  await readingSession.flush()
  location.href = '/'
}
```

Replace the old unload-only function with:

```javascript
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    readingSession.flush({ lifecycle: true })
  }
})
window.addEventListener('pagehide', () => {
  readingSession.flush({ lifecycle: true })
})
window.addEventListener('beforeunload', () => {
  readingSession.flush({ lifecycle: true })
})
```

- [ ] **Step 4: Run JS, API contract, and reader E2E tests**

Run:

```bash
node --test tests/js/reading-session.test.mjs
uv run pytest tests/test_api.py tests/test_reader_typography.py -v
```

Expected: all tests pass. If Playwright is unavailable locally, record that exact environment failure and still require it in NAS/pre-release verification.

- [ ] **Step 5: Commit only this task**

```bash
git add app/static/reader.js tests/js/reading-session.test.mjs tests/test_api.py
git commit -m "feat: persist reading during active and background use"
```

---

### Task 6: Add daily-job input and outcome observability

**Files:**
- Modify: `app/jobs.py:257-324`
- Modify: `tests/test_jobs.py`

**Interfaces:**
- Consumes: existing `reading_log`, `highlights`, generation helpers, and pytest `capsys`.
- Produces: one structured console line for input counts and one for skipped, failed, or created outcomes.

- [ ] **Step 1: Write failing log-output tests**

Add:

```python
def test_full_flow_no_reading_log_reports_counts(self, capsys):
    ids = run_daily_job()
    output = capsys.readouterr().out
    assert ids == []
    assert "[readflow] daily_cards input: reading_logs=0 highlights=0" in output
    assert "[readflow] daily_cards skipped: no recent reading input" in output

def test_full_flow_reports_created_count(self, monkeypatch, capsys):
    monkeypatch.setattr(
        "app.jobs._generate_knowledge_and_blindspots",
        lambda: [{"card_type": "knowledge", "title": "T", "body": "B"}],
    )
    monkeypatch.setattr("app.jobs._generate_recommendations", lambda parents: [])
    ids = run_daily_job()
    output = capsys.readouterr().out
    assert len(ids) == 1
    assert "[readflow] daily_cards created: cards=1" in output
```

- [ ] **Step 2: Run focused job tests and verify RED**

Run:

```bash
uv run pytest tests/test_jobs.py -v
```

Expected: the new output assertions fail.

- [ ] **Step 3: Add input counting and outcome logs**

At the start of `run_daily_job()` query:

```python
with db.get_conn() as conn:
    reading_log_count = conn.execute(
        """SELECT COUNT(*) FROM reading_log
           WHERE created_at >= datetime('now', '-1 day')"""
    ).fetchone()[0]
    highlight_count = conn.execute(
        """SELECT COUNT(*) FROM highlights
           WHERE created_at >= datetime('now', '-1 day')"""
    ).fetchone()[0]
print(
    "[readflow] daily_cards input: "
    f"reading_logs={reading_log_count} highlights={highlight_count}"
)
```

Before the existing empty return print:

```python
print("[readflow] daily_cards skipped: no recent reading input")
```

Before the final return print:

```python
print(f"[readflow] daily_cards created: cards={len(ids)}")
```

Keep existing exception messages, but prefix them consistently with
`[readflow] daily_cards failed:` and the step name.

- [ ] **Step 4: Run all job tests**

Run:

```bash
uv run pytest tests/test_jobs.py -v
```

Expected: all job tests pass.

- [ ] **Step 5: Commit only this task**

```bash
git add app/jobs.py tests/test_jobs.py
git commit -m "chore: report daily card input and outcomes"
```

---

### Task 7: Full regression and production-readiness verification

**Files:**
- Modify only if verification exposes a defect in files already listed above.

**Interfaces:**
- Consumes: deliverables from Tasks 1-6.
- Produces: a tested `v2.5` branch ready to build for NAS.

- [ ] **Step 1: Run formatting and whitespace checks**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 2: Run backend regression suite**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass with no failures.

- [ ] **Step 3: Run browser state-machine suite**

Run:

```bash
node --test tests/js/reading-session.test.mjs
```

Expected: all tests pass.

- [ ] **Step 4: Exercise the API twice against a disposable database**

Use the existing FastAPI test to make the exact same
`session_id + segment_no` request twice, then query:

```sql
SELECT session_id, segment_no, COUNT(*) AS copies, length(text) AS chars
FROM reading_log
GROUP BY session_id, segment_no
HAVING session_id IS NOT NULL;
```

Expected: every row has `copies = 1` and `chars > 0`.

- [ ] **Step 5: Review the final diff for scope**

Run:

```bash
git status --short
git diff --stat HEAD~6..HEAD
git log --oneline -7
```

Expected: the six implementation commits touch only the files declared in this plan; the user's pre-existing unrelated working-tree changes remain uncommitted.

- [ ] **Step 6: Perform NAS smoke test after deployment**

On NAS:

```bash
docker compose build readflow
docker compose up -d readflow
docker compose logs --since=10m readflow
```

Read and scroll a test EPUB, background the mobile browser, then inspect:

```bash
docker compose exec readflow python -c "import sqlite3; c=sqlite3.connect('/data/readflow.db'); print(c.execute('SELECT id,book_id,session_id,segment_no,start_spine_index,end_spine_index,length(text),created_at FROM reading_log ORDER BY id DESC LIMIT 5').fetchall())"
```

Expected: a new row appears for the test book with non-null session fields, valid spine indices, and positive text length.

- [ ] **Step 7: Record final verification without committing unrelated files**

If no code changed during verification, do not create an empty commit. Report the backend test count, Node test count, branch name, latest commit, and the NAS smoke-test result.
