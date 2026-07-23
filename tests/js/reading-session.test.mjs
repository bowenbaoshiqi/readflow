import test from 'node:test'
import assert from 'node:assert/strict'
import {
  ReadingSession,
  normalizeRelocation,
} from '../../app/static/reading-session.js'

test('uses Foliate section.current as the spine index', () => {
  assert.deepEqual(
    normalizeRelocation({
      cfi: 'epubcfi(/6/8!/4)',
      fraction: 0.4,
      section: { current: 3, total: 10 },
    }),
    {
      cfi: 'epubcfi(/6/8!/4)',
      fraction: 0.4,
      index: 3,
    },
  )
})

const loc = (cfi, fraction, index = 1) => ({ cfi, fraction, index })

test('does not submit without movement', async () => {
  const sent = []
  const session = new ReadingSession({
    sessionId: 's1',
    send: async payload => (sent.push(payload), true),
  })
  await session.relocate(loc('a', 0.1))
  await session.flush()
  assert.equal(sent.length, 0)
})

test('success advances the next segment from the prior endpoint', async () => {
  const sent = []
  const session = new ReadingSession({
    sessionId: 's1',
    send: async payload => (sent.push(payload), true),
  })
  await session.relocate(loc('a', 0.1, 1))
  await session.relocate(loc('b', 0.2, 1))
  await session.flush()
  await session.relocate(loc('c', 0.3, 2))
  assert.deepEqual(
    sent.map(payload => [
      payload.segment_no,
      payload.start_cfi,
      payload.end_cfi,
    ]),
    [
      [1, 'a', 'b'],
      [2, 'b', 'c'],
    ],
  )
})

test('failed send retries the same idempotency key', async () => {
  const sent = []
  let succeeds = false
  const session = new ReadingSession({
    sessionId: 's1',
    send: async payload => (sent.push(payload), succeeds),
  })
  await session.relocate(loc('a', 0.1))
  await session.relocate(loc('b', 0.2))
  await session.flush()
  succeeds = true
  await session.flush()
  assert.deepEqual(sent.map(payload => payload.segment_no), [1, 1])
})

test('five minute timer flushes only dirty state', async () => {
  const sent = []
  let now = 0
  let tick
  const session = new ReadingSession({
    sessionId: 's1',
    send: async payload => (sent.push(payload), true),
    now: () => now,
    setIntervalFn: fn => {
      tick = fn
      return 1
    },
    clearIntervalFn: () => {},
  })
  session.start()
  await session.relocate(loc('a', 0.1))
  await session.relocate(loc('b', 0.2))
  tick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(sent.length, 0)
  now = 5 * 60 * 1000
  tick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(sent.length, 1)
})

test('timer functions are invoked without an illegal receiver', () => {
  let timerCallback
  function browserLikeSetInterval(fn) {
    assert.equal(this, undefined)
    timerCallback = fn
    return 9
  }
  function browserLikeClearInterval(timer) {
    assert.equal(this, undefined)
    assert.equal(timer, 9)
  }
  const session = new ReadingSession({
    sessionId: 's1',
    send: async () => true,
    setIntervalFn: browserLikeSetInterval,
    clearIntervalFn: browserLikeClearInterval,
  })
  session.start()
  assert.equal(typeof timerCallback, 'function')
  session.stop()
})

test('lifecycle flag is passed to the sender', async () => {
  const lifecycleFlags = []
  const session = new ReadingSession({
    sessionId: 's1',
    send: async (_payload, lifecycle) => {
      lifecycleFlags.push(lifecycle)
      return true
    },
  })
  await session.relocate(loc('a', 0.1))
  await session.relocate(loc('b', 0.2))
  await session.flush({ lifecycle: true })
  assert.deepEqual(lifecycleFlags, [true])
})

test('movement during an in-flight send remains pending', async () => {
  const sent = []
  let resolveFirst
  const firstResult = new Promise(resolve => {
    resolveFirst = resolve
  })
  const session = new ReadingSession({
    sessionId: 's1',
    send: async payload => {
      sent.push(payload)
      return sent.length === 1 ? firstResult : true
    },
  })
  await session.relocate(loc('a', 0.1))
  await session.relocate(loc('b', 0.2))
  const firstFlush = session.flush()
  await session.relocate(loc('c', 0.3))
  resolveFirst(true)
  await firstFlush
  await session.flush()
  assert.deepEqual(
    sent.map(payload => [payload.start_cfi, payload.end_cfi]),
    [['a', 'b'], ['b', 'c']],
  )
})
