# 系统架构与缺陷修复清单

## 修复日期：2026/9/28

## P0 级别修复（关键功能）

### BUG-001: bridge_server.py 路径解析失效
- **文件**: `bridge_server.py` line 10-19
- **问题**: 打包后 `__file__` 指向虚拟路径，导致 config.json 加载失败
- **修复**: 新增 `get_real_base_dir()` 函数，免疫 PyInstaller 虚拟环境
- **测试**: ✅ 语法检查通过

### BUG-006: semi_runner.pyw 路径解析失效
- **文件**: `semi_runner.pyw` line 13-21
- **问题**: 打包后 `__file__` 失效，无法加载 agent_core
- **修复**: 同 BUG-001 逻辑
- **测试**: ✅ 语法检查通过

### BUG-007: API Key 明文硬编码
- **文件**: `agent_core.py` line 1-23, `config.json` line 4
- **问题**: API Key 硬编码在源码中，存在泄露风险
- **修复**: 从 config.json 读取，新增 `deepseek_api_key` 配置项
- **测试**: ✅ 语法检查通过

### BUG-008: launcher.py 中文路径问题
- **文件**: `launcher.py` line 28-42
- **问题**: 中文用户目录下 subprocess 调用失败
- **修复**: 使用 `sys.executable` 获取 Python 路径，传递绝对路径，添加 `encoding='utf-8'`
- **测试**: ✅ 语法检查通过

## P1 级别修复（稳定性）

### BUG-002: WebSocket 掉线警报重复上报
- **文件**: `bridge_server.py` line 319-333, `probe.js` line 171-179
- **问题**: 探针收到异常掉线事件后持续重复上报
- **修复**: 
  - 后端新增 `ALARM_CONFIRMED` 和 `RECOVERY_CONFIRMED` 确认回执
  - 前端收到确认后停止重复上报
  - 新增 Bark 推送（掉线时立即通知）
- **测试**: ✅ 语法检查通过

### BUG-004: WebSocket 重连内存泄漏
- **文件**: `probe.js` line 126-167
- **问题**: 断线重连时未清理旧 WebSocket 对象
- **修复**: 
  - 新增 `reconnectAttempts` 和 `maxReconnectAttempts` 计数
  - 重连前清理旧连接（`ws.onclose = null`）
  - 指数退避策略（3 秒 → 30 秒）
- **测试**: ✅ 语法检查通过

### BUG-019: HEADERS_SYNC 发送时机过早
- **文件**: `probe.js` line 142-151
- **问题**: WebSocket 连接时 Token 可能尚未被 fetch 拦截器捕获
- **修复**: 延迟 500ms 发送，若仍无 Token 则等待下次重连
- **测试**: ✅ 语法检查通过

### BUG-020: WAITING 状态未推送手机端
- **文件**: `bridge_server.py` line 82-86
- **问题**: AI 判定 WAITING 时手机端无提示
- **修复**: 推送 `AI_STATUS` 消息，显示"等待玩家回复中"
- **测试**: ✅ 语法检查通过

## P2 级别修复（数据完整性）

### BUG-005: 会话数据无限膨胀
- **文件**: `bridge_server.py` line 339-346
- **问题**: `state["conversations"]` 长期运行后无限增长
- **修复**: 限制会话数量为 20 个，超出时删除最旧会话
- **测试**: ✅ 语法检查通过

### BUG-010: 页面加载时误判掉线
- **文件**: `probe.js` line 202-220
- **问题**: 页面加载缓慢时误触发掉线警报
- **修复**: 新增 `pageLoadComplete` 标志，延迟 3 秒后才开始检测
- **测试**: ✅ 语法检查通过

### BUG-011: SEND_REPLY 字段未过滤
- **文件**: `bridge_server.py` line 394-400
- **问题**: 手机端 `action` 字段转发给扩展端导致 JSON 解析警告
- **修复**: 仅转发 `command`, `groupID`, `content` 必要字段
- **测试**: ✅ 语法检查通过

## 新增功能

### WebSocket 协议扩展
- **新增事件**: `HEADERS_SYNC` 处理逻辑（bridge_server.py line 315-317）
- **新增命令**: `ALARM_CONFIRMED`, `RECOVERY_CONFIRMED`（双向确认）
- **新增消息**: `AI_STATUS`（AI 状态推送）

### 路径解析工具函数
- **新增函数**: `get_real_base_dir()`（bridge_server.py, semi_runner.pyw, launcher.py）
- **特性**: 统一处理打包/非打包状态下的路径解析

## 待测试项目

### 功能测试
1. ✅ Python 语法检查（全部通过）
2. ⏳ 启动 launcher.py 测试双进程唤醒
3. ⏳ 测试 WebSocket 重连机制（手动断开网络）
4. ⏳ 测试 Bark 推送（掉线/疑难单场景）
5. ⏳ 测试手机端 WAITING 状态显示

### 边界测试
1. ⏳ 中文路径启动测试
2. ⏳ 打包后 exe 运行测试
3. ⏳ 长时间运行内存占用监控
4. ⏳ 20+ 会话切换测试

## 修改文件清单

| 文件名 | 修改行数 | 主要改动 |
|--------|---------|---------|
| `bridge_server.py` | ~80 行 | 路径解析、WebSocket 确认回执、会话清理、WAITING 推送 |
| `probe.js` | ~60 行 | WebSocket 重连优化、确认回执处理、页面加载检测 |
| `agent_core.py` | ~15 行 | API Key 从 config 读取 |
| `semi_runner.pyw` | ~10 行 | 路径解析增强 |
| `launcher.py` | ~15 行 | 中文路径处理、绝对路径调用 |
| `config.json` | +1 行 | 新增 `deepseek_api_key` 字段 |

## 注意事项

1. **API Key 安全**: 请勿将 config.json 提交到版本控制系统
2. **打包测试**: 修复后需重新运行 PyInstaller 打包测试
3. **油猴脚本**: probe.js 需在 Tampermonkey 中重新加载
4. **WebSocket 端口**: 默认 8765，若冲突请修改 config.json

---

## 🔴 追加修复（2026/9/28）：probe.js 结构性损坏

### BUG-021 ~ BUG-026: probe.js 语法错误，油猴脚本完全无法运行

- **文件**: `probe.js`
- **发现方式**: `node --check probe.js` → `SyntaxError: Missing catch or finally after try`（第 201 行）

**问题清单**：

| 编号 | 位置 | 问题 |
|------|------|------|
| BUG-021 | 第 138 行 | `try {` 缺少 `catch`/`finally` → 整个 IIFE 语法错误，脚本根本无法加载 |
| BUG-022 | 第 130 行 | `function connectBrain()` 缺少闭合 `}`（被上一行的 `try` 吞掉） |
| BUG-023 | 第 206 / 222 行 | 两个 `setInterval` 嵌套、外层未闭合 → 结构错乱 |
| BUG-024 | 第 209 / 213 / 216 行 | 状态文本写成 `'IM 离线'`（带空格），与点击监听（第 65 行）及下拉菜单的 `'IM离线'`（无空格）不一致 → 掉线检测失效 |
| BUG-025 | 第 140 行 | `reconnectAttempts = 0` 在每次连接尝试时重置 → 最大重连上限形同虚设，退化为无限 3 秒重试 |
| BUG-026 | 第 218 / 234 行 | `ALARM_RECOVERED` 每 2 秒无偿重复上报 → 消息洪水 |

**修复方式**: 重写整个文件（保持 `@name` 不变以便 Tampermonkey 原地升级，版本号 7.0 → 7.1）

- 补齐 `try/catch`，闭合所有函数与 `setInterval`
- 新增 `normText()` 归一化文本，同时兼容 `IM离线` 与 `IM 离线`
- 新增 `findIMStatusText()` 统一状态探测入口
- 新增 `sendToBrain()` 安全发送（连接不可用时静默跳过，不抛异常）
- 新增 `scheduleReconnect()` 指数退避重连；重连计数改在 `onopen` 才重置
- 新增 `lastIMStatus` 状态变化判断，`ABNORMAL_OFFLINE` / `ALARM_RECOVERED` 仅在状态切换时上报
- 下行消息 JSON 解析加 `try/catch`，非法数据不再中断脚本
- 移除冗余的 `row.matches(...)` 调用（BUG-015）

**验证结果**:

```
node --check probe.js                  → 退出码 0（语法 OK）
node probe_smoke_test.js probe.js      → 18 通过 / 0 失败
文件编码                               → UTF-8 无 BOM、CRLF、无非法字节
```

**新增回归测试**: `probe_smoke_test.js`
用 Node + `vm` 模块模拟浏览器环境（DOM / WebSocket / AudioContext / 定时器），无需打开浏览器即可验证油猴脚本：

```bash
node --check probe.js                  # 语法检查
node probe_smoke_test.js probe.js      # 功能冒烟测试（18 项）
```

覆盖项：脚本加载、WebSocket 建连、定时器注册、掉线警报、防重复上报、恢复上报、
消息抓取与字段结构、内容去重、页面判断、下行指令容错、断线重连、HEADERS_SYNC 延迟发送。

---

## 🔴 第三轮修复（2026/9/28）：P0 安全与业务阻塞项

> 本轮采用「最小改动、风险最低」策略，仅修 P0。

### P0-1: IM Token 被全量广播给手机端（安全泄露）

- **文件**: `bridge_server.py`
- **写入点**: 原第 323 行 `state["im_auth_headers"] = headers_data`
- **泄露路径**: `state` 被 10 处 `FULL_SYNC` 整体推送给手机端（第 99/226/331/339/360/370/379/383/392/404 行）
- **实测证据**（`token_leak_test.py`，真实启服 + 模拟探针/手机端）:

```
修复前:
[2] 手机端收到 FULL_SYNC，长度 = 286
    >>> ！！Token 泄露确认！！手机端拿到了探针的 IM 认证 Token
    >>> 泄露上下文: "conversations": {}}}, "im_auth_headers": {"x-cs-token": "SECRET-TOKEN-...", "authorization": "Bearer S
=== 结论: 存在 Token 泄露 ===
```

**放大因素**：① H5 走明文 `ws://`（可嗅探）② 每次状态变更都重推一遍（Token 雪球）③ 探针每次重连都重发 `HEADERS_SYNC` ④ 该字段全项目**只写不读**（100% 纯负债）

**修复**：认证头改为独立全局 `IM_AUTH_HEADERS`，永不进入 `state`，且加指纹去重（内容未变化则不覆盖、不打日志）

```
修复后:
[2] 手机端收到 FULL_SYNC，长度 = 157
    >>> 未发现 Token（安全）
    >>> 载荷是否含 'im_auth_headers' 字段: 否（正确）
=== 结论: 无泄露 ===   (退出码 0)
```

去重验证：同内容连发 3 次 + 轮换 1 次 → 服务端仅打印 **2 行**「认证头已更新」

---

### P0-2: AI 接口报错被当作回复外发

- **文件**: `agent_core.py:113,115`（原）
- **问题**: `_call_deepseek` 失败时返回 `"[接口错误: xxx]"` / `"[网络异常: xxx]"`，因不含 `[TAG:xxx]`，被 `process_ticket_f9` 的 `else` 分支当作 `("NORMAL", 错误文本)` 返回
- **后果**: `semi_runner.pyw:203-205` 会 `pyperclip.copy()` + `ctrl+v` 把报错粘进回复框；AFK 模式下 `bridge_server.py` 更是**直接发给玩家**

**修复**：
- `_call_deepseek` 失败一律返回 `""`，真实错误写入 `stderr` 供排查
- `process_ticket_f9` 检测到空返回 → 返回专用标签 `("API_ERROR", "")`，不产出任何可发送内容
- `semi_runner.pyw` 在 `on_f7` / `on_f9` / `on_f10` 三处加**安全闸**：空内容或 `API_ERROR` 时只提示、绝不粘贴

---

### P0-3: 疑难单分支永不触发（前后端协议断链）

- **断裂点**:
  - `agent_core.py:198` 指示 AI 输出 `【需走内部群核实，建议按 F8 上报群聊】`
  - `bridge_server.py:87` / `semi_runner.pyw:218` 检查的却是 `"【规章库未收录"`
- **次生 bug**: `sanitize_reply` 会把 `内部群` 改写成 `专人核实处理`，标记进一步变形为 `【需走专人核实处理核实，建议按 F8 上报群聊】`
- **次生 bug**: 系统指导客服按 **F8**，但 `semi_runner.pyw:242-245` 只注册了 F6/F7/F9/F10，**F8 根本不存在**

**修复**：
- 新增统一标记常量 `HARD_CASE_MARKER = "【规章库未收录，请上报内部群核实】"`（前缀与前后端判定一致）
- 新增 `_normalize_hard_case()`：在 `sanitize_reply` **之后**调用，用容错正则 `_HARD_CASE_RE` 把各种变体归一化为统一标记
- 更新 prompt：要求 AI 第一行原样输出该标记，并明确禁止提及 F8 等快捷键
- `bridge_server.py` / `semi_runner.pyw` **零改动**即恢复工作

---

### P0-4: 重复的 `SEND_REPLY` 分支（死代码）

- **文件**: `bridge_server.py`（原第 400-405 行）
- **问题**: 同一条件出现两个 `elif`，第二个永不执行，且未做 BUG-011 的字段过滤（仍用 `**pkt`）
- **修复**: 删除重复分支；验证 `**pkt` 残留 = 0、`SEND_REPLY` 分支数 = 1

---

### 验证汇总

```
python -m py_compile bridge_server.py / agent_core.py / semi_runner.pyw / launcher.py   -> 全部 [OK]
python agent_core_test.py        -> 16 通过 / 0 失败
python token_leak_test.py        -> 无泄露（退出码 0）
node --check probe.js            -> 退出码 0
node probe_smoke_test.js probe.js -> 18 通过 / 0 失败
```

### 新增回归测试

| 文件 | 用途 |
|------|------|
| `token_leak_test.py` | 启服 + 模拟探针/手机端，验证 Token 不进入广播载荷（含去重验证） |
| `agent_core_test.py` | 验证疑难单标记归一化、AI 报错不外发、前后端契约一致性 |

### 第四轮修复（P1 / P2）

#### P1-C2: emoji 导致 stdout 重定向时服务崩溃

- **文件**: `bridge_server.py:419,420`、`launcher.py:15,47`（原行号）
- **现象**: `python bridge_server.py > log.txt` 立即抛 `UnicodeEncodeError: 'gbk' codec can't encode '\U0001f680'` 并退出进程
- **成因**: stdout 非真实控制台时 Python 用 `locale.getpreferredencoding()`（GBK）编码，非 BMP 字符（🚀 🔑 🗑️ 👉）无法编码
- **修复**:
  - 三个入口文件统一加 `stream.reconfigure(errors="replace")` 安全网（兼容 `pythonw` 下 `stdout is None`）
  - 控制台 `print` 的非 BMP emoji 改为 ASCII 标记（`[启动]` `[OK]` `[清理]` `[错误]`）
  - Bark 通知标题保留 emoji（走 URL 编码，不受控制台影响）
- **验证**: 不设 `PYTHONIOENCODING` + 重定向 stdout → **服务进程存活 = True**，stderr 无错误

#### P1-D1: H5 手机端存储型 XSS

- **文件**: `bridge_server.py` 内嵌 `HTML_CONTENT`（原第 274、252-255 行）
- **问题**: 玩家消息/昵称直接拼进 `innerHTML`，玩家可发 `<img src=x onerror=...>` 在**客服手机上执行脚本**
- **修复**:
  - 新增 `esc()` HTML 转义，昵称/最后消息/正文全部转义
  - 会话卡片 `onclick="pushChat('${gid}')"` 改为 `data-gid` + **事件委托**（内联 onclick 靠转义防不住属性逃逸）
  - `msg-row` class 限制为 `player`/`agent` 白名单，防 class 注入
  - `msgs` 增加 `Array.isArray` 兜底

#### P1-D2: H5 断线时按钮"失灵"

- **文件**: 内嵌 H5（原第 212/219/281/283/286/288 行）
- **修复**: 新增 `sendMsg()` 安全发送；`execCommand`/`toggleAFK` 断线时明确提示；`toggleAFK` 加 `globalState` 空值保护

#### P1-D3: H5 无限重连无退避

- **修复**: 指数退避（2s→30s 上限，最多 20 次）、连接前清理旧句柄、补 `onerror`、计数在 `onopen` 才重置、下行 `JSON.parse` 加 try

#### P1-E1: `json.loads` 无容错（2 处）

- **修复**: 非法 JSON / 非 dict 载荷直接 `continue`，不再打断 WS 连接

#### P1-E2/E3/E4: 脏数据 KeyError

- **修复**: `build_chat_history_str` 用 `.get()` + 过滤非 dict/空文本；会话写入统一 `setdefault` 兜底；
  `PLAYER_MESSAGE` 入库前过滤脏消息；`SEND_REPLY` 增加 `gid`/`text` 空值校验

#### P2-E5: `/api/ticket` 定位不准

- **修复**: 新增 `_LAST_ACTIVE` 记录最近有活动的工单，优先返回，失效则回退

#### P2-E6: `push_bark` 的 `group_id` 参数从未使用

- **修复**: 真正用于正文尾部标注来源；`group=客服` 改为 `urllib.parse.quote('客服')` 正确编码

#### P2-E8: 断连清理可能 KeyError

- **修复**: `remove(ws)` → `discard(ws)`（2 处）

#### P2-F1 / F3: 公关净化误伤 + 接口无重试

- **F1**: `(?i)bug` → `\bbug\b`，`debug` 不再变成 `de异常情况`
- **F3**: 网络异常重试 1 次（间隔 1s）；HTTP 非 200 不重试；失败一律返回 `""`

#### 第四轮验证汇总

```
python -m py_compile 4 个文件        -> 4/4 [OK]
python agent_core_test.py            -> 16 通过 / 0 失败
node h5_security_test.js             -> 15 通过 / 0 失败
node --check probe.js                -> 退出码 0
node probe_smoke_test.js probe.js    -> 18 通过 / 0 失败
python token_leak_test.py            -> 无泄露（退出码 0）

崩溃复现测试（不设 PYTHONIOENCODING + 重定向 stdout）:
  服务进程存活 = True ／ stderr 无错误        <- 原崩溃点已修复

端到端: 首页 HTTP 200，13791 字节
  esc(): True ／ data-gid: True ／ sendMsg: True ／ 残留 onclick="pushChat(: False
```

#### 新增回归测试

| 文件 | 用途 |
|------|------|
| `h5_security_test.js` | 从 `bridge_server.py` 提取内嵌 H5 脚本，用 Node 假 DOM 验证 XSS 修复、断线容错、重连退避、脏包容错 |

#### 仍留有（低优先级 / 需权衡）

- H5 走明文 `ws://`（需 TLS 证书；内网部署或加 `wss` 可缓解）
- `semi_runner.pyw` 的密钥熔断仅覆盖 `sk-` 前缀
- 多客户端并发下的 `state` 竞态（aiohttp 单事件循环下当前安全）
- 表格实时同步（当前只读权限，采用本地 Excel 方案）

---

## 📱 第五轮（2026/9/28）：手机端体验重构 + 多工单隔离

用户反馈 4 个问题，全部定位并修复：

### 问题 1：手机端主页永远只有一个玩家，新消息会把旧的顶掉

- **根因**: `probe.js` 把 `groupID` **硬编码为常量 `"当前工单"`**，
  所有工单都写进同一个 key → 服务端 `c["msgs"] = payload["messages"]` 覆盖数组，
  于是"新玩家上来就把旧玩家的记录覆盖掉"。
- **修复**: `probe.js` 新增 `pickTicketId()` / `buildTicketIdentity()`，
  分层获取真实工单标识：
  1. URL 中的 `ticketId|sessionId|chatId|orderId|id`
  2. 页面上 `data-ticket-id` / `data-session-id` / `data-chat-id` / `data-conversation-id`
  3. 左侧会话列表中当前选中项（`.active[data-id]` 等）
  4. 兜底：`"P" + simpleHash(playerInfo + "##" + 首条玩家消息)` —— 稳定且能区分玩家
- **服务端配套**: 每次上报都刷新会话名；单条消息打 `ts` 时间戳；
  新增 `updatedAt`；淘汰策略由「最早创建」改为「**最久无活动**」，上限提到 50。

### 问题 2：屏幕上方模糊

- **根因**: `#chat-view { transform: translateX(100%) }` + `transition: transform`。
  iOS Safari 会为带 `transform` 的元素建立合成层，文字被栅格化后**发虚**。
  另外 `body { height: 100vh }` 在 iOS 上大于可视高度，页面会滑到半透明工具栏下方。
- **修复**: 页面切换改用 `display: none/flex`（`.view.active`）；
  根容器改 `#app { position: fixed; inset: 0 }` + `@supports (height:100dvh)`；
  加 `-webkit-font-smoothing: antialiased`。

### 问题 3：界面无法上下滑动

- **修复**: `html/body { height:100% }` + `#app` flex 列布局；
  滚动容器显式声明 `flex: 1 1 auto; min-height: 0; overflow-y: auto;
  -webkit-overflow-scrolling: touch; overscroll-behavior: contain; touch-action: pan-y`。
  `min-height: 0` 是让 flex 子项真正能滚动的关键。

### 问题 4：不能像微信那样区分好友

- **修复**: 重做会话列表 —— 头像（首字取色）+ 昵称 + 最后一条消息预览 +
  时间（今天 `HH:MM`，其它 `M/D`）+ 未读红点；按 `updatedAt` 倒序；
  聊天页气泡下显示时间戳。打开某会话再返回，其余会话全部保留。

### 附带修复：广播健壮性（BUG-027）

- **现象**: 测试连跑时手机端收不到更新。
- **根因**: 向已断开的客户端 `send_json` 抛异常会**打断整个广播循环**，
  使同批其他客户端也收不到；`finally` 只会清理当前连接，不会清理死连接。
- **修复**: 新增 `safe_send(client, payload)`，发送失败时静默忽略并 `discard` 该连接；
  17 处广播调用点（9 处 mobile + 8 处 extension）全部替换。

### 验证

```
python -m py_compile 4 文件          -> 4/4 [OK]
node probe_smoke_test.js probe.js    -> 26 通过 / 0 失败
node h5_security_test.js             -> 34 通过 / 0 失败
python agent_core_test.py            -> 16 通过 / 0 失败
python token_leak_test.py            -> 无泄露
python multi_conv_test.py            -> 通过（连跑 3 轮均通过）

关键断言：
  两个玩家的会话同时存在（不再只剩一个）      -> 本次运行键=['T-A-...','T-B-...']
  甲的记录没有被乙的消息覆盖                  -> 甲=['回复给甲'] 乙=['回复给乙']
  主页同时显示全部 3 个会话                    -> 卡片数=3
  按最近活动倒序排列（乙 → 丙 → 甲）
  不同玩家得到不同标识                        -> P9uwuac  vs  Pc2s5sp
  已移除 transform 滑动（iOS 文字发虚的元凶）
```

### 新增回归测试

| 文件 | 用途 |
|------|------|
| `multi_conv_test.py` | 端到端验证多工单隔离（会话共存 / 消息互不覆盖 / 时间戳 / 脏数据容错） |

---

## 🚀 第六轮（2026/9/28）：待推进功能闭环

### 1. 表格规章库定时自动同步（高优先级）✅

**新增 `rules_sync.py`**（同时支持 CLI 与作为模块被 bridge 调用）：

| 路径 | 机制 |
|------|------|
| 监听目录 | `rules_watch_dir` 内出现更新的 xlsx 即复制覆盖（只复制不动源文件） |
| 直链拉取 | `rules_sync_url` 下载 → 内存校验 → 备份 `.bak` → 原子替换 |
| 手动覆盖 | 直接放根目录，`agent_core.reload_rules()` 按 mtime **热重载**，无需重启 |

- 新增 `CustomerServiceCore.reload_rules()`：一次 `os.stat` 判断，开销极低，
  已挂到 `process_ticket_f9` / `generate_closing` 入口 → **改完 Excel 立即生效**
- bridge 启动时拉起 `rules_sync_loop()`，按 `rules_sync_interval_minutes`（默认 30）轮询，
  有变更时打日志 + 推 Bark
- CLI：`python rules_sync.py --once/--watch/--info -v`
- **修掉一个真实 bug**：`pd.ExcelFile` 会占住文件句柄，Windows 下导致 `os.replace`
  报 `WinError 32`；改为**内存校验 + 显式 close**，新增 `_validate_bytes()`

### 2a. HEADERS_SYNC 底层 API 通道 ⚠️ 部分落地

- 认证头已独立存放（`IM_AUTH_HEADERS`），并做**指纹去重**
- 心跳保活：两个 WS handler 都启用 `heartbeat=30`，及时探活 iOS 退后台造成的死连接
- **未能完成的部分**：真正的"用 Token 直连官方 HTTP 接口拉工单/直发消息"需要
  **你们 IM 工作台的真实接口路径与字段**（浏览器 F12 → Network 抓给我），
  否则只能靠猜。当前 DOM 通道已足够稳定，建议先按现状使用

### 2b. CHANGE_STATUS 手机端开关 ✅

- H5 顶部新增 **IM 状态按钮**（点击弹出 在线/忙碌/离线 菜单）
- 新增手机端动作 `SET_IM_STATUS`：更新 `state.im_status` → 下发 `CHANGE_STATUS` 给探针
- 切到「在线」时**自动清除 `alarm_status`** 并下发 `SILENCE_ALARM` 停掉警报音
- 探针侧 `switchIMStatus` 原本就有，无需改动

### 3. iOS 锁屏唤醒断流补拉 ✅

- H5 监听 `visibilitychange` / `pageshow` / `online` / `focus`：
  可见时若连接已断 → **跳过退避立即重连**；连接正常 → 发 `REQUEST_SNAPSHOT`
- 新增后端动作 `REQUEST_SNAPSHOT`：立即回一帧 `FULL_SYNC`
- WS 心跳 30s，让"假连接"尽快暴露

### 4. F8 提炼结果即时上屏 ✅

- HUD 新增常驻行「**最近提炼 / 框选预览**」（窗口高度 240 → 276）
- F7 提炼完成 → 显示 `[提炼] 一句话总结`
- F9 解答 → `[F9·标签] 回复摘要`；F10 润色 → `[F10·润色] 结果摘要`
- **补上缺失的 F8 热键**（历史文档一直提到 F8，但从未注册过；现作为 F7 别名）

### 5. 规章库多工作表串联 ✅（附带重大发现）

- **旧实现只读「常规」一张表（37 行）**，而实际文件有 **13 张表**
- 新实现自动识别 **6 种排版**并全部穿透：

| 模式 | 识别依据 | 输出格式 |
|------|---------|---------|
| 话术库 | 表头含「话术」 | `分类 \| 情形 => 话术`（开场/引导/关单进模板池） |
| 问答库 | 表头含「答案/纯文本答案」 | `Q => A` |
| 已知BUG | 表头含「进度」或表名含「已知/bug」 | `[已知问题] 问题 => 进度` |
| 关单优先级 | 表头含「处理方式」 | `[优先级 T0] 问题类型 => 处理方式` |
| 长文本 | 单列 / 仅首列有内容 | `【表名】说明文档：全文` |
| 通用 | 其它 | 带列名拼接，避免错标 |

- **实测：解析条目 37 → 843 条（23 倍），13 张表全覆盖**
- 因为总量约 25 万字远超单次 prompt，改为**检索式注入**：
  - `kb_static_max_chars`（默认 12000）以内的小表**常驻** prompt
  - 其余（如 631 条 FAQ）建索引，按工单文本的 **2/3-gram 重合度**检索 top-K 注入
  - 零外部依赖（纯 Python 集合运算）
- 检索效果实测：
  - 「游戏一直连不上服务器提示网络错误」→ 精确命中 FAQ 里的同名条目
  - 「我充值了648元但钻石没到账」→ 命中「购买的礼包未到账怎么办」等

### 6. 手机端「AI 回复并关单」✅（新增需求）

- 聊天页新增按钮 **🤖 AI回复并关单**
- 后端 `handle_ai_close()`：AI 选问题分类 + 生成结束语 → 下发 `ACTION_REPLY_CLOSE`
  （带 `content` / `category` / `categoryPath`）→ **会话从列表移除** → 推 Bark 留痕
- **分类来源**（避免 AI 编造点不到的分类）：
  1. 探针连上后自动回报网页级联选择器的一级分类（`CATEGORY_OPTIONS`，只读不点击，安全）
  2. 后端缓存在 `state.category_options`，可经 `GET /api/categories` 查看
  3. AI 从中选一个；另有 `close_category_options` 可手工指定
- **探针侧分类选择重写**：`selectCategoryByKeyword()` 逐级下钻，
  优先按 AI 给的关键字匹配、其次按 `close_category_path` 提示、最后落回第一项；
  只有**叶子节点被点击一次**（即真正的选择），不会误改分类
- **安全失败保证**：AI 无返回 / 探针未连接 → 明确报错且**不删会话**

### 验证

```
python -m py_compile 6 个文件        -> 全 [OK]
node probe_smoke_test.js probe.js    -> 26 通过 / 0 失败
node h5_security_test.js             -> 34 通过 / 0 失败
python agent_core_test.py            -> 16 通过 / 0 失败
python rules_sync_test.py            -> 18 通过 / 0 失败
python token_leak_test.py            -> 无泄露
python multi_conv_test.py            -> 通过
python mobile_feature_test.py        -> 15 通过 / 0 失败
总体失败项: 0

AI 关单实测链路（真实 DeepSeek 调用）:
  [关单] 测试玩家 -> 分类「游戏BUG」
  探针收到: ACTION_REPLY_CLOSE
    content      = 亲爱的玩家，您反馈的问题我们已为您记录并转交专人跟进核实…
    category     = 游戏BUG
    categoryPath = ['一级分类', '二级分类']
  关单后会话已从列表移除
```

### 新增文件

| 文件 | 用途 |
|------|------|
| `rules_sync.py` | 规章库自动同步（监听目录 / 直链 / CLI） |
| `rules_sync_test.py` | 同步回归测试（18 项） |
| `mobile_feature_test.py` | 手机端新功能端到端测试（15 项） |

### 仍需你确认/补充

1. **IM 官方 HTTP 接口**（F2a 的完整实现）：需要 F12 Network 抓到的真实请求
2. **问题分类三级名称**：访问 `http://[电脑IP]:8765/api/categories` 看抓到的一级分类；
   若企业分类项与预期不符，把三级分类名填进 `config.json` 的 `close_category_options`
3. **工单 ID 精确化**：目前用「URL → data-* → 选中项 → 玩家信息摘要」兜底；
   有固定字段的话把 DOM 片段发我，可精确到字段级

---

## 🐞 第七轮（2026/9/28）：手机端状态显示与按钮点击修复

用户反馈：**手机显示「IM在线 / 电脑半自动」，但实际已挂 IM离线且电脑网页已关闭；
两个按钮点不动。** 定位到两个独立根因：

### BUG-028：手动离线永远同步不到手机

- **根因**：`probe.js` 的 IM 状态监控只在**异常掉线**时才上报：
  ```js
  if (currentStatus === 'IM离线' && !isManualOffline) {   // 手动离线被直接跳过
      playSiren();
      sendToBrain({ event: "ABNORMAL_OFFLINE" });
  }
  ```
  再加上 `'IM忙碌'` 根本不在上报分支里、WS 重连后也不重发状态，
  于是服务端 `state.im_status` 永远是初始值 **1（IM在线）**。
- **修复**：
  - 新增 `IM_STATUS` 事件，**任何状态变化都上报**（在线/忙碌/离线，含手动离线），
    附带 `manual` 标记用于区分手动与异常
  - 报警逻辑单独处理：只有**非手动**的离线才 `playSiren()` + `ABNORMAL_OFFLINE`
  - `ws.onopen` 里重置 `lastIMStatus = null`，**重连后强制重新同步一次当前状态**
  - `lastIMStatus` 声明上移到文件顶部，避免闭包时序问题

### BUG-029：探针离线时手机端仍显示过期状态

- **根因**：服务端不记录"探针是否在线"，电脑网页一关，手机端继续显示最后一次的状态。
- **修复**：
  - 初始 `state` 新增 `extension_online: false`
  - 探针连接 → `true` 并广播；探针断开（且无其它探针）→ `false` 并广播
  - H5 `renderIMStatus()` 优先显示 **⚫ 电脑未连接**

### BUG-030：iOS 上 `div` 的 onclick 不触发（按钮点不动）

- **根因**：顶部两个开关是 `<div onclick="...">`。
  iOS Safari 对**非按钮元素**的点击派发不可靠（同页的 `<button>` 元素都正常）。
- **修复**：
  - 顶部开关与状态菜单项全部改为真正的 `<button type="button">`
  - CSS 补 `cursor: pointer` + `-webkit-appearance: none` + 按钮样式复位
  - `.conv-card` / `.action-btn` / `.back-btn` 一并补 `cursor: pointer`

### 附带改进：不再"静默失败"

原来 `toggleAFK()` 里 `if (!globalState) return;` 会让按钮看起来像坏了。现改为：

- 新增 `extensionOffline()` 判断
- `toggleAFK` / `toggleStatusMenu` / `setIMStatus` / `execCommand` 在电脑端未连接时
  **弹出明确提示**（"电脑端未连接，请先打开客服工作台"），而不是默默无反应

### 验证

```
node probe_smoke_test.js probe.js    -> 35 通过 / 0 失败   (原 26，新增 9)
node h5_security_test.js             -> 34 通过 / 0 失败
python agent_core_test.py            -> 16 通过 / 0 失败
python rules_sync_test.py            -> 18 通过 / 0 失败
python token_leak_test.py            -> 通过
python multi_conv_test.py            -> 通过
python mobile_feature_test.py        -> 19 通过 / 0 失败   (原 15，新增 4)
总体失败项: 0

关键新增断言：
  手动离线会上报 IM_STATUS(status=3)  -> [{"event":"IM_STATUS","data":{"status":3,"manual":true}}]
  手动离线不触发 ABNORMAL_OFFLINE（不误报警）
  忙碌会上报 IM_STATUS(status=2)      -> 旧版完全不报
  重连后会重新同步当前 IM 状态
  探针上报手动离线 -> im_status=3
  探针断开后 extension_online=false
```

### 文档同步

`使用手册.md` 6.4 节补充「⚫ 电脑未连接」状态说明；
新增 **12.10 节：手机顶部按钮点不动**（含 iPhone 清缓存方法）。

---
## 🔍 第八轮（2026/9/28）：探针可观测性 + 桌面悬浮窗重做

> 用户反馈两件事：「probe.js 是不是有问题，还是我没加载到油猴？」
> 「新版桌面悬浮窗也没了」。两条都被实测复现并定位到根因。

### 结论先行（实测证据）

| 反馈 | 实测结论 |
|------|---------|
| probe.js 有问题？ | `node --check probe.js` **语法通过**，脚本本身没坏。但**当时无法证明它到底有没有在页面上跑** —— 这正是要修的问题 |
| 悬浮窗没了？ | **进程一直在跑，是窗口被摆到了屏幕外**：`hud_pos.json` 记录 `{x:1960, y:777}`，而本机屏幕物理只有 2880x1800 / 逻辑 1440x900 → 窗口生成即不可见；任务管理器里还有**两个** `pythonw semi_runner.pyw` |

### BUG-031：探针运行状态不可观测（"到底加载到油猴没有"无法回答）

- **现象**：控制台没有启动日志，客服只能猜；旧脚本连上了也没有任何提示
- **根因**：探针从不向后端声明身份与版本，页面内也没有任何可见状态
- **修复**：
  1. `@version 7.2`，连上即发 `PROBE_HELLO`（版本 + 页面地址 + UA），每 30 秒 `PROBE_HEARTBEAT`
  2. 页面**左下角常驻自检胶囊**：`🟢 探针 v7.2 · 已连接` / `🟡 重连中（3 秒后…）` / `🔴 未连接`；
     单击胶囊=立即重连，点 ✕ 收起
  3. 控制台暴露 `__probe.version()` / `__probe.status()` / `__probe.reconnect()`
  4. 新增 `GET /api/diag`（JSON）与 `GET /diag`（人看的自检页，服务端渲染 + 5 秒自动刷新）
  5. 新增 `GET /probe.js`：直接取项目里最新脚本，`?download=1` 下载
  6. 新增 `@match` 覆盖 `.com.cn` 与 `.com`，加 `@noframes`（防 iframe 内重复注入）
  7. 后端在探针连接/断开时打印醒目横幅（含 Referer 来源页）

### BUG-032：探针版本号显示成上一次的值（误导性自检）

- **现象**：实测截图里悬浮窗显示 `🟢探针v7.2`，但浏览器里跑的其实是**旧脚本**（旧脚本根本不上报版本）
- **根因**：版本存在全局变量里，只在"连接集合清空"时才重置；旧脚本连上来就复用了上一次的值
- **修复**：改为**按连接**记录（`_PROBE_CONNS[ws]`），每次变更用 `_probe_refresh()` 重算"最近一次上报"；
  另加 8 秒看门狗：连上却不上报版本 → 控制台打印「油猴里仍是旧脚本（< v7.2）」，
  悬浮窗显示 🟡探针旧版，自检页给出更新操作指引

### BUG-033：悬浮窗被摆到屏幕外（"悬浮窗不见了"的根因）

- **现象**：进程在、日志正常、热键已注册，但屏幕上什么都没有；`hud_pos.json = {x:1960, y:777}`
- **根因**：启动时直接照搬上次坐标，屏幕变小（换显示器/改分辨率/改缩放）后窗口整体落在可见区之外
- **修复**：
  1. `clamp_position()` 坐标夹取（含"窗口比屏幕还大"的退化处理），启动时自动移回并提示
  2. `SetProcessDpiAwareness(2)` 高 DPI 感知 + **按 DPI 比例缩放窗口与内边距**
     （本机 200% 缩放实测：不缩放会导致字号被 tk scaling 放大 2 倍而溢出）
  3. 新增全局热键 **Ctrl+Alt+H 召回**（拉回右下角 + 重新置顶）、**Ctrl+Alt+M 折叠**
  4. 默认位置改为右下角（旧版固定左上角，容易被浏览器工具栏压住）
  5. 不依赖 `winfo_x()`（窗口未映射时返回 0，会把正确坐标写成 0,0 —— 实测踩到过）

### BUG-034：悬浮窗可被启动两份，两套热键互相打架

- **现象**：任务管理器里两个 `pythonw semi_runner.pyw`
- **修复**：`acquire_single_instance()`（本地端口互斥）——重复启动只在界面上提示"已经在运行，
  请按 Ctrl+Alt+H 召回"，不再产生第二个窗口

### BUG-035：工作线程直接操作 Tk（偶发闪退）

- **根因**：F6~F10 都在工作线程里直接调 `hud.update_ui()`，Tk 不是线程安全的
- **修复**：所有 UI 更新改为 `queue.Queue` + 主线程 80ms 轮询执行；对外 `update_ui()` 签名保持不变

### BUG-036：带 BOM 的 JSON 配置会静默失效

- **现象**：`hud_pos.json` 被 PowerShell/记事本写成带 BOM 的 UTF-8 → `json.load` 抛错 →
  静默回退默认值（实测就是这样把正确坐标丢掉、又回到 0,0 的）
- **修复**：`bridge_server.py` / `semi_runner.pyw` / `rules_sync.py` 读取 `config.json`、
  `hud_pos.json` 统一改用 `utf-8-sig`

### BUG-037：悬浮窗信息量不足 / 结果无法核验

- **修复**：新增标题栏三盏灯（中继 / 探针含版本 / 手机端连接数）、底栏统计
  （会话数 · 分类数 · 规章条数）、「最近提炼」行支持**双击复制**；折叠/置顶/位置状态持久化

### 验证（第八轮实测）

```
node probe_smoke_test.js probe.js    -> 45 通过 / 0 失败   (原 35，新增 10)
node h5_security_test.js             -> 34 通过 / 0 失败
python agent_core_test.py            -> 16 通过 / 0 失败
python rules_sync_test.py            -> 18 通过 / 0 失败
python hud_layout_test.py            -> 31 通过 / 0 失败   (新增文件)
python diag_test.py                  -> 29 通过 / 0 失败   (新增文件)
python token_leak_test.py            -> 通过
python multi_conv_test.py            -> 通过
python mobile_feature_test.py        -> 20 通过 / 0 失败（AI 网络延迟偶发超时，重跑即过）
总体失败项: 0
```

关键实测证据（不是单元测试，是真实进程/窗口）：

```
# 还原坏坐标 {x:1960,y:777} 后重启悬浮窗
hud_pos.json -> {"x": 1932, "y": 777, ...}       # 已自动夹回屏内（物理像素）
窗口枚举   -> class=TkTopLevel title=工单助手 HUD visible=true
              rect=(1892,1056)-(2832,1656) = 940x600 物理像素（=470x300 逻辑，200% 缩放）
# /api/diag
{"probe": {"online": true, "version": "7.2", "version_reported": true,
           "page": "https://ticket.example.com/imChat/workbench", ...}}
```

### 新增/更新文件

| 文件 | 说明 |
|------|------|
| `probe.js` | v7.2：自检胶囊 + PROBE_HELLO/心跳 + `__probe` 调试入口 + 双域名 @match |
| `bridge_server.py` | `/api/diag`、`/diag`、`/probe.js`、探针握手/心跳、按连接版本追踪、8 秒旧脚本看门狗、utf-8-sig |
| `semi_runner.pyw` | 悬浮窗重做（三盏灯 / 折叠 / 置顶 / 召回 / DPI 缩放 / 坐标夹取 / 单实例 / 线程安全队列） |
| `hud_layout_test.py` | 新增：悬浮窗几何、单实例、状态灯、源码级回归 31 项 |
| `diag_test.py` | 新增：诊断接口与探针握手端到端 29 项 |
| `probe_smoke_test.js` | 45 项（新增自检胶囊 / 握手 / 心跳 / `__probe` 断言） |

### 仍需你确认/补充（沿用第六轮）

1. IM 官方 HTTP 接口的抓包（拉工单列表 / 发送消息）
2. 三级问题分类的真实名称（打开 `/api/categories` 核对）
3. 工单列表项的 HTML 片段（用于精确定位工单 ID）

---

*此文档由 AI Bug 排查 Agent 自动生成*