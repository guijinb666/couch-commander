function vibrate(ms = 10) {
  try { navigator.vibrate?.(ms); } catch { /* iOS 不支持时保持纯视觉反馈。 */ }
}

// 方向键与 A/B/C 快捷键统一走「物理键盘 key.press」：按下手机端的按钮，
// 等价于在电脑上直接敲一次对应的物理键。后端 key.press 每次独立完整
// down/up，无吞键、无节流，连续点按与组合键（Ctrl+C 之类）都可靠。
const DIRECTION_KEYS = { up: 'up', down: 'down', left: 'left', right: 'right' };

export class Dpad {
  constructor(root, state, connection, { onToast } = {}) {
    this.root = root;
    this.state = state;
    this.connection = connection;
    this.onToast = onToast;
    this.activeHold = null;
    root.querySelectorAll('.dpad-key').forEach(button => this.bindDirection(button));
    root.querySelectorAll('.volume-key').forEach(button => this.bindRepeat(button, () => {
      // 左右音量弧键控制系统音量（标准多媒体物理键），不属于视频内的
      // 快捷键 / 方向键，保留系统音量控制语义。
      connection.send('media.control', { action: button.dataset.action });
    }));
    this.bindPlay(root.querySelector('#play-key'));
    root.querySelectorAll('.page-slot').forEach(button => {
      // pointerdown 触发，绕开 iOS Safari 双击吞 click（与键盘连点同源问题）；
      // 键盘 / 辅助技术合成的 click（detail===0）仍保留，二者互斥不重发。
      const run = event => {
        if (event.type === 'click' && event.detail !== 0) return;
        const slot = ['A', 'B', 'C'][Number(button.dataset.pageSlot)];
        const hotkey = this.findHotkey(slot);
        if (!hotkey || !Array.isArray(hotkey.keys) || !hotkey.keys.length) {
          this.onToast?.('未设置快捷键，去设置里添加/套用模板');
          return;
        }
        // 直接发物理键组合，不再走 hotkey.run 按 id 查表，彻底避开前端 /
        // 服务端 config 不同步（断连态套模板、双写竞态）时的 NOT_FOUND。
        this.pressKeys(hotkey.keys);
      };
      button.addEventListener('pointerdown', event => { event.preventDefault(); run(event); });
      button.addEventListener('click', event => { if (event.detail === 0) run(event); });
    });
    this.bindProgress();
    state.addEventListener('config', () => this.renderSlots());
    state.addEventListener('page-slots', () => this.renderSlots());
    this.renderSlots();
  }

  // 当前页面优先；页面 id 未对齐时回退到全局同 slot 的快捷键，避免 A/B/C 无反应。
  findHotkey(slot) {
    return this.state.activeHotkeys.find(item => item.slot === slot)
      || (this.state.config.hotkeys || []).find(item => item.slot === slot);
  }

  // 本次按键要操控的应用名：优先已锁定的那张卡，否则用当前选中的应用卡。
  targetAppName() {
    const apps = this.state.config.apps || [];
    const locked = apps.find(item => item && item.id === this.state.lockedAppId);
    if (locked?.name) return locked.name;
    const selected = apps[this.state.selectedApp];
    return selected?.name || '';
  }

  pressKeys(keys) {
    if (!Array.isArray(keys) || !keys.length) return false;
    // 附带目标应用名：后端在注入前把该应用置前，保证 SendInput 的按键
    // 落进目标窗口（否则用户点回手机浏览器后按键会打进别的窗口）。
    const app = this.targetAppName();
    this.connection.send('key.press', app ? { keys, app } : { keys });
    return true;
  }

  bindDirection(button) {
    const key = DIRECTION_KEYS[button.dataset.direction];
    if (!key) return;
    // 上 / 下 = 上一个 / 下一个视频，左 / 右 = 快退 / 快进：全部按物理
    // 方向键，不再依赖 isLocked、不再切应用、不再用系统虚拟媒体键。
    const send = () => this.pressKeys([key]);
    this.bindPress(button, send, { repeat: ['left', 'right'].includes(button.dataset.direction) });
  }

  bindRepeat(button, send) { this.bindPress(button, send, { repeat: true }); }

  bindPress(button, send, { repeat = false } = {}) {
    let timer = 0;
    let interval = 0;
    const clear = () => {
      clearTimeout(timer);
      clearInterval(interval);
      button.classList.remove('pressed');
      if (this.activeHold === button) this.activeHold = null;
    };
    button.addEventListener('pointerdown', event => {
      if (event.button !== 0 || this.activeHold) return;
      event.preventDefault();
      try { button.setPointerCapture(event.pointerId); } catch { /* 退化：up/cancel 仍在元素上监听 */ }
      button.classList.add('pressed');
      this.activeHold = button;
      send();
      vibrate();
      if (repeat) timer = setTimeout(() => {
        interval = setInterval(send, 120);
      }, 350);
    });
    button.addEventListener('pointerup', clear);
    button.addEventListener('pointercancel', clear);
    button.addEventListener('lostpointercapture', clear);
    button.addEventListener('click', event => {
      // 键盘和辅助技术仍可获得与轻点相同的离散命令。
      if (event.detail === 0) send();
    });
  }

  bindPlay(button) {
    const ring = button.querySelector('#play-ring-fill');
    let holdTimer = 0;
    let ringTimer = 0;
    let frame = 0;
    let startedAt = 0;
    let held = false;
    const reset = () => {
      clearTimeout(holdTimer);
      clearTimeout(ringTimer);
      cancelAnimationFrame(frame);
      button.classList.remove('holding', 'pressed');
      ring.style.strokeDashoffset = '351.858';
      if (this.activeHold === button) this.activeHold = null;
    };
    const tick = () => {
      const progress = Math.min(1, (performance.now() - startedAt) / this.state.touchpad.lock_hold_ms);
      ring.style.strokeDashoffset = String(351.858 * (1 - progress));
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    button.addEventListener('pointerdown', event => {
      if (event.button !== 0 || this.activeHold) return;
      event.preventDefault();
      try { button.setPointerCapture(event.pointerId); } catch { /* 退化 */ }
      button.classList.add('holding');
      this.activeHold = button;
      held = false;
      startedAt = performance.now();
      frame = requestAnimationFrame(tick);
      // 长按 2 秒仍锁定 / 解锁当前应用（app.activate / app.release）。
      holdTimer = setTimeout(() => {
        held = true;
        vibrate(20);
        const app = this.state.selectedAppData;
        if (this.state.isLocked) {
          this.connection.send('app.release', {});
          this.state.setLocked(null);
        } else if (app) {
          this.connection.send('app.activate', { id: app.id });
          this.state.setLocked(app.id);
        }
        reset();
      }, this.state.touchpad.lock_hold_ms);
    });
    button.addEventListener('pointerup', () => {
      if (!held && this.activeHold === button) {
        // 短按 = 敲一次物理空格键（抖音 / 网页播放器播放或暂停）。
        this.pressKeys(['space']);
        this.state.togglePlay();
      }
      reset();
    });
    button.addEventListener('pointercancel', reset);
    button.addEventListener('lostpointercapture', reset);
    button.addEventListener('click', event => {
      if (event.detail === 0) {
        this.pressKeys(['space']);
        this.state.togglePlay();
      }
    });
  }

  bindProgress() {
    const track = document.querySelector('#progress-track');
    if (!track) return;
    let dragging = false;
    let raf = 0;
    let pending = null;
    let lastSentAt = 0;
    // 后端现在有 SMTC 真实通道（try_change_playback_position_async），
    // 直接把 0..1 目标位置发过去就能跳到位，不再靠方向键次数估算。
    // 拖动时按 ~80ms 节流发送，避免一次拖动发上百条 WS 消息。
    const flush = () => {
      raf = 0;
      if (pending === null) return;
      const position = pending;
      pending = null;
      lastSentAt = performance.now();
      this.connection.send('media.seek', { position });
    };
    const queue = position => {
      pending = position;
      const elapsed = performance.now() - lastSentAt;
      if (elapsed >= 80) {
        if (raf) cancelAnimationFrame(raf);
        flush();
      } else if (!raf) {
        raf = requestAnimationFrame(() => setTimeout(flush, 80 - elapsed));
      }
    };
    const seek = event => {
      const rect = track.getBoundingClientRect();
      if (!rect.width) return;
      const target = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      // 本地即时反馈：进度条跟手，服务端回执后再由 media 状态校正。
      this.state.setMedia({ position: target });
      queue(target);
    };
    track.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      // 某些浏览器对合成事件或不活跃 pointerId 会抛 NotFoundError，捕获失败时退化为普通拖动。
      try { track.setPointerCapture(event.pointerId); } catch { /* 退化 */ }
      dragging = true;
      // 拖动期间暂停「每秒 media.state 拉真实进度」，否则服务端回包会把
      // 手指刚拖到的位置拽回播放器当前进度。
      document.body.dataset.seeking = '1';
      seek(event);
      const move = e => { if (dragging) seek(e); };
      const up = () => {
        dragging = false;
        delete document.body.dataset.seeking;
        if (raf) cancelAnimationFrame(raf);
        flush();
        track.removeEventListener('pointermove', move);
        track.removeEventListener('pointerup', up);
        track.removeEventListener('pointercancel', up);
      };
      track.addEventListener('pointermove', move);
      track.addEventListener('pointerup', up);
      track.addEventListener('pointercancel', up);
    });
  }

  renderSlots() {
    this.root.querySelectorAll('.page-slot').forEach(button => {
      const slot = ['A', 'B', 'C'][Number(button.dataset.pageSlot)];
      const hotkey = this.findHotkey(slot);
      button.classList.toggle('has-page', Boolean(hotkey));
      button.querySelector('span').textContent = hotkey?.name || slot;
      button.querySelector('small').textContent = hotkey ? `${slot} · ${hotkey.keys.join(' + ')}` : '';
      button.setAttribute('aria-label', hotkey ? `执行${hotkey.name}` : `未配置快捷键 ${slot}`);
    });
  }
}
