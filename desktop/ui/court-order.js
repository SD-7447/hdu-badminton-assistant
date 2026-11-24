class CourtOrder {
  constructor(list, participants, toggle, feedback, onChange) {
    this.list = list
    this.feedback = feedback
    this.onChange = onChange
    this.reducedMotion = matchMedia('(prefers-reduced-motion: reduce)')
    this.cards = new Map()
    this.checks = new Map()
    this.active = []
    this.gesture = null

    for (let court = 1; court <= 12; court++) {
      const card = document.createElement('li')
      const handle = document.createElement('button')
      handle.type = 'button'
      handle.className = 'court-card'
      handle.textContent = court
      handle.dataset.court = court
      handle.setAttribute('aria-describedby', 'court-keys')
      handle.addEventListener('pointerdown', event => this.grab(event, court))
      handle.addEventListener('keydown', event => this.key(event, court))
      card.append(handle)
      this.cards.set(court, card)

      const label = document.createElement('label')
      const check = document.createElement('input')
      check.type = 'checkbox'
      check.addEventListener('change', () => this.setParticipation(court, check.checked))
      label.append(check, court + ' 号')
      participants.append(label)
      this.checks.set(court, check)
    }
    toggle.addEventListener('click', () => {
      this.finish(false)
      participants.hidden = !participants.hidden
      toggle.setAttribute('aria-expanded', String(!participants.hidden))
    })
    list.addEventListener('pointermove', event => this.movePointer(event))
    list.addEventListener('pointerup', event => this.releasePointer(event))
    list.addEventListener('pointercancel', event => this.cancelPointer(event))
    list.addEventListener('lostpointercapture', event => this.cancelPointer(event))
    window.addEventListener('keydown', event => {
      if (event.key === 'Escape' && this.gesture) {
        event.preventDefault()
        this.finish(false)
      }
    })
    window.addEventListener('blur', () => this.finish(false))
    window.addEventListener('resize', () => this.finish(false))
  }

  button(court) {
    return this.cards.get(court).firstElementChild
  }

  load(order) {
    this.active = [...order]
    this.render(order)
    this.syncChecks()
  }

  syncChecks() {
    for (const [court, check] of this.checks) {
      check.checked = this.active.includes(court)
      check.disabled = check.checked && this.active.length === 1
    }
  }

  setParticipation(court, enabled) {
    this.finish(false)
    this.active = enabled ? [...this.active, court] : this.active.filter(value => value !== court)
    this.render(this.active)
    this.syncChecks()
    this.feedback.textContent = court + (enabled ? ' 号场已加入排序末尾' : ' 号场已停用')
    this.onChange()
  }

  render(order, animate = false) {
    const focus = document.activeElement
    const cards = order.map(court => this.cards.get(court))
    const before = new Map()
    for (const card of [...this.list.children]) {
      const handle = card.firstElementChild
      before.set(card, handle.getBoundingClientRect())
      handle.getAnimations().forEach(animation => animation.cancel())
      if (!cards.includes(card)) card.remove()
    }
    cards.forEach((card, index) => {
      const current = this.list.children[index]
      if (current !== card) this.list.insertBefore(card, current || null)
      const handle = card.firstElementChild
      handle.setAttribute('aria-label', handle.dataset.court + ' 号场，第 ' + (index + 1) + ' 顺位')
    })
    if (focus instanceof HTMLElement && this.list.contains(focus) && focus !== document.activeElement) {
      focus.focus({ preventScroll: true })
    }
    if (!animate || this.reducedMotion.matches) return
    for (const card of cards) {
      const start = before.get(card)
      if (start && card !== this.gesture?.card) this.animateFrom(card.firstElementChild, start)
    }
  }

  animateFrom(handle, start) {
    const end = handle.getBoundingClientRect()
    const dx = start.left - end.left
    const dy = start.top - end.top
    if (!dx && !dy) return
    return handle.animate([{ transform: 'translate(' + dx + 'px, ' + dy + 'px)' }, { transform: 'none' }],
      { duration: 180, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' })
  }

  grab(event, court) {
    const handle = this.button(court)
    if (!event.isPrimary || event.button !== 0 || handle.matches(':disabled')) return
    this.finish(false)
    const box = handle.getBoundingClientRect()
    this.gesture = {
      court, card: this.cards.get(court), order: [...this.active], pointer: event.pointerId,
      x: event.clientX, y: event.clientY, offsetX: event.clientX - box.left, offsetY: event.clientY - box.top,
      box,
    }
    this.list.setPointerCapture(event.pointerId)
  }

  movePointer(event) {
    const drag = this.gesture
    if (!drag || drag.pointer !== event.pointerId) return
    if (!drag.ghost) {
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 4) return
      const handle = this.button(drag.court)
      handle.getAnimations().forEach(animation => animation.cancel())
      drag.ghost = document.createElement('div')
      drag.ghost.className = 'court-card court-ghost'
      drag.ghost.textContent = drag.court
      drag.ghost.setAttribute('aria-hidden', 'true')
      drag.ghost.style.width = drag.box.width + 'px'
      drag.ghost.style.height = drag.box.height + 'px'
      document.body.append(drag.ghost)
      handle.classList.add('court-dragged')
    }
    drag.ghost.style.transform = 'translate3d(' + (event.clientX - drag.offsetX) + 'px, '
      + (event.clientY - drag.offsetY) + 'px, 0)'
    if (!this.inside(event)) return
    drag.lastInside = drag.ghost.getBoundingClientRect()

    const x = event.clientX - drag.offsetX + drag.box.width / 2
    const y = event.clientY - drag.offsetY + drag.box.height / 2
    const target = [...this.list.children].findIndex(card => {
      const box = card.getBoundingClientRect()
      return Math.abs(y - (box.top + box.height / 2)) <= box.height / 2
        && Math.abs(x - (box.left + box.width / 2)) <= box.width * .35
    })
    if (target >= 0) this.moveTo(target, true)
  }

  inside(event) {
    const box = this.list.getBoundingClientRect()
    return event.clientX >= box.left && event.clientX <= box.right
      && event.clientY >= box.top && event.clientY <= box.bottom
  }

  releasePointer(event) {
    if (this.gesture?.pointer !== event.pointerId) return
    this.finish(this.inside(event))
  }

  cancelPointer(event) {
    if (this.gesture?.pointer === event.pointerId) this.finish(false)
  }

  moveTo(index, animate) {
    const drag = this.gesture
    const from = drag.order.indexOf(drag.court)
    if (index === from || index < 0 || index >= drag.order.length) return
    drag.order.splice(from, 1)
    drag.order.splice(index, 0, drag.court)
    this.render(drag.order, animate)
    if (!animate) this.feedback.textContent = drag.court + ' 号场移到第 ' + (index + 1) + ' 顺位'
  }

  key(event, court) {
    const key = event.key
    if (key === ' ' && !event.repeat && this.gesture?.pointer == null) {
      event.preventDefault()
      if (this.gesture) {
        this.finish(true)
      } else {
        this.gesture = { court, card: this.cards.get(court), order: [...this.active] }
        this.button(court).classList.add('court-key-grabbed')
        this.feedback.textContent = '已抓取 ' + court + ' 号场'
      }
    }
    if (this.gesture?.court !== court || this.gesture.pointer != null) return
    const columns = getComputedStyle(this.list).gridTemplateColumns.split(' ').length
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns, ArrowDown: columns }[key]
    if (!step) return
    event.preventDefault()
    this.moveTo(this.gesture.order.indexOf(court) + step, false)
  }

  finish(commit) {
    const drag = this.gesture
    if (!drag) return
    this.gesture = null
    if (drag.pointer != null && this.list.hasPointerCapture(drag.pointer)) {
      this.list.releasePointerCapture(drag.pointer)
    }
    const handle = this.button(drag.court)
    handle.classList.remove('court-key-grabbed', 'court-dragged')
    const dropBox = commit ? drag.ghost?.getBoundingClientRect() : drag.lastInside
    drag.ghost?.remove()
    if (drag.pointer != null && !drag.ghost) return
    const changed = commit && drag.order.some((value, index) => value !== this.active[index])
    if (changed) this.active = drag.order
    this.render(this.active, Boolean(dropBox))
    if (dropBox && !this.reducedMotion.matches) {
      handle.getAnimations().forEach(animation => animation.cancel())
      handle.classList.add('court-settling')
      const animation = this.animateFrom(handle, dropBox)
      if (animation) {
        animation.finished.catch(() => {}).then(() => {
          if (!handle.getAnimations().some(item => item.playState === 'running')) handle.classList.remove('court-settling')
        })
      } else handle.classList.remove('court-settling')
    }
    if (changed) {
      this.feedback.textContent = drag.court + ' 号场已排到第 ' + (this.active.indexOf(drag.court) + 1) + ' 顺位'
      this.onChange()
    } else if (!commit) {
      this.feedback.textContent = '已取消排序'
    }
  }
}
