import os
import sys
import json
import time
import asyncio
import socket
import urllib.request
import urllib.parse
from datetime import datetime
from aiohttp import web

# ==================== 控制台输出健壮性（防止编码问题导致服务崩溃） ====================
# Windows 上若 stdout 被重定向到文件/管道/后台任务，Python 会用 locale 编码（GBK）输出，
# 遇到非 BMP 字符（如 🚀）会抛 UnicodeEncodeError，直接让服务进程退出。
# 这里把 errors 改为 replace，保证任何输出环境下都不会因编码问题中断服务。
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is not None:
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

try:
    import rules_sync                     # 规章库自动同步（监听目录 / 直链拉取）
except Exception:
    rules_sync = None
    print("[警告] rules_sync 模块加载失败，规章库自动同步将不可用")
# ==================== 路径解析增强（修复打包后__file__失效问题） ====================
def get_real_base_dir():
    """获取脚本/打包后的真实物理路径，免疫 PyInstaller 虚拟环境"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_real_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f: config = json.load(f)
except Exception:
    config = {"port": 8765, "bark_key": "YOUR_BARK_KEY"}

try:
    from agent_core import CustomerServiceCore
    core = CustomerServiceCore()
except ImportError:
    print("[错误] 无法导入 agent_core.py，服务无法启动")
    sys.exit(1)

BARK_SERVER_URL = f"https://api.day.app/{config.get('bark_key', 'YOUR_BARK_KEY')}"
PORT = config.get("port", 8765)

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception: return "127.0.0.1"

state = {
    "afk_mode": False,
    "alarm_status": False,
    "im_status": 1,                 # 1=IM在线 2=IM忙碌 3=IM离线（由探针上报真实值）
    "extension_online": False,      # 电脑端探针是否在线（决定手机端能否远程操作）
    "category_options": [],         # 从网页级联选择器抓到的真实问题分类（供 AI 选分类）
    "companies": { "main": { "name": "示例专线", "status": 1, "conversations": {} } }
}
active_clients = {"extension": set(), "mobile": set()}

# ==================== 探针认证头独立存放（安全隔离） ====================
# 注意：绝对不能放进 state！
# state 会被 FULL_SYNC 全量广播给手机端（共 10 处），放入 state 等于把 IM Token 泄露到手机浏览器。
# 本变量仅供服务端内部使用，永不参与任何序列化/广播。
IM_AUTH_HEADERS = {}
_IM_AUTH_FP = {"value": None}   # 上一次认证头的指纹，用于去重，避免重复覆盖与日志刷屏

# 最近一次有消息活动的工单 ID（供 /api/ticket 定位"当前工单"，比按插入顺序取最后一个更准）
_LAST_ACTIVE = {"gid": None}


async def safe_send(client, payload):
    """向单个客户端发送；失败（对端已断开）时静默忽略并剔除该连接。

    避免一个失效连接抛异常打断整个广播循环，导致其他客户端收不到更新。
    """
    try:
        await client.send_json(payload)
    except Exception:
        for _group in active_clients.values():
            _group.discard(client)


def push_bark(title, body, group_id=""):
    """推送 Bark 通知。

    group_id: 可选，用于在正文尾部标注来源（如工单标识），便于在通知列表区分。
    """
    if not BARK_SERVER_URL or "YOUR_BARK_KEY" in BARK_SERVER_URL: return
    if group_id:
        body = f"{body} [{group_id}]"
    def _run():
        try:
            target = (f"{BARK_SERVER_URL.rstrip('/')}/"
                      f"{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
                      f"?group={urllib.parse.quote('客服')}&sound=chime&isArchive=1")
            urllib.request.urlopen(urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"}), timeout=3)
        except Exception: pass
    # 修复 BUG-009：事件循环可能已关闭时改用线程池
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.run_in_executor(None, _run)
        else:
            _run()
    except RuntimeError:
        # 事件循环不存在时同步执行
        _run()

def build_chat_history_str(group_id: str) -> str:
    msgs = state["companies"]["main"]["conversations"].get(group_id, {}).get("msgs", [])
    if not isinstance(msgs, list):
        return ""
    recent_msgs = msgs[-8:] if len(msgs) > 8 else msgs
    lines = []
    for m in recent_msgs:
        if not isinstance(m, dict):
            continue
        text = m.get("text", "")
        if not text:
            continue
        lines.append(f"{'【玩家】' if m.get('sender') == 'player' else '【客服】'} {text}")
    return "\n".join(lines)

async def handle_ai_automation(group_id: str):
    history_str = build_chat_history_str(group_id)
    if not history_str: return

    try:
        tag, reply = await asyncio.get_event_loop().run_in_executor(None, core.process_ticket_f9, history_str)
    except Exception: return

    if tag == "WAITING":
        # 新增：向手机端推送 WAITING 状态（修复 BUG-020）
        for m in list(active_clients["mobile"]):
            await safe_send(m, {"type": "AI_STATUS", "groupID": group_id, "status": "waiting", "message": "等待玩家回复中"})
        return

    if "【规章库未收录" in reply:
        if state["afk_mode"]:
            for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "ACTION_HANGUP", "groupID": group_id})
            push_bark("🚨 疑难单等待接入", "请在手机端介入处理", group_id)
        else:
            for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "FILL_DRAFT", "content": reply})
        return

    if reply:
        now_str = datetime.now().strftime("%H:%M:%S")
        if state["afk_mode"]:
            conv = state["companies"]["main"]["conversations"].setdefault(
                group_id, {"name": group_id, "msgs": []})
            if not isinstance(conv.get("msgs"), list):
                conv["msgs"] = []
            conv["msgs"].append({"sender": "agent", "text": reply, "time": now_str,
                                 "ts": int(time.time() * 1000)})
            conv["updatedAt"] = int(time.time() * 1000)
            _LAST_ACTIVE["gid"] = group_id
            for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})

            if "TIMEOUT_CLOSE" in tag:
                for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "ACTION_REPLY_CLOSE", "category": "其他", "content": reply, "groupID": group_id})
            else:
                for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "SEND_REPLY", "content": reply, "groupID": group_id})
        else:
            # 半自动模式：草稿推到网页与手机输入框
            for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "FILL_DRAFT", "content": reply, "category": "其他"})
            for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FILL_DRAFT", "content": reply})


# ================= 手机端 H5 界面 =================
HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>Agent Workspace</title>
  <style>
    :root { --bg: #131314; --card-bg: #1E1F20; --line: #2C2D2F; --text-primary: #E3E3E3; --text-secondary: #9AA0A6; --accent: #A8C7FA; --safe-top: env(safe-area-inset-top, 0px); --safe-bottom: env(safe-area-inset-bottom, 0px); }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    html, body { height: 100%; }
    body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--text-primary); -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; overflow: hidden; }
    #app { position: fixed; left: 0; right: 0; top: 0; bottom: 0; display: flex; flex-direction: column; overflow: hidden; }
    @supports (height: 100dvh) { #app { height: 100dvh; } }
    header { flex: 0 0 auto; padding: calc(var(--safe-top) + 10px) 16px 12px; display: flex; justify-content: space-between; align-items: center; background: var(--bg); border-bottom: 1px solid var(--line); }
    .page-title { font-size: 19px; font-weight: 600; letter-spacing: .2px; }
    .afk-toggle { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700; color: var(--text-secondary); background: #2A2B2D; padding: 6px 12px; border-radius: 999px; user-select: none; border: 1px solid transparent; font-family: inherit; line-height: 1.45; cursor: pointer; -webkit-appearance: none; appearance: none; }
    .afk-toggle:active { opacity: .7; }
    .afk-toggle.active { background: rgba(168, 199, 250, 0.18); color: var(--accent); border-color: var(--accent); }
    .hdr-right { display: flex; align-items: center; gap: 6px; }
    .status-menu { position: absolute; right: 14px; top: calc(var(--safe-top) + 46px); min-width: 136px; z-index: 200;
                   background: var(--card-bg); border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
                   display: none; box-shadow: 0 8px 24px rgba(0,0,0,.5); }
    .status-menu.show { display: block; }
    .status-item { display: block; width: 100%; text-align: left; font-family: inherit; background: transparent; border: none; border-bottom: 1px solid var(--line); padding: 11px 16px; font-size: 14px; color: var(--text-primary); cursor: pointer; -webkit-appearance: none; appearance: none; }
    .status-item:last-child { border-bottom: none; }
    .status-item:active { background: #2A2B2D; }
    #toast { position: fixed; left: 0; right: 0; bottom: calc(var(--safe-bottom) + 84px); margin: 0 auto;
             width: fit-content; max-width: 86%; text-align: center; z-index: 300;
             background: rgba(0,0,0,.86); color: #fff; font-size: 13px; padding: 9px 16px; border-radius: 999px;
             opacity: 0; pointer-events: none; transition: opacity .2s; }
    #toast.show { opacity: 1; }
    
    .view-container { flex: 1 1 auto; min-height: 0; position: relative; }
    .view { display: none; position: absolute; left: 0; right: 0; top: 0; bottom: 0; flex-direction: column; background: var(--bg); }
    .view.active { display: flex; }
    .conv-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior: contain; touch-action: pan-y; padding-bottom: calc(var(--safe-bottom) + 12px); }
    .conv-card { display: flex; gap: 12px; align-items: center; padding: 12px 16px; background: var(--bg); border-bottom: 1px solid var(--line); cursor: pointer; }
    .conv-card:active { background: #1B1C1E; }
    .avatar { flex: 0 0 auto; width: 44px; height: 44px; border-radius: 50%; background: linear-gradient(135deg, #1A73E8, #A8C7FA); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 17px; color: #131314; }
    .conv-body { flex: 1 1 auto; min-width: 0; }
    .conv-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
    .conv-name { font-size: 16px; font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .conv-time { flex: 0 0 auto; font-size: 12px; color: var(--text-secondary); }
    .conv-lastmsg { margin-top: 3px; font-size: 13px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .unread-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #FF5A5F; margin-left: 6px; vertical-align: middle; }
    .conv-empty { padding: 56px 24px; text-align: center; color: #6B7075; font-size: 14px; line-height: 1.9; }
    
    .chat-nav { flex: 0 0 auto; padding: calc(var(--safe-top) + 6px) 12px 10px; display: flex; align-items: center; gap: 10px; background: var(--card-bg); border-bottom: 1px solid var(--line); }
    .back-btn { flex: 0 0 auto; background: none; border: none; color: var(--accent); font-size: 22px; padding: 4px 8px; cursor: pointer; }
    .chat-title { flex: 1 1 auto; min-width: 0; font-size: 16px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    
    .action-bar { flex: 0 0 auto; display: flex; gap: 8px; padding: 8px 12px; background: var(--card-bg); border-bottom: 1px solid var(--line); overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .action-btn { flex: 0 0 auto; padding: 7px 14px; border-radius: 999px; border: 1px solid var(--line); background: var(--bg); color: var(--text-primary); font-size: 13px; font-family: inherit; cursor: pointer; -webkit-appearance: none; appearance: none; }
    .action-btn:active { opacity: .75; }
    .action-btn.ai { color: var(--accent); border-color: var(--accent); background: rgba(168,199,250,0.1); }
    
    .chat-stream { flex: 1 1 auto; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior: contain; touch-action: pan-y; padding: 14px 12px 18px; display: flex; flex-direction: column; gap: 12px; }
    .msg-row { display: flex; flex-direction: column; width: 100%; }
    .msg-row.player { align-items: flex-start; } 
    .msg-row.agent { align-items: flex-end; }
    .msg-bubble { max-width: 78%; padding: 10px 14px; font-size: 15px; line-height: 1.5; border-radius: 16px; word-break: break-word; white-space: pre-wrap; }
    .msg-time { margin-top: 4px; font-size: 11px; color: var(--text-secondary); }
    .msg-row.player .msg-bubble { background: #282A2C; color: #E3E3E3; border-bottom-left-radius: 4px; }
    .msg-row.agent .msg-bubble { background: #1A73E8; color: #FFFFFF; border-bottom-right-radius: 4px; }
    
    .input-bar { flex: 0 0 auto; padding: 10px 12px calc(var(--safe-bottom) + 10px); background: var(--card-bg); display: flex; gap: 10px; align-items: center; border-top: 1px solid var(--line); }
    .chat-text-input { flex: 1 1 auto; min-width: 0; background: var(--bg); border: 1px solid var(--line); border-radius: 999px; padding: 10px 16px; color: #fff; font-size: 15px; outline: none; }
    .send-btn { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 50%; background: var(--accent); border: none; color: #131314; font-size: 18px; font-weight: 700; }
    
    #alarm-overlay { position: fixed; left: 0; right: 0; top: 0; bottom: 0; background: rgba(180, 0, 0, 0.92); z-index: 9999; display: flex; flex-direction: column; align-items: center; justify-content: center; opacity: 0; pointer-events: none; transition: opacity 0.2s; }
    #alarm-overlay.active { opacity: 1; pointer-events: auto; }
    .alarm-title { font-size: 28px; font-weight: bold; color: white; margin-bottom: 20px; animation: blink 1s infinite; }
    .silence-btn { background: white; color: red; font-size: 18px; font-weight: bold; padding: 12px 32px; border-radius: 24px; border: none; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
    @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  </style>
</head>
<body>
  <div id="alarm-overlay"><div class="alarm-title">🚨 异常掉线警报 🚨</div><div style="color:white; margin-bottom: 40px;">VPN 或网页网络连接断开</div><button class="silence-btn" onclick="silenceAlarm()">点击静音并忽略</button></div>

  <div id="app">
  <header>
    <div class="page-title">Agent Workspace</div>
    <div class="hdr-right">
      <button type="button" class="afk-toggle" id="im-status-btn" onclick="toggleStatusMenu(event)">🟢 IM在线</button>
      <button type="button" class="afk-toggle" id="afk-btn" onclick="toggleAFK()">🔒 半自动</button>
    </div>
    <div class="status-menu" id="status-menu">
      <button type="button" class="status-item" onclick="setIMStatus(1)">🟢 IM 在线</button>
      <button type="button" class="status-item" onclick="setIMStatus(2)">🟡 IM 忙碌</button>
      <button type="button" class="status-item" onclick="setIMStatus(3)">🔴 IM 离线</button>
    </div>
  </header>
  <div id="toast"></div>

  <div class="view-container">
    <div class="view active" id="list-view"><div class="conv-list" id="conv-container"></div></div>
    <div class="view" id="chat-view">
      <div class="chat-nav">
        <button class="back-btn" onclick="popChat()">←</button>
        <div class="chat-title" id="chat-player-name" style="font-size:18px;">Player Name</div>
      </div>
      <div class="action-bar">
        <button class="action-btn ai" onclick="execCommand('AI_CLOSE')">🤖 AI回复并关单</button>
        <button class="action-btn ai" onclick="execCommand('F9')">✨ AI起草(预览)</button>
        <button class="action-btn" onclick="execCommand('HANGUP')">⏸ 挂起</button>
        <button class="action-btn" onclick="execCommand('CLOSE')">✅ 关单</button>
      </div>
      <div class="chat-stream" id="chat-stream"></div>
      <div class="input-bar">
        <input type="text" class="chat-text-input" id="chat-input" placeholder="输入发送内容...">
        <button class="send-btn" onclick="execCommand('SEND')">↑</button>
      </div>
    </div>
  </div>
  </div>

  <script>
    let globalState = null; let activeGroupId = null; let ws = null;
    let audioCtx = null; let sirenInterval = null;
    let wsAttempts = 0;

    // HTML 转义：玩家消息与昵称属于不可信输入，直接拼进 innerHTML 会造成存储型 XSS
    function esc(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // 安全发送：连接不可用时静默忽略，避免点击按钮抛 TypeError 导致按钮"失灵"
    function sendMsg(obj) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
        }
        return false;
    }

    // 取会话表（带防御，结构异常时不会报错）
    function getConvs() {
        const c = globalState && globalState.companies && globalState.companies['main'];
        return (c && c.conversations) || {};
    }
    function getConv(gid) {
        if (!gid) return null;
        return getConvs()[gid] || null;
    }

    // 时间显示：今天显示 HH:MM，其它日期显示 M/D（微信式）
    function fmtTime(ts) {
        if (!ts) return '';
        const d = new Date(Number(ts));
        if (isNaN(d.getTime())) return '';
        const now = new Date();
        const p = n => (n < 10 ? '0' + n : '' + n);
        if (d.toDateString() === now.toDateString()) return p(d.getHours()) + ':' + p(d.getMinutes());
        return (d.getMonth() + 1) + '/' + d.getDate();
    }

    // 页面切换（列表 / 会话）：用 class 控制 display，不用 transform（避免 iOS 文字发虚）
    function showList() {
        document.getElementById('list-view').classList.add('active');
        document.getElementById('chat-view').classList.remove('active');
    }
    function showChat() {
        document.getElementById('list-view').classList.remove('active');
        document.getElementById('chat-view').classList.add('active');
    }

    // ==================== 远程 IM 状态切换（在线 / 忙碌 / 离线） ====================
    const IM_STATUS_TEXT = { 1: '🟢 IM在线', 2: '🟡 IM忙碌', 3: '🔴 IM离线' };

    // 电脑端探针是否在线：离线时远程操作没有意义，直接给出明确提示而不是静默失败
    function extensionOffline() {
        return !globalState || globalState.extension_online === false;
    }

    function renderIMStatus() {
        const el = document.getElementById('im-status-btn');
        if (!el) return;
        if (extensionOffline()) {
            el.innerText = '⚫ 电脑未连接';
            el.className = 'afk-toggle';
            return;
        }
        const st = (globalState && globalState.im_status) || 1;
        el.innerText = IM_STATUS_TEXT[st] || IM_STATUS_TEXT[1];
        el.className = 'afk-toggle' + (st === 1 ? ' active' : '');
    }

    function toggleStatusMenu(ev) {
        if (ev && ev.stopPropagation) ev.stopPropagation();
        if (extensionOffline()) { toast('电脑端未连接，请先打开客服工作台'); return; }
        const m = document.getElementById('status-menu');
        if (m) m.classList.toggle('show');
    }

    function setIMStatus(st) {
        const m = document.getElementById('status-menu');
        if (m) m.classList.remove('show');
        if (extensionOffline()) { toast('电脑端未连接，无法切换状态'); return; }
        if (!sendMsg({ action: 'SET_IM_STATUS', status: st })) {
            toast('连接已断开，正在重连');
            return;
        }
        toast('已切换为 ' + (IM_STATUS_TEXT[st] || ''));
    }

    document.addEventListener('click', () => {
        const m = document.getElementById('status-menu');
        if (m) m.classList.remove('show');
    });

    // ==================== 轻提示 ====================
    let toastTimer = null;
    function toast(msg) {
        const el = document.getElementById('toast');
        if (!el) return;
        el.innerText = String(msg || '');
        el.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), 2800);
    }

    // ==================== iOS 锁屏 / 退后台恢复后主动补拉 ====================
    function resyncNow(reason) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            sendMsg({ action: 'REQUEST_SNAPSHOT' });   // 主动要一次最新快照，避免停留在几分钟前
        } else {
            wsAttempts = 0;                            // 立刻重连，不等指数退避
            try { initWS(); } catch (e) {}
        }
    }
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) resyncNow('visibilitychange');
    });
    window.addEventListener('pageshow', () => resyncNow('pageshow'));
    window.addEventListener('online', () => resyncNow('online'));
    window.addEventListener('focus', () => resyncNow('focus'));

    function playMobileSiren() {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (sirenInterval) return;
        sirenInterval = setInterval(() => {
            try {
                const osc = audioCtx.createOscillator(); const gain = audioCtx.createGain();
                osc.type = 'sawtooth'; osc.frequency.setValueAtTime(600, audioCtx.currentTime); osc.frequency.linearRampToValueAtTime(800, audioCtx.currentTime + 0.4);
                gain.gain.setValueAtTime(0.5, audioCtx.currentTime); gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.8);
                osc.connect(gain); gain.connect(audioCtx.destination); osc.start(); osc.stop(audioCtx.currentTime + 0.8);
            } catch(e) {}
        }, 1000);
    }
    function stopMobileSiren() { if (sirenInterval) { clearInterval(sirenInterval); sirenInterval = null; } }

    function toggleAFK() {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (extensionOffline()) { toast('电脑端未连接，无法切换托管模式'); return; }
        const isAFK = !globalState.afk_mode;
        if (!sendMsg({ action: 'TOGGLE_AFK', status: isAFK })) {
            toast('连接已断开，正在重连');
        }
    }

    function silenceAlarm() {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        document.getElementById('alarm-overlay').classList.remove('active');
        stopMobileSiren();
        sendMsg({ action: 'SILENCE_ALARM' }); 
    }
    
    function initWS() {
      // 清理旧连接，避免句柄泄漏与多路重连
      if (ws) {
        ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
        try { ws.close(); } catch (e) {}
        ws = null;
      }
      ws = new WebSocket((window.location.protocol === 'https:' ? 'wss://' : 'ws://') + window.location.host + '/ws/mobile');
      ws.onopen = () => { wsAttempts = 0; };   // 连接成功才重置重连计数
      ws.onmessage = (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (err) { return; }   // 脏包不打断脚本
        if (!payload) return;
        if (payload.type === 'FULL_SYNC') { 
            globalState = payload.data || null;
            // 若当前打开的会话已不存在（如被清理），自动退回列表
            if (activeGroupId && !getConv(activeGroupId)) { activeGroupId = null; showList(); }
            renderAll(); 
            renderIMStatus();
            const btn = document.getElementById('afk-btn');
            if(globalState && globalState.afk_mode) { btn.className = 'afk-toggle active'; btn.innerText = '🚀 AFK 已接管'; }
            else { btn.className = 'afk-toggle'; btn.innerText = '🔒 电脑半自动'; }
            if(globalState && globalState.alarm_status) { document.getElementById('alarm-overlay').classList.add('active'); playMobileSiren(); }
            else { document.getElementById('alarm-overlay').classList.remove('active'); stopMobileSiren(); }
        }
        else if (payload.type === 'FILL_DRAFT') {
            document.getElementById('chat-input').value = payload.content || '';
        }
        else if (payload.type === 'AI_STATUS') {
            if (payload.message) toast(payload.message);   // 关单结果 / AI 状态提示
        }
      };
      ws.onerror = () => { /* 出错后浏览器会触发 onclose，由 onclose 统一调度重连 */ };
      // 指数退避重连（2s→4s→…→30s 上限，最多 20 次），替代原先的固定 2.5s 无限重连
      ws.onclose = () => {
        if (wsAttempts >= 20) { console.error('[手机端] 重连次数已达上限，请刷新页面'); return; }
        wsAttempts++;
        setTimeout(initWS, Math.min(2000 * wsAttempts, 30000));
      };
    }

    function renderAll() {
      if (!globalState) return;
      const compData = globalState.companies['main'] || {};
      const container = document.getElementById('conv-container');
      const convs = compData.conversations || {};
      // 按最后活动时间倒序：最近有新消息的排最前（像微信会话列表）
      const keys = Object.keys(convs).sort((a, b) => {
        const ta = (convs[a] && convs[a].updatedAt) || 0;
        const tb = (convs[b] && convs[b].updatedAt) || 0;
        return tb - ta;
      });
      if (keys.length === 0) {
        container.innerHTML = '<div class="conv-empty">暂无会话<br>请在电脑端打开玩家工单</div>';
        return;
      }
      
      container.innerHTML = keys.map(gid => {
        const c = convs[gid] || {};
        const msgs = Array.isArray(c.msgs) ? c.msgs : [];
        const last = msgs.length > 0 ? (msgs[msgs.length - 1] || {}) : null;
        const name = c.name || gid;
        const preview = (last && last.text) ? last.text : '（暂无消息）';
        const time = fmtTime((last && last.ts) || c.updatedAt);
        // 未读标记：最后一条是玩家发的，且不是当前正在看的会话
        const unread = !!(last && last.sender === 'player' && gid !== activeGroupId);
        // 全部走 esc() 转义；gid 改用 data-* 传递，避免内联 onclick 属性逃逸
        return `<div class="conv-card" data-gid="${esc(gid)}">
            <div class="avatar">${esc(String(name).charAt(0) || '玩')}</div>
            <div class="conv-body">
              <div class="conv-top">
                <div class="conv-name">${esc(name)}${unread ? '<span class="unread-dot"></span>' : ''}</div>
                <div class="conv-time">${esc(time)}</div>
              </div>
              <div class="conv-lastmsg">${esc(preview)}</div>
            </div>
          </div>`;
      }).join('');

      // 事件委托绑定（不再把数据拼进 onclick）
      container.querySelectorAll('.conv-card').forEach(el => {
        el.addEventListener('click', () => pushChat(el.dataset.gid));
      });
      
      if (activeGroupId && getConv(activeGroupId)) renderChatStream(getConv(activeGroupId));
    }

    function pushChat(gid) {
      const conv = getConv(gid);
      if (!conv) return;
      activeGroupId = gid;
      document.getElementById('chat-player-name').innerText = conv.name || gid;
      renderChatStream(conv);
      showChat();
      renderAll();                 // 刷新列表（清掉该会话的未读点）
    }

    function popChat() {
      activeGroupId = null;
      showList();
      renderAll();
    }

    function renderChatStream(conv) {
      const stream = document.getElementById('chat-stream');
      const currentScroll = stream.scrollTop;
      const isAtBottom = (stream.scrollHeight - stream.clientHeight) <= currentScroll + 20;
      
      const rows = (conv && Array.isArray(conv.msgs)) ? conv.msgs : [];
      if (rows.length === 0) { stream.innerHTML = '<div class="conv-empty">暂无消息</div>'; return; }
      // sender 仅允许 player/agent，防止通过 class 注入；text 全量转义，防存储型 XSS
      stream.innerHTML = rows.map(m => {
        const who = (m && m.sender === 'player') ? 'player' : 'agent';
        const t = (m && m.ts) ? `<div class="msg-time">${esc(fmtTime(m.ts))}</div>` : '';
        return `<div class="msg-row ${who}"><div class="msg-bubble">${esc((m || {}).text)}</div>${t}</div>`;
      }).join('');
      if (isAtBottom) stream.scrollTop = stream.scrollHeight;
    }

    function execCommand(cmd) {
      if (!activeGroupId) return;
      if (extensionOffline()) { toast('电脑端未连接，请先打开客服工作台'); return; }
      if (!ws || ws.readyState !== WebSocket.OPEN) {
          alert('连接已断开，正在重连，请稍后再试');
          return;
      }
      if (cmd === 'AI_CLOSE') {
          if (!confirm('AI 将自动选择问题分类并生成结束语，然后回复并关单。\n关单后该会话会从列表移除，确定继续？')) return;
          if (!sendMsg({ action: 'AI_CLOSE', groupID: activeGroupId })) {
              toast('连接已断开，正在重连');
              return;
          }
          toast('AI 正在生成结束语…');
          return;
      }
      if (cmd === 'F9') {
          sendMsg({ action: 'TRIGGER_F9', groupID: activeGroupId });
      } else if (cmd === 'HANGUP') {
          sendMsg({ action: 'EXT_COMMAND', command: 'ACTION_HANGUP', groupID: activeGroupId });
      } else {
          const input = document.getElementById('chat-input'); const text = input.value.trim();
          if (cmd === 'CLOSE') sendMsg({ action: 'EXT_COMMAND', command: 'ACTION_REPLY_CLOSE', groupID: activeGroupId, content: text, category: "其他" });
          else if (cmd === 'SEND' && text) {
              sendMsg({ action: 'SEND_REPLY', groupID: activeGroupId, content: text });
          }
          input.value = '';
      }
    }
    
    initWS();
  </script>
</body>
</html>"""

# ==== 提供给电脑端快捷键一键读取的 API ====
async def api_current_ticket(request):
    conversations = state["companies"]["main"]["conversations"]
    if not conversations: return web.json_response({})
    # 优先返回最近有消息活动的工单；若已失效则回退到后插入的会话
    gid = _LAST_ACTIVE["gid"]
    if gid not in conversations:
        gid = list(conversations.keys())[-1]
    return web.json_response(conversations[gid])

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type="text/html")


async def api_categories(request):
    """查看从网页级联选择器抓到的真实问题分类（便于配置 close_category_options）。"""
    return web.json_response({
        "options": state.get("category_options") or [],
        "close_category_path": config.get("close_category_path"),
        "close_category_default": config.get("close_category_default"),
        "hint": "若 options 为空，请确认电脑端探针已连上，并已打开过一次工单（含问题分类选择器）",
    })

async def ws_ext_handler(request):
    # heartbeat=30：定期 ping，及时发现 iOS 退后台/网络抖动造成的死连接
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    active_clients["extension"].add(ws)
    state["extension_online"] = True
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    # 探针一连上就请它回报网页上的真实问题分类（供手机端 AI 一键关单选分类）
    await safe_send(ws, {"command": "REQUEST_CATEGORIES"})
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    pkt = json.loads(msg.data)
                except Exception:
                    continue          # 忽略非法 JSON，不让脏包打断连接
                if not isinstance(pkt, dict):
                    continue
                ev = pkt.get("event")
                # 处理 HEADERS_SYNC 事件（探针注入认证头）
                if ev == "HEADERS_SYNC":
                    headers_data = pkt.get("data", {})
                    if isinstance(headers_data, dict) and headers_data:
                        fp = json.dumps(headers_data, sort_keys=True)
                        if fp != _IM_AUTH_FP["value"]:
                            # 仅在内容真正变化时才更新（探针每次重连都会重发，避免重复覆盖与日志刷屏）
                            IM_AUTH_HEADERS.clear()
                            IM_AUTH_HEADERS.update(headers_data)
                            _IM_AUTH_FP["value"] = fp
                            print(f"[OK] 探针认证头已更新：{len(headers_data)} 个字段（内容变更）")
                    continue  # 不回复探针，且绝不写入 state

                # 探针回报的问题分类（用于手机端 AI 自动选分类关单）
                if ev == "CATEGORY_OPTIONS":
                    opts = (pkt.get("data") or {}).get("options", [])
                    if isinstance(opts, list) and opts:
                        state["category_options"] = [str(o)[:40] for o in opts][:300]
                        print(f"[分类] 已获取 {len(state['category_options'])} 个问题分类")
                        for m in list(active_clients["mobile"]):
                            await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                # 探针上报的 IM 状态（含手动离线 / 忙碌，手机端据此显示真实状态）
                if ev == "IM_STATUS":
                    data = pkt.get("data") or {}
                    try:
                        st = int(data.get("status", 1))
                    except Exception:
                        st = 1
                    if st not in (1, 2, 3):
                        st = 1
                    state["im_status"] = st
                    if st == 1:
                        state["alarm_status"] = False
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                if ev == "ABNORMAL_OFFLINE":
                    # 新增：异常掉线警报闭环
                    state["alarm_status"] = True
                    state["im_status"] = 3
                    # 推送给手机端
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    # 推送 Bark 通知（P0 修复：缺失的 Bark 警报）
                    push_bark("🚨 异常掉线警报", "VPN 或网页网络连接断开，请立即检查！")
                    # 向探针发送确认回执（修复 BUG-002：防止重复上报）
                    await ws.send_json({"command": "ALARM_CONFIRMED"})
                    
                elif ev == "ALARM_RECOVERED":
                    state["alarm_status"] = False
                    state["im_status"] = 1
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    # 向探针发送确认回执
                    await ws.send_json({"command": "RECOVERY_CONFIRMED"})
                    
                elif ev == "PLAYER_MESSAGE":
                    payload = pkt.get("data", {})
                    gid = payload.get("groupID")
                    
                    target = state["companies"]["main"]
                    # 会话上限 50，超出时淘汰"最久没有新消息"的那个（而不是最早创建的），避免误删活跃工单
                    if len(target["conversations"]) >= 50:
                        oldest_gid = min(target["conversations"],
                                         key=lambda k: target["conversations"][k].get("updatedAt") or 0)
                        if oldest_gid != gid:
                            del target["conversations"][oldest_gid]
                            print(f"[清理] 移除最久未活动的会话：{oldest_gid}")
                    
                    if not gid:
                        continue
                    # 会话名称：优先用探针传来的玩家名，其次从 playerInfo 首段推断
                    name = str(payload.get("name") or "").strip()[:20]
                    if not name:
                        name = str(payload.get("playerInfo", "")).split('|')[0].strip()[:12] or "玩家"

                    c = target["conversations"].setdefault(gid, {"name": name, "msgs": [], "updatedAt": 0})
                    c["name"] = name                       # 每次都刷新名称，不再只在首次写入
                    raw_msgs = payload.get("messages", [])
                    now_ms = int(time.time() * 1000)
                    # 覆盖数组杜绝雪球；过滤脏数据；并为每条消息补时间戳（保留已有消息的原时间）
                    if isinstance(raw_msgs, list):
                        prev_ts = {}
                        for pm in (c.get("msgs") or []):
                            if isinstance(pm, dict):
                                prev_ts[(pm.get("sender"), pm.get("text"))] = pm.get("ts")
                        cleaned = []
                        for m in raw_msgs:
                            if not (isinstance(m, dict) and m.get("text")):
                                continue
                            sender = m.get("sender") if m.get("sender") in ("player", "agent") else "agent"
                            key = (sender, m.get("text"))
                            cleaned.append({"sender": sender, "text": m.get("text"),
                                            "ts": prev_ts.get(key) or now_ms})
                        c["msgs"] = cleaned
                    else:
                        c["msgs"] = []
                    c["playerInfo"] = payload.get("playerInfo", "")
                    c["updatedAt"] = now_ms
                    _LAST_ACTIVE["gid"] = gid
                    
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    asyncio.create_task(handle_ai_automation(gid))
    finally:
        active_clients["extension"].discard(ws)
        if not active_clients["extension"]:
            # 电脑端网页关闭/探针掉线：手机端必须能看出来，避免显示过期状态
            state["extension_online"] = False
            for m in list(active_clients["mobile"]):
                await safe_send(m, {"type": "FULL_SYNC", "data": state})
    return ws

async def handle_ai_close(group_id: str):
    """手机端「AI 回复并关单」。

    流程：AI 选问题分类 + 生成结束语 -> 通知探针执行「回复并关单」-> 会话从列表移除。
    任一步失败都会明确返回错误，绝不误删会话。
    """
    conv = state["companies"]["main"]["conversations"].get(group_id)
    if not conv:
        return False, "会话不存在（可能已关单）"

    history_str = build_chat_history_str(group_id) or str(conv.get("playerInfo") or "") or "（无聊天记录）"
    options = state.get("category_options") or config.get("close_category_options") or []

    try:
        category, content = await asyncio.get_event_loop().run_in_executor(
            None, core.generate_closing, history_str, options)
    except Exception as e:
        return False, f"AI 生成失败: {e}"

    if not content:
        return False, "AI 未生成结束语，已取消关单"
    if not active_clients["extension"]:
        return False, "电脑端探针未连接，无法关单"

    path = config.get("close_category_path") or ["一级分类", "二级分类"]
    payload = {
        "command": "ACTION_REPLY_CLOSE",
        "content": content,
        "category": category,
        "categoryPath": path,
        "groupID": group_id,
    }
    for ext in list(active_clients["extension"]):
        await safe_send(ext, payload)

    # 关单后从列表移除（对应需求：关单之后消息从列表消失）
    name = conv.get("name") or group_id
    state["companies"]["main"]["conversations"].pop(group_id, None)
    if _LAST_ACTIVE.get("gid") == group_id:
        _LAST_ACTIVE["gid"] = None
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})

    push_bark("已回复并关单", f"{name}　分类：{category}　{content[:40]}", group_id)
    print(f"[关单] {name} -> 分类「{category}」")
    return True, category


async def _ai_close_and_notify(gid: str):
    try:
        ok, info = await handle_ai_close(gid)
    except Exception as e:
        ok, info = False, f"关单异常: {e}"
    msg = (f"已回复并关单 · 分类：{info}") if ok else str(info)
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "AI_STATUS", "groupID": gid,
                            "status": "closed" if ok else "error", "message": msg})


async def ws_mobile_handler(request):
    # heartbeat=30：手机退后台/锁屏时能尽快探活，配合前端重连即补拉快照
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    active_clients["mobile"].add(ws)
    await ws.send_json({"type": "FULL_SYNC", "data": state})
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    pkt = json.loads(msg.data)
                except Exception:
                    continue          # 忽略非法 JSON，不让脏包打断连接
                if not isinstance(pkt, dict):
                    continue
                act = pkt.get("action")

                # 补拉快照：iOS 锁屏/退后台恢复后前端主动要一次最新状态，防止界面停留在几分钟前
                if act == "REQUEST_SNAPSHOT":
                    await safe_send(ws, {"type": "FULL_SYNC", "data": state})
                    continue

                # 远程切换 IM 状态：1=在线 2=忙碌 3=离线；切到在线时自动解除异常掉线警报
                if act == "SET_IM_STATUS":
                    try:
                        st = int(pkt.get("status", 1))
                    except Exception:
                        st = 1
                    if st not in (1, 2, 3):
                        st = 1
                    state["im_status"] = st
                    if st == 1:
                        state["alarm_status"] = False
                    for ext in list(active_clients["extension"]):
                        await safe_send(ext, {"command": "CHANGE_STATUS", "status": st})
                        if st == 1:
                            await safe_send(ext, {"command": "SILENCE_ALARM"})
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                # AI 一键回复并关单（AI 选问题分类 + 生成结束语，关单后会话从列表消失）
                if act == "AI_CLOSE":
                    gid = pkt.get("groupID")
                    if gid:
                        asyncio.create_task(_ai_close_and_notify(gid))
                    continue

                if act == "TOGGLE_AFK":
                    state["afk_mode"] = pkt.get("status", False)
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                elif act == "SILENCE_ALARM":
                    state["alarm_status"] = False
                    for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "SILENCE_ALARM"})
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                elif act == "TRIGGER_F9":
                    asyncio.create_task(handle_ai_automation(pkt.get("groupID")))
                elif act == "EXT_COMMAND":
                    for ext in list(active_clients["extension"]): await safe_send(ext, pkt)
                elif act == "SEND_REPLY":
                    gid = pkt.get("groupID")
                    text = pkt.get("content")
                    if not gid or not text:
                        continue
                    conv = state["companies"]["main"]["conversations"].setdefault(gid, {"name": gid, "msgs": []})
                    if not isinstance(conv.get("msgs"), list):
                        conv["msgs"] = []
                    conv["msgs"].append({"sender": "agent", "text": text,
                                         "time": datetime.now().strftime("%H:%M:%S"),
                                         "ts": int(time.time() * 1000)})
                    conv["updatedAt"] = int(time.time() * 1000)
                    _LAST_ACTIVE["gid"] = gid
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    # 修复 BUG-011：过滤 action 字段，仅转发必要字段
                    for ext in list(active_clients["extension"]): 
                        await safe_send(ext, {
                            "command": act,
                            "groupID": gid,
                            "content": text
                        })
    finally:
        active_clients["mobile"].discard(ws)
    return ws

# ==================== 后台任务 ====================
async def rules_sync_loop():
    """定时同步规章库（监听目录 / 直链拉取），并热重载知识库。

    这样线上规章一变更，AI 答复就会自动跟上，不再需要手动下载或重启服务。
    """
    if rules_sync is None or not config.get("rules_auto_sync", True):
        print("[规章库] 自动同步未开启（rules_auto_sync=false）")
        return
    interval = max(60, int(config.get("rules_sync_interval_minutes", 30)) * 60)
    print(f"[规章库] 自动同步已开启：每 {interval // 60} 分钟检查一次")
    while True:
        try:
            changed, message = await asyncio.get_event_loop().run_in_executor(
                None, rules_sync.sync_once, core, config, False)
            if changed:
                print(f"[规章库] {message}")
                push_bark("规章库已更新", message)
        except Exception as e:
            print(f"[规章库] 自动同步异常: {e}", file=sys.stderr)
        await asyncio.sleep(interval)


async def category_refresh_loop():
    """定期让探针重新回报问题分类（运营可能调整页面分类项）。"""
    while True:
        await asyncio.sleep(600)
        for ext in list(active_clients["extension"]):
            await safe_send(ext, {"command": "REQUEST_CATEGORIES"})


async def _on_startup(app):
    app["rules_task"] = asyncio.create_task(rules_sync_loop())
    app["cat_task"] = asyncio.create_task(category_refresh_loop())


async def _on_cleanup(app):
    for key in ("rules_task", "cat_task"):
        task = app.get(key)
        if task:
            task.cancel()


app = web.Application()
app.on_startup.append(_on_startup)
app.on_cleanup.append(_on_cleanup)
app.router.add_get("/", index_handler)
app.router.add_get("/api/ticket", api_current_ticket)
app.router.add_get("/api/categories", api_categories)
app.router.add_get("/ws/extension", ws_ext_handler)
app.router.add_get("/ws/mobile", ws_mobile_handler)

if __name__ == "__main__":
    local_ip = get_local_ip()
    print("=" * 60)
    print("[启动] IM 移动端中继与 AI 自动托管服务已启动！")
    print(f"手机访问: http://{local_ip}:{PORT}")
    print("=" * 60)
    web.run_app(app, host="0.0.0.0", port=PORT)