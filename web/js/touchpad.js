/** Pointer Events 多指手势状态机；高频移动与滚动在 rAF 内合批。 */
export class Touchpad {
  constructor(element, state, connection, { onFeedback }) {
    this.element = element;
    this.state = state;
    this.connection = connection;
    this.onFeedback = onFeedback;
    this.pointers = new Map();
    this.gesture = null;
    this.dragging = false;
    this.dragTimer = 0;
    this.doubleTimer = 0;
    this.lastTap = null;
    this.lastPinch = 0;
    this.frame = 0;
    this.moves = [];
    this.scrolls = [];
    element.addEventListener('pointerdown', event => this.down(event));
    element.addEventListener('pointermove', event => this.move(event));
    element.addEventListener('pointerup', event => this.up(event));
    element.addEventListener('pointercancel', event => this.cancel(event));
    element.addEventListener('contextmenu', event => event.preventDefault());
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.reset(); });
  }

  point(event) {
    return { id: event.pointerId, x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, lastX: event.clientX, lastY: event.clientY, startedAt: performance.now(), moved: false };
  }

  down(event) {
    if (event.button !== 0 || this.pointers.size >= 3) return;
    event.preventDefault();
    this.element.focus({ preventScroll: true });
    try { this.element.setPointerCapture(event.pointerId); } catch { /* 合成事件或失效指针时按坐标跟踪，不依赖捕获 */ }
    this.pointers.set(event.pointerId, this.point(event));
    this.element.classList.add('pressed');
    if (this.pointers.size === 1) {
      this.gesture = { type: 'tap-candidate', moved: false, total: 0, startedAt: performance.now() };
      this.dragTimer = setTimeout(() => {
        if (this.pointers.size === 1 && this.gesture?.type === 'tap-candidate' && !this.gesture.moved) {
          this.dragging = true;
          this.gesture.type = 'drag';
          this.element.classList.add('dragging');
          this.connection.send('mouse.down', { button: 'left' });
          this.feedback('拖拽');
        }
      }, this.state.touchpad.drag_hold_ms);
    } else if (this.pointers.size === 2) {
      clearTimeout(this.dragTimer);
      clearTimeout(this.doubleTimer);
      if (this.dragging) this.endDrag();
      this.gesture = { type: 'two', moved: false, total: 0, startedAt: performance.now() };
      this.lastPinch = this.pinchDistance();
    } else if (this.pointers.size === 3) {
      this.gesture = { type: 'three', moved: false, total: 0, startedAt: performance.now() };
    }
  }

  move(event) {
    const point = this.pointers.get(event.pointerId);
    if (!point) return;
    event.preventDefault();
    const dx = event.clientX - point.x;
    const dy = event.clientY - point.y;
    point.x = event.clientX;
    point.y = event.clientY;
    const total = Math.hypot(point.x - point.startX, point.y - point.startY);
    if (total > 8) point.moved = true;
    if (this.gesture) {
      this.gesture.total += Math.hypot(dx, dy);
      if (this.gesture.total > 8) this.gesture.moved = true;
    }
    if (this.pointers.size === 1 && (this.dragging || point.moved)) {
      const sensitivity = Number(this.state.touchpad.move_sensitivity) || 1.6;
      this.moves.push({ dx: dx * sensitivity, dy: dy * sensitivity });
      this.schedule();
    } else if (this.pointers.size === 2) {
      if (point.moved) {
        const pinch = this.pinchDistance();
        if (this.lastPinch && Math.abs(pinch - this.lastPinch) > 8) {
          // 双指间距变化为捏合，滚动同步暂停，避免两个手势相互污染。
          this.connection.send('mouse.pinch', { scale: pinch / this.lastPinch });
          this.lastPinch = pinch;
          this.scrolls = [];
          return;
        }
        let speed = Number(this.state.touchpad.scroll_speed) || .6;
        if (this.state.preferences.natural_scroll) speed *= -1;
        // 协议约定 dy>0 为向上滚动；内容随手指上移时发送正值。
        this.scrolls.push({ dx: dx * speed, dy: -dy * speed });
        this.schedule();
      }
    }
  }

  up(event) {
    const point = this.pointers.get(event.pointerId);
    if (!point) return;
    const count = this.pointers.size;
    const duration = performance.now() - (this.gesture?.startedAt || point.startedAt);
    const moved = this.gesture?.moved || [...this.pointers.values()].some(item => item.moved);
    const wasDragging = this.dragging;
    this.pointers.delete(event.pointerId);
    clearTimeout(this.dragTimer);
    if (wasDragging && count === 1) this.endDrag();
    if (count === 1 && !moved && !wasDragging && duration < 350) {
      const now = performance.now();
      const doubleClickMs = this.state.touchpad.double_click_ms;
      if (this.lastTap && now - this.lastTap.time < doubleClickMs && Math.hypot(point.x - this.lastTap.x, point.y - this.lastTap.y) < 20) {
        clearTimeout(this.doubleTimer);
        this.lastTap = null;
        this.connection.send('mouse.click', { button: 'left', clicks: 2 });
        this.feedback('双击');
      } else {
        this.lastTap = { time: now, x: point.x, y: point.y };
        this.doubleTimer = setTimeout(() => {
          this.connection.send('mouse.click', { button: 'left', clicks: 1 });
          this.feedback('左键');
          this.lastTap = null;
        }, doubleClickMs);
      }
    } else if (count === 2 && !moved && duration < 350) {
      this.connection.send('mouse.click', { button: 'right', clicks: 1 });
      this.feedback('右键');
    } else if (count === 3 && moved && duration < 600) {
      const centerX = [...this.pointers.values(), point].reduce((sum, item) => sum + item.x - item.startX, 0) / 3;
      if (Math.abs(centerX) > 20) {
        const direction = centerX < 0 ? 'next' : 'prev';
        this.connection.send('window.switch', { direction });
        this.feedback(direction === 'next' ? '下一个应用' : '上一个应用');
      }
    }
    if (this.pointers.size === 0) {
      this.gesture = null;
      this.element.classList.remove('pressed');
      this.lastPinch = 0;
    }
  }

  cancel(event) {
    this.pointers.delete(event.pointerId);
    if (this.pointers.size === 0) this.reset();
  }

  reset() {
    clearTimeout(this.dragTimer);
    clearTimeout(this.doubleTimer);
    this.pointers.clear();
    this.gesture = null;
    this.lastTap = null;
    if (this.dragging) this.endDrag();
    this.element.classList.remove('pressed', 'dragging');
  }

  endDrag() {
    if (!this.dragging) return;
    this.dragging = false;
    this.element.classList.remove('dragging');
    this.connection.send('mouse.up', { button: 'left' });
  }

  pinchDistance() {
    const [a, b] = [...this.pointers.values()];
    return a && b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      const sum = values => values.reduce((result, value) => ({ dx: result.dx + value.dx, dy: result.dy + value.dy }), { dx: 0, dy: 0 });
      if (this.moves.length) this.connection.send('mouse.move', sum(this.moves));
      if (this.scrolls.length) this.connection.send('mouse.scroll', sum(this.scrolls));
      this.moves = [];
      this.scrolls = [];
    });
  }

  feedback(text) { this.onFeedback?.(text); }
}
