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

### 1. 官方规章库定时自动同步（高优先级）✅

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

`README.md` 6.4 节补充「⚫ 电脑未连接」状态说明；
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
           "page": "https://你的工单域名/imChat/workbench", ...}}
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

## 🛡️ 第九轮（2026/9/28）：手动离线被改成在线 + 手机端 UI 重做

### 现象（客服原话）

> "我自己在网页上手动设置的离线，它自己变成在线了 —— 这是不允许的。"

### 根因（三条叠加，缺一条都不会出现这个现象）

| # | 根因 | 证据 |
|---|------|------|
| 1 | **中继重启把状态顶回默认值**：`state["im_status"]` 只在内存里，进程一重启就是 `1`（在线），而探针**只在状态变化时**才上报 —— 状态没变就永远不纠正 | 本次调试期间反复重启 `bridge_server.py`，重启后手机端立刻变🟢在线 |
| 2 | **探针重连会彻底放弃**：`scheduleReconnect()` 重试 10 次后 `return`，此后即使中继恢复也永不再连 → 手机端一直停在旧状态 | 旧代码 `if (reconnectAttempts >= maxReconnectAttempts) { …; return; }` |
| 3 | **网页自己会跳回在线**：IM 工作台在窗口重新获得焦点时会自动上线（客服在页面间切来切去），探针如实上报"在线"，看起来就像"我设的离线被改了" | 探针 `findIMStatusText()` 也可能读到**隐藏的**下拉选项（旧版只做文本匹配，不看可见性） |

### 修复

| # | 修复 | 位置 |
|---|------|------|
| 1 | **状态记忆落盘**：`im_state.json` 记录 `{status, manual, ts}`，启动即恢复；新增 `im_status_known` 标记"本进程是否已从网页核实" | `bridge_server.py` `load/save/apply_im_status()` |
| 2 | **唯一写入口** `apply_im_status()`：IM_STATUS / ABNORMAL_OFFLINE / ALARM_RECOVERED / SET_IM_STATUS 全部改走它（内存 + 落盘 + 清警报） | `bridge_server.py` |
| 3 | **状态不凭空显示**：`known=false` 时手机端顶部显示 `⏳ 正在获取…`；探针掉线时 `known=false` → 显示 `⚫ 电脑未连接` | H5 `renderIMStatus()` |
| 4 | **强制复核**：新增 `REQUEST_IM_STATUS`（手机端一连上、回前台、点开页面自动发）→ 服务端转发给探针 → 探针**无论状态是否变化**都重新读 DOM 并上报 | `probe.js` / `bridge_server.py` |
| 5 | **连上即复核**：探针 `onopen` 100ms 后强制上报一次真实状态（中继重启也不会残留旧值）；`POLICY` 命令下发守护开关 | `probe.js` |
| 6 | **手动离线守护**：手动挂离线后，网页自己跳回在线/忙碌 → 探针点回「IM离线」并上报 `guarded:true`；上限 3 次/10 秒（超过则如实上报，不与网页无限对抗）；自己点在线/忙碌有 1.5s 免打扰且人的点击永远优先 | `probe.js` `restoreManualOffline()` |
| 7 | **状态读取只看可见节点**：隐藏的下拉选项不再被当"当前状态"，状态显示区优先、菜单项仅兜底 | `probe.js` `findIMStatusText()` |
| 8 | **重连永不放弃**：10 次后不再停止，改为每 30 秒持续重连（胶囊提示"持续重连中"） | `probe.js` `scheduleReconnect()` |
| 9 | **诊断可查**：`/api/diag` 新增 `im.{status,status_text,known,manual,state_file}`，`/diag` 自检页新增「IM 状态」卡片（含状态记忆时间与守护开关） | `bridge_server.py` |

### 手机端 UI 重做（按要求"精美一点、简约一点"）

| 项 | 之前 | 现在 |
|----|------|------|
| 顶部控件 | 状态按钮 + 弹出菜单；AFK 按钮 | **两个原生 `<select>` 下拉框**（IM 状态 / 托管模式），iOS 点一下就是系统选择器，不会点不动 |
| 状态可信度 | 直接显示服务端默认值（可能凭空🟢在线） | 先 `⏳ 正在获取…` → 问到真实值再显示；探针掉线显示 `⚫ 电脑未连接` |
| 顶部信息 | 无 | 状态条：`中继 已连接` · `电脑探针 v7.3` · `N 个会话` |
| 视觉 | 纯色卡片、无层次 | 深色玻璃拟态顶栏、圆角卡片/头像、渐变发送键、气泡尾巴、安全区内边距、去 transform（iOS 文字不发虚） |
| 会话页 | 单行列表 | 头像 + 昵称 + 未读红点 + 最后一条消息 + 时间（微信式），另有进行中数量提示 |
| 聊天页 | 标题 + 操作栏 | 标题带工单号、操作按钮胶囊化（AI 回复并关单 / AI 起草 / 挂起 / 关单）、输入区悬浮圆角 |

### 验证

```
node probe_smoke_test.js          -> 57 通过 / 0 失败（新增：强制复核、离线守护、重连永不放弃、__probe.config）
node h5_security_test.js          -> 49 通过 / 0 失败（新增：两个下拉框、未核实不显示在线、掉线显示未连接）
python diag_test.py               -> 30 通过 / 0 失败（新增：im 区块）
python mobile_feature_test.py     -> 23 通过 / 0 失败（新增：REQUEST_IM_STATUS 复核链路）
```

**重启不失忆实测**（本次真实跑过）：

```
# 1) 手机端手动设为离线（写盘 im_state.json -> {"status":3,"manual":true}）
# 2) 强杀 bridge_server.py 并重启
python _verify_restart.py after
after -> {"status": 3, "status_text": "离线", "known": false, "manual": true,
          "state_file": "im_state.json", ...}
# 结论：重启后状态仍是「离线」且标记为"未核实"，手机端显示 ⏳ 正在获取… → 探针 100ms 内复核为 🔴 离线，
#       全程没有任何一刻显示🟢在线
```

### 新增/更新文件

| 文件 | 说明 |
|------|------|
| `probe.js` | **v7.3**：离线守护 + 连上即复核 + `REQUEST_IM_STATUS`/`POLICY` + 只认可见状态节点 + 重连永不放弃 + `__probe.config`/`refreshStatus` |
| `bridge_server.py` | **v7.3**：IM 状态落盘与恢复、`apply_im_status()`、`im_status_known`、`REQUEST_IM_STATUS` 转发、`POLICY` 下发、`/api/diag.im`、`/diag` IM 状态卡、手机端 H5 全量重做 |
| `config.json` | 新增 `keep_manual_offline`（默认 `true`，离线守护开关） |
| `h5_security_test.js` | 34 → **49** 项（含假 `<select>` 语义） |
| `probe_smoke_test.js` | 45 → **57** 项；默认读取 `probe.js`（不再需要 `probe_new.js`） |
| `diag_test.py` | 29 → **30** 项（`im` 区块） |
| `mobile_feature_test.py` | 20 → **23** 项（状态复核链路） |
| `README.md`（使用手册） | 6.4/6.5/6.6 改为下拉框说明、12.1/12.10 新增"离线被改回""状态不对"排查、接口表补 `REQUEST_IM_STATUS`/`POLICY`/`im.*` |

---

## 🔊 第十轮（2026/9/28）：F9 免框选 + 动作回执 + 提示音收敛

### 客服反馈（三条）

1. 「F9 智能回复好像没了」
2. 「手机端挂起/恢复没反应」
3. 「关单估计也有问题，问题类型的下拉选项我感觉 AI/程序搞不定」
4. 「电脑上点进以前的对话，或者把一个挂起的对话点了恢复，能不能不要冒出声音，这种不叫新消息」

### 根因与修复

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | F9 完全没反应（连报错音都没有） | **F9 热键由悬浮窗 `semi_runner.pyw` 注册**，而悬浮窗当时没在运行 → 按键无人接收 | 已重启悬浮窗；`launcher.bat` 会同时拉起中继 + 悬浮窗（关掉黑框窗口 = 关掉手机端服务） |
| 2 | F9 按了没反应/提示"未选中文本"，与手册写的"免框选"不符 | `on_f9()` 第一行就是 `safe_capture_selection()`，**没选字直接 return**（静默）；而且靠 `Ctrl+V` 粘贴，**回复框没聚焦就白填** | ① 没选字时**回退用当前工单聊天记录**（真免框选）；② 新增 `POST /api/fill_draft`，**探针直接把文案写进网页回复框**（不依赖焦点），失败才退回剪贴板；F10 同样处理 |
| 3 | 手机点「挂起」没反应 | 探针只在 `.im-action-btn` 里找**文本完全相等**的按钮，找不到就静默什么都不做；**根本没有"恢复"这个动作** | ① 关键字匹配（完全相等优先→包含），优先按钮类元素、再退 span/div，且只点短标签（不误点容器）；② 新增 `ACTION_RESUME`（恢复/接入/接单/继续）；③ 新增 `ACTION_RESULT` 回执 → 手机 toast + 中继日志 `[动作] ❌ 挂起：未找到「挂起」按钮，页面上的按钮：…` |
| 4 | 担心分类选错 | `selectCategoryByKeyword()` 匹配不上时 `target = list[0]`（**盲选第一项**）→ 分类被选错且无人知道 | 改为**宁可不选**并回报候选（`分类候选里没有匹配「X」的项；本层可选：…`）；同时支持扁平 `el-select` 分类；AI 的兜底分类由 `close_category_default` 显式提供，不再靠"第一项" |
| 5 | 点开旧会话/恢复挂起就"叮咚" | 响铃条件写的是 `最后一条是玩家发的` 或 `内容变长` → 打开历史、切会话、自己回复全都会响 | 改成：**同一会话 + 历史消息前缀完全一致 + 末尾真的多出玩家消息**才响；切会话/重排历史/自己回复一律不响 |

### 验证（全绿）

```
node probe_smoke_test.js   -> 69 通过 / 0 失败（新增：5 个响铃场景 + 挂起/恢复/关单回执 + 不盲选分类）
node h5_security_test.js   -> 56 通过 / 0 失败（新增：恢复按钮、ACTION_RESUME/ACTION_HANGUP 下发、失败原因 toast）
python hud_layout_test.py  -> 34 通过 / 0 失败（新增：F9 免框选/直填网页 源码级回归）
python diag_test.py        -> 30 通过 / 0 失败
python mobile_feature_test.py -> 27 通过 / 0 失败（新增：/api/fill_draft 直填回复框 + 空内容拒绝）
agent_core 16 / rules_sync 18 全通过
```

### 新增/更新文件

> 版本号说明：本轮 `probe.js` 与 `bridge_server.py` 内容都变了，版本号一起升到 **v7.4**
> （油猴里必须换成 v7.4，页面左下角胶囊显示 `🟢 探针 v7.4` 才算装好）。

| 文件 | 说明 |
|------|------|
| `probe.js` | 响铃判定改为"纯追加的玩家新消息"；`safeClickActionBtn` 关键字匹配 + `listActionButtons()`；新增 `ACTION_RESUME`/`LIST_ACTIONS`；所有动作回报 `ACTION_RESULT`；分类不再盲选 |
| `bridge_server.py` | 新增 `ACTION_RESULT` 转发（手机 toast + 控制台 `[动作]`）；`ACTION_REPLY_CLOSE` 带 `defaultCategory`；新增 `POST /api/fill_draft`；H5 操作栏新增「▶ 恢复」+ 挂起/恢复即时提示 |
| `semi_runner.pyw` | F9 免框选回退（`fetch_ticket_history`）、`fill_into_page()` 直填网页回复框、`safe_capture_selection(quiet=)`；F10 同步 |
| `probe_smoke_test.js` | 57 → **69** 项 |
| `h5_security_test.js` | 49 → **56** 项 |
| `hud_layout_test.py` | 31 → **34** 项 |
| `mobile_feature_test.py` | 23 → **27** 项 |
| `README.md`（使用手册） | 5.1/5.2/5.4 热键与提示音、6.3 挂起恢复、11.x 协议、12.1/12.12 排查、13.3 测试清单 |

---

## 📱 第十一轮（2026/9/28）：手机端界面修形 + 重复卡片 + 回复模式可关

### 客服反馈（附截图）

1. 「手机端左上角 UI 越界，信息不全」
2. 「玩家信息是什么？而且有重复的卡片」
3. 「电脑网页的"问题选择"下拉菜单老是自己跑下来」
4. 「手机端 IM 状态和回复模式的下拉菜单太丑了，还有框框和卡顿」
5. 「AI 自动起草和 AI 润色两个快捷功能也点不了，默认是打开状态关不掉」

### 根因与修复

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | 左上角品牌名被胶囊压住、越界 | `.brand-name` 只有 `white-space:nowrap`，**没有 overflow/text-overflow**，flex 子项也不收缩 | 品牌区改 `flex:1 1 92px; min-width:0; overflow:hidden` + 标题 `text-overflow:ellipsis`；顶栏 `flex-wrap` 允许换行 |
| 2 | 所有卡片都叫「玩家信息」 | 右侧面板第一行是**栏目名**「玩家信息」，旧代码直接拿第一行当玩家名 | 新增 `parsePlayerIdentity()`：优先「昵称/角色名: XXX」，其次逐段挑（排除栏目名、`UID:` 这类 key:value），最后用 `玩家+UID后4位` 兜底 |
| 3 | 同一玩家出现**两张重复卡片** | 工单指纹含「首条玩家消息」；列表虚拟滚动/重渲染让首条消息变化 → 同一工单算出两个 gid | 指纹只用**玩家身份**（uid+name，绝不含聊天内容）；另加"沿用上次 gid"连续性（工单号时有时无也不分裂） |
| 4 | 聊天页"信息不全" | 面板字段没展示 | 聊天页新增**玩家信息卡**（默认 2 行，点一下展开全部）；卡片列表第二行显示 `UID …` |
| 5 | 电脑网页分类下拉**自己弹出来** | 探针每次连接都被要求 `REQUEST_CATEGORIES`，且 `peekCategoryOptions()` 会**点开**分类下拉去读选项 | 探针内置分类缓存（用户自己点开时顺手采集）；缓存命中/面板已开就不再点；读完**再点一下收起**；中继改为**只在没拿到过分类时**才请求 |
| 6 | 顶部下拉"丑、有框框、卡顿" | 上一版改成原生 `<select>`，iOS 上会带系统边框与原生弹层 | 改为**自绘胶囊按钮 + 底部选择面板**（真按钮 + 真按钮列表，无原生控件）；面板项带说明与 ✓ 当前档 |
| 7 | 「AI 自动起草关不掉」 | 半自动模式下每条玩家消息都会触发 `handle_ai_automation()` 自动起草，**没有开关** | 新增三档 `reply_mode`：`manual`（只提醒，**不起草**）/ `semi`（默认）/ `afk`；手机端下拉里可随时切 |

### 验证（全绿）

```
node probe_smoke_test.js   -> 79 通过 / 0 失败（新增：玩家名解析 / 标识稳定（重复卡片）/ 分类缓存）
node h5_security_test.js   -> 73 通过 / 0 失败（新增：自绘下拉+面板 / 三档模式 / 卡片 UID / 玩家信息卡）
python hud_layout_test.py  -> 34 通过 / 0 失败
python diag_test.py        -> 30 通过 / 0 失败
python mobile_feature_test.py -> 28 通过 / 0 失败（新增：已有分类缓存时不再请求分类）
agent_core 16 / rules_sync 18 全通过
```

### 新增/更新文件

| 文件 | 说明 |
|------|------|
| `probe.js` | `parsePlayerIdentity()` 玩家名/UID 解析；工单指纹去消息化（修重复卡片）；分类缓存 `categoryCache` + 不主动点开 + 读完收起 |
| `bridge_server.py` | H5 顶栏自绘下拉 + 底部面板；`convInfo`/玩家信息卡；三档 `reply_mode` + `SET_MODE`；`handle_ai_automation` 手动模式不起草；分类只在首次请求；H5 内嵌脚本**不使用任何反斜杠转义**（避免 Python 与测试语义不一致） |
| `h5_security_test.js` | 56 → **73** 项 |
| `probe_smoke_test.js` | 69 → **79** 项 |
| `mobile_feature_test.py` | 27 → **28** 项 |
| `README.md`（使用手册） | 6.2/6.3/6.5 界面与三档模式、12.1/12.13/12.14 排查、11.4 协议、13.3 测试清单 |

---

## ⏳ 第十二轮（2026/9/28）：自动回复节奏（不秒回）+ 表格无答案转人工

### 客服原话

> "F9 是不是读取当前会话玩家发送的内容，一键发给 ai？……**AI 不要回复那么快，设置 1-3 分钟的随机延迟**，
> 不然系统可能判定我使用脚本，玩家也要投诉没有人工客服。……**玩家发来消息后延迟 1-3 分钟，不发了再响应**，
> 确保他说完想说的话。玩家第一次发消息来时，需要**发送对应的开头语，严格使用表格的**，发送出去之后等待玩家发信息
> （有信息也等 1 分钟），没有新消息就回复。大部分回复内容需要和表格内对应，**首先要分析玩家问题能否在表格中找到**
> 对应的，找不到就发一个『亲爱的玩家，您的问题我已经收到啦，正在为您查询相关信息，请稍等片刻哦~』这样的，
> 然后**给我长报警（区分来消息的提示音，掉线也是长报警，也和这个区分）并把该会话置顶，玩家信息和问题总结一并复制给我**。"
> 程序需要自行判断是否把客服说的话加在玩家消息中间发给 ai。

### 交付内容

| # | 需求 | 实现 |
|---|------|------|
| 1 | 不要秒回，**1~3 分钟随机延迟**，玩家继续说话就重新计时 | `schedule_auto_reply()` + `_auto_reply_after()`：每条玩家消息都 `cancel + 重新排队`；延迟 = `random.uniform(min,max)`（`state.auto_delay_min/max`，来自 `config.json` 两个参数，可用 `SET_AUTO_DELAY` 动态改） |
| 2 | 只有"玩家说完"才回（不抢话、不重复回） | `should_auto_reply()`：最后一条必须是**玩家**发言，且该条 `ts > conv.last_reply_ts`（回过的就不再回；客服最后发言时绝不插话） |
| 3 | 首次发言**立刻发开场语，严格取自表格** | 会话首次收到玩家消息时 `pick_greeting()` → 从 `core.tpl_no_desc`（话术库「没有描述问题 / 直接转人工」那一类）随机取一条 → `SEND_REPLY` 真发出去；表格缺失才用兜底句。实测发的是表格里的 `亲爱的玩家，很高兴为您服务~` |
| 4 | 表格里找不到答案 → 安抚 + **长报警** + **置顶** + **复制信息** | `send_hold_and_alert()`：① 发 `HOLD_TEXT`（客户指定原话）② Bark + 手机 `HUMAN_ALERT`（专属三声）③ `conv.pinned=True` ④ 入队 `state.human_alerts`，桌面 HUD 轮询 `/api/diag.alerts` → **三短一长长报警** + `pyperclip.copy(玩家信息+问题总结+处理建议)` |
| 5 | 三种提示音必须能区分 | ① 新消息"叮咚"（两个正弦音）② 掉线连续警笛（锯齿波循环 + 手机全屏红层）③ **需要人工：三短一长**（手机三声方波 880Hz；电脑 700Hz×3 + 500Hz 长音） |
| 6 | 程序自行判断是否把客服的话带进 AI 上下文 | `build_chat_history_str(..., for_ai=True)`：最后一条是客服 → 返回空（不回）；**丢掉"玩家开口之前"的客服发言**；玩家之后的客服发言只留最近 2 条（省 token） |
| 7 | （顺带）AI 自动起草能关掉 | 三档回复模式 `manual/semi/afk`（第十一轮已加，本轮延续） |

### 验证（全绿）

```
python auto_reply_test.py   -> 21 通过 / 0 失败（新增：延迟区间随机、不抢话/不重复回、开场语来自表格、上下文裁剪、安抚话术）
python mobile_feature_test.py -> 36 通过 / 0 失败（新增：首条消息立刻发开场语、排队 1~3 分钟、继续发言重新计时、定时器到期出队、/api/alerts）
node probe_smoke_test.js    -> 79 通过 / 0 失败
node h5_security_test.js    -> 84 通过 / 0 失败（新增：需人工横幅/专属音/置顶排序/ACK_ALERT）
python diag_test.py 30 / hud_layout 34 / agent_core 16 / rules_sync 18 全通过
```

### 新增/更新文件

| 文件 | 说明 |
|------|------|
| `bridge_server.py` | 延迟调度（`_PENDING` 任务表）、`pick_greeting/pick_reply_delay/should_auto_reply`、`send_hold_and_alert`、`build_chat_history_str(for_ai)`、`SET_AUTO_DELAY`/`ACK_ALERT` 动作、`/api/alerts` + `/api/alerts/ack`、`/api/diag.auto_reply/alerts`、手机端"需人工"横幅与专属提示音、置顶排序 |
| `semi_runner.pyw` | `long_human_alarm()`（三短一长）+ `_handle_human_alerts()`（长报警 + 自动复制玩家信息&问题总结 + 自动 ack） |
| `auto_reply_test.py` | **新增**：自动回复节奏/开场语/上下文裁剪 21 项 |
| `config.json` | 新增 `auto_reply_delay_min_sec=60`、`auto_reply_delay_max_sec=180`、`auto_send_greeting=true` |
| `mobile_feature_test.py` | 28 → **36** 项 |
| `h5_security_test.js` | 73 → **84** 项 |
| `README.md`（使用手册） | 新增 6.6 自动回复节奏 / 6.7 三种提示音 / 12.15 排查；配置表、协议表、诊断字段、测试清单同步 |

---

## 🔘 第十三轮（2026/9/28）：电脑小窗加自动化开关（冲突时以手机端为准）

### 客服原话

> "给电脑端小窗也加个自动化开关，但是有冲突的时候以手机端为准，默认情况都是半自动，放在输入框不发送。"

### 交付内容

| 需求 | 实现 |
|------|------|
| 电脑小窗（HUD）加自动化开关 | 悬浮窗标题栏新增按钮 `🤖半自动 / 🚀AFK / ✋手动`，**点一下循环切换**（`cycle_mode()`）；切换结果显示在状态行 |
| **默认半自动**，只把草稿填进输入框**不发送** | 模式初值 `semi`（`auto_draft=True` + `afk_mode=False`）→ AI 只 `FILL_DRAFT` 到网页回复框；**AFK** 才 `SEND_REPLY` 真发 |
| **冲突时以手机端为准** | 中继是唯一权威：`apply_reply_mode(mode, source)` 记录 `mode_owner`（mobile/desktop/default）与 `mode_ts`。**手机端刚设过（默认 600 秒 = `mobile_mode_priority_sec`）时，小窗的切换被拒绝**并回执提示「手机端已设为…，以手机端为准（约 N 秒后可再切）」；窗口过后小窗可自由切 |
| 小窗不与手机打架 | 小窗**不保存自己的模式副本**，每 5 秒从 `/api/diag.auto_reply` 同步显示（`_sync_mode_ui`），带 `·手机` 后缀标识归属 |
| 重启不丢 | 模式落盘 `mode_state.json`（含 owner/ts），中继重启后恢复，**不会被"小窗启动"顶回默认值** |

### 验证（全绿）

```
python auto_reply_test.py   -> 33 通过 / 0 失败（新增：默认半自动/不发送、手机端优先拒绝小窗、窗口过期后小窗可切、非法模式拒绝）
python mobile_feature_test.py -> 40 通过 / 0 失败（新增：手机端切 AFK 归属 mobile、小窗切换被拒、手机端切回半自动）
python hud_layout_test.py        -> 39 通过 / 0 失败（新增：开关按钮/循环顺序/走 /api/mode/每 5 秒同步/被拒提示）
node probe_smoke_test.js 79 / h5_security 84 / diag 30 / agent_core 16 / rules_sync 18 全通过
```

### 新增/更新文件

| 文件 | 说明 |
|------|------|
| `semi_runner.pyw` | `btn_mode` 自动化开关按钮 + `cycle_mode()` + `_push_mode()`（POST /api/mode）+ `_sync_mode_ui()`（跟随中继显示，带"·手机"标识） |
| `bridge_server.py` | `apply_reply_mode()`（手机端优先规则）、`load/save_mode_state()`（落盘 `mode_state.json`）、`POST /api/mode`、`/api/diag.auto_reply.mode/mode_owner/mode_label/mobile_priority_sec`；`SET_MODE`/`TOGGLE_AFK` 统一走 `apply_reply_mode` |
| `auto_reply_test.py` | 21 → **33** 项 |
| `mobile_feature_test.py` | 36 → **40** 项 |
| `hud_layout_test.py` | 34 → **39** 项 |
| `config.json` | 新增 `mobile_mode_priority_sec=600` |
| `.gitignore` | 忽略运行期文件 `mode_state.json` |
| `README.md`（使用手册） | 5.3 悬浮窗加"自动化开关"说明与示意图、6.5 冲突规则、配置表、接口表、12.1 排查、13.3 测试清单 |

---

## 🚦 第十四轮（2026/9/28）：话术出站安全闸（内部群/补偿/承诺 绝不进玩家对话框）

### 客服要求

> "AI 回复到与玩家的对话框吗？**切记不可出现内部群、补偿、承诺等话语。**"

### 这一轮的澄清 + 加固

**1) 逻辑澄清（原实现的问题）**
- AI 判断"表格里没答案"时，会在回复**第一行**输出 `【规章库未收录，请上报内部群核实】`（给系统看的标记），
  第二行起才是给玩家的话术；`agent_core` 会把这个标记归一化，`bridge_server` 据此判定"疑难单"。
- **旧文档写"AFK 模式下会自动挂起 + 推送手机"** —— 这是过时描述；第十二轮已改为
  "发安抚话术 + 长报警 + 置顶 + 复制玩家信息&总结"，**不再自动挂起**（本轮把文档一并改正）。
- **关键风险**：标记本身就含"内部群"三个字；而旧代码只有几条轻量正则（只挡 `群内客服/内部群/补偿您`），
  `承诺/保证/赔偿/补发/群聊/上报/时间承诺` 等**都可能漏到玩家**。

**2) 加固措施：出站安全闸（唯一出口）**
- `agent_core` 新增 `OUTBOUND_RULES` + `sanitize_outbound()` + `find_forbidden()`：
  - **整块删除**：`【…规章库未收录/内部群/上报群聊/按F9…】` 这类内部提示
  - **改写**：`内部群/群内客服/群聊/上报/报备群` → `专人核实处理`；`补偿/赔偿/补发` → `为您记录并跟进`（**连同紧跟的金额**，如"补偿您100钻石"整段换掉）；`承诺…/保证…/一定会修复/48小时内修复/马上修复` → 中性话术；`BUG/漏洞/程序错误` → `异常情况`；`F6~F10` → `对应功能`
  - **复核**：`find_forbidden()` 逐词复查，调用方可据此兜底
- `bridge_server` 新增 `safe_outbound()` + `send_to_player()`：**所有发给电脑端探针、会落到玩家对话框的指令
  都必须走 `send_to_player`**，并在中继窗口打印 `[安全闸]` 清洗日志。覆盖：AFK 自动回复 / 超时关单 /
  半自动草稿 / 开场语 / 安抚话术 / F9·F10 直填（`/api/fill_draft`）/ 手机端代发与关单（`SEND_REPLY`、`EXT_COMMAND`）
- **清洗后为空 = 只有内部提示** → 不走发送，直接转人工（发安抚话术 + 长报警）；`/api/fill_draft` 返回 400 并说明原因

**3) 验证（全绿）**
```
python auto_reply_test.py    -> 44 通过 / 0 失败（新增第 7 节：清洗后零禁词、内部提示整块删除、
                                补偿金额一并抹掉、BUG/F9 改写、安抚话术与表格开场语零禁词、纯标记会被清空）
python mobile_feature_test.py -> 44 通过 / 0 失败（新增第 8 节：/api/fill_draft 下发的是清洗后文本、
                                纯内部提示被拦 400、手机端代发也被改写）
node probe_smoke 79 / h5_security 84 / hud_layout 39 / diag 30 / agent_core 16 / rules_sync 18 全通过
```

### 新增/更新文件

| 文件 | 说明 |
|------|------|
| `agent_core.py` | `OUTBOUND_RULES` / `FORBIDDEN_OUTBOUND` / `sanitize_outbound()` / `find_forbidden()`（出站禁词表与改写规则） |
| `bridge_server.py` | `safe_outbound()` + `send_to_player()` 安全闸；AFK/草稿/开场语/安抚话术/F9直填/手机代发 全部接入；顺手修掉 `push_bark` 被误改 async 的隐患 |
| `README.md` | 5.2 改正"规章未收录"的实际行为（内部标记永不发给玩家、不自动挂起）；新增 **5.5 话术安全闸**（禁词表 + 闸门位置 + 自查方法）；6.6 表格同步 |
| `auto_reply_test.py` | 33 → **44** 项 |
| `mobile_feature_test.py` | 40 → **44** 项 |

---

## 🕶️ 第十五轮（2026/9/28）：对外发布改造 —— 品牌/内部词汇全部隐去（仓库干净，本机话术不变）

### 需求

> "推送新版 git，**把内部品牌词、玩家尊称这类词汇都隐去**。"

### 为什么不能直接在代码里删掉这些词

这些词是**运行时话术**（"亲爱的×××，您的问题我已经收到啦…"、AI 提示词里的游戏名/称谓）。
直接删改会让**你本机实际发给玩家的话**跟着变 —— 属于"为了仓库干净而牺牲生产"。
所以做法是：**源码里只留通用词，真实值放本地 `config.json`（不入库）**。

### 1) 新增品牌配置层（`brand`）

`agent_core.py` 里新增 `BRAND` / `HONORIFIC`，默认值全是通用词，启动时被 `config.json` 的 `brand` 段覆盖：

| 键 | 源码里的默认（仓库可见） | 你的真实值（只在你本机 config.json） |
|----|--------------------------|--------------------------------------|
| `company` | `示例公司` | 公司名 |
| `product` | `客服助手` | 产品名 |
| `game` | `本游戏` | 游戏名 |
| `im` | `IM 系统` | IM 系统名 |
| `honorific` | `亲爱的玩家` | 玩家统一尊称 |

生效范围：AI 提示词（系统/关单/润色三处）、开场语兜底句、安抚话术 `HOLD_TEXT`、手机端 H5 标题、
自检页标题、中继日志里的专线名。`bridge_server` 从 `agent_core` 复用同一份 `BRAND`，避免两处不一致。

### 2) 个人补丁库改为"本地私有优先"

- 新增 `patch_rules.local.txt`（**不入库**，`.gitignore` 已忽略）；`get_patch_rules()` 按
  `patch_rules.local.txt` > `patch_rules.txt` 顺序取用；
- 仓库里的 `patch_rules.txt` 换成**通用示例**（无内部称谓、无内部流程细节）。

### 3) 从仓库撤出的内部资料（文件仍留在你本机）

| 文件 | 原因 |
|------|------|
| 规章库 xlsx（`local_excel_path` 指向的那个） | 内部话术/规则数据，`.gitignore` 已忽略 `*.xlsx` |
| 内部搭建笔记 5 个 md（规章同步指南 / Git 配置 / 打包清理 等） | 含内部域名与流程细节，属内部资料 |

代码侧配套：规章库定位改为 `local_excel_path` > `rules.xlsx` > 旧文件名（兼容），
配置键 `rules_sync_url` 优先、**旧键名用拼接方式兼容读取**（老 `config.json` 不用改也能继续用）。

### 4) 顺手修掉一个真实行为问题（多会话测试暴露）

探针若推来**空会话快照**（`messages` 为空），旧代码照样走"首次消息→发开场语"分支，
会出现**玩家一句话没说却先收到开场语**的诡异现象。现在加了护栏：
`has_player_msg`（快照里确实有玩家发言）才发开场语。
另外 `/api/diag.auto_reply.pending_gids` 从前 10 条放宽到前 50 条（排障/测试不再"看不到新排队会话"）。

### 5) 验证（全绿）

```
agent_core 16 · auto_reply 44 · mobile_feature 44 · multi_conv 通过（此前因开场语特性失配）
diag 30 · hud_layout 39 · rules_sync 18 · probe_smoke 79 · h5_security 84
```
- 本机实测：`HOLD_TEXT` = 配置里的真实话术（"…您的问题我已经收到啦…"）→ **生产话术零变化**；
- 仓库扫描：`玩家尊称 / IM 系统名 / 游戏名 / 公司名 / 平台名` 在**源码与文档中均已 0 命中**，
  唯一保留的是探针 `@match` 域名（油猴脚本必须精确匹配工单域名才能注入，删了就不工作）；
- 历史提交同样做了一次清洗（品牌词替换 + 内部资料从所有历史里移除）。

### 6) 探针命中域名不再写进仓库（仓库留占位 + 本机注入）

`probe.js` 的 `@match` 必须是**精确的工作台域名**才能注入页面，所以它没法直接删。做法：

- 仓库里的 `probe.js` 只放占位域名 `*://ticket.example.com/*`；
- 真实域名写在本地 `config.json` 的 `workbench_domains`（可填多个）；
- 中继新增 `render_probe_js()`：响应 `/probe.js` 时把占位行替换成真实域名（多个会自动展开成多行 `@match`）。

⇒ 你从 `http://127.0.0.1:8765/probe.js` 复制粘贴到油猴，拿到的永远是**能用的版本**；仓库侧永远只有占位域名。
**注意：以后要从这个地址复制脚本，不要再从项目目录里的 `probe.js` 复制。**

### 7) 测试稳定性（多客户端 + 旧帧）

- IM 状态断言改为"主动补拉快照"取值（广播队列里可能残留上一步/刚连接时的旧帧，取"最后一帧"会误判）；
- 检测到**另有真实探针在线**（客服自己开着的工单页会如实上报网页状态）时，跳过 IM 状态值断言、
  只核对指令已下发 —— 与既有的 `extension_online` 多客户端处理保持一致；离线时仍严格断言；
- 测试收尾把 IM 状态复位为在线，避免给真实使用留下"离线/忙碌"。

---

## 🧹 第十六轮（2026/9/29）：工程整理 —— 发布流程沉淀成 Skill + 根目录瘦身 + 启动方式明确

### 1) 发布流程沉淀为可复用 Skill

把第十五轮"仓库脱敏 + 历史清洗 + 推送核验"的完整流程写成 Cline Skill（三层：

| 位置 | 用途 |
|------|------|
| `.cline/skills/repo-sanitize-publish/SKILL.md` | Cline 原生格式，本对话里可自动/斜杠调用（`/repo-sanitize-publish`） |
| `repo-sanitize-publish.skill` | **单文件版**（内容含 SKILL.md + 两个脚本 + 坑位清单），方便拷贝/分享 |
| `~/.cline/skills/repo-sanitize-publish/` | 全局安装，**任何项目**都能用 |

随附两个可直接跑的脚本：
- `scripts/scan_internal_words.py`：扫工作区入库文件 + **文件名** + 指定远端的敏感词命中（比 `git log -S` 可靠）；
- `scripts/scrub_history.py`：同一张替换表既用于工作区批量替换，也可作为
  `git filter-branch --tree-filter` 的执行体清洗全历史（含 GBK `.bat` 与点文件处理）。

`docs/pitfalls.md` 记录 11 个实战坑：PowerShell 引号地狱、CRLF 混行导致编辑器匹配失败、
扩展名白名单漏点文件、`git log -S` 不等于 tree 命中、`filter-branch` 会回写工作区（曾连带改坏探针域名）、
测试"翻广播队列取最后一帧"必翻车、真实探针在线导致状态类断言偶发失败等。

### 2) 根目录瘦身

| 处理 | 对象 | 说明 |
|------|------|------|
| 删除 | `build/`、`dist/`、`__pycache__/` | 打包/编译中间产物，可重建 |
| 移出仓库 | `launcher.spec` | PyInstaller 自动生成，`一键打包清理.bat` 本来就会删它 |
| 移入 `_internal/` | 5 份内部文档（规章同步/ Git / 打包说明） | 根目录清爽，内容仍在本机（如需彻底删除可随时删） |
| 改名 | 旧的启动 vbs（**文件名里带品牌词**）→ **`启动服务.vbs`** | ① 去掉品牌词（文件名也算泄漏面）；② 内容升级为**一次启动两个服务**（中继带窗口 + 悬浮窗静默） |
| 保留 | `launcher.exe`、`launcher.bat`、`Ironman.ico/jpg`、`token_leak_test.py` | 都是仍在用的本机工具/测试 |

### 3) 启动方式写入文档（README 4.1 改成四种方式）

1. 双击 `launcher.exe`（最省事）
2. 双击 `启动服务.vbs`（中继带窗口、悬浮窗静默）
3. 双击 `launcher.bat`（本机自带、未入库）
4. 命令行：`python launcher.py`，或分两步 `python bridge_server.py` + `pythonw semi_runner.pyw`

> 说明：`launcher.bat` 是**未入库**的本机脚本，公开仓库的新用户按方式 4 走；`launcher.spec` 已不入库，
> 打包命令统一为 `pyinstaller --onefile --icon="Ironman.ico" launcher.py`。

### 4) 验证

```
python .cline/skills/repo-sanitize-publish/scripts/scan_internal_words.py
  -> 入库文件数：27 · 工作区结论：CLEAN（文件名也无命中）
9 套回归测试全绿：agent_core 16 / auto_reply 44 / mobile_feature 44 / multi_conv 通过 /
diag 30 / hud_layout 39 / rules_sync 18 / probe_smoke 79 / h5_security 84（+ token_leak 无泄露）
```

---

*此文档由 AI Bug 排查 Agent 自动生成*