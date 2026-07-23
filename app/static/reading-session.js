export function normalizeRelocation(location) {
  return {
    cfi: location?.cfi,
    fraction: location?.fraction ?? 0,
    index: location?.section?.current ?? location?.index ?? 0,
  }
}

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
