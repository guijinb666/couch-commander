/** 协议唯一出口：所有 WebSocket 消息在这里封装为统一信封。 */
export class RemoteConnection {
  constructor({ onMessage, onStatus, onLatency }) {
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.onLatency = onLatency;
    this.ws = null;
    this.counter = 0;
    this.timer = 0;
    this.reconnectTimer = 0;
    this.retries = 0;
    this.missedPings = 0;
    this.restFailures = 0;
    this.closedByUser = false;
    this.status = 'idle';
    this.lastMessageAt = 0;
    // file:// 直接打开、或显式带 ?demo=1 时进入演示模式：不发网络请求，只把信封写进控制台。
    this.offline = location.protocol === 'file:' || new URLSearchParams(location.search).has('demo');
    // 系统网络恢复 / 从 bfcache 恢复时主动重连，避免「断网一次后一直显示断连」。
    window.addEventListener('online', () => {
      if (!this.offline && !this.closedByUser) this.reconnect();
    });
    window.addEventListener('pageshow', event => {
      if (event.persisted && !this.offline && this.ws?.readyState !== WebSocket.OPEN) this.reconnect();
    });
  }

  connect({ manual = false } = {}) {
    clearTimeout(this.reconnectTimer);
    if (manual) this.closedByUser = false;
    if (this.offline) {
      this.setStatus('demo', '演示模式 · 消息仅写入控制台');
      return;
    }
    if (this.ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(this.ws.readyState)) {
      // 底层连接其实还在：把状态纠正回来。旧实现在这里直接 return，
      // 一旦 status 被误判成 disconnected（见 noteRestFailure），就再也不会
      // 自动恢复 —— 界面永远显示「连接已断开」，而控制其实全部可用。
      if (this.ws.readyState === WebSocket.OPEN && this.status !== 'connected') {
        this.setStatus('connected');
      }
      return;
    }
    this.setStatus('connecting', this.closedByUser ? '已断开' : '连接中…');
    let socket;
    try {
      const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new WebSocket(`${scheme}//${location.host || 'localhost:8765'}/ws`);
      this.ws = socket;
    } catch (error) {
      console.warn('[remote] WebSocket 初始化失败', error);
      this.disconnected();
      return;
    }
    socket.addEventListener('open', () => {
      if (socket !== this.ws) return;
      this.retries = 0;
      this.missedPings = 0;
      this.restFailures = 0;
      this.lastMessageAt = Date.now();
      this.setStatus('connected');
      this.send('hello', { client: 'iphone-safari', version: '1.0' });
      clearInterval(this.timer);
      this.timer = setInterval(() => {
        if (socket.readyState !== WebSocket.OPEN) return;
        // ping 仅用于延迟显示与保活；前端绝不主动关闭 WebSocket。
        // iPhone Safari 锁屏 / 切后台会冻结 setInterval，ping 发不出来，
        // 任何「按时间未收到消息就 close」的兜底都会误杀健康连接：
        // 断线只交给浏览器 / 服务端的真实 close 事件，回到前台由
        // online / pageshow / visibilitychange 触发重连。
        this.send('ping', { echo_ts: Date.now() });
      }, 5000);
    });
    socket.addEventListener('message', event => {
      if (socket !== this.ws) return;
      let message;
      try { message = JSON.parse(event.data); } catch (error) {
        console.warn('[remote] 忽略非 JSON 消息', error);
        return;
      }
      if (!message || typeof message.type !== 'string') return;
      // 任意服务端消息都证明链路存活（status 每 10s 广播一次）：刷新健康时间戳。
      this.missedPings = 0;
      this.lastMessageAt = Date.now();
      // 能在 OPEN 的连接上收到消息，就是最强的「已连接」证据。把它写回状态，
      // 任何被误判成断连的情况都会在下一帧自愈（服务端 status 每 10s 一次）。
      if (this.status !== 'connected' && !this.offline) this.setStatus('connected');
      if (message.type === 'pong') {
        if (typeof message.payload?.echo_ts === 'number') this.onLatency?.(Math.max(0, Date.now() - message.payload.echo_ts));
      }
      this.onMessage?.(message);
    });
    socket.addEventListener('close', () => {
      if (socket !== this.ws) return;
      this.disconnected();
    });
    socket.addEventListener('error', () => {
      // 由 close 事件统一进入重连，避免重复计时。
    });
  }

  /**
   * 回到前台时调用：验证「看起来还连着」的连接是否真的活着。
   *
   * 手机切后台（尤其 iOS Safari、部分安卓的省电策略）会把页面定时器冻结，
   * 同时底层 TCP 可能已在 Wi-Fi 侧悄然失效，而浏览器既不触发 `close`
   * 也不把 `readyState` 改掉 —— 于是回到前台时状态仍是 connected，
   * 用户点任何按钮都没有反应，观感就是「后台连着连着突然断了」。
   *
   * 判定依据只能是**有没有真的收到过服务端消息**（服务端每 10s 广播一次
   * status，所以健康连接的空闲时间不会超过十几秒）：
   *   * 超过 22s 没收到任何帧 → 判定僵死，直接重建；
   *   * 否则补发一次 ping，4s 内仍无回音再重建。
   */
  resume() {
    if (this.offline || this.closedByUser) return;
    const socket = this.ws;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      this.reconnect();
      return;
    }
    const idleMs = this.lastMessageAt ? Date.now() - this.lastMessageAt : Infinity;
    if (idleMs > 22000) {
      this.reconnect();
      return;
    }
    // 看起来还活着：主动探一次，给 4s 窗口等任意回帧。
    this.send('ping', { echo_ts: Date.now() });
    clearTimeout(this.livenessTimer);
    this.livenessTimer = setTimeout(() => {
      const idle = this.lastMessageAt ? Date.now() - this.lastMessageAt : Infinity;
      if (idle > 4000) this.reconnect();
    }, 4000);
  }

  disconnected() {
    clearInterval(this.timer);
    this.setStatus(this.offline ? 'demo' : 'disconnected');
    if (!this.offline && !this.closedByUser) this.scheduleReconnect();
  }

  scheduleReconnect() {
    clearTimeout(this.reconnectTimer);
    // 退避上限压到 4s：手机上「断了一下就再也连不回来」的观感，很大一部分
    // 来自 8s 上限叠加 Safari 后台定时器节流（后台的 setTimeout 可能被推迟
    // 到几十秒），用户以为程序坏了。
    const delay = Math.min(4000, 600 * (2 ** this.retries));
    this.retries += 1;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  reconnect() {
    this.closedByUser = false;
    clearTimeout(this.reconnectTimer);
    clearInterval(this.timer);
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.retries = 0;
    this.connect({ manual: true });
  }

  send(type, payload = {}) {
    const envelope = { type, payload, ts: Date.now(), id: `c-${++this.counter}` };
    console.log('[remote →]', JSON.stringify(envelope));
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(envelope));
      return true;
    }
    return false;
  }

  setStatus(status, detail = '') {    this.status = status;
    this.onStatus?.(status, detail);
  }

  noteRestFailure() {
    this.restFailures += 1;
    // REST 只是辅助通道（读写配置、扫描应用），控制链路是 WebSocket。
    // 单个 REST 请求失败（手机弱网、Safari 后台限流、服务端某个端点慢）
    // 绝不能据此判定「连接已断开」：那会让人看到断连提示，而实际所有按钮
    // 都还能用。只有 WebSocket 自己断了才算断连。
    if (this.ws?.readyState === WebSocket.OPEN) {
      // 链路没断：清掉计数，避免下一次偶发失败继续累积。
      this.restFailures = 0;
      return;
    }
    // WS 也不在 OPEN：可能正处于 connecting，等它自己的结果；只有彻底没有
    // 可用 socket 时才进入断连。
    if (!this.ws || this.ws.readyState === WebSocket.CLOSED) this.disconnected();
  }

  async request(path, options = {}) {
    if (this.offline) throw new Error('当前为离线演示，未发起网络请求');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    let gotResponse = false;
    try {
      const response = await fetch(path, { ...options, signal: controller.signal, headers: { 'Content-Type': 'application/json', ...options.headers } });
      gotResponse = true;
      // 收到任何 HTTP 响应即证明链路可达：重置计数。HTTP 4xx/5xx 是业务响应，
      // 不该被当成「断连」（例如 GET /api/apps/icon 按契约就会返回 404 NO_ICON）。
      this.restFailures = 0;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const contentType = response.headers.get('content-type') || '';
      return contentType.includes('application/json') ? response.json() : response.text();
    } catch (error) {
      // 只有网络层失败（断网 / DNS / 超时 abort）才计入断连判定。
      if (!gotResponse) this.noteRestFailure();
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  saveConfig(config) {
    if (this.ws?.readyState === WebSocket.OPEN) this.send('config.put', { config });
    // REST 端点接收完整对象本身；只有 WS config.put 才包一层 config。
    return this.request('/api/config', { method: 'PUT', body: JSON.stringify(config) });
  }

  addApp(app) {
    return this.request('/api/apps', { method: 'POST', body: JSON.stringify(app) });
  }

  getApps() {
    return this.request('/api/apps');
  }

  getState() {
    return this.request('/api/state');
  }
}
