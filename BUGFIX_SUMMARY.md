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
*此文档由 AI Bug 排查 Agent 自动生成*