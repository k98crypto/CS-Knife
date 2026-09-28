import os
import sys
import json
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
            await m.send_json({"type": "AI_STATUS", "groupID": group_id, "status": "waiting", "message": "等待玩家回复中"})
        return

    if "【规章库未收录" in reply:
        if state["afk_mode"]:
            for ext in list(active_clients["extension"]): await ext.send_json({"command": "ACTION_HANGUP", "groupID": group_id})
            push_bark("🚨 疑难单等待接入", "请在手机端介入处理", group_id)
        else:
            for ext in list(active_clients["extension"]): await ext.send_json({"command": "FILL_DRAFT", "content": reply})
        return

    if reply:
        now_str = datetime.now().strftime("%H:%M:%S")
        if state["afk_mode"]:
            conv = state["companies"]["main"]["conversations"].setdefault(
                group_id, {"name": group_id, "msgs": []})
            if not isinstance(conv.get("msgs"), list):
                conv["msgs"] = []
            conv["msgs"].append({"sender": "agent", "text": reply, "time": now_str})
            _LAST_ACTIVE["gid"] = group_id
            for m in list(active_clients["mobile"]): await m.send_json({"type": "FULL_SYNC", "data": state})

            if "TIMEOUT_CLOSE" in tag:
                for ext in list(active_clients["extension"]): await ext.send_json({"command": "ACTION_REPLY_CLOSE", "category": "其他", "content": reply, "groupID": group_id})
            else:
                for ext in list(active_clients["extension"]): await ext.send_json({"command": "SEND_REPLY", "content": reply, "groupID": group_id})
        else:
            # 半自动模式：草稿推到网页与手机输入框
            for ext in list(active_clients["extension"]): await ext.send_json({"command": "FILL_DRAFT", "content": reply, "category": "其他"})
            for m in list(active_clients["mobile"]): await m.send_json({"type": "FILL_DRAFT", "content": reply})


# ================= 手机端 H5 界面 =================
HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>Agent Workspace</title>
  <style>
    :root { --bg: #131314; --card-bg: #1E1F20; --surface-variant: #444746; --text-primary: #E3E3E3; --text-secondary: #C4C7C5; --accent: #A8C7FA; --safe-top: env(safe-area-inset-top, 24px); --safe-bottom: env(safe-area-inset-bottom, 24px); }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    body { font-family: 'Google Sans', -apple-system, sans-serif; background: var(--bg); color: var(--text-primary); height: 100vh; overflow: hidden; display: flex; flex-direction: column; -webkit-overflow-scrolling: touch; }
    header { padding: calc(var(--safe-top) + 12px) 20px 16px 20px; display: flex; justify-content: space-between; align-items: center; background: #131314; border-bottom: 1px solid #333; z-index: 10;}
    .page-title { font-size: 20px; font-weight: 500; }
    .afk-toggle { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: bold; color: var(--text-secondary); background: #333; padding: 6px 12px; border-radius: 20px; }
    .afk-toggle.active { background: rgba(168, 199, 250, 0.2); color: var(--accent); border: 1px solid var(--accent); }
    
    .view-container { position: relative; flex: 1; overflow: hidden; }
    .view { position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; background: var(--bg); transition: transform 0.3s ease; }
    #chat-view { transform: translateX(100%); z-index: 100; }
    #chat-view.active { transform: translateX(0); }
    .conv-list { flex: 1; overflow-y: auto; padding: 8px 16px; -webkit-overflow-scrolling: touch; }
    .conv-card { padding: 16px; margin-bottom: 8px; border-radius: 16px; background: var(--card-bg); display: flex; gap: 16px; align-items: center; }
    .avatar { width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(135deg, #1A73E8, #A8C7FA); display: flex; align-items: center; justify-content: center; font-weight: bold; color: #131314; }
    .conv-meta { flex: 1; overflow: hidden; }
    .conv-name { font-size: 16px; margin-bottom: 4px; color: var(--text-primary); }
    .conv-lastmsg { font-size: 14px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    
    .chat-nav { padding: calc(var(--safe-top) + 8px) 16px 12px 16px; display: flex; align-items: center; gap: 16px; background: var(--card-bg); }
    .back-btn { background: none; border: none; color: var(--accent); font-size: 24px; cursor: pointer; }
    
    .action-bar { display: flex; gap: 8px; padding: 10px 16px; background: var(--card-bg); border-bottom: 1px solid #333; overflow-x: auto; white-space: nowrap; }
    .action-btn { flex-shrink: 0; padding: 8px 14px; border-radius: 18px; border: 1px solid #444; background: var(--bg); color: var(--text-primary); font-size: 13px; font-weight: 500; cursor: pointer; }
    .action-btn.ai { color: var(--accent); border-color: var(--accent); background: rgba(168,199,250,0.1); }
    
    .chat-stream { flex: 1; overflow-y: auto; padding: 20px 16px; display: flex; flex-direction: column; gap: 24px; -webkit-overflow-scrolling: touch; }
    .msg-row { display: flex; flex-direction: column; width: 100%; }
    .msg-row.player { align-items: flex-start; } 
    .msg-row.agent { align-items: flex-end; }
    .msg-bubble { max-width: 80%; padding: 12px 16px; font-size: 15px; line-height: 1.5; border-radius: 18px; word-break: break-word; }
    .msg-row.player .msg-bubble { background: #282A2C; color: #E3E3E3; border-bottom-left-radius: 4px; }
    .msg-row.agent .msg-bubble { background: #1A73E8; color: #FFFFFF; border-bottom-right-radius: 4px; }
    
    .input-bar { padding: 12px 16px calc(var(--safe-bottom) + 12px) 16px; background: var(--card-bg); display: flex; gap: 12px; align-items: center;}
    .chat-text-input { flex: 1; background: var(--bg); border: 1px solid #444; border-radius: 24px; padding: 10px 16px; color: white; font-size: 15px; outline: none; }
    .send-btn { width: 40px; height: 40px; border-radius: 50%; background: var(--accent); border: none; display: flex; align-items: center; justify-content: center; font-weight:bold; }
    
    #alarm-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(180, 0, 0, 0.9); z-index: 9999; display: flex; flex-direction: column; align-items: center; justify-content: center; opacity: 0; pointer-events: none; transition: opacity 0.2s; }
    #alarm-overlay.active { opacity: 1; pointer-events: auto; }
    .alarm-title { font-size: 28px; font-weight: bold; color: white; margin-bottom: 20px; animation: blink 1s infinite; }
    .silence-btn { background: white; color: red; font-size: 18px; font-weight: bold; padding: 12px 32px; border-radius: 24px; border: none; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
    @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  </style>
</head>
<body>
  <div id="alarm-overlay"><div class="alarm-title">🚨 异常掉线警报 🚨</div><div style="color:white; margin-bottom: 40px;">VPN 或网页网络连接断开</div><button class="silence-btn" onclick="silenceAlarm()">点击静音并忽略</button></div>

  <header>
    <div class="page-title">✨ Workspace</div>
    <div class="afk-toggle" id="afk-btn" onclick="toggleAFK()">🔒 半自动</div>
  </header>

  <div class="view-container">
    <div class="view" id="list-view"><div class="conv-list" id="conv-container"></div></div>
    <div class="view" id="chat-view">
      <div class="chat-nav">
        <button class="back-btn" onclick="popChat()">←</button>
        <div class="chat-title" id="chat-player-name" style="font-size:18px;">Player Name</div>
      </div>
      <div class="action-bar">
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
        if (!globalState) return;                       // 尚未同步到服务端状态，避免空指针
        const isAFK = !globalState.afk_mode;
        if (!sendMsg({ action: 'TOGGLE_AFK', status: isAFK })) {
            alert('连接已断开，正在重连，请稍后再试');
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
            globalState = payload.data; 
            renderAll(); 
            const btn = document.getElementById('afk-btn');
            if(globalState && globalState.afk_mode) { btn.className = 'afk-toggle active'; btn.innerText = '🚀 AFK 已接管'; }
            else { btn.className = 'afk-toggle'; btn.innerText = '🔒 电脑半自动'; }
            if(globalState && globalState.alarm_status) { document.getElementById('alarm-overlay').classList.add('active'); playMobileSiren(); }
            else { document.getElementById('alarm-overlay').classList.remove('active'); stopMobileSiren(); }
        }
        else if (payload.type === 'FILL_DRAFT') {
            document.getElementById('chat-input').value = payload.content || '';
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
      const compData = globalState.companies['main'];
      const container = document.getElementById('conv-container');
      const keys = Object.keys(compData.conversations || {});
      if(keys.length === 0) { container.innerHTML = '<div style="text-align:center; padding: 40px; color: #666;">暂无会话</div>'; return; }
      
      container.innerHTML = keys.map(gid => {
        const c = compData.conversations[gid] || {};
        const msgs = Array.isArray(c.msgs) ? c.msgs : [];
        const lastMsg = msgs.length > 0 ? (msgs[msgs.length - 1] || {}).text : '...';
        const name = c.name || gid;
        // 全部走 esc() 转义；gid 改用 data-* 传递，避免内联 onclick 属性逃逸
        return `<div class="conv-card" data-gid="${esc(gid)}">
            <div class="avatar">${esc(String(name).charAt(0) || '玩')}</div>
            <div class="conv-meta"><div class="conv-name">${esc(name)}</div><div class="conv-lastmsg">${esc(lastMsg || '...')}</div></div>
          </div>`;
      }).join('');

      // 事件委托绑定（不再把数据拼进 onclick）
      container.querySelectorAll('.conv-card').forEach(el => {
        el.addEventListener('click', () => pushChat(el.dataset.gid));
      });
      
      if (activeGroupId && compData.conversations[activeGroupId]) renderChatStream(compData.conversations[activeGroupId]);
    }

    function pushChat(gid) {
      if (!globalState || !gid) return;
      const conv = globalState.companies['main'].conversations[gid];
      if (!conv) return;
      activeGroupId = gid;
      document.getElementById('chat-player-name').innerText = conv.name || gid;
      renderChatStream(conv); document.getElementById('chat-view').classList.add('active');
    }

    function popChat() { activeGroupId = null; document.getElementById('chat-view').classList.remove('active'); }

    function renderChatStream(conv) {
      const stream = document.getElementById('chat-stream');
      const currentScroll = stream.scrollTop;
      const isAtBottom = (stream.scrollHeight - stream.clientHeight) <= currentScroll + 20;
      
      const rows = Array.isArray(conv.msgs) ? conv.msgs : [];
      // sender 仅允许 player/agent，防止通过 class 注入；text 全量转义，防存储型 XSS
      stream.innerHTML = rows.map(m => {
        const who = (m && m.sender === 'player') ? 'player' : 'agent';
        return `<div class="msg-row ${who}"><div class="msg-bubble">${esc((m || {}).text)}</div></div>`;
      }).join('');
      if (isAtBottom) stream.scrollTop = stream.scrollHeight;
    }

    function execCommand(cmd) {
      if (!activeGroupId) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
          alert('连接已断开，正在重连，请稍后再试');
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

async def ws_ext_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    active_clients["extension"].add(ws)
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
                
                if ev == "ABNORMAL_OFFLINE":
                    # 新增：异常掉线警报闭环
                    state["alarm_status"] = True
                    # 推送给手机端
                    for m in list(active_clients["mobile"]): await m.send_json({"type": "FULL_SYNC", "data": state})
                    # 推送 Bark 通知（P0 修复：缺失的 Bark 警报）
                    push_bark("🚨 异常掉线警报", "VPN 或网页网络连接断开，请立即检查！")
                    # 向探针发送确认回执（修复 BUG-002：防止重复上报）
                    await ws.send_json({"command": "ALARM_CONFIRMED"})
                    
                elif ev == "ALARM_RECOVERED":
                    state["alarm_status"] = False
                    for m in list(active_clients["mobile"]): await m.send_json({"type": "FULL_SYNC", "data": state})
                    # 向探针发送确认回执
                    await ws.send_json({"command": "RECOVERY_CONFIRMED"})
                    
                elif ev == "PLAYER_MESSAGE":
                    payload = pkt.get("data", {})
                    gid = payload.get("groupID")
                    
                    # 修复 BUG-005：清理旧会话数据，防止无限膨胀
                    target = state["companies"]["main"]
                    # 限制会话数量为最新的 20 个
                    if len(target["conversations"]) >= 20:
                        # 删除最旧的会话
                        oldest_gid = next(iter(target["conversations"]))
                        del target["conversations"][oldest_gid]
                        print(f"[清理] 移除最早会话：{oldest_gid}")
                    
                    if not gid:
                        continue
                    c = target["conversations"].setdefault(
                        gid, {"name": str(payload.get("playerInfo", "玩家")).split('|')[0][:6], "msgs": []})
                    raw_msgs = payload.get("messages", [])
                    # 覆盖数组杜绝雪球；同时过滤脏数据（非 dict / 缺 text），避免下游 KeyError
                    if isinstance(raw_msgs, list):
                        c["msgs"] = [m for m in raw_msgs if isinstance(m, dict) and m.get("text")]
                    else:
                        c["msgs"] = []
                    c["playerInfo"] = payload.get("playerInfo", "")
                    _LAST_ACTIVE["gid"] = gid
                    
                    for m in list(active_clients["mobile"]): await m.send_json({"type": "FULL_SYNC", "data": state})
                    asyncio.create_task(handle_ai_automation(gid))
    finally:
        active_clients["extension"].discard(ws)
    return ws

async def ws_mobile_handler(request):
    ws = web.WebSocketResponse()
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
                
                if act == "TOGGLE_AFK":
                    state["afk_mode"] = pkt.get("status", False)
                    for m in list(active_clients["mobile"]): await m.send_json({"type": "FULL_SYNC", "data": state})
                elif act == "SILENCE_ALARM":
                    state["alarm_status"] = False
                    for ext in list(active_clients["extension"]): await ext.send_json({"command": "SILENCE_ALARM"})
                    for m in list(active_clients["mobile"]): await m.send_json({"type": "FULL_SYNC", "data": state})
                elif act == "TRIGGER_F9":
                    asyncio.create_task(handle_ai_automation(pkt.get("groupID")))
                elif act == "EXT_COMMAND":
                    for ext in list(active_clients["extension"]): await ext.send_json(pkt)
                elif act == "SEND_REPLY":
                    gid = pkt.get("groupID")
                    text = pkt.get("content")
                    if not gid or not text:
                        continue
                    conv = state["companies"]["main"]["conversations"].setdefault(gid, {"name": gid, "msgs": []})
                    if not isinstance(conv.get("msgs"), list):
                        conv["msgs"] = []
                    conv["msgs"].append({"sender": "agent", "text": text, "time": datetime.now().strftime("%H:%M:%S")})
                    _LAST_ACTIVE["gid"] = gid
                    for m in list(active_clients["mobile"]): await m.send_json({"type": "FULL_SYNC", "data": state})
                    # 修复 BUG-011：过滤 action 字段，仅转发必要字段
                    for ext in list(active_clients["extension"]): 
                        await ext.send_json({
                            "command": act,
                            "groupID": gid,
                            "content": text
                        })
    finally:
        active_clients["mobile"].discard(ws)
    return ws

app = web.Application()
app.router.add_get("/", index_handler)
app.router.add_get("/api/ticket", api_current_ticket)
app.router.add_get("/ws/extension", ws_ext_handler)
app.router.add_get("/ws/mobile", ws_mobile_handler)

if __name__ == "__main__":
    local_ip = get_local_ip()
    print("=" * 60)
    print("[启动] IM 移动端中继与 AI 自动托管服务已启动！")
    print(f"手机访问: http://{local_ip}:{PORT}")
    print("=" * 60)
    web.run_app(app, host="0.0.0.0", port=PORT)