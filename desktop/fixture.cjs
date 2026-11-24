// Development-only task scenarios. This module is excluded from release builds.
const { EventEmitter } = require('node:events')
const assert = require('node:assert/strict')

module.exports = class Fixture extends EventEmitter {
  constructor() {
    super()
    const date = new Date(Date.now() + (48 + 8) * 3600000).toISOString().slice(0, 10)
    this.config = { target_date: date, earliest_start: '10:00', min_minutes: 60, max_minutes: 120,
      court_priority: [6, 7, 5, 8, 1, 2, 3, 4, 9, 10, 11, 12], release_days_before: 2,
      release_time: '20:00', release_override: '', auto_refresh: true }
    this.timers = []
    this.runs = 0
    this.bulletin = { body: '综合馆开放安排\n'.repeat(9), synced: new Date().toISOString() }
    this.noticeReads = 0
  }

  later(ms, event) {
    this.timers.push(setTimeout(() => this.emit('event', event), ms))
  }

  async call(action, config, mode) {
    if (action === 'openVenueLogin') return {}
    if (action === 'restartDingTalk') return {}
    if (action === 'load') this.config.target_date = new Date(Date.now() + 56 * 3600000).toISOString().slice(0, 10)
    if (action === 'notice') {
      this.noticeReads += 1
      const response = this.noticeReads === 2 ? { failed: true }
        : { notice: this.noticeReads === 3 ? { ...this.bulletin, body: '' } : this.bulletin }
      this.later(50, { event: 'venue_notice', ...response })
      return {}
    }
    if (action === 'stop') {
      await this.shutdown()
      this.emit('event', { event: 'result', state: 'STOPPED', message: '已停止，恢复记录已保留' })
      this.emit('event', { event: 'finished', state: 'STOPPED' })
      return {}
    }
    if (action === 'retryLogin') {
      this.later(20, { event: 'login_status', phase: 'verified', verified_at: Date.now() / 1000 })
      this.later(100, { event: 'result', state: 'STOPPED', message: '验证完成' })
      this.later(130, { event: 'finished', state: 'STOPPED' })
      return {}
    }
    if (action === 'save' || action === 'start') {
      assert(config)
      this.config = config
    }
    const values = action === 'schedule' ? config : this.config
    const releaseEpoch = values.release_override
      ? Date.parse(values.release_override.replace(' ', 'T') + '+08:00')
      : Date.parse(values.target_date + 'T' + values.release_time + '+08:00') - values.release_days_before * 86400000
    const data = { config: values, releaseEpoch, recovery: null, today: new Date(Date.now() + 8 * 3600000).toISOString().slice(0,10),
      windowMs: 30000, notice: this.bulletin }
    if (action !== 'start') return data
    if (mode === 'preview') {
      this.later(150, { event: 'preview', count: 3, message: '只读演练完成', candidates: [
        { site_id: 6, start: '14:00', end: '16:00', minutes: 120 },
        { site_id: 7, start: '15:00', end: '17:00', minutes: 120 },
        { site_id: 5, start: '10:00', end: '11:00', minutes: 60 },
      ] })
      this.later(170, { event: 'finished', state: 'PREVIEW' })
    } else if (mode === 'refresh' || mode === 'restart') {
      this.later(200, { event: 'result', state: 'VERIFIED', message: '当前场馆登录有效' })
      this.later(250, { event: 'finished', state: 'VERIFIED' })
    } else if (++this.runs === 1) {
      this.later(60, { event: 'log', phase: 'waiting', message: '等待放号前会话检查' })
    } else if (this.runs === 2) {
      this.later(50, { event: 'log', phase: 'renewing', message: '正在刷新令牌' })
      this.later(150, { event: 'log', phase: 'restarting', message: '重启执行进程' })
      this.later(350, { event: 'result', state: 'SUCCESS', message: '请立即在钉钉完成同行邀请',
        order: { site_id: 6, start_time: '14:00', end_time: '16:00', order_date: config.target_date } })
      this.later(400, { event: 'finished', state: 'SUCCESS' })
    } else if (this.runs === 4) {
      this.later(40, { event: 'log', phase: 'await_login', message: '请完成钉钉登录' })
    } else {
      this.later(80, { event: 'result', state: 'UNKNOWN', message: '请在钉钉查看我的预约' })
      this.later(120, { event: 'finished', state: 'UNKNOWN', recovery: { date: config.target_date } })
    }
    return data
  }

  async shutdown() {
    for (const timer of this.timers) clearTimeout(timer)
    this.timers = []
  }
}
