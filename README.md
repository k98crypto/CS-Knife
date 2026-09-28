# 🚀 智能客服自动化系统

> **客服助手** - 基于 AI 的游戏智能客服自动化解决方案

## 📋 项目简介

这是一个游戏智能客服自动化系统，通过 AI 辅助客服快速回复玩家工单，支持全自动/半自动两种模式，集成表格规章库、公关敏感词过滤与 Bark 推送通知。

## 🏗️ 系统架构

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│   probe.js      │◄────►│  bridge_server.py │◄────►│  agent_core.py  │
│  (油猴探针)     │ WebSocket │  (中继服务)    │  AI   │  (业务大脑)     │
└─────────────────┘      └──────────────────┘      └─────────────────┘
         │                        │                         │
         │                        │                         │
         ▼                        ▼                         ▼
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  IM 工作台网页  │      │  手机 H5 端      │      │  表格规章库    │
│  (DOM 抓取)      │      │  (监控/控制)     │      │  (Excel)        │
└─────────────────┘      └──────────────────┘      └─────────────────┘
```

### 核心模块

| 模块 | 文件 | 功能 |
|------|------|------|
| **探针层** | `probe.js` | 油猴脚本，抓取 DOM、注入请求头、执行 AI 回复 |
| **中继层** | `bridge_server.py` | WebSocket+HTTP 服务，连接探针与 AI 核心 |
| **大脑层** | `agent_core.py` | 规章库匹配、AI 决策生成、公关话术润色 |
| **交互层** | `semi_runner.pyw` | 桌面 HUD 悬浮窗，全局热键（F6/F7/F9/F10） |
| **启动器** | `launcher.py` | 双进程一键启动 |

## ✨ 核心功能

### AI 自动化
- **智能工单分析**: 自动识别玩家问题，匹配表格规章库
- **公关话术润色**: 敏感词过滤，统一称谓（"亲爱的玩家"）
- **多标签判定**: `GREETING` / `NEED_INFO` / `NORMAL` / `WAITING` / `TIMEOUT_CLOSE`

### 热键操作
| 热键 | 功能 | 说明 |
|------|------|------|
| **F6** | 下载媒体 | 提取选中图片/视频链接到桌面 |
| **F7** | 工单提炼 | 后台提取工单并 AI 总结 |
| **F9** | 智能解答 | AI 分析工单并填入回复（免框选） |
| **F10** | 润色草稿 | 将粗略草稿润色为官方话术 |

### 移动端支持
- **手机 H5 监控**: iPhone 访问中继服务查看工单状态
- **Bark 推送**: 疑难单、异常掉线实时通知
- **AFK 模式**: 全自动托管，AI 直接回复

## 🔧 快速开始

### 环境要求
- Python 3.8+
- Node.js（可选，用于油猴脚本开发）
- Tampermonkey 浏览器扩展

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置 API Key

编辑 `config.json`：

```json
{
  "port": 8765,
  "bark_key": "YOUR_BARK_KEY",
  "deepseek_api_key": "sk-YOUR_API_KEY",
  "rules_sync_url": "https://example.com/api/export_rules_csv"
}
```

> ⚠️ **安全提示**: `config.json` 已被 `.gitignore` 忽略，请勿上传敏感信息！

### 启动系统

**方式一：一键启动器（推荐）**
```bash
python launcher.py
# 或双击 launcher.bat
```

**方式二：分步启动**
```bash
# 1. 启动中继服务
python bridge_server.py

# 2. 启动桌面热键悬浮窗
pythonw semi_runner.pyw
```

### 安装油猴脚本

1. 打开 Tampermonkey 管理面板
2. 点击"创建新脚本"
3. 复制 `probe.js` 完整内容
4. 保存并启用

### 访问手机 H5

启动后，在手机浏览器访问：
```
http://[你的电脑IP]:8765
```

## 📁 项目结构

```
CS-Knife/
├── probe.js                 # 油猴探针脚本
├── bridge_server.py         # WebSocket 中继服务
├── agent_core.py            # AI 业务核心
├── semi_runner.pyw          # 桌面 HUD 悬浮窗
├── launcher.py              # 一键启动器
├── config.json              # 配置文件（已忽略）
├── .gitignore               # Git 忽略规则
├── .gitattributes           # Git 属性配置
├── requirements.txt         # Python 依赖
├── README.md                # 项目说明
├── BUGFIX_SUMMARY.md        # Bug 修复日志
├── rules.xlsx        # 表格规章库
├── patch_rules.txt          # 个人补丁库
└── Ironman.ico              # 图标资源
```

## 🔒 安全配置

### 敏感文件保护

以下文件已被 `.gitignore` 自动忽略：
- ✅ `config.json`（包含 API Key）
- ✅ `*.log`（日志文件）
- ✅ `__pycache__/`（Python 缓存）
- ✅ `build/`, `dist/`（打包输出）

### 创建 config.json 模板

```bash
cp config.json config.json.template
# 然后编辑 config.json.template，移除真实 API Key
```

## 🛠️ 开发与调试

### 本地测试

```bash
# 语法检查
python -m py_compile bridge_server.py
python -m py_compile agent_core.py

# 导入测试
python -c "from agent_core import CustomerServiceCore; print('✅ 加载成功')"
```

### 打包为 EXE

```bash
pyinstaller launcher.spec
```

### 油猴脚本调试

1. 打开浏览器开发者工具
2. 在 Console 中查看 `[探针]` 日志
3. 在 Network 中查看 WebSocket 连接

## 📊 WebSocket 协议

### 上行事件（探针 → 中继）
| 事件 | 说明 | 数据格式 |
|------|------|---------|
| `HEADERS_SYNC` | Token 注入 | `{event: "HEADERS_SYNC", data: {...}}` |
| `PLAYER_MESSAGE` | 玩家消息 | `{event: "PLAYER_MESSAGE", data: {groupID, messages, playerInfo}}` |
| `ABNORMAL_OFFLINE` | 异常掉线 | `{event: "ABNORMAL_OFFLINE"}` |
| `ALARM_RECOVERED` | 警报恢复 | `{event: "ALARM_RECOVERED"}` |

### 下行命令（中继 → 探针）
| 命令 | 说明 | 参数 |
|------|------|------|
| `FILL_DRAFT` | 填入草稿 | `content`, `category` |
| `SEND_REPLY` | 发送回复 | `content`, `groupID` |
| `ACTION_REPLY_CLOSE` | 回复并关单 | `content`, `category`, `groupID` |
| `ACTION_HANGUP` | 挂起工单 | `groupID` |
| `CHANGE_STATUS` | 切换 IM 状态 | `status` (1/2/3) |
| `SILENCE_ALARM` | 静音警报 | - |
| `ALARM_CONFIRMED` | 警报确认 | - |

## 🐛 已知问题

详见 [BUGFIX_SUMMARY.md](BUGFIX_SUMMARY.md)

## 📝 更新日志

### v7.0 (2026/9/28)
- 🔧 修复路径解析问题（打包后无法读取 config.json）
- 🔧 修复 WebSocket 重连内存泄漏
- 🔧 修复 API Key 硬编码安全问题
- 🔧 新增 WebSocket 双向确认机制
- 🔧 新增会话数量限制（防止无限膨胀）
- 🆕 新增 `AI_STATUS` 推送（手机端显示 WAITING 状态）

## 🤝 贡献指南

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

## 📄 许可证

本项目仅供内部使用，未开源。

## 📧 联系方式

- 内部文档：表格知识库
- 技术支持：内部技术群

---

**最后更新**: 2026/9/28  
**维护者**: 项目作者