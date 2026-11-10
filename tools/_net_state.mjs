// 直接对 web/js/net.js 做单元级验证：用假的 WebSocket / location / fetch
// 驱动 RemoteConnection，复现「功能都好着却显示断连」的状态分叉。
//
// 关键疑点：net.js 的 noteRestFailure() 连续 2 次网络失败会调 disconnected()，
// 把 status 置为 'disconnected' 并安排重连；但 connect() 开头有一条
//   if (this.ws && [OPEN, CONNECTING].includes(this.ws.readyState)) return;
// 提前返回，不恢复 status。于是 WS 仍然 OPEN（send 全部照常工作），
// 界面却永远停在「连接已断开」。这正好对应 YG 的反馈。
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/+/, '')), '..');
const results = [];
const rec = (ok, name, detail) => {
  results.push({ ok, name, detail });
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`);
};

// ---- 假的浏览器环境 -------------------------------------------------
class FakeWS {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = FakeWS.CONNECTING;
    this.sent = [];
    this.listeners = {};
    FakeWS.instances.push(this);
    // 立即"连上"，模拟同一 Wi-Fi 下的正常握手
    setTimeout(() => {
      this.readyState = FakeWS.OPEN;
      this.emit('open');
    }, 0);
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, extra = {}) { for (const fn of this.listeners[type] || []) fn(extra); }
  send(data) { this.sent.push(data); }
  close() {
    this.readyState = FakeWS.CLOSED;
    this.emit('close');
  }
  // 从服务端"下发"一条消息
  deliver(obj) {
    this.emit('message', { data: JSON.stringify(obj) });
  }
}

let fetchShouldFail = 0;
globalThis.WebSocket = FakeWS;
globalThis.location = {
  protocol: 'https:',
  host: '127.0.0.1:8961',
  search: '',
  hash: '',
};
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.fetch = async () => {
  if (fetchShouldFail > 0) {
    fetchShouldFail -= 1;
    throw new TypeError('Failed to fetch');
  }
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ ok: true }),
    text: async () => 'ok',
  };
};

const modUrl = pathToFileURL(path.join(ROOT, 'web/js/net.js')).href;
const { RemoteConnection } = await import(modUrl);

const statuses = [];
const conn = new RemoteConnection({
  onMessage: () => {},
  onStatus: (status) => statuses.push(status),
  onLatency: () => {},
});
conn.connect();
await new Promise(r => setTimeout(r, 60));

const ws = FakeWS.instances.at(-1);
rec(conn.status === 'connected', '建连后状态为已连接', `status=${conn.status}`);
rec(ws?.readyState === FakeWS.OPEN, '底层 WebSocket 处于 OPEN', `readyState=${ws?.readyState}`);

// ---- 复现：两次 REST 网络层失败 ------------------------------------
fetchShouldFail = 2;
for (let i = 0; i < 2; i += 1) {
  try { await conn.request('/api/apps'); } catch { /* 预期失败 */ }
}
await new Promise(r => setTimeout(r, 50));

const statusAfter = conn.status;
rec(statusAfter === 'connected',
  '两次 REST 网络失败后，状态仍应为已连接（真实 WS 没断）',
  `status=${statusAfter}`);

// 关键证据：WS 有没有真的断？没有 —— 还能正常发消息
const beforeCount = ws.sent.length;
conn.send('ping', { echo_ts: Date.now() });
const sentOk = ws.sent.length > beforeCount;
rec(sentOk, '此时 send() 仍然成功（说明控制功能全部可用）', `sent=${ws.sent.length - beforeCount}`);

// 关键证据：底层 socket 仍 OPEN
rec(ws.readyState === FakeWS.OPEN, '底层 WebSocket 仍是 OPEN（根本没断）',
  `readyState=${ws.readyState}`);

// ---- 自愈检查：等 10 秒看会不会自己回到 connected -------------------
await new Promise(r => setTimeout(r, 10000));
const healed = conn.status === 'connected';
rec(healed, '不操作界面，10 秒内自动恢复为已连接', `status=${conn.status}`);

// ---- 服务端仍在发消息，页面是否认账 --------------------------------
ws.deliver({ type: 'status', payload: { connected: true, latency_ms: 12 }, ts: Date.now() });
await new Promise(r => setTimeout(r, 30));
rec(conn.status === 'connected', '收到服务端 status 后状态为已连接', `status=${conn.status}`);

const passed = results.filter(r => r.ok).length;
const failed = results.length - passed;
console.log(`\nNET_STATE_RESULT PASS=${passed} FAIL=${failed} TOTAL=${results.length}`);
console.log(`STATUS_TRAIL=${statuses.join(',')}`);
process.exit(0);
