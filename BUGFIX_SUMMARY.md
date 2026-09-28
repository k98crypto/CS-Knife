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
*此文档由 AI Bug 排查 Agent 自动生成*