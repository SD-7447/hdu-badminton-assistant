const { app, BrowserWindow, dialog, ipcMain, net, Notification, protocol, session } = require('electron')
const { spawn } = require('node:child_process')
const { EventEmitter } = require('node:events')
const { createInterface } = require('node:readline')
const { join, resolve } = require('node:path')
const { pathToFileURL } = require('node:url')
const { mkdirSync } = require('node:fs')

const origin = 'app://venue'
const home = origin + '/index.html'
const assets = new Set(['/index.html', '/style.css', '/renderer.js', '/court-order.js', '/date-time.js', '/mark.svg'])
const state = join(process.env.LOCALAPPDATA || app.getPath('appData'), 'HDU Badminton Assistant')
const fixture = !app.isPackaged && process.argv.includes('--fixture')
const uiState = process.env.HDU_DESKTOP_TEST_UI || join(state, 'desktop')
mkdirSync(uiState, { recursive: true })
app.setPath('userData', uiState)
protocol.registerSchemesAsPrivileged([{ scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true } }])

class Engine extends EventEmitter {
  constructor() {
    super()
    this.requests = new Map()
    this.sequence = 0
    this.closing = false
    const directory = app.isPackaged ? process.resourcesPath : join(__dirname, '.bundle')
    const executable = join(directory, 'venue-engine', 'venue-engine.exe')
    this.process = spawn(executable, ['--bridge', '--state-dir', state], {
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'ignore'],
    })
    this.process.stdin.on('error', () => this.fail())
    this.process.on('error', () => this.fail())
    this.closed = new Promise(done => {
      this.process.once('close', () => {
        this.fail()
        done()
      })
    })
    createInterface({ input: this.process.stdout }).on('line', line => {
      let reply
      try { reply = JSON.parse(line) } catch { return }
      if (reply.event) {
        this.emit('event', reply.event)
        return
      }
      const request = this.requests.get(reply.id)
      if (!request) return
      clearTimeout(request.timer)
      this.requests.delete(reply.id)
      if (reply.ok) request.resolve(reply.data)
      else request.reject(new Error(reply.error || '后台无法完成操作'))
    })
  }

  fail() {
    const wasAvailable = !this.failed
    this.failed = true
    for (const request of this.requests.values()) {
      clearTimeout(request.timer)
      request.reject(new Error('无法连接预约后台'))
    }
    this.requests.clear()
    if (wasAvailable && !this.closing) {
      this.emit('event', { event: 'finished', state: 'DISCONNECTED', message: '预约后台已断开，请重开助手并核对运行记录。' })
    }
  }

  call(action, config, mode) {
    if (this.failed || this.closing) return Promise.reject(new Error('预约后台不可用，请重开助手'))
    const id = ++this.sequence
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.requests.delete(id)
        // A timed-out start may already have reached the engine. Close its
        // input so it cancels the task rather than letting a second start race it.
        this.process.stdin.end()
        this.fail()
        reject(new Error('预约后台响应超时'))
      }, 8000)
      this.requests.set(id, { resolve, reject, timer })
      this.process.stdin.write(JSON.stringify({ id, action, config, mode }) + '\n')
    })
  }

  async shutdown() {
    this.closing = true
    this.process.stdin.end() // the bridge signals cancellation and joins the supervisor
    const timer = setTimeout(() => this.process.kill(), 18000)
    await this.closed
    clearTimeout(timer)
  }
}

let engine
let window
let closing = false
let canClose = false

function registerBridge() {
  for (const action of ['load', 'save', 'start', 'stop', 'retryLogin', 'restartDingTalk', 'openVenueLogin', 'schedule', 'notice']) {
    ipcMain.handle('venue:' + action, async (event, config, mode) => {
      if (event.sender !== window?.webContents || event.senderFrame?.url !== home || closing) {
        return { ok: false, error: '此操作只能在预约窗口执行' }
      }
      try {
        return { ok: true, data: await engine.call(action, config, mode) }
      } catch (error) {
        return { ok: false, error: error.message }
      }
    })
  }
}

function createWindow() {
  window = new BrowserWindow({
    width: 1176,
    height: 818,
    minWidth: 790,
    minHeight: 640,
    title: '杭电羽毛球助手',
    backgroundColor: '#edf2f5',
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: join(__dirname, 'preload.cjs'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  })
  window.removeMenu()
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', event => event.preventDefault())
  engine.on('event', event => {
    if (event.phase === 'await_login' && event.notify && !closing) {
      window.flashFrame(true)
      if (Notification.isSupported()) {
        const notification = new Notification({ title: '抢场助手正在等待钉钉登录', body: event.message })
        notification.on('click', () => { window.restore(); window.show(); window.focus() })
        notification.show()
      }
    }
    if (event.phase === 'restarting' || event.event === 'finished') window.flashFrame(false)
    if (!window.isDestroyed()) window.webContents.send('venue:event', event)
  })
  window.once('ready-to-show', () => window.show())
  window.on('close', event => {
    if (canClose) return
    event.preventDefault()
    if (closing) return
    closing = true
    window.webContents.send('venue:event', { event: 'closing' })
    engine.shutdown().finally(() => {
      canClose = true
      if (!window.isDestroyed()) window.close()
    })
  })
  window.loadURL(home)
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (window?.isMinimized()) window.restore()
    window?.focus()
  })
  app.whenReady().then(() => {
    app.setAppUserModelId('HDU.BadmintonAssistant')
    protocol.handle('app', request => {
      const url = new URL(request.url)
      if (url.hostname !== 'venue' || !assets.has(url.pathname) || request.method !== 'GET') {
        return new Response(null, { status: 404 })
      }
      return net.fetch(pathToFileURL(resolve(__dirname, 'ui', url.pathname.slice(1))).href)
    })
    session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
    session.defaultSession.setPermissionCheckHandler(() => false)
    engine = fixture ? new (require('./fixture.cjs'))() : new Engine()
    registerBridge()
    createWindow()
  }).catch(() => {
    dialog.showErrorBox('杭电羽毛球助手', '应用无法启动，请重新解压后打开。')
    app.quit()
  })
  app.on('window-all-closed', () => app.quit())
}
