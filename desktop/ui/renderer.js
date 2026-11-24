const node = id => document.getElementById(id)
const dateFormat = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})
const weekdayFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', weekday: 'long' })
const timeFormat = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', timeStyle: 'medium' })
const fields = ['target_date', 'earliest_start', 'min_minutes', 'max_minutes', 'court_priority',
  'release_days_before', 'release_time', 'release_override', 'auto_refresh']
const numbers = new Set(['min_minutes', 'max_minutes', 'release_days_before'])
const phaseLabels = {
  waiting: ['等待放号', 'green'], ready: ['余量监测中', 'green'], booking: ['正在预约', 'green'],
  renewing: ['正在验证钉钉登录', 'warn'], restarting: ['恢复任务', 'warn'],
  await_login: ['等待钉钉登录', 'warn'], manual_retry: ['正在验证登录', 'warn'],
}
const results = {
  SUCCESS: ['预约已确认', 'green'], UNKNOWN: ['预约结果待核对', 'warn'],
  EXPIRED: ['放号窗口已结束', 'warn'], NO_AVAILABILITY: ['本轮未取得预约', 'warn'],
  ERROR: ['任务遇到问题', 'red'], STOPPED: ['任务已停止', 'neutral'],
  VERIFIED: ['登录有效', 'green'], DISCONNECTED: ['后台已断开', 'red'],
}
let loaded = false
let busy = ''
let planned = NaN
let clockOffset = 0
let logCount = 0
let lastResult = ''
let lastVerified = ''
let openingLogin = false
let restartingDingTalk = false
let loginBusy = false
let calendarDay = ''
let manualDate = false
let rollover = null
let scheduleVersion = 0
let windowMs = 30000
let resetTimer
let hiddenRecovery = ''
let bulletin = null
let noticeTimer
let fetchingNotice = false
let deferredNotice = false
const courtOrder = new CourtOrder(node('courts'), node('participants'), node('participation-toggle'),
  node('court-feedback'), clearPreview)
const picker = new DateTimePicker(node('date-time-picker'), dateFormat, () => planned, configurationChanged)

function displayBulletin(status = '') {
  node('notice-meta').dataset.status = status
  const body = node('bulletin-body')
  const content = bulletin ? bulletin.body || '暂无公告' : '暂未获取公告'
  if (body.textContent !== content) {
    body.textContent = content
    node('notice-expand').setAttribute('aria-expanded', 'false')
  }
  const expanded = node('notice-expand').getAttribute('aria-expanded') === 'true'
  body.classList.toggle('collapsed', !expanded)
  node('notice-expand').hidden = !expanded && body.scrollHeight <= body.clientHeight + 1
  node('notice-expand').textContent = expanded ? '收起' : '展开全文'
  node('notice-meta').textContent = [status,
    bulletin?.published && '发布时间 ' + bulletin.published,
    bulletin?.synced && '上次成功同步 ' + dateFormat.format(new Date(bulletin.synced))].filter(Boolean).join(' · ')
}

function deferNotice(delay) {
  deferredNotice = true
  clearTimeout(noticeTimer)
  noticeTimer = setTimeout(refreshBulletin, Math.max(100, delay))
  displayBulletin('放号窗口结束后更新')
}

async function refreshBulletin() {
  if (!loaded || fetchingNotice) return
  const now = Date.now() + clockOffset
  if (now >= planned - 60000 && now < planned + windowMs) return deferNotice(planned + windowMs - now)
  deferredNotice = false
  fetchingNotice = true
  node('notice-refresh').disabled = true
  clearTimeout(noticeTimer)
  const reply = await window.venue.notice()
  if (!reply.ok || reply.data.deferMs) {
    fetchingNotice = false
    node('notice-refresh').disabled = false
    if (reply.ok) deferNotice(reply.data.deferMs)
    else displayBulletin(bulletin ? '更新失败，显示上次内容' : '更新失败')
  }
}

function clearDisplay() {
  clearTimeout(resetTimer)
  hiddenRecovery = node('target_date').value
  lastResult = lastVerified = ''
  logCount = 0
  node('log-entries').replaceChildren()
  node('log-count').textContent = ''
  node('logs').open = false
  node('login-status').textContent = '登录状态尚未验证'
  node('login-entry-feedback').textContent = ''
  node('retry-login').hidden = true
  clearPreview()
  notice('就绪', '可先演练，确认候选和放号时间。')
}

function notice(title, message, tone = 'neutral') {
  node('phase').textContent = title
  node('phase').dataset.tone = tone
  node('notice').dataset.tone = tone
  node('notice-title').textContent = title
  node('notice-message').textContent = message
}

function controls() {
  if (!loaded || busy) {
    courtOrder.finish(false)
    picker.close()
  }
  node('settings-fields').disabled = !loaded || Boolean(busy)
  node('clear-status').disabled = !loaded || Boolean(busy)
  for (const id of ['save', 'refresh', 'preview', 'start']) node(id).disabled = !loaded || Boolean(busy)
  node('stop').hidden = !busy || busy === 'closing'
  node('start').hidden = Boolean(busy)
  node('actions-hint').textContent = busy ? '任务进行中，设置已锁定' : '请保持钉钉登录'
}

function readConfig() {
  const values = {}
  for (const name of fields) {
    const input = node(name)
    if (name === 'auto_refresh') values[name] = input.checked
    else if (name === 'court_priority') values[name] = [...courtOrder.active]
    else if (numbers.has(name)) values[name] = input.valueAsNumber
    else values[name] = input.value.trim()
  }
  return values
}

function updateHeader(epoch) {
  const config = readConfig()
  node('hero-date').textContent = config.target_date || '—'
  const day = new Date(config.target_date + 'T12:00:00+08:00')
  node('weekday').textContent = isNaN(day) ? '' : weekdayFormat.format(day)
  planned = epoch
  node('release').textContent = Number.isFinite(epoch) ? dateFormat.format(epoch) : '—'
  tick()
}

function tick() {
  const finalMinute = busy === 'run' && Date.now() + clockOffset >= planned - 60000
  const blocked = !loaded || loginBusy || finalMinute || (busy && busy !== 'run')
  node('open-venue-login').disabled = Boolean(blocked || openingLogin || restartingDingTalk)
  node('restart-dingtalk').disabled = Boolean(blocked || openingLogin || restartingDingTalk)
  if (!Number.isFinite(planned)) {
    node('countdown').textContent = '—'
    return
  }
  const seconds = Math.ceil((planned - Date.now() - clockOffset) / 1000)
  if (seconds <= 0) {
    node('countdown').textContent = Date.now() + clockOffset >= planned + windowMs ? '本轮已结束' : '已到放号时间'
    return
  }
  const days = Math.floor(seconds / 86400)
  const time = [Math.floor(seconds / 3600) % 24, Math.floor(seconds / 60) % 60, seconds % 60]
    .map(value => String(value).padStart(2, '0')).join(':')
  node('countdown').textContent = (days ? days + '天 ' : '') + time
}

function loadConfig(config, releaseEpoch) {
  for (const name of fields) {
    if (name === 'court_priority') courtOrder.load(config[name])
    else if (name === 'auto_refresh') node(name).checked = config[name]
    else node(name).value = config[name]
  }
  picker.sync()
  updateHeader(releaseEpoch)
}

function showRecovery(recovery) {
  notice('预约结果待核对', '预约记录' + (recovery.date ? '（' + recovery.date + '）' : '')
    + '仍待核对，请在钉钉查看「我的预约」。', 'warn')
}

function clearPreview(text = '运行只读演练查看候选时段') {
  node('candidate-table').hidden = true
  node('candidates').replaceChildren()
  node('candidate-count').textContent = ''
  node('empty').hidden = false
  node('empty').textContent = text
}

function showPreview(event) {
  const rows = (event.candidates || []).map((candidate, i) => {
    const row = document.createElement('tr')
    for (const value of [String(i + 1).padStart(2, '0'), candidate.site_id + ' 号场',
      candidate.start + ' – ' + candidate.end, candidate.minutes + ' 分钟']) {
      const cell = document.createElement('td')
      cell.textContent = value
      row.append(cell)
    }
    return row
  })
  node('candidates').replaceChildren(...rows)
  node('candidate-table').hidden = rows.length === 0
  node('empty').hidden = rows.length > 0
  node('empty').textContent = '当前没有符合设置的候选'
  node('candidate-count').textContent = '共 ' + event.count + ' 个' + (event.count > rows.length ? ' · 显示前 ' + rows.length + ' 个' : '')
  notice('演练完成', event.existing_booking ? '该日已有预约，正式任务会直接停止。' : '候选按实际提交顺序排列。', 'green')
  lastResult = 'PREVIEW'
}

function appendLog(message) {
  if (!message) return
  const list = node('log-entries')
  if (!logCount) list.replaceChildren()
  const line = document.createElement('li')
  line.textContent = timeFormat.format(new Date()) + '　' + message
  list.append(line)
  if (list.children.length > 120) list.firstElementChild.remove()
  node('log-count').textContent = String(++logCount)
  list.scrollTop = list.scrollHeight
}

function receive(event) {
  if (event.event === 'venue_notice') {
    fetchingNotice = false
    node('notice-refresh').disabled = false
    if (event.deferMs) return deferNotice(event.deferMs)
    if (event.notice) bulletin = event.notice
    displayBulletin(event.failed ? (bulletin ? '更新失败，显示上次内容' : '更新失败') : '')
    return
  }
  if (event.event === 'login_status' || ['await_login', 'renewing', 'unverified'].includes(event.phase)) {
    if (event.phase === 'verified') lastVerified = dateFormat.format(event.verified_at * 1000)
    const status = { verified: '预约接口登录有效', unverified: '暂时无法确认登录状态',
      await_login: '等待钉钉登录', renewing: '正在通过新场馆页验证登录' }
    node('login-status').textContent = (status[event.phase] || '正在验证登录')
      + (lastVerified ? ' · 最近验证成功 ' + lastVerified : '')
  }
  if (event.phase === 'renewing') loginBusy = true
  if (['restarting', 'await_login', 'verified'].includes(event.phase) || event.event === 'finished') loginBusy = false
  if (event.phase === 'await_login') {
    node('retry-login').hidden = false
    node('retry-login').disabled = false
  } else if (['manual_retry', 'restarting', 'verified', 'renewing'].includes(event.phase)
      || ['result', 'finished', 'closing'].includes(event.event)) {
    node('retry-login').hidden = true
  }
  if (event.event === 'closing') {
    busy = 'closing'
    controls()
    notice('正在关闭', '等待后台停止并保留预约记录。')
    return
  }
  let message = event.message || ''
  if (typeof event.clock_offset_ms === 'number') {
    clockOffset = event.clock_offset_ms
    tick()
  }
  if (event.event === 'finished') {
    if (busy !== 'closing') busy = ''
    controls()
    if (event.recovery) showRecovery(event.recovery)
    else if (!lastResult && results[event.state]) notice(results[event.state][0], message, results[event.state][1])
    if (!event.recovery && !['UNKNOWN', 'ERROR', 'DISCONNECTED'].includes(event.state)) {
      clearTimeout(resetTimer)
      resetTimer = setTimeout(() => { if (!busy) clearDisplay() }, 60000)
    }
  } else if (event.event === 'preview') {
    showPreview(event)
  } else if (event.event === 'result') {
    lastResult = event.state
    const label = results[event.state] || ['任务已结束', 'neutral']
    if (event.order?.site_id && event.order?.start_time && event.order?.end_time) {
      const order = event.order
      message = [order.order_date, order.site_id + ' 号场', order.start_time + '–' + order.end_time]
        .filter(Boolean).join(' · ') + '。' + message
    }
    notice(label[0], message, label[1])
  } else if (event.phase && phaseLabels[event.phase] && busy && busy !== 'closing') {
    const label = phaseLabels[event.phase]
    notice(label[0], message, label[1])
  }
  appendLog(message)
}

function formConfig() {
  const invalid = node('configuration').querySelector('input:invalid')
  if (invalid) invalid.closest('details')?.setAttribute('open', '')
  if (!node('configuration').reportValidity()) return null
  return readConfig()
}

async function start(mode) {
  if (busy) return
  await refreshDay()
  if (busy) return
  const config = formConfig()
  if (!config) return
  clearDisplay()
  hiddenRecovery = ''
  ++scheduleVersion
  busy = mode
  lastVerified = ''
  node('login-status').textContent = '正在验证登录'
  lastResult = ''
  controls()
  if (mode === 'preview') clearPreview('正在读取候选时段…')
  const label = { preview: '正在演练', run: '正在准备', refresh: '正在检查登录', restart: '正在重启钉钉' }
  notice(label[mode], mode === 'restart' ? '退出客户端后会重新打开场馆速约。' : '正在验证场馆登录。', 'green')
  const response = await window.venue.start(config, mode)
  if (!response.ok) {
    busy = ''
    controls()
    if (mode === 'preview') clearPreview()
    notice('任务无法启动', response.error, 'red')
  } else {
    updateHeader(response.data.releaseEpoch)
  }
}

node('configuration').addEventListener('submit', event => event.preventDefault())
async function updateSchedule(showHistory = false) {
  const id = ++scheduleVersion
  const reply = await window.venue.schedule(readConfig())
  if (id !== scheduleVersion || busy) return
  if (reply.ok) {
    windowMs = reply.data.windowMs
    updateHeader(reply.data.releaseEpoch)
    if (showHistory && reply.data.recovery && hiddenRecovery !== reply.data.recovery.date) showRecovery(reply.data.recovery)
    if (deferredNotice) refreshBulletin()
  } else updateHeader(NaN)
}

async function refreshDay() {
  const usesDefault = () => !manualDate && !node('release_override').value && node('release_days_before').valueAsNumber === 2
  if (busy || !loaded || !usesDefault() || calendarDay === dateFormat.format(Date.now()).slice(0, 10)) return
  if (rollover) return rollover
  rollover = (async () => {
    const reply = await window.venue.load()
    if (!reply.ok || busy || !usesDefault()) return
    calendarDay = reply.data.today
    node('target_date').value = reply.data.config.target_date
    picker.sync()
    clearDisplay()
    hiddenRecovery = ''
    await updateSchedule(true)
  })().finally(() => { rollover = null })
  return rollover
}

function configurationChanged(field, rolling = false) {
  const dateChanged = field === 'target_date'
  if (dateChanged) manualDate = !rolling
  if (dateChanged) { clearDisplay(); hiddenRecovery = '' }
  updateSchedule(dateChanged)
  clearPreview()
}
node('configuration').addEventListener('input', event => {
  if (fields.includes(event.target.id)) configurationChanged(event.target.id)
})
node('save').addEventListener('click', async () => {
  if (busy) return
  await refreshDay()
  if (busy) return
  const config = formConfig()
  if (!config) return
  node('save').disabled = true
  const id = ++scheduleVersion
  const response = await window.venue.save(config)
  controls()
  if (id !== scheduleVersion || busy) return
  if (response.ok) {
    clearTimeout(resetTimer)
    updateHeader(response.data.releaseEpoch)
    if (response.data.recovery && hiddenRecovery !== response.data.recovery.date) showRecovery(response.data.recovery)
    else notice('配置已保存', '您可以演练或启动定时任务。', 'green')
  } else notice('配置未保存', response.error, 'red')
})
node('preview').addEventListener('click', () => start('preview'))
node('clear-status').addEventListener('click', clearDisplay)
node('notice-refresh').addEventListener('click', refreshBulletin)
node('notice-expand').addEventListener('click', event => {
  const button = event.currentTarget
  button.setAttribute('aria-expanded', String(button.getAttribute('aria-expanded') !== 'true'))
  displayBulletin(node('notice-meta').dataset.status || '')
})
node('start').addEventListener('click', () => start('run'))
node('refresh').addEventListener('click', () => start('refresh'))
node('restart-dingtalk').addEventListener('click', async () => {
  if (restartingDingTalk) return
  if (!busy) return start('restart')
  if (busy !== 'run') return
  restartingDingTalk = true
  tick()
  try {
    const reply = await window.venue.restartDingTalk()
    node('login-entry-feedback').textContent = reply.ok
      ? '已请求重启。助手会保留本轮任务并重新验证登录。' : reply.error
  } finally {
    restartingDingTalk = false
    tick()
  }
})
node('open-venue-login').addEventListener('click', async () => {
  if (openingLogin) return
  openingLogin = true
  tick()
  try {
    const reply = await window.venue.openVenueLogin()
    node('login-entry-feedback').textContent = reply.ok
      ? '已唤起钉钉场馆登录入口，请在钉钉查看。' : reply.error
  } finally {
    openingLogin = false
    tick()
  }
})
node('retry-login').addEventListener('click', async () => {
  node('retry-login').disabled = true
  const response = await window.venue.retryLogin()
  if (!response.ok) {
    node('retry-login').disabled = false
    notice('重新验证未启动', response.error, 'warn')
  }
})
node('stop').addEventListener('click', async () => {
  node('stop').disabled = true
  notice('正在停止', '等待正在处理的请求结束。')
  const response = await window.venue.stop()
  if (!response.ok) notice('停止请求失败', response.error, 'red')
  node('stop').disabled = false
})
window.venue.onEvent(receive)
window.venue.load().then(response => {
  if (!response.ok) {
    notice('无法载入配置', response.error, 'red')
    return
  }
  loadConfig(response.data.config, response.data.releaseEpoch)
  calendarDay = response.data.today
  windowMs = response.data.windowMs
  bulletin = response.data.notice
  displayBulletin()
  loaded = true
  controls()
  node('notice-refresh').disabled = false
  refreshBulletin()
  if (response.data.recovery) showRecovery(response.data.recovery)
  else notice('就绪', '可先演练，确认候选和放号时间。', 'green')
})
setInterval(tick, 250)
setInterval(refreshDay, 1000)
window.addEventListener('focus', refreshDay)
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshDay() })
setInterval(refreshBulletin, 600000)
