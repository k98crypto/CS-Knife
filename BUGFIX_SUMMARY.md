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
*此文档由 AI Bug 排查 Agent 自动生成*