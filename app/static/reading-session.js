export function normalizeRelocation(location) {
  return {
    cfi: location?.cfi,
    fraction: location?.fraction ?? 0,
    index: location?.section?.current ?? location?.index ?? 0,
  }
}

export function createSessionId(cryptoApi = globalThis.crypto) {
  if (typeof cryptoApi?.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }
  const bytes = new Uint8Array(16)
  if (typeof cryptoApi?.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('')
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}

export class ReadingSession {
  constructor({
    sessionId = createSessionId(),
    send,
    now = () => Date.now(),
    setIntervalFn = setInterval,
    clearIntervalFn = clearInterval,
    flushAfterMs = 5 * 60 * 1000,
    pollMs = 60 * 1000,
  }) {
    Object.assign(this, {
      sessionId,
      send,
      now,
      setIntervalFn,
      clearIntervalFn,
      flushAfterMs,
      pollMs,
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
    const previousIndex = this.endLocation.index
    if (location.cfi === this.endLocation.cfi) return
    this.endLocation = { ...location }
    this.dirty = this.startLocation.cfi !== this.endLocation.cfi
    this.dirtySince ??= this.now()
    if (previousIndex !== location.index) await this.flush()
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
        this.startLocation = {
          cfi: payload.end_cfi,
          fraction: payload.percent_to,
          index: payload.end_spine_index,
        }
        this.segmentNo += 1
        this.dirty = this.startLocation.cfi !== this.endLocation.cfi
        if (!this.dirty) this.dirtySince = null
      }
      return ok
    } finally {
      this.inFlight = null
    }
  }

  start() {
    if (this.timer) return
    const schedule = this.setIntervalFn
    this.timer = schedule(() => {
      if (
        this.dirty
        && this.dirtySince !== null
        && this.now() - this.dirtySince >= this.flushAfterMs
      ) {
        this.flush()
      }
    }, this.pollMs)
  }

  stop() {
    if (this.timer) {
      const cancel = this.clearIntervalFn
      cancel(this.timer)
    }
    this.timer = null
  }
}
