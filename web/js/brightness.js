export class BrightnessControl {
  constructor(state, connection, { onToast }) {
    this.state = state;
    this.connection = connection;
    this.onToast = onToast;
    this.track = document.querySelector('#brightness-track');
    this.fill = document.querySelector('#brightness-fill');
    this.head = document.querySelector('#brightness-head');
    this.value = 1;
    this.pending = false;
    this.bind();
  }

  bind() {
    if (!this.track) return;
    const send = value => {
      this.value = Math.max(0, Math.min(1, value));
      this.render();
      if (!this.pending) {
        this.pending = true;
        requestAnimationFrame(() => {
          this.pending = false;
          this.connection.send('brightness.set', { value: this.value });
        });
      }
    };
    const seek = event => {
      const rect = this.track.getBoundingClientRect();
      send((event.clientX - rect.left) / rect.width);
    };
    this.track.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      try { this.track.setPointerCapture(event.pointerId); } catch { /* 合成事件或失效指针时退化为普通拖动 */ }
      seek(event);
      const move = e => seek(e);
      const up = () => {
        this.track.removeEventListener('pointermove', move);
        this.track.removeEventListener('pointerup', up);
        this.track.removeEventListener('pointercancel', up);
      };
      this.track.addEventListener('pointermove', move);
      this.track.addEventListener('pointerup', up);
      this.track.addEventListener('pointercancel', up);
    });
  }

  setValue(value) {
    this.value = Math.max(0, Math.min(1, value));
    this.render();
  }

  render() {
    const pct = `${this.value * 100}%`;
    if (this.fill) this.fill.style.width = pct;
    if (this.head) this.head.style.left = pct;
    if (this.track) this.track.setAttribute('aria-valuenow', String(Math.round(this.value * 100)));
    // 右侧数字：让用户一眼看出亮度到底动没动（后端可能降级到 gamma，
    // 视觉变化很小，纯看屏幕容易误以为「亮度调节不行」）。
    const label = document.querySelector('#brightness-value');
    if (label) label.textContent = `${Math.round(this.value * 100)}%`;
  }
}
