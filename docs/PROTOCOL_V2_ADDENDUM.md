# 通信契约 V2 追加（协议扩展，不改义 V1）

> 状态：**冻结**。本文件是 `docs/PROTOCOL.md`（v1.0）的**追加层**，只做「新增 / 扩展」，
> **不删除、不改名、不改义** V1 任何命令、字段、端点、状态机、键名表。
> V1 的 22 条命令、8 条下发消息、10 条传输层端点、§6 顶层结构全部继续有效。
> 后端子 agent（写 `server/**`）与独立前端会话（写 `web/**`）**都以此文件为 V2 唯一权威**。
> 冲突时：V1 冻结项 > 本文件 V2 追加项。
>
> 触发来源：YG 实测反馈（m00566）+ 三平台键位表（m00581）+ muse 网址（m00594）。
> 分工：Lead 出方案与契约 → 后端交子 agent 直接改 `server/**` → 前端整理成
> `docs/FRONTEND_BRIEF_V2.md` 交 YG 带到另一会话，写完回传 Lead 总和。

---

## 0. 前后端职责矩阵（本轮全部需求逐条归属）

| # | 需求/问题 | 归属 | 落点 |
|---|-----------|------|------|
| 1 | 应用页按键崩盘、与参考图差距大、按键错位 | **纯前端** | `web/css/app-page.css`、`web/js/dpad.js`、`index.html` 布局重排 |
| 2 | 进度条调节不灵敏 | **纯前端** | `web/js/dpad.js` 改 pointerdown+setPointerCapture+pointermove 连续 seek（rAF 节流）+ 加大透明热区 |
| 3 | 键盘同一键连续单击只出一个字母 | **主前端 + 后端回归** | 前端 `web/js/keyboard.js` 改 pointerdown（iOS Safari 吞 click）；后端 `selftest.py` 加「同 `key.press` 连发 N 次」回归用例证明不吞键 |
| 4 | 快捷键默认显示 A/B/C、默认无设置 | **后端默认 + 前端渲染** | 后端 `config_store.default_config()` 的 `hotkeys` 由 3 个清空为 `[]`；前端 `dpad.js renderSlots()` 已 `hotkey?.name \|\| slot`，确认即可 |
| 5 | 设置快捷键可自由选键组合 + 提供模板 | **纯前端** | 前端设置页从 §5 键名表自由多选组装 `keys[]`；3 个模板内置前端；后端 `key.press`/`key_press` 已支持 `keys[]` 组合，零改动 |
| 6 | 三个模板（抖音/B站/腾讯视频，A弹幕/B清屏/C全屏） | **纯前端数据** | 键位见 §7；前端把模板转成 `config.hotkeys` 写入；后端 `hotkey.run{id}` 从 config 取 `keys` 执行，零改动 |
| 7 | 首页默认应用换 5 个国内应用 | **后端默认 + 前端 demo** | 后端 `default_config().apps`、前端 `state.js DEMO_APPS` 两处同步，见 §2 |
| 8 | 选中应用先检索本地软件、没有才开官网 | **纯后端** | `apps.py launch_app()` 双路径判定，见 §3 |
| 9 | 应用卡片显示电脑图标、新增卡片也显示、无图标显示首字 | **后端图标端点 + 前端渲染** | 后端新增 `GET /api/apps/icon`、`app.list` 带 `icon_url`；前端 `img` 失败/无 url 回退名称首字，见 §4 |
| 10 | 应用页/触控板之间加语音按钮，长按开 muse+语音、松手发送不退出 | **前端采集/手势 + 后端落地** | 前端按钮+长按+`getUserMedia`/识别；后端 `voice.*` 命令打开并聚焦 muse、送文本、不退出，见 §6 |
| 11 | 进度条下方加亮度滑条、两滑条加视频/音量小图标且居中 | **纯前端 UI + 后端亮度命令** | 前端滑条+图标布局；后端 `brightness.set/get`，见 §5 |

**一句话边界**：所有「看得见、摸得着」的布局/动效/手势/模板/采集在前端；所有「电脑真的去做」的启动应用/提图标/改亮度/开网页并送输入在后端。命令名字段以本文件为准，两侧不得私自改名。

---

## 1. 传输层（沿用 V1，仅登记新增端点）

- 端口、`/`→`web/index.html`、静态根、`WS /ws`、UTF-8 JSON、CORS `*`、信封 `{type,payload,ts,id}`、`ack`/`error` 结构、高频命令不逐条回 ack —— **全部沿用 `docs/PROTOCOL.md` §1/§2，不变**。
- V2 新增 1 条 REST 端点（见 §4）：`GET /api/apps/icon`。
- `selftest.py` 的 `PROTOCOL_ENDPOINTS`（冻结 10 条）**不动**；`EXTRA_ENDPOINTS` 由
  `("POST /api/apps",)` 扩为 `("POST /api/apps", "GET /api/apps/icon")`；
  `EXPECTED_ENDPOINTS` 同步按 `http_api.rest_endpoints()` 实际注册顺序追加（见 §8）。

---

## 2. `config.json` V2 结构改动（改 §6 默认值，不改义字段）

顶层 6 个键 `version / pages / active_page_id / hotkeys / apps / touchpad` **全部保留**；
`touchpad` 5 参数、`version:1`、`pages`(pg-1 常用⭐/pg-2 办公💼/pg-3 娱乐🎮)、`active_page_id:"pg-1"` **不变**。

### 2.1 `hotkeys` 默认清空（对应需求 4）

```json
"hotkeys": []
```

- 默认「无设置」，前端 A/B/C 槽显示字母 A/B/C 本身（不显示复制/粘贴/显示桌面）。
- 用户后续在设置页自由组合或从模板套用后，`hotkeys` 才被写入；结构仍是
  `{id,name,icon,keys,page_id,slot}`，`slot ∈ {A,B,C}` 且同 `page_id` 内唯一（V1 §6 规则不变）。

### 2.2 `apps` 默认 5 张换为国内应用（对应需求 7）

每项**保留** V1 字段 `id/name/path/icon/color`，**追加** `web`、`local`、`icon_url`（可选）：

```json
"apps": [
  {"id":"app-1","name":"抖音","path":"https://www.douyin.com/","web":"https://www.douyin.com/","local":{"names":["抖音","douyin","抖音电脑版"],"exe":["douyin.exe","Douyin.exe"],"paths":[]},"icon":"🎵","color":"#111118"},
  {"id":"app-2","name":"bilibili","path":"https://www.bilibili.com/","web":"https://www.bilibili.com/","local":{"names":["哔哩哔哩","bilibili","bilibili pc"],"exe":["bilibili.exe","bili.exe"],"paths":[]},"icon":"📺","color":"#FB7299"},
  {"id":"app-3","name":"QQ音乐","path":"https://y.qq.com/","web":"https://y.qq.com/","local":{"names":["QQ音乐","QQMusic","QQMusicPC"],"exe":["QQMusic.exe"],"paths":[]},"icon":"🎧","color":"#31C27C"},
  {"id":"app-4","name":"网易云音乐","path":"https://music.163.com/","web":"https://music.163.com/","local":{"names":["网易云音乐","NeteaseCloudMusic","cloudmusic"],"exe":["cloudmusic.exe"],"paths":[]},"icon":"🎶","color":"#C20C0C"},
  {"id":"app-5","name":"腾讯视频","path":"https://v.qq.com/","web":"https://v.qq.com/","local":{"names":["腾讯视频","QQLive","TencentVideo"],"exe":["QQLive.exe","TencentVideo.exe"],"paths":[]},"icon":"▶","color":"#FF5C00"}
]
```

字段语义（**冻结**）：

- `path`：V1 兼容字段，恒等于 `web`（官网 URL），保证 V1 `launch_app`/`merged` 不改义。
- `web`：官网 URL，`local` 未命中时 `webbrowser.open` 打开它。
- `local`：本地软件匹配描述，`{"names":[...],"exe":[...],"paths":[...]}`：
  - `names`：与 `AppStore.cached()` 已安装应用名做规范化模糊匹配（去空格/大小写/中英文别名）；
  - `exe`：`app.activate` 时按进程名在缓存里找目标，或作为常见安装候选；
  - `paths`：可选，额外的绝对安装路径候选（默认空数组，由扫描结果补齐）。
- `icon_url`：可选，`app.list` 下发时按本地命中结果填 `"/api/apps/icon?path=<urlencoded>"`；
  config 里不落盘该字段（运行期派生）。无本地命中时**不下发** `icon_url`。

> 颜色/emoji 只是首字兜底前的占位；真实图标由 §4 端点提供，前端优先用 `icon_url`。

### 2.3 新增可选顶层字段 `brightness`（记住上次亮度）

```json
"brightness": {"value": 1.0}
```

- 可选；`selftest.EXPECTED_DEFAULT_CONFIG` 若包含此键则逐字段比较，**两处必须同步**。
- 为避免 V1「6 顶层键」自检项被改写，**决定：不新增顶层键**，亮度上次值由后端内存保存（进程重启回 1.0），
  `config.json` 顶层键严格保持 6 个不变。这样 V1 §6 与 `selftest` 的「6 顶层字段」检查不破。

> 结论：`config.json` 顶层键数量与键名**完全不变**；只改 `hotkeys` 默认值与 `apps` 内容（apps 项追加 `web/local` 两字段）。

---

## 3. `app.activate` 语义扩展：双路径启动（对应需求 8）

V1 `app.activate{id}` → 回 `status(locked_app_id)` + `ack`。**命令名/参数/extra 结构不变**，
只把 `apps.py launch_app()` 的启动判定升级为「本地优先、官网兜底」：

```
launch_app(app):
  1. 若 app.local 存在 → locate_local(app):
       a. 在 AppStore.cached() 已安装列表里按 local.names 规范化模糊匹配 → 命中取该项 path
       b. 未命中则按 local.exe / local.paths 探测常见安装位置（Program Files / AppData / 开始菜单 .lnk 目标）
       c. 命中 → 启动本地（复用 V1 的 .lnk / 绝对路径 / 裸命令四类分支），method="local"
  2. 未命中任何本地软件 → webbrowser.open(app.web or app.path)，method="web"
  3. 一律不抛异常；可选依赖缺失降级 best_effort
```

`ack.payload.extra`（V1 `launch_app` 已返回该 dict）**追加/明确**以下字段，不删原有：

```json
{"id":"app-1","name":"抖音","launched":true,"focused":false,
 "method":"local","matched_path":"C:\\...\\douyin.exe","web_fallback":false,"best_effort":true}
```

- `method`: `"local" | "web" | "webbrowser.open" | "os.startfile(lnk)" | "subprocess.Popen" | "shell" | ""`；
  双路径判定结果用 `"local"`/`"web"` 覆盖标注到额外字段 `route`（`"local"|"web"`），保留 `method` 原义。
- 前端可据 `route` 显示「已打开本地客户端 / 已打开官网」。
- `AppStore.merged()` 继续把 `config.apps` 置前、去重；新增字段 `web/local` 由 `merged` 的
  「保留前端可能追加的其它字段」逻辑原样透传（V1 `apps.py:444-447` 已支持，无需改）。

---

## 4. 应用图标端点（对应需求 9）

### 4.1 新增 REST 端点

```
GET /api/apps/icon?path=<urlencoded 本地路径或 .lnk>
```

- 成功：`200`，`Content-Type: image/png`，body 为提取到的 **32×32 PNG 图标**。
- 无图标 / 路径不存在 / 提取失败：`404`，`{"ok":false,"code":"NO_ICON","message":"..."}`（V1 §7 错误结构）。
- 实现：优先 `win32gui.ExtractIconEx` / `win32api` 或 `PIL.Image` 从 `.exe`/`.lnk` 提 `HICON` →
  `GetIconInfo`+`GetDIBits` 转 RGBA → PNG；可选依赖（pywin32/`PIL`）任一缺失或调用失败 → 404，
  **绝不 500、绝不崩服务**。所有第三方调用包 try/except。
- 供前端 `<img src="/api/apps/icon?path=...">` 按需加载；`icon_url` 由 `app.list`/`/api/apps` 派生。

### 4.2 `app.list` / `GET /api/apps` 每项追加可选字段

V1 `{id,name,path,icon,color}` 全部保留，追加：

```json
{"id":"app-1","name":"抖音","path":"https://www.douyin.com/","icon":"🎵","color":"#111118",
 "web":"https://www.douyin.com/","local":{...},"route":"local",
 "icon_url":"/api/apps/icon?path=C%3A%5C...%5Ddouyin.exe","has_local":true}
```

- `icon_url`：仅 `has_local:true` 且本地路径可提图标时下发；否则省略。
- `has_local`：本地是否命中（供前端决定是否显示真实图标）。
- `route`：本地命中 `"local"`，否则 `"web"`。
- 前端渲染规则（写进 `FRONTEND_BRIEF_V2.md`）：有 `icon_url` → `<img>` 显示真实图标；
  `img` `onerror` 或无 `icon_url` → 回退**名称第一个字**（如「抖音」→「抖」，「bilibili」→「b」）。
  设置页「添加到应用页」后新增卡片同样走这套渲染（`web/js/settings.js` 的 `addApp` 结果也应带 `icon_url`，
  前端按 id 回填或重新 `app.list` 拉取）。

---

## 5. 屏幕亮度命令（对应需求 11）

### 5.1 新增 WS 命令

```
brightness.set  payload {"value": 0..1}   → ack，extra {"applied":true,"method":"wmi"|"gamma","value":0.6,"best_effort":true}
brightness.get  payload {}                → 下发消息 "brightness" {"value":0.6,"method":"wmi"|"gamma","best_effort":true}
```

- `value` 越界或非法 → `error{code:"BAD_VALUE",message}`。
- 实现优先级：
  1. **WMI** `root\WMI` → `WmiMonitorBrightnessMethods.WmiSetBrightness(Timeout=1, Brightness=<0..100>)`
     （笔记本内置屏真实亮度，需 `wmi`/`win32com`；`method="wmi"`）。
  2. WMI 不可用（台式显示器 / 依赖缺失）→ **`SetDeviceGammaRamp`**（软件 gamma 近似，`method="gamma"`，
     `best_effort:true`）。gamma 只改色阶不改背光，前端/文档须如实标注。
  3. 两者都失败 → `ack` 带 `applied:false,best_effort:true,message`，不抛异常、不崩。
- **高频约定**：`brightness.set` 视同 V1 §8 高频通道——前端 rAF 合批每帧最多 1 条，
  后端可不逐条回 ack（但回也无妨，前端不依赖）。绝不写 JSON 日志、绝不阻塞事件循环
  （WMI 调用放 `loop.run_in_executor`）。
- `brightness.get` 供前端初始化滑条位置（连接后可选调用，非必须）。

---

## 6. 语音命令（对应需求 10，muse = https://muse.ai/）

### 6.1 交互语义（前端手势 + 后端落地）

- 长按语音按钮：前端 `getUserMedia({audio:true})` 触发手机麦克风 + 语音识别（Web Speech API，
  Safari 用 `webkitSpeechRecognition`；不可用时降级 MediaRecorder 录音），并 `voice.start` 让后端打开/聚焦 muse。
- 识别过程中：前端可连续 `voice.text{text,final:false}` 预览（后端不逐条 ack，纯低频）。
- 松手：前端 `voice.text{text,final:true}`（或先 `voice.text` 再 `voice.stop`）→ 后端把文本送入 muse 输入框并触发发送。
- **muse 界面不退出**：后端任何 `voice.*` 都**不关闭**已打开的 muse 标签页/窗口；
  `voice.stop` 只结束本次「按住说话」会话，不导航、不关闭、不清空输入历史。

### 6.2 新增 WS 命令

```
voice.start  payload {"url":"https://muse.ai/"}      → ack，extra {"opened":true,"focused":true,"best_effort":true}
voice.text   payload {"text":"...","final":false}     → 不逐条 ack（final:false）；final:true 回 ack
voice.stop   payload {}                              → ack，extra {"sent":true,"best_effort":true}
```

- `voice.start`：`webbrowser.open(url)`（幂等——已开则聚焦，不重复开新窗口）；
  `focused` 表示是否成功把窗口置前。可选依赖缺失/无桌面会话降级 `best_effort`，不崩。
- `voice.text final:true`：后端聚焦 muse 窗口后，对当前焦点输入框先 `key.type{text}` 再
  `key.press{keys:["enter"]}` 提交（**复用 V1 输入注入，不新造键**）。
- `voice.stop`：结束会话并标记「已发送」；**不关闭 muse**。
- 如实限制（写 `server/README.md`）：Windows 无「把手机音频流注入 muse 麦克风」的虚拟音频设备能力，
  因此本实现走「手机端识别为文本 → 后端把文本送入 muse」的 best-effort 路径；
  若目标是「真实麦克风音频输入」需另装 VB-CABLE 等虚拟声卡并配合前端推流，属后续扩展，本版不承诺。
- `ack`/`error` 结构、`best_effort` 语义沿用 V1。

---

## 7. 三平台快捷键模板数据（**纯前端**，后端仅执行）

来源 YG 键位表（m00581），**PC 端网页/客户端**，手机端无键盘快捷键只能点按钮。
「清屏」= 隐藏界面控件（不是清弹幕）。

| 模板 id | 平台 | A 弹幕开关 | B 清屏 | C 全屏 | 退出全屏 |
|---------|------|-----------|--------|--------|----------|
| `tpl-douyin` | 抖音 | `[b]` | `[j]` | `[h]`（网页 H/F 均可，取 H 与 PC 一致） | `[esc]` |
| `tpl-bilibili` | bilibili | `[d]` | `[w]`（网页全屏/宽屏去控件，无独立清屏键） | `[f]` | `[esc]` |
| `tpl-tencent` | 腾讯视频 | —（官方无原生弹幕快捷键，仅鼠标点【弹】） | —（官方无原生清屏键） | `[f11]`（或双击画面） | `[esc]` |

- 腾讯视频模板 **A/B 如实留空**（前端显示「该平台无原生键，可自行录制/改用鼠标」），
  C=`[f11]`。**不得编造键位**。用户「自由组合」仍可给 A/B 手动录任意键。
- 模板套用 = 前端把上表转成 3 条 `hotkeys`（`slot:A/B/C`，`page_id`=当前页，`keys`=上表数组，
  `name/icon` 自定义）写进 `config.hotkeys` → `PUT /api/config` + WS `config.put`。
- 后端 `hotkey.run{id}` 从 config 取 `keys` 调 `input_win.key_press(keys)`，`[f11]`/`[esc]`/`[h]`/`[j]`/`[w]`/`[b]`/`[d]`/`[f]`
  均在 V1 §5 键名表内（`f1..f12`、`esc`、单字母），**后端零改动**。

---

## 8. `selftest.py` 必须同步的 5 处（否则离线自检会红）

V1 的静态自检会程序化比对命令集合、端点集合、默认配置 —— 后端改 `server/**` 时**必须同步**：

1. `EXPECTED_COMMANDS`（selftest.py:49）：追加 3 条 `brightness.set`、`brightness.get`、`voice.start`、
   `voice.text`、`voice.stop`（共 5 条 V2 命令，按 `dispatcher.HANDLERS` 实际注册为准，
   与 `implemented_commands()` 完全一致）。
2. `PROTOCOL_ENDPOINTS`（:75）：**不动**（冻结 10 条）。
3. `EXTRA_ENDPOINTS`（:91）：`("POST /api/apps", "GET /api/apps/icon")`。
4. `EXPECTED_ENDPOINTS`（:94）：按 `http_api.rest_endpoints()` 实际顺序（含新增
   `GET /api/apps/icon`），与 `rest_endpoints()` 返回值逐条相等。
5. `EXPECTED_DEFAULT_CONFIG`（:109）：`hotkeys:[]`；`apps` 换成 §2.2 的 5 个国内应用
   （含 `web`/`local` 字段），与 `config_store.default_config()` **逐字段深度相等**。

另新增回归用例（需求 3 的后端侧证明）：

- **「同 `key.press` 连发 N 次」**：联机自检里连发 5 次 `key.press{keys:["a"]}`，
  断言每次都独立收到 `ack`（或该命令按 V1 规定不回 ack 时不报 error），
  证明后端 `input_win.key_press` 无去重/节流/吞键；把结论写进 `server/README.md`，
  明确「连续单击只出一个字母」是前端 iOS Safari 吞 click，不是后端问题。
- 新增离线断言：`default_config()["hotkeys"] == []`、`default_config()["apps"]` 5 项 name 等于
  [抖音, bilibili, QQ音乐, 网易云音乐, 腾讯视频] 且每项含 `web`/`local`；
  `launch_app` 双路径、`GET /api/apps/icon` 已注册；`brightness`/`voice` 命令在 `HANDLERS` 内。

`python selftest.py --offline` 最终仍须 **FAIL=0**；端点/命令集合三处比对逻辑（"协议端点全注册"
"追加端点正确登记" "集合完全一致"）保持不变，只改数据不改判定框架。

---

## 9. 后端实现约束（写 `server/**` 的子 agent 必读）

- **只写 `server/**`**，不得写 `web/**`、`docs/**`、根 `README.md`、`reference/**`；不建 git commit。
- **不改义 V1**：22 条命令/8 下发/10 协议端点/6 顶层 config 键/§5 键名表/`MOUSEEVENTF_MOVE` 相对移动/
  `os.replace` 原子写/`drain 0.5s`/心跳/状态机 —— 一律保留。
- **所有新增第三方能力（WMI、图标提取、`webbrowser`、语音）必须 try/except 降级**，
  缺依赖/非桌面会话/非 Windows 一律 `best_effort` 不崩服务；这一点是 V1 已确立的健壮性红线。
- **高频命令**（`brightness.set`、WMI 调用）放 executor，不阻塞 aiohttp 事件循环，不写 JSON 日志。
- 中文注释与 `server/README.md` 用中文，标识符/命令名/JSON 键保持英文原样。
- 完整可运行，禁止 TODO/占位/省略/骨架（沿用 `full-output-enforcement`）。
- 完成后 `python -m py_compile` 全量自检 + `python selftest.py --offline` FAIL=0；
  用 `send_message` 给 `lead` 发结构化回执：文件清单 / 职责 / 新增命令·端点逐条 / V1 兼容性 /
  §8 五处同步 / 连续按键回归 / 双路径与图标实现 / 自测结论 / 已知限制与 best_effort / 需协调项。

---

## 10. 待办与后续（Lead 回收后端后总和）

- 后端子 agent 完成后，Lead 以本文件 + V1 逐字段核对 `server/**`，必要时补集成胶水
  （如同 V1 阶段补 `POST /api/apps`）。
- 前端会话交付后，Lead 读 `web/**` + `web/HANDOFF_V2.md`，核对 §0 矩阵里 11 项前端职责，
  更新根 `README.md`、§PLAN.md，如实记录分工与本轮变更，再 present 给 YG。
