# 通信契约（冻结版 v1.0）

> Lead 冻结，两个子 Agent **必须逐字段遵守**。任何一端不得改名、改类型、改大小写。若有必须新增的字段，只能追加，不得删除或改义，并在各自 `README.md` 里注明。汇总时 Lead 以本文件核对。

## 1. 传输层

| 项 | 值 |
|---|---|
| 端口 | `8765`（可用 `--port` 覆盖；前端从 `location.port` 推导，不硬编码） |
| 静态根 | `GET /` → `web/index.html`；`GET /<file>` → `web/<file>` |
| REST 前缀 | `/api/` |
| WebSocket | `ws://<host>:<port>/ws`（HTTPS 页面时 `wss`，本地 LAN 用 ws） |
| 编码 | UTF-8 JSON；所有文本字段 `Content-Type: application/json; charset=utf-8` |
| 跨域 | 同源部署，无需 CORS；后端仍允许 `Access-Control-Allow-Origin: *` 便于本机调试 |

## 2. 通用消息信封

客户端 → 服务端（WS）与服务端 → 客户端（WS）共用：

```json
{
  "type": "mouse.move",
  "payload": { "dx": 12, "dy": -4 },
  "ts": 1731000000000,
  "id": "c-17"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 命令名，见第 3 / 4 节，全小写点分命名 |
| `payload` | object | 是 | 各命令参数，无参数时传 `{}` |
| `ts` | number | 是 | 客户端毫秒时间戳（用于延迟显示与去重） |
| `id` | string | 否 | 客户端自增序号，服务端 `ack` 原样带回 |

服务端对每条命令回 `ack`（`mouse.move` / `mouse.scroll` 等高频命令**不逐条回 ack**，避免放大流量；仅低频命令回）：

```json
{ "type": "ack", "payload": { "id": "c-17", "ok": true }, "ts": 1731000000000 }
```

出错统一回：

```json
{ "type": "error", "payload": { "id": "c-17", "code": "BAD_PAYLOAD", "message": "dx must be number" }, "ts": 1731000000000 }
```

## 3. 客户端 → 服务端命令表

### 3.1 连接与心跳
| type | payload | 说明 |
|---|---|---|
| `hello` | `{"client":"iphone-safari","version":"1.0"}` | 连接后首条，服务端回 `hello` |
| `ping` | `{}` | 每 5000ms 一次，服务端回 `pong` 并带上 `ts` 回显 |
| `status.get` | `{}` | 请求立即回一次 `status` |

### 3.2 鼠标 / 触控板
| type | payload | 说明 |
|---|---|---|
| `mouse.move` | `{"dx": number, "dy": number}` | **相对增量**（像素），服务端 `MOUSEEVENTF_MOVE` 累加；前端合批后发 |
| `mouse.down` | `{"button": "left"\|"right"\|"middle"}` | 拖拽开始 |
| `mouse.up` | `{"button": "left"\|"right"\|"middle"}` | 拖拽结束 |
| `mouse.click` | `{"button": "left"\|"right"\|"middle", "clicks": 1\|2}` | 轻点 / 双击 / 右键 |
| `mouse.scroll` | `{"dx": number, "dy": number}` | 双指滚动，`dy>0` 向上滚；服务端按 120 单位为格换算 |
| `mouse.pinch` | `{"scale": number}` | 捏合比例（>1 放大），服务端映射 Ctrl+滚轮；可选 |
| `window.switch` | `{"direction": "prev"\|"next"}` | 三指滑动；`next`→Alt+Tab，`prev`→Alt+Shift+Tab；可选 |

### 3.3 键盘
| type | payload | 说明 |
|---|---|---|
| `key.press` | `{"keys": ["a"]}` 或 `{"keys": ["ctrl","c"]}` | 单键或组合键；键名见第 5 节 |
| `key.type` | `{"text": "hello 世界"}` | 直接输入文本（Unicode 用 `KEYEVENTF_UNICODE`） |
| `hotkey.run` | `{"id": "hk-3"}` | 执行设置里保存的快捷键；服务端查 `config.json` 后转 `key.press` |

### 3.4 媒体
| type | payload | 说明 |
|---|---|---|
| `media.control` | `{"action": "play_pause"\|"next"\|"prev"\|"seek_forward"\|"seek_back"\|"volume_up"\|"volume_down"\|"mute"}` | 系统虚拟媒体键；长按快进/快退由前端重复发 `seek_forward`/`seek_back` |
| `media.seek` | `{"position": 0.0}` | 进度条 0..1；服务端尽力映射方向键次数，返回 `ack` 说明 `best_effort` |
| `media.state` | `{}` | 请求当前播放状态，服务端回 `media` |

### 3.5 应用
| type | payload | 说明 |
|---|---|---|
| `app.list` | `{}` | 请求应用列表，服务端回 `app.list` |
| `app.scan` | `{}` | 触发重新扫描已安装应用，完成后回 `app.list` |
| `app.activate` | `{"id": "app-7"}` | 锁定：启动/置前并聚焦该应用，回 `status` 带 `locked_app_id` |
| `app.release` | `{}` | 解锁：清除锁定，回 `status` |

### 3.6 配置（WS 与 REST 等效，推荐 REST）
| type | payload | 说明 |
|---|---|---|
| `config.get` | `{}` | 回 `config` |
| `config.put` | `{"config": { ...第 6 节结构... }}` | 全量覆盖保存，回 `ack` + `config` |

## 4. 服务端 → 客户端消息表

| type | payload | 触发 |
|---|---|---|
| `hello` | `{"device":"<计算机名>","version":"1.0","lan_ip":"192.168.x.x","port":8765}` | WS 建连后立刻下发 |
| `pong` | `{"echo_ts": <ping 的 ts>}` | 收到 `ping` |
| `status` | `{"connected": true,"latency_ms": 12,"locked_app_id": null\|"app-7","active_app": "TikTok","service_up": true}` | 建连、锁定变化、每 10s 一次 |
| `ack` | `{"id","ok":true,"extra":{}}` | 低频命令完成 |
| `error` | `{"id","code","message"}` | 参数或执行错误 |
| `app.list` | `{"apps":[{"id":"app-7","name":"TikTok","path":"C:\\...","icon":"🎵","color":"#000000"}]}` | `app.list` / `app.scan` |
| `config` | `{"config": { ... }}` | `config.get` / `config.put` 后 |
| `media` | `{"playing": true\|false,"title": "","position": 0.0,"best_effort": true}` | `media.state`；无法读取时 `best_effort:true` 且字段为默认 |
| `pong` | 见上 | 心跳 |

**前端连接状态机**（`net.js` 必须实现）：
`connecting` → `connected`（收到 `hello`）→ `disconnected`（WS close / 心跳 2 次无 `pong` / REST 连续失败）→ 指数退避自动重连（1s→2s→4s→8s，上限 8s）。状态栏按 `status.latency_ms` 显示「12ms」，断开时显示「未连接 / 已断开」并置灰控制区、弹出遮罩。**断开后所有手势只本地缓冲不报错**。

## 5. 键名表（`key.press.keys[]` 取值）

小写字母 `a`–`z`、数字 `0`–`9`；功能键：

```
enter, esc, tab, space, backspace, delete, home, end, pageup, pagedown,
up, down, left, right, f1..f12,
ctrl, shift, alt, win,
capslock, insert, printscreen,
`, -, =, [, ], \, ;, ', ,, ., /
```

修饰键写在数组前面，例如 `Ctrl+C` → `["ctrl","c"]`；`Alt+Tab` → `["alt","tab"]`；`Win+D` → `["win","d"]`。服务端负责按下顺序与逆序释放。未知键名回 `error {code:"UNKNOWN_KEY"}`。

## 6. 配置结构（`config.json`，REST `/api/config` 与 WS `config` 同源）

```json
{
  "version": 1,
  "pages": [
    { "id": "pg-1", "name": "常用", "icon": "⭐" },
    { "id": "pg-2", "name": "办公", "icon": "💼" },
    { "id": "pg-3", "name": "娱乐", "icon": "🎮" }
  ],
  "active_page_id": "pg-1",
  "hotkeys": [
    { "id": "hk-1", "name": "复制", "icon": "📋", "keys": ["ctrl","c"], "page_id": "pg-1", "slot": "A" },
    { "id": "hk-2", "name": "粘贴", "icon": "📄", "keys": ["ctrl","v"], "page_id": "pg-1", "slot": "B" },
    { "id": "hk-3", "name": "显示桌面", "icon": "🖥", "keys": ["win","d"], "page_id": "pg-1", "slot": "C" }
  ],
  "apps": [
    { "id": "app-1", "name": "文件资源管理器", "path": "explorer.exe", "icon": "📁", "color": "#1E5EFF" },
    { "id": "app-2", "name": "YouTube",       "path": "https://youtube.com", "icon": "▶",  "color": "#FF0033" },
    { "id": "app-3", "name": "TikTok",        "path": "https://tiktok.com",  "icon": "🎵", "color": "#111118" },
    { "id": "app-4", "name": "Spotify",       "path": "spotify.exe",         "icon": "🎧", "color": "#1DB954" },
    { "id": "app-5", "name": "Steam",         "path": "steam.exe",           "icon": "🎮", "color": "#1B2838" }
  ],
  "touchpad": { "move_sensitivity": 1.6, "scroll_speed": 0.6, "double_click_ms": 300, "drag_hold_ms": 350, "lock_hold_ms": 2000 }
}
```

- `hotkeys[].slot` 取 `A` / `B` / `C`，对应应用页三个快捷键按钮；同一 `page_id` 内每个 slot 唯一。
- `apps` 初始按参考图给 5 张卡；设置页可从 `/api/apps` 追加，追加项 `id` 用 `app-<n>` 递增。
- `touchpad` 里的参数前端读取后用于第 7 节手势判定，实现「可调灵敏度」。
- `PUT /api/config` 请求体就是上面的完整对象（**不是**包一层 `config`）；WS `config.put` 才包一层。两者服务端都要接受并存同一文件。原子写：先写 `config.json.tmp` 再 `os.replace`。

## 7. REST 端点明细

| 方法 | 路径 | 请求体 | 响应 |
|---|---|---|---|
| `GET` | `/` | — | `web/index.html` |
| `GET` | `/<file>` | — | `web/` 下静态文件，含正确 MIME（`.js`→`text/javascript`、`.css`→`text/css`） |
| `GET` | `/api/status` | — | `{"connected":true,"device":"...","lan_ip":"...","port":8765,"version":"1.0","uptime_ms":123,"clients":1}` |
| `GET` | `/api/qr.png` | — | `image/png`，编码 `http://<LAN_IP>:<PORT>/` |
| `GET` | `/api/apps` | — | `{"apps":[...]}`（含已安装应用 + `config.apps` 合并去重） |
| `POST` | `/api/apps/scan` | — | `{"apps":[...]}` 重新扫描后的结果 |
| `GET` | `/api/config` | — | 第 6 节完整对象 |
| `PUT` | `/api/config` | 第 6 节完整对象 | `{"ok":true,"config":{...}}` |
| `GET` | `/api/media` | — | `{"playing":false,"title":"","position":0.0,"best_effort":true}` |

错误统一：`{"ok":false,"code":"...","message":"..."}`，HTTP 状态 400 / 404 / 500 对应语义。

## 8. 高频通道约定（低延迟关键）

- `mouse.move` / `mouse.scroll` 前端用 `requestAnimationFrame` 合批，每帧最多 1 条；同一帧内多指位移先累加再发。
- 服务端对这两类命令**不回 ack、不做 JSON 序列化日志**，直接进入 `SendInput`。
- 服务端 `SendInput` 用相对 `MOUSEEVENTF_MOVE`，不调用 `SetCursorPos`（避免多显示器/坐标跳变与权限问题）。
- 心跳与状态广播走独立低频通道，不与移动消息抢队列；WS 发送使用 `send_json` 且 `drain` 超时 0.5s 即丢弃该客户端的积压状态消息。

## 9. 联调自检脚本（后端提供 `server/selftest.py`）

按顺序执行并打印结果：连接 WS → 收 `hello` → 发 `ping` 验 `pong` → `GET /api/status` → `GET /api/config` → `PUT /api/config` 回读一致 → `key.press ["a"]` → `media.control play_pause` → `mouse.move {dx:20,dy:0}` → `app.list` → `hotkey.run`。前端无需该文件，但 Lead 汇总时会看它的存在与输出格式。
