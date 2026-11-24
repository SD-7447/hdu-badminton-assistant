const pickerFields = ['target_date', 'earliest_start', 'release_time', 'release_override']

function dateKey(date) {
  return date.toISOString().slice(0, 10)
}

function shiftDay(key, offset) {
  const date = new Date(key + 'T00:00:00Z')
  date.setUTCDate(date.getUTCDate() + offset)
  return date
}

function pickerElement(tag, text, className) {
  const element = document.createElement(tag)
  if (text) element.textContent = text
  if (className) element.className = className
  if (tag === 'button') element.type = 'button'
  return element
}

class DateTimePicker {
  constructor(panel, format, currentRelease, onChange) {
    this.panel = panel
    this.format = format
    this.currentRelease = currentRelease
    this.onChange = onChange
    this.triggers = new Map()
    for (const button of document.querySelectorAll('[data-picker]')) {
      this.triggers.set(button.dataset.picker, button)
      button.addEventListener('click', event => this.open(button, event.detail === 0))
    }
    panel.addEventListener('beforetoggle', event => {
      if (event.newState === 'closed') this.onClose()
    })
    panel.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault()
        this.close(true)
      }
    })
    window.addEventListener('resize', () => this.position())
    document.addEventListener('scroll', event => {
      if (!this.panel.contains(event.target)) this.position()
    }, { capture: true, passive: true })
  }

  input(field) {
    return document.getElementById(field)
  }

  sync() {
    for (const [field, trigger] of this.triggers) {
      trigger.textContent = this.input(field).value || (field === 'release_override' ? '未设置' : '—')
    }
  }

  close(restoreFocus = false) {
    if (!this.panel.matches(':popover-open')) return
    const button = this.trigger
    this.panel.hidePopover()
    if (restoreFocus) button.focus({ preventScroll: true })
  }

  onClose() {
    const button = this.trigger
    this.trigger = null
    if (!button) return
    button.setAttribute('aria-expanded', 'false')
    if (this.panel.contains(document.activeElement)) button.focus({ preventScroll: true })
  }

  today() {
    return this.format.format(Date.now()).slice(0, 10)
  }

  open(trigger, keyboard) {
    if (trigger.matches(':disabled')) return
    if (this.trigger === trigger) return this.close(true)
    this.close()
    this.trigger = trigger
    const field = trigger.dataset.picker
    const override = field === 'release_override'
    const dateOnly = field === 'target_date'
    const fallback = Number.isFinite(this.currentRelease()) ? this.currentRelease() : Date.now()
    const rawValue = this.input(field).value
    const value = override ? rawValue || this.format.format(fallback) : rawValue
    const heading = pickerElement('h3', document.getElementById(trigger.getAttribute('aria-labelledby').split(' ')[0]).textContent)
    heading.id = 'picker-heading'
    this.panel.replaceChildren(heading)
    this.panel.classList.toggle('picker-instant', keyboard)
    this.selected = value.slice(0, 10)
    this.dateCursor = this.selected

    if (dateOnly || override) {
      this.calendar = pickerElement('div', '', 'picker-calendar')
      this.panel.append(this.calendar)
      this.renderCalendar()
    }
    if (!dateOnly) this.renderTime(override ? value.slice(11) : value)
    trigger.setAttribute('aria-expanded', 'true')
    this.panel.showPopover({ source: trigger })
    this.position()
    const first = this.panel.querySelector('.day[tabindex="0"], select')
    first?.focus({ preventScroll: true })
  }

  position() {
    if (!this.trigger || !this.panel.matches(':popover-open')) return
    const anchor = this.trigger.getBoundingClientRect()
    if (anchor.bottom < 0 || anchor.top > innerHeight) return this.close()
    const width = this.panel.offsetWidth
    const height = this.panel.offsetHeight
    const left = Math.max(12, Math.min(anchor.left, innerWidth - width - 12))
    const below = innerHeight - anchor.bottom
    const above = anchor.top
    const upward = below < height + 12 && above > below
    const ideal = upward ? anchor.top - height - 6 : anchor.bottom + 6
    const top = Math.max(12, Math.min(ideal, innerHeight - height - 12))
    this.panel.style.left = left + 'px'
    this.panel.style.top = top + 'px'
    this.panel.style.transformOrigin = (anchor.left + Math.min(anchor.width / 2, 32) - left)
      + 'px ' + (upward ? 'bottom' : 'top')
  }

  commit(value, rolling = false) {
    const field = this.trigger.dataset.picker
    this.input(field).value = value
    this.sync()
    this.close(true)
    this.onChange(field, rolling)
  }

  chooseDate(key, rolling = false) {
    const field = this.trigger.dataset.picker
    if (field === 'target_date') return this.commit(key, rolling)
    this.selected = this.dateCursor = key
    this.renderCalendar(true)
  }

  turnMonth(step) {
    const date = shiftDay(this.dateCursor, 0)
    const day = date.getUTCDate()
    date.setUTCDate(1)
    date.setUTCMonth(date.getUTCMonth() + step)
    if (date.getUTCFullYear() < 1 || date.getUTCFullYear() > 9999) return
    const last = new Date(date)
    last.setUTCMonth(last.getUTCMonth() + 1, 0)
    date.setUTCDate(Math.min(day, last.getUTCDate()))
    this.dateCursor = dateKey(date)
    this.renderCalendar(true)
  }

  renderCalendar(focusDay = false) {
    const month = this.dateCursor.slice(0, 7)
    const firstDay = shiftDay(month + '-01', 0)
    const firstColumn = (firstDay.getUTCDay() + 6) % 7
    const today = this.today()
    const header = pickerElement('div', '', 'calendar-head')
    const previous = pickerElement('button', '‹')
    previous.setAttribute('aria-label', '上个月')
    previous.disabled = month === '0001-01'
    previous.addEventListener('click', () => this.turnMonth(-1))
    const next = pickerElement('button', '›')
    next.setAttribute('aria-label', '下个月')
    next.disabled = month === '9999-12'
    next.addEventListener('click', () => this.turnMonth(1))
    const title = pickerElement('strong', firstDay.getUTCFullYear() + ' 年 ' + (firstDay.getUTCMonth() + 1) + ' 月')
    title.setAttribute('aria-live', 'polite')
    header.append(previous, title, next)

    const grid = pickerElement('div', '', 'calendar-grid')
    grid.setAttribute('role', 'grid')
    grid.setAttribute('aria-label', title.textContent)
    const weekdays = pickerElement('div', '', 'calendar-row')
    weekdays.setAttribute('role', 'row')
    for (const day of '一二三四五六日') {
      const label = pickerElement('span', day, 'calendar-weekday')
      label.setAttribute('role', 'columnheader')
      weekdays.append(label)
    }
    grid.append(weekdays)
    for (let row = 0; row < 6; row++) {
      const week = pickerElement('div', '', 'calendar-row')
      week.setAttribute('role', 'row')
      for (let column = 0; column < 7; column++) {
        const date = shiftDay(month + '-01', 7 * row + column - firstColumn)
        const cell = pickerElement('div', '', 'calendar-cell')
        cell.setAttribute('role', 'gridcell')
        if (date.getUTCFullYear() >= 1 && date.getUTCFullYear() <= 9999 && dateKey(date).startsWith(month)) {
          const key = dateKey(date)
          const button = pickerElement('button', String(date.getUTCDate()), 'day')
          button.dataset.date = key
          button.tabIndex = key === this.dateCursor ? 0 : -1
          cell.setAttribute('aria-selected', String(key === this.selected))
          button.setAttribute('aria-label', key)
          if (key === today) button.setAttribute('aria-current', 'date')
          button.addEventListener('click', () => this.chooseDate(key))
          button.addEventListener('keydown', event => this.calendarKey(event, key))
          cell.append(button)
        }
        week.append(cell)
      }
      grid.append(week)
    }
    const shortcuts = pickerElement('div', '', 'picker-shortcuts')
    for (const [offset, label] of ['今天', '明天', '后天'].entries()) {
      const button = pickerElement('button', label)
      button.addEventListener('click', () => this.chooseDate(dateKey(shiftDay(this.today(), offset)), offset === 2))
      shortcuts.append(button)
    }
    this.calendar.replaceChildren(header, grid, shortcuts)
    if (focusDay) grid.querySelector('[tabindex="0"]')?.focus({ preventScroll: true })
  }

  calendarKey(event, key) {
    const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key]
    if (delta) {
      event.preventDefault()
      const date = shiftDay(key, delta)
      if (date.getUTCFullYear() < 1 || date.getUTCFullYear() > 9999) return
      this.dateCursor = dateKey(date)
      this.renderCalendar(true)
    } else if (['PageUp', 'PageDown'].includes(event.key)) {
      event.preventDefault()
      this.turnMonth(event.key === 'PageUp' ? -1 : 1)
    }
  }

  timeSelect(label, start, end, value) {
    const wrapper = pickerElement('label', label)
    const select = document.createElement('select')
    select.setAttribute('aria-label', label)
    for (let number = start; number <= end; number++) {
      const text = String(number).padStart(2, '0')
      const option = pickerElement('option', text)
      option.value = text
      select.append(option)
    }
    select.value = value
    if (!select.value) select.selectedIndex = 0
    wrapper.append(select)
    return wrapper
  }

  renderTime(value) {
    const field = this.trigger.dataset.picker
    const override = field === 'release_override'
    const earliest = field === 'earliest_start'
    const values = value.split(':')
    const inputs = pickerElement('div', '', 'picker-time')
    inputs.append(this.timeSelect('小时', earliest ? 10 : 0, 23, values[0]),
      this.timeSelect('分钟', 0, 59, values[1]))
    if (override) inputs.append(this.timeSelect('秒', 0, 59, values[2] || '00'))
    const actions = pickerElement('div', '', 'picker-actions')
    const apply = pickerElement('button', '应用', 'primary')
    apply.addEventListener('click', () => {
      const time = [...inputs.querySelectorAll('select')].map(select => select.value).join(':')
      this.commit(override ? this.selected + ' ' + time : time)
    })
    if (override && this.input(field).value) {
      const clear = pickerElement('button', '清除覆盖', 'text-button')
      clear.addEventListener('click', () => this.commit(''))
      actions.append(clear)
    }
    actions.append(apply)
    if (!override) {
      const shortcuts = pickerElement('div', '', 'picker-shortcuts')
      for (const time of earliest ? ['10:00', '12:00', '14:00', '18:00'] : ['19:00', '20:00', '21:00']) {
        const button = pickerElement('button', time)
        button.addEventListener('click', () => this.commit(time))
        shortcuts.append(button)
      }
      this.panel.append(shortcuts)
    }
    this.panel.append(inputs, actions)
  }
}
