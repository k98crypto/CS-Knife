import os
import sys
import json
import time
import random
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
    # utf-8-sig：兼容带 BOM 的 config.json（记事本另存/PowerShell 重定向都可能加 BOM，
    # 否则 json.load 会失败并静默回退到默认配置 —— 表现为"密钥/端口突然失效"）
    with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f: config = json.load(f)
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
    "auto_draft": True,             # 半自动模式下是否自动起草（手动模式 = False，"AI 自动起草"可关）
    "reply_mode": "semi",           # manual=只提醒 / semi=AI 起草到输入框(不发送) / afk=AI 直接发送
    "mode_owner": "default",        # 模式是谁设的：mobile=手机端（优先）/ desktop=电脑小窗 / default=默认
    "mode_ts": 0,                   # 上面这次设置的时间戳（秒），用于"手机端优先"生效窗口
    "auto_delay_min": 60,           # 玩家说完后最少等多少秒再回复（防抢话、省 token）
    "auto_delay_max": 180,          # 最多等多少秒（1~3 分钟随机）
    "human_alerts": [],             # 需要人工介入的告警（表格里没有答案时产生，桌面 HUD 轮询播放长报警）
    "alarm_status": False,
    "im_status": 1,                 # 1=IM在线 2=IM忙碌 3=IM离线（由探针上报真实值）
    "im_status_known": False,       # 本进程是否已从"电脑网页"核实过真实状态（未核实前手机端显示"正在获取…"）
    "im_status_manual": False,      # 该状态是否来自客服手动选择（手动离线必须稳住，不能被自动改成在线）
    "extension_online": False,      # 电脑端探针是否在线（决定手机端能否远程操作）
    "probe_version": "",            # 探针（油猴脚本）版本号，来自 PROBE_HELLO/PROBE_HEARTBEAT
    "probe_last_seen": 0,           # 探针最近一次心跳时间戳（秒），手机端可据此判断新鲜度
    "category_options": [],         # 从网页级联选择器抓到的真实问题分类（供 AI 选分类）
    "companies": { "main": { "name": "示例专线", "status": 1, "conversations": {} } }
}

# ==================== IM 状态记忆（重启后不再"自己变回在线"） ====================
# 踩坑记录：state 只在内存里，中继服务一重启 im_status 就回到默认值 1（在线）；而探针
# 只在"状态变化"时才上报 —— 于是手机端会一直显示🟢在线，哪怕客服早就手动挂了离线。
# 处理：① 落盘记忆并在启动时恢复；② im_status_known 标记"本进程是否已从网页核实"；
#       ③ 手机端一连上就请探针强制复核一次（见 REQUEST_IM_STATUS）。
IM_STATE_PATH = os.path.join(BASE_DIR, "im_state.json")


def load_im_state():
    """读取上次已知的 IM 状态，返回 (status, manual, ts)。"""
    try:
        with open(IM_STATE_PATH, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        st = int(d.get("status", 1))
        if st not in (1, 2, 3):
            st = 1
        return st, bool(d.get("manual", False)), int(d.get("ts", 0) or 0)
    except Exception:
        return 1, False, 0


def save_im_state(status, manual):
    """IM 状态落盘（失败只提示，不影响主流程）。"""
    try:
        with open(IM_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"status": int(status), "manual": bool(manual), "ts": int(time.time())}, f)
    except Exception as e:
        print(f"[警告] IM 状态记忆写入失败：{e}")


def apply_im_status(status, manual=None):
    """IM 状态唯一写入口：内存 + 落盘 + 标记"已核实"，返回是否有变化。"""
    try:
        st = int(status)
    except Exception:
        st = 1
    if st not in (1, 2, 3):
        st = 1
    changed = (state.get("im_status") != st) or (not state.get("im_status_known"))
    state["im_status"] = st
    state["im_status_known"] = True
    if manual is not None:
        state["im_status_manual"] = bool(manual)
    if st == 1:
        state["alarm_status"] = False
    save_im_state(st, state.get("im_status_manual", False))
    return changed


_im_st, _im_manual, _im_ts = load_im_state()
state["im_status"] = _im_st
state["im_status_manual"] = _im_manual
state["im_status_known"] = False      # 未从电脑网页核实前，手机端显示"正在获取…"，绝不编一个"在线"
if _im_ts:
    print(f"[状态] 已恢复上次记忆：{ {1: '在线', 2: '忙碌', 3: '离线'}.get(_im_st, _im_st) }"
          f"（{time.strftime('%m-%d %H:%M', time.localtime(_im_ts))}，等待电脑网页核实）")

# ==================== 回复模式：电脑小窗与手机端共用一个权威状态 ====================
# 规则（客服要求）：
#   ① 默认 = 半自动（AI 只把草稿填进输入框，**不发送**）；
#   ② 电脑小窗也能切（手动 / 半自动 / AFK）；
#   ③ 冲突时以**手机端为准**：手机端改过之后的一段时间内，电脑小窗的改动会被拒绝并提示，
#      直到手机端再次改动或窗口期结束（避免"小窗一重启就把手机端的 AFK 顶回半自动"）。
MODE_FILE = os.path.join(BASE_DIR, "mode_state.json")
MOBILE_PRIORITY_SEC = int(config.get("mobile_mode_priority_sec", 600))   # 手机端优先窗口（秒）
MODE_LABEL = {"manual": "✋手动（只提醒，不起草）", "semi": "🤖半自动（起草到输入框，不发送）",
              "afk": "🚀AFK 全自动（AI 直接回复）"}


def load_mode_state():
    """恢复上次的回复模式（重启不丢，也不会被"小窗启动"顶回默认）。"""
    try:
        with open(MODE_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        mode = str(d.get("reply_mode") or "semi")
        if mode not in MODE_LABEL:
            mode = "semi"
        owner = str(d.get("mode_owner") or "default")
        if owner not in ("mobile", "desktop", "default"):
            owner = "default"
        return mode, owner, float(d.get("mode_ts") or 0)
    except Exception:
        return "semi", "default", 0.0


def save_mode_state():
    try:
        with open(MODE_FILE, "w", encoding="utf-8") as f:
            json.dump({"reply_mode": state.get("reply_mode", "semi"),
                       "mode_owner": state.get("mode_owner", "default"),
                       "mode_ts": state.get("mode_ts", 0)}, f)
    except Exception as e:
        print(f"[警告] 回复模式落盘失败：{e}")


def apply_reply_mode(mode: str, source: str = "mobile", force: bool = False):
    """切换回复模式。返回 (ok, note)：手机端优先窗口内拒绝电脑小窗的改动。"""
    if mode not in MODE_LABEL:
        return False, "未知模式"
    now = time.time()
    owner = state.get("mode_owner") or "default"
    if (not force) and source == "desktop" and owner == "mobile" \
            and (now - float(state.get("mode_ts") or 0)) <= MOBILE_PRIORITY_SEC:
        left = int(MOBILE_PRIORITY_SEC - (now - float(state.get("mode_ts") or 0)))
        return False, f"手机端已设为{MODE_LABEL.get(state.get('reply_mode'))}，以手机端为准（约 {left} 秒后可再切）"
    state["reply_mode"] = mode
    state["afk_mode"] = (mode == "afk")
    state["auto_draft"] = (mode != "manual")
    state["mode_owner"] = source
    state["mode_ts"] = now
    save_mode_state()
    print(f"[模式] {MODE_LABEL[mode]} · 来源：{'手机端' if source == 'mobile' else ('电脑小窗' if source == 'desktop' else '默认')}")
    return True, MODE_LABEL[mode]


_mode, _mode_owner, _mode_ts = load_mode_state()
state["reply_mode"] = _mode
state["mode_owner"] = _mode_owner
state["mode_ts"] = _mode_ts
state["afk_mode"] = (_mode == "afk")
state["auto_draft"] = (_mode != "manual")
print(f"[模式] 已加载：{MODE_LABEL[_mode]}（来源：{_mode_owner}）")


# 客服反馈：玩家一发消息就秒回，既像脚本（可能被系统判定），又浪费 token，还容易打断玩家说话。
# 规则：玩家发来消息后等 1~3 分钟（随机）；期间再来消息就**重新计时**；确认不发了才回复。
AUTO_DELAY_MIN = max(0.0, float(config.get("auto_reply_delay_min_sec", 60)))
AUTO_DELAY_MAX = max(0.0, float(config.get("auto_reply_delay_max_sec", 180)))
if AUTO_DELAY_MAX < AUTO_DELAY_MIN:
    AUTO_DELAY_MIN, AUTO_DELAY_MAX = AUTO_DELAY_MAX, AUTO_DELAY_MIN
state["auto_delay_min"] = int(AUTO_DELAY_MIN)
state["auto_delay_max"] = int(AUTO_DELAY_MAX)

# 表格里查不到答案时，先给玩家这句安抚话术（严格照客服给的原话）
HOLD_TEXT = "亲爱的玩家，您的问题我已经收到啦，正在为您查询相关信息，请稍等片刻哦~"

_PENDING = {}          # gid -> asyncio.Task：正在等待"玩家说完"的延迟回复


def _pending_tasks_snapshot():
    return {"pending": len(_PENDING), "gids": list(_PENDING.keys())[:10]}


def pick_reply_delay():
    """1~3 分钟随机（可在 config.json / 手机端调整）。"""
    lo = float(state.get("auto_delay_min", 60) or 0)
    hi = float(state.get("auto_delay_max", 180) or 0)
    if hi < lo:
        lo, hi = hi, lo
    if hi <= 0:
        return 0.0
    return random.uniform(lo, hi)


def pick_greeting():
    """开场语严格取自表格（话术库"没有描述问题/直接转人工"那一类）；表格缺失时才用兜底句。"""
    try:
        pool = [str(x).strip() for x in (getattr(core, "tpl_no_desc", None) or []) if str(x).strip()]
        if pool:
            return random.choice(pool)
    except Exception:
        pass
    return "亲爱的玩家，欢迎来到本游戏~ 请问有什么可以帮您？"


def last_player_ts(conv):
    """会话里最后一条玩家消息的时间戳。"""
    try:
        for m in reversed(conv.get("msgs") or []):
            if isinstance(m, dict) and m.get("sender") == "player":
                return int(m.get("ts") or 0)
    except Exception:
        pass
    return 0


def should_auto_reply(conv) -> bool:
    """是否该由程序回复：
       ① 会话存在且最后一条是**玩家**发言（客服最后发言=等玩家，不抢话）
       ② 这条玩家消息还没被回复过（避免重复回、避免和人工回复打架）
    """
    if not isinstance(conv, dict):
        return False
    msgs = conv.get("msgs")
    if not isinstance(msgs, list) or not msgs:
        return False
    last = msgs[-1] if isinstance(msgs[-1], dict) else {}
    if last.get("sender") != "player":
        return False
    return int(conv.get("last_reply_ts") or 0) < int(last.get("ts") or 0)


def cancel_pending_reply(gid: str):
    task = _PENDING.pop(gid, None)
    if task is not None and not task.done():
        task.cancel()


def schedule_auto_reply(gid: str):
    """（重新）安排延迟回复：玩家每来一条消息就重置计时，确保把话说完再回。"""
    if not gid:
        return
    cancel_pending_reply(gid)
    delay = pick_reply_delay()
    try:
        _PENDING[gid] = asyncio.create_task(_auto_reply_after(gid, delay))
    except RuntimeError:
        return                       # 没有事件循环（理论上不会发生）
    print(f"[自动回复] {gid} 已排队：{int(delay)} 秒后再回（玩家若继续发言会重新计时）")


async def _auto_reply_after(gid: str, delay: float):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return                       # 玩家又发消息了 -> 由新的定时器负责
    _PENDING.pop(gid, None)
    conv = state["companies"]["main"]["conversations"].get(gid)
    if not conv:
        return
    if not should_auto_reply(conv):
        print(f"[自动回复] {gid} 已跳过（最后一条不是玩家发言 / 已经回过了）")
        return
    await handle_ai_automation(gid, source="auto-delay")


active_clients = {"extension": set(), "mobile": set()}

# ==================== 探针认证头独立存放（安全隔离） ====================
# 注意：绝对不能放进 state！
# state 会被 FULL_SYNC 全量广播给手机端（共 10 处），放入 state 等于把 IM Token 泄露到手机浏览器。
# 本变量仅供服务端内部使用，永不参与任何序列化/广播。
IM_AUTH_HEADERS = {}
_IM_AUTH_FP = {"value": None}   # 上一次认证头的指纹，用于去重，避免重复覆盖与日志刷屏

# 最近一次有消息活动的工单 ID（供 /api/ticket 定位"当前工单"，比按插入顺序取最后一个更准）
_LAST_ACTIVE = {"gid": None}

# ==================== 探针自检元数据（排障用，见 GET /api/diag） ====================
# 作用：把"油猴脚本到底加载了没、跑的是哪一版"从猜测变成可查事实。
# _PROBE_CONNS 按"每条连接"记录，避免旧脚本（不上报版本）连接后
# 还继续显示上一次的版本号（第七轮实测到的误导：界面显示 v7.2，实际浏览器里是旧脚本）。
_PROBE_CONNS = {}            # ws -> {"version","page","ua","last_seen","hello"}
_PROBE_META = {"version": "", "page": "", "ua": "", "last_seen": 0.0, "hello_count": 0}
SERVER_START = time.time()
SERVER_VER = "7.4"


def _probe_refresh():
    """按"最近一次上报"刷新全局探针元数据；没有任何上报时清空（不回显旧值）。"""
    live = [v for v in _PROBE_CONNS.values() if v.get("last_seen")]
    if not live:
        _PROBE_META.update({"version": "", "page": "", "ua": "", "last_seen": 0.0, "hello_count": 0})
        state["probe_version"] = ""
        state["probe_last_seen"] = 0
        return
    newest = max(live, key=lambda v: v["last_seen"])
    _PROBE_META["version"] = newest.get("version", "") or ""
    _PROBE_META["page"] = newest.get("page", "") or ""
    _PROBE_META["ua"] = newest.get("ua", "") or ""
    _PROBE_META["last_seen"] = newest["last_seen"]
    _PROBE_META["hello_count"] = sum(1 for v in live if v.get("hello"))
    state["probe_version"] = _PROBE_META["version"]
    state["probe_last_seen"] = int(_PROBE_META["last_seen"])


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

def build_chat_history_str(group_id: str, for_ai: bool = False) -> str:
    """拼给 AI 看的聊天记录。

    for_ai=True 时会**自行判断要不要把客服说的话带进去**（客服反馈的第 5 点）：
      ① 最后一条是客服 -> 玩家还没回，直接返回空串（不抢话）
      ② 丢掉"玩家开口之前"的客服发言（多半是上一轮流程/开场白，塞进去只会干扰）
      ③ 客服发言最多只留最近 2 条（省 token，避免把整段流程塞给 AI）
    """
    msgs = state["companies"]["main"]["conversations"].get(group_id, {}).get("msgs", [])
    if not isinstance(msgs, list):
        return ""
    rows = [m for m in msgs if isinstance(m, dict) and m.get("text")]
    if for_ai:
        if rows and rows[-1].get("sender") != "player":
            return ""
        first_player = next((i for i, m in enumerate(rows) if m.get("sender") == "player"), None)
        if first_player is None:
            return ""
        rows = rows[first_player:]
        agent_idx = [i for i, m in enumerate(rows) if m.get("sender") != "player"]
        if len(agent_idx) > 2:
            keep = set(agent_idx[-2:])
            rows = [m for i, m in enumerate(rows) if (m.get("sender") == "player") or (i in keep)]
    recent_msgs = rows[-8:] if len(rows) > 8 else rows
    lines = []
    for m in recent_msgs:
        text = m.get("text", "")
        if not text:
            continue
        lines.append(f"{'【玩家】' if m.get('sender') == 'player' else '【客服】'} {text}")
    return "\n".join(lines)


def _say_as_agent(group_id: str, text: str):
    """把"程序自动发出的客服消息"记进会话（时间线/手机端立刻可见）。"""
    conv = state["companies"]["main"]["conversations"].setdefault(group_id, {"name": group_id, "msgs": []})
    if not isinstance(conv.get("msgs"), list):
        conv["msgs"] = []
    now_ms = int(time.time() * 1000)
    conv["msgs"].append({"sender": "agent", "text": text,
                         "time": datetime.now().strftime("%H:%M:%S"), "ts": now_ms})
    conv["updatedAt"] = now_ms
    conv["last_reply_ts"] = last_player_ts(conv)
    _LAST_ACTIVE["gid"] = group_id
    return conv

async def send_hold_and_alert(group_id: str, conv: dict, history_str: str, reply: str = ""):
    """表格里查不到答案时：
       ① 给玩家发安抚话术（客户指定的原话）
       ② 长报警（Bark + 手机端专属提示音 + 桌面 HUD 长鸣）——与"新消息提示音""掉线警报"都不同
       ③ 该会话置顶 + 把玩家信息和问题总结复制给客服（复制由桌面 HUD 完成）
    """
    # ① 先安抚玩家（有电脑端在就连网页一起发，保证玩家真的收到）
    for ext in list(active_clients["extension"]):
        await safe_send(ext, {"command": "SEND_REPLY", "content": HOLD_TEXT, "groupID": group_id})
    _say_as_agent(group_id, HOLD_TEXT)

    # ② 问题总结（给客服看的，不是给玩家的）
    try:
        summary = await asyncio.get_event_loop().run_in_executor(None, core.summarize_player_issue, history_str)
    except Exception as e:
        summary = f"（总结失败：{e}）"
    summary = str(summary or "").strip()[:400]

    name = str(conv.get("name") or group_id)
    alert = {
        "id": f"{int(time.time() * 1000)}-{group_id}",
        "ts": int(time.time() * 1000),
        "groupID": group_id,
        "name": name,
        "playerInfo": str(conv.get("playerInfo") or ""),
        "summary": summary,
        "hold": HOLD_TEXT,
        "reason": "表格里没有对应答案，已发送安抚话术",
        "ai_reply": str(reply or "")[:300],
    }
    # ③ 置顶 + 入队告警
    conv["pinned"] = True
    conv["alert"] = True
    conv["alertTs"] = alert["ts"]
    state["human_alerts"] = ([alert] + [a for a in state.get("human_alerts", []) if a.get("groupID") != group_id])[:20]

    print(f"[人工] 🙋 需要人工介入：{name}（{group_id}）· {summary[:60]}")
    push_bark("🙋 需要人工介入", f"{name}：表格里没有对应答案，已发安抚话术", group_id)
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "HUMAN_ALERT", "groupID": group_id, "name": name,
                            "message": f"{name}：{alert['reason']}", "summary": summary,
                            "playerInfo": alert["playerInfo"]})
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    return alert


async def handle_ai_automation(group_id: str, source: str = "", force: bool = False):
    conv = state["companies"]["main"]["conversations"].get(group_id)
    if not conv:
        return

    # ★ 「手动模式」只提醒、不自动起草：客服说"AI 自动起草关不掉"，就是这里没有开关。
    #   （force=True 表示"手机上主动点的按钮"，任何时候都放行）
    if not force and not state.get("afk_mode") and not state.get("auto_draft", True):
        for m in list(active_clients["mobile"]):
            await safe_send(m, {"type": "AI_STATUS", "groupID": group_id, "status": "manual",
                                "message": "新消息（手动模式：AI 未自动起草）"})
        return

    # ★ 只在"玩家最后发言、且这条还没被回过"时才动手（不抢话、不重复回）
    if not force and not should_auto_reply(conv):
        return

    history_str = build_chat_history_str(group_id, for_ai=True)
    if not history_str:
        return

    try:
        tag, reply = await asyncio.get_event_loop().run_in_executor(None, core.process_ticket_f9, history_str)
    except Exception:
        return

    if tag == "GREETING" and conv.get("greeted") and not force:
        return                       # 开场语已经在首条消息时发过了，不再重复发一遍

    if tag == "WAITING" and not force:
        for m in list(active_clients["mobile"]):
            await safe_send(m, {"type": "AI_STATUS", "groupID": group_id, "status": "waiting", "message": "等待玩家回复中"})
        return

    if "【规章库未收录" in reply:
        await send_hold_and_alert(group_id, conv, history_str, reply)
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
                for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "ACTION_REPLY_CLOSE", "category": "其他", "categoryPath": (config.get("close_category_path") or ["一级分类", "二级分类"]), "defaultCategory": config.get("close_category_default", "其他"), "content": reply, "groupID": group_id})
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
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <meta name="theme-color" content="#0B0C0E">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <title>客服助手 · 客服台</title>
  <style>
    :root{
      --bg:#0B0C0E; --bg-soft:#101216; --card:#16181D; --card-2:#1D2026;
      --line:#262A32; --line-soft:#1E2127;
      --text:#E9EBEE; --text-2:#9BA3AE; --text-3:#6B7280;
      --accent:#7CC4FF; --brand:#4EC9B0; --ok:#34D399; --warn:#FBBF24; --danger:#F87171;
      --radius:16px;
      --safe-top:env(safe-area-inset-top, 0px); --safe-bottom:env(safe-area-inset-bottom, 0px);
    }
    *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
    html,body{height:100%}
    body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei UI",system-ui,sans-serif;
         background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;overflow:hidden}
    #app { position: fixed; left:0; right:0; top:0; bottom:0; display:flex; flex-direction:column; overflow:hidden;
           background:radial-gradient(120% 55% at 50% 0%, #14171C 0%, var(--bg) 62%); }
    @supports (height: 100dvh) { #app { height: 100dvh; } }

    /* ==================== 顶部：标题 + 两个下拉框 ==================== */
    header{flex:0 0 auto;display:flex;flex-direction:column;gap:9px;position:relative;z-index:20;
           padding:calc(var(--safe-top) + 12px) 14px 10px;border-bottom:1px solid var(--line-soft);
           background:rgba(11,12,14,.82);backdrop-filter:saturate(180%) blur(14px);-webkit-backdrop-filter:saturate(180%) blur(14px)}
    .hdr-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .brand{display:flex;align-items:center;gap:8px;flex:1 1 92px;min-width:0;overflow:hidden}
    .brand-logo{width:28px;height:28px;border-radius:10px;display:grid;place-items:center;font-size:14px;flex:0 0 auto;
                background:linear-gradient(145deg, rgba(78,201,176,.30), rgba(124,196,255,.22));
                border:1px solid rgba(124,196,255,.28)}
    .brand-txt{display:flex;flex-direction:column;line-height:1.16;min-width:0;overflow:hidden}
    .brand-name{font-size:14.5px;font-weight:700;letter-spacing:.2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .brand-sub{font-size:10.5px;color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .hdr-controls{display:flex;align-items:center;gap:6px;flex:0 0 auto}
    /* 顶部下拉：自绘按钮 + 底部选择面板（不再用原生 select，避免"框框"和卡顿） */
    .pill{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 10px;border-radius:999px;
          background:var(--card-2);border:1px solid var(--line);color:var(--text);font:inherit;font-size:12.5px;
          font-weight:650;white-space:nowrap;cursor:pointer;-webkit-appearance:none;appearance:none}
    .pill:active{background:#242A33}
    .pill .dot{width:7px;height:7px;border-radius:50%;background:var(--text-3);flex:0 0 auto}
    .pill .dot.ok{background:var(--ok);box-shadow:0 0 0 3px rgba(52,211,153,.16)}
    .pill .dot.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(251,191,36,.16)}
    .pill .dot.bad{background:var(--danger);box-shadow:0 0 0 3px rgba(248,113,113,.16)}
    .pill .caret{font-size:9px;color:var(--text-3)}
    .pill.off{opacity:.55}
    #sheet-backdrop{position:fixed;left:0;right:0;top:0;bottom:0;background:rgba(0,0,0,.5);z-index:60;
                    opacity:0;pointer-events:none;transition:opacity .16s}
    #sheet-backdrop.show{opacity:1;pointer-events:auto}
    #sheet{position:fixed;left:10px;right:10px;bottom:calc(var(--safe-bottom) + 12px);z-index:61;
           background:var(--card);border:1px solid var(--line);border-radius:18px;padding:10px;
           box-shadow:0 18px 50px rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .16s}
    #sheet.show{opacity:1;pointer-events:auto}
    .sheet-title{font-size:11.5px;font-weight:700;color:var(--text-3);letter-spacing:.4px;padding:4px 10px 8px}
    .sheet-item{display:flex;align-items:center;gap:10px;padding:12px;border-radius:12px;font-size:14.5px;color:var(--text)}
    .sheet-item:active{background:var(--card-2)}
    .sheet-item.cur{background:rgba(124,196,255,.12);color:#D3ECFF}
    .sheet-item .tick{margin-left:auto;color:var(--accent);font-weight:700}
    .sheet-item .hint{margin-left:auto;font-size:11px;color:var(--text-3);text-align:right}
    .sheet-cancel{margin-top:6px;text-align:center;padding:12px;border-radius:12px;font-size:14px;color:var(--text-2)}
    .chips{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
    .chip{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;color:var(--text-2);
          background:var(--bg-soft);border:1px solid var(--line-soft);padding:3px 9px;border-radius:999px}
    .chip .dot{width:6px;height:6px;border-radius:50%;background:var(--text-3)}
    .chip .dot.ok{background:var(--ok)} .chip .dot.bad{background:var(--danger)}

    /* ==================== 视图容器 ==================== */
    .view-container{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;position:relative}
    .view{display:none;flex:1 1 auto;min-height:0;flex-direction:column}
    .view.active { display: flex; }

    /* ==================== 会话列表（微信式） ==================== */
    .list-head{flex:0 0 auto;display:flex;align-items:baseline;justify-content:space-between;padding:12px 16px 4px}
    .list-title{font-size:12.5px;font-weight:700;color:var(--text-2);letter-spacing:.4px}
    .list-count{font-size:11px;color:var(--text-3)}
    .conv-list{flex:1 1 auto;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;
               padding:4px 10px calc(var(--safe-bottom) + 20px)}
    .conv-card{display:flex;align-items:center;gap:12px;padding:12px;margin-bottom:8px;border-radius:var(--radius);
               background:var(--card);border:1px solid var(--line-soft);transition:background .15s,border-color .15s}
    .conv-card:active{background:var(--card-2);border-color:var(--line)}
    .avatar{width:44px;height:44px;border-radius:14px;flex:0 0 auto;display:grid;place-items:center;
            font-size:16px;font-weight:700;color:#0B0C0E;background:linear-gradient(145deg,#7CC4FF,#4EC9B0)}
    .conv-body{flex:1 1 auto;min-width:0}
    .conv-top{display:flex;align-items:center;justify-content:space-between;gap:8px}
    .conv-name{display:flex;align-items:center;gap:6px;min-width:0;font-size:15px;font-weight:650;
               white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .conv-time{flex:0 0 auto;font-size:11px;color:var(--text-3);font-variant-numeric:tabular-nums}
    .conv-lastmsg{margin-top:3px;font-size:12.5px;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .conv-info{margin-top:2px;font-size:10.5px;color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .unread-dot{width:8px;height:8px;border-radius:50%;background:var(--danger);flex:0 0 auto;
                box-shadow:0 0 0 3px rgba(248,113,113,.18)}
    .conv-empty{text-align:center;color:var(--text-3);font-size:13px;line-height:2;padding:58px 24px}
    /* 需要人工介入的横幅（表格里没有答案时） */
    #human-banner{flex:0 0 auto;display:none;gap:10px;align-items:flex-start;margin:10px 10px 0;padding:11px 12px;
                  border-radius:14px;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.45)}
    #human-banner.show{display:flex}
    #human-banner .hb-ico{font-size:18px;line-height:1.1}
    #human-banner .hb-body{flex:1 1 auto;min-width:0}
    #human-banner .hb-title{font-size:13px;font-weight:700;color:var(--warn)}
    #human-banner .hb-text{font-size:12px;color:var(--text-2);margin-top:3px;line-height:1.6;word-break:break-word}
    #human-banner .hb-btn{flex:0 0 auto;height:30px;padding:0 12px;border-radius:999px;border:1px solid rgba(251,191,36,.5);
                          background:transparent;color:var(--warn);font:inherit;font-size:12px;font-weight:650}
    .pin-badge{font-size:10px;color:var(--warn);flex:0 0 auto}

    /* ==================== 聊天页 ==================== */
    .chat-nav{flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:9px 12px;
              border-bottom:1px solid var(--line-soft);background:var(--bg-soft)}
    .back-btn{width:34px;height:34px;border-radius:12px;flex:0 0 auto;display:grid;place-items:center;
              background:var(--card);border:1px solid var(--line);color:var(--text);font-size:16px}
    .back-btn:active{background:var(--card-2)}
    .chat-title{flex:1 1 auto;min-width:0;font-size:15px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .chat-meta{flex:0 0 auto;font-size:10.5px;color:var(--text-3);font-variant-numeric:tabular-nums}
    .action-bar{flex:0 0 auto;display:flex;gap:7px;padding:9px 12px;overflow-x:auto;-webkit-overflow-scrolling:touch;
                border-bottom:1px solid var(--line-soft)}
    .action-bar::-webkit-scrollbar{display:none}
    .action-btn{flex:0 0 auto;height:32px;padding:0 13px;border-radius:999px;font-size:12.5px;font-weight:650;
                white-space:nowrap;background:var(--card);border:1px solid var(--line);color:var(--text)}
    .action-btn:active{background:var(--card-2)}
    .action-btn.ai{color:#D3ECFF;border-color:rgba(124,196,255,.34);
                   background:linear-gradient(135deg, rgba(78,201,176,.20), rgba(124,196,255,.16))}
    .chat-stream{flex:1 1 auto;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;
                 padding:14px 12px 10px;display:flex;flex-direction:column;gap:10px}
    .player-card{flex:0 0 auto;margin:8px 12px 0;padding:10px 12px;border-radius:14px;background:var(--card);
                 border:1px solid var(--line-soft);font-size:12px;color:var(--text-2);line-height:1.75;
                 white-space:pre-wrap;word-break:break-word}
    .player-card .pc-tip{color:var(--text-3);font-size:10.5px}
    .msg-row{display:flex;flex-direction:column;max-width:82%}
    .msg-row.player{align-self:flex-start;align-items:flex-start}
    .msg-row.agent{align-self:flex-end;align-items:flex-end}
    .msg-bubble{padding:9px 13px;border-radius:16px;font-size:14.5px;line-height:1.5;word-break:break-word;white-space:pre-wrap}
    .msg-row.player .msg-bubble{background:#22262D;color:var(--text);border-bottom-left-radius:6px}
    .msg-row.agent .msg-bubble{background:linear-gradient(135deg,#2E7CF6,#1A73E8);color:#FFFFFF;border-bottom-right-radius:6px}
    .msg-time{margin-top:4px;font-size:10px;color:var(--text-3);font-variant-numeric:tabular-nums}
    .input-bar{flex:0 0 auto;display:flex;align-items:center;gap:9px;padding:10px 12px calc(var(--safe-bottom) + 10px);
               border-top:1px solid var(--line-soft);background:var(--bg-soft)}
    .chat-text-input{flex:1 1 auto;min-width:0;height:40px;border-radius:999px;padding:0 16px;font-size:15px;
                     color:var(--text);background:var(--card);border:1px solid var(--line);outline:none}
    .chat-text-input:focus{border-color:rgba(124,196,255,.5)}
    .send-btn{flex:0 0 auto;width:40px;height:40px;border-radius:50%;border:none;font-size:17px;font-weight:700;
              color:#0B0C0E;background:linear-gradient(145deg,#7CC4FF,#4EC9B0)}
    .send-btn:active{opacity:.8}

    /* ==================== 轻提示 / 警报 ==================== */
    #toast{position:fixed;left:0;right:0;margin:0 auto;width:max-content;max-width:84%;
           top:calc(var(--safe-top) + 14px);z-index:99;padding:10px 16px;border-radius:999px;
           background:rgba(29,32,38,.97);border:1px solid var(--line);color:var(--text);font-size:13px;
           box-shadow:0 10px 30px rgba(0,0,0,.45);opacity:0;pointer-events:none;transition:opacity .2s}
    #toast.show{opacity:1}
    #alarm-overlay{position:fixed;left:0;right:0;top:0;bottom:0;z-index:9999;display:flex;flex-direction:column;
                   align-items:center;justify-content:center;gap:14px;opacity:0;pointer-events:none;transition:opacity .2s;
                   background:radial-gradient(120% 80% at 50% 0%, #B00020 0%, #7A0016 70%)}
    #alarm-overlay.active{opacity:1;pointer-events:auto}
    .alarm-icon{font-size:52px;animation:blink 1s infinite}
    .alarm-title{font-size:24px;font-weight:800;color:#fff;letter-spacing:1px}
    .alarm-sub{font-size:13px;color:rgba(255,255,255,.85);text-align:center;line-height:1.7}
    .silence-btn{margin-top:10px;padding:12px 30px;border-radius:999px;border:none;font-size:15px;font-weight:700;
                 color:#7A0016;background:#fff;box-shadow:0 8px 24px rgba(0,0,0,.35)}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:.45}}
  </style>
</head>
<body>
  <div id="alarm-overlay">
    <div class="alarm-icon">🚨</div>
    <div class="alarm-title">异常掉线警报</div>
    <div class="alarm-sub">VPN 或网页网络连接断开<br>请立即检查电脑端工作台</div>
    <button class="silence-btn" onclick="silenceAlarm()">静音并忽略</button>
  </div>

  <div id="app">
    <header>
      <div class="hdr-top">
        <div class="brand">
          <div class="brand-logo">⚡</div>
          <div class="brand-txt">
            <div class="brand-name">客服助手 · 客服台</div>
            <div class="brand-sub" id="brand-sub">正在连接中继…</div>
          </div>
        </div>
        <div class="hdr-controls">
          <button type="button" class="pill" id="im-pill" onclick="openSheet('im')">
            <span class="dot idle" id="im-dot"></span>
            <span id="im-label">连接中…</span>
            <span class="caret">▾</span>
          </button>
          <button type="button" class="pill" id="afk-pill" onclick="openSheet('mode')">
            <span class="dot idle" id="afk-dot"></span>
            <span id="afk-label">半自动</span>
            <span class="caret">▾</span>
          </button>
        </div>
      </div>
      <div class="chips" id="link-chips"></div>
    </header>

    <div id="sheet-backdrop" onclick="closeSheet()"></div>
    <div id="sheet">
      <div class="sheet-title" id="sheet-title">选择</div>
      <div id="sheet-body"></div>
      <div class="sheet-cancel" onclick="closeSheet()">取消</div>
    </div>
    <div id="toast"></div>

    <div class="view-container">
      <div class="view active" id="list-view">
        <div id="human-banner">
          <div class="hb-ico">🙋</div>
          <div class="hb-body">
            <div class="hb-title" id="hb-title">需要人工介入</div>
            <div class="hb-text" id="hb-text"></div>
          </div>
          <button type="button" class="hb-btn" onclick="ackHumanAlert()">知道了</button>
        </div>
        <div class="list-head"><div class="list-title">会话</div><div class="list-count" id="list-count"></div></div>
        <div class="conv-list" id="conv-container"></div>
      </div>

      <div class="view" id="chat-view">
        <div class="chat-nav">
          <button class="back-btn" onclick="popChat()">←</button>
          <div class="chat-title" id="chat-player-name">玩家</div>
          <div class="chat-meta" id="chat-ticket-id"></div>
        </div>
        <div class="action-bar">
          <button class="action-btn ai" onclick="execCommand('AI_CLOSE')">🤖 AI 回复并关单</button>
          <button class="action-btn ai" onclick="execCommand('F9')">✨ AI 起草</button>
          <button class="action-btn" onclick="execCommand('HANGUP')">⏸ 挂起</button>
          <button class="action-btn" onclick="execCommand('RESUME')">▶ 恢复</button>
          <button class="action-btn" onclick="execCommand('CLOSE')">✅ 关单</button>
        </div>
        <div class="chat-stream" id="chat-stream"></div>
        <div class="player-card" id="player-card" onclick="togglePlayerCard()"></div>
        <div class="input-bar">
          <input type="text" class="chat-text-input" id="chat-input" placeholder="输入回复内容…">
          <button class="send-btn" onclick="execCommand('SEND')">↑</button>
        </div>
      </div>
    </div>
  </div>

  <script>
    let globalState = null; let activeGroupId = null; let ws = null;
    let audioCtx = null; let sirenInterval = null;
    let wsAttempts = 0;
    let imStatusRequested = false;      // 已向电脑端索要过真实状态（4 秒内不重复要）

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

    // ==================== 顶部两个下拉框：IM 状态 / 回复模式 ====================
    // 状态一律以"电脑网页"为准：没拿到之前只显示"正在获取…"，绝不自己编一个"在线"给客服看。
    const IM_STATUS_TEXT = { 1: 'IM 在线', 2: 'IM 忙碌', 3: 'IM 离线' };
    const IM_DOT_CLASS = { 1: 'ok', 2: 'warn', 3: 'bad' };
    const IM_STATUS_FULL = { 1: '🟢 IM 在线', 2: '🟡 IM 忙碌', 3: '🔴 IM 离线' };
    const MODE_TEXT = { manual: '手动', semi: '半自动', afk: 'AFK 全自动' };

    function extensionOffline() {
        return !globalState || globalState.extension_online === false;
    }

    function setDot(id, cls) {
        const el = document.getElementById(id);
        if (el) el.className = 'dot ' + cls;
    }

    function setText(id, text) {
        const el = document.getElementById(id);
        if (el) el.innerText = text;
    }

    // 请求电脑端把"网页上的真实 IM 状态"重新上报一次（手机端启动 / 回到前台时调用）
    function requestStatusFromPC() {
        if (imStatusRequested) return;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        imStatusRequested = true;
        sendMsg({ action: 'REQUEST_IM_STATUS' });
        setTimeout(function () { imStatusRequested = false; }, 4000);
    }

    function renderIMStatus() {
        const pill = document.getElementById('im-pill');
        if (!globalState) {
            setText('im-label', '连接中…'); setDot('im-dot', 'idle');
            if (pill) pill.className = 'pill off';
            return;
        }
        if (extensionOffline()) {
            setText('im-label', '电脑未连接'); setDot('im-dot', 'bad');
            if (pill) pill.className = 'pill off';
            return;
        }
        if (!globalState.im_status_known) {
            // 还没从电脑网页拿到真实状态：显示"正在获取"，并主动去要一次
            setText('im-label', '正在获取…'); setDot('im-dot', 'warn');
            if (pill) pill.className = 'pill off';
            requestStatusFromPC();
            return;
        }
        const st = Number(globalState.im_status) || 1;
        if (pill) pill.className = 'pill';
        setText('im-label', IM_STATUS_TEXT[st] || IM_STATUS_TEXT[1]);
        setDot('im-dot', IM_DOT_CLASS[st] || 'idle');
    }

    function currentMode() {
        if (!globalState) return 'semi';
        if (globalState.reply_mode) return String(globalState.reply_mode);
        return globalState.afk_mode ? 'afk' : 'semi';
    }

    function renderAFK() {
        const pill = document.getElementById('afk-pill');
        const mode = currentMode();
        setText('afk-label', MODE_TEXT[mode] || '半自动');
        setDot('afk-dot', mode === 'afk' ? 'ok' : (mode === 'manual' ? 'idle' : 'warn'));
        if (pill) pill.className = 'pill' + (extensionOffline() ? ' off' : '');
    }

    // 会话卡片副信息（UID / 关键字段），让客服一眼分清"玩家信息"到底是谁
    function convInfo(conv) {
        const raw = String((conv && conv.playerInfo) || '');
        if (!raw) return '';
        const mu = raw.match(/(?:UID|uid|账号|account)[ ]*[:：][ ]*([A-Za-z0-9_-]{2,24})/);
        if (mu) return 'UID ' + mu[1];
        const parts = raw.split('|')
            .map(function (s) { return String(s).replace(/^[^:：]{1,10}[:：]/, '').trim(); })
            .filter(Boolean);
        return parts.length ? parts.slice(0, 2).join(' · ').slice(0, 26) : '';
    }

    // 玩家完整信息（聊天页，点一下展开/收起）
    let playerCardOpen = false;
    const NL = String.fromCharCode(10);   // 不写反斜杠转义：见 H5 顶部约定
    function renderPlayerCard(conv) {
        const box = document.getElementById('player-card');
        if (!box) return;
        const raw = String((conv && conv.playerInfo) || '').trim();
        if (!raw) {
            if (box.style) box.style.display = 'none';
            box.innerText = '';
            return;
        }
        const lines = raw.split('|').map(function (s) { return String(s).trim(); }).filter(Boolean);
        const show = playerCardOpen ? lines : lines.slice(0, 2);
        let text = '👤 ' + show.join(NL);
        if (lines.length > 2) text += NL + (playerCardOpen ? '（点一下收起）' : '（点一下展开全部 ' + lines.length + ' 项）');
        if (box.style) box.style.display = 'block';
        box.innerText = text;
    }

    function togglePlayerCard() {
        playerCardOpen = !playerCardOpen;
        renderPlayerCard(getConv(activeGroupId));
    }

    // ==================== 底部选择面板（自绘下拉，替代原生 select） ====================
    // 真按钮 + 面板：既没有原生 select 的"框框/卡顿"，也不会像旧版 <div> 那样在 iOS 上点不动。
    function openSheet(kind) {
        const sheet = document.getElementById('sheet');
        const back = document.getElementById('sheet-backdrop');
        const title = document.getElementById('sheet-title');
        const body = document.getElementById('sheet-body');
        if (!sheet || !body) return;
        if (extensionOffline()) { toast('电脑端未连接，请先打开客服工作台'); return; }

        if (kind === 'im') {
            if (title) title.innerText = 'IM 状态（取自电脑网页）';
            if (!globalState || !globalState.im_status_known) {
                body.innerHTML = '<div class="sheet-item"><span class="hint">正在从电脑网页获取真实状态…</span></div>';
            } else {
                const cur = Number(globalState.im_status) || 1;
                body.innerHTML = [1, 2, 3].map(function (n) {
                    return '<div class="sheet-item' + (n === cur ? ' cur' : '') + '" data-set="im:' + n + '">' +
                        IM_STATUS_FULL[n] + (n === cur ? '<span class="tick">✓</span>' : '') + '</div>';
                }).join('');
            }
        } else {
            if (title) title.innerText = '回复模式';
            const cur = currentMode();
            const list = [
                ['manual', '✋ 手动', '只提醒新消息，AI 不自动起草'],
                ['semi', '🤖 半自动', 'AI 自动起草到回复框，你确认后发送'],
                ['afk', '🚀 AFK 全自动', 'AI 直接回复玩家（短期离开时用）'],
            ];
            body.innerHTML = list.map(function (it) {
                return '<div class="sheet-item' + (it[0] === cur ? ' cur' : '') + '" data-set="mode:' + it[0] + '">' +
                    '<span>' + it[1] + '</span>' +
                    (it[0] === cur ? '<span class="tick">✓</span>' : '<span class="hint">' + it[2] + '</span>') + '</div>';
            }).join('');
        }

        // 事件委托（不把数据拼进内联 onclick）
        body.querySelectorAll('.sheet-item[data-set]').forEach(function (el) {
            el.addEventListener('click', function () {
                const v = String(el.dataset.set || '');
                closeSheet();
                if (v.indexOf('im:') === 0) setIMStatus(Number(v.slice(3)));
                else if (v.indexOf('mode:') === 0) setReplyMode(v.slice(5));
            });
        });
        if (back) back.classList.add('show');
        sheet.classList.add('show');
    }

    function closeSheet() {
        const sheet = document.getElementById('sheet');
        const back = document.getElementById('sheet-backdrop');
        if (sheet) sheet.classList.remove('show');
        if (back) back.classList.remove('show');
    }

    function setIMStatus(st) {
        st = Number(st);
        if (st !== 1 && st !== 2 && st !== 3) return false;
        if (extensionOffline()) { toast('电脑端未连接，无法切换状态'); renderIMStatus(); return false; }
        if (!sendMsg({ action: 'SET_IM_STATUS', status: st })) {
            toast('连接已断开，正在重连');
            renderIMStatus();
            return false;
        }
        if (globalState) { globalState.im_status = st; globalState.im_status_known = true; }
        renderIMStatus();
        toast('已切换为 ' + (IM_STATUS_TEXT[st] || ''));
        return true;
    }

    // 回复模式：manual（只提醒）/ semi（AI 自动起草）/ afk（AI 直接回复）
    function setReplyMode(mode) {
        if (['manual', 'semi', 'afk'].indexOf(mode) === -1) return false;
        if (extensionOffline()) { toast('电脑端未连接，无法切换回复模式'); renderAFK(); return false; }
        if (!sendMsg({ action: 'SET_MODE', mode: mode })) { toast('连接已断开，正在重连'); renderAFK(); return false; }
        if (globalState) {
            globalState.reply_mode = mode;
            globalState.afk_mode = (mode === 'afk');
            globalState.auto_draft = (mode !== 'manual');
        }
        renderAFK();
        toast('回复模式：' + (MODE_TEXT[mode] || mode));
        return true;
    }

    function toggleAFK() {                  // 兼容旧入口 / 旧习惯
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        return setReplyMode(currentMode() === 'afk' ? 'semi' : 'afk');
    }

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
    function resyncNow() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            sendMsg({ action: 'REQUEST_SNAPSHOT' });   // 主动要一次最新快照，避免停留在几分钟前
            requestStatusFromPC();                    // 同时把"网页上的真实 IM 状态"再要一次
        } else {
            wsAttempts = 0;                            // 立刻重连，不等指数退避
            try { initWS(); } catch (e) {}
        }
    }
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) resyncNow();
    });
    window.addEventListener('pageshow', () => resyncNow());
    window.addEventListener('online', () => resyncNow());
    window.addEventListener('focus', () => resyncNow());

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

    // ★ 人工介入专属提示音（第三种声音）：
    //   ① 新消息"叮咚"= 两个正弦音；② 掉线警报 = 连续锯齿波警笛；③ 需要人工 = 三声急促短音。
    function playHumanAlert() {
        try {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            const base = audioCtx.currentTime;
            for (let i = 0; i < 3; i++) {
                const t = base + i * 0.28;
                const osc = audioCtx.createOscillator(), gain = audioCtx.createGain();
                osc.type = 'square';
                osc.frequency.setValueAtTime(880, t);
                gain.gain.setValueAtTime(0, t);
                gain.gain.linearRampToValueAtTime(0.55, t + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.01, t + 0.22);
                osc.connect(gain); gain.connect(audioCtx.destination);
                osc.start(t); osc.stop(t + 0.25);
            }
        } catch (e) {}
    }

    // ==================== 需要人工介入（表格里没有答案） ====================
    let humanAlertIds = [];
    let lastAlertSoundId = '';
    function showHumanBanner(alerts) {
        const banner = document.getElementById('human-banner');
        if (!banner) return;
        const list = Array.isArray(alerts) ? alerts : [];
        if (!list.length) {
            banner.classList.remove('show');
            humanAlertIds = [];
            return;
        }
        const a = list[0] || {};
        const title = document.getElementById('hb-title');
        const text = document.getElementById('hb-text');
        if (title) title.innerText = '🙋 需要人工介入 · ' + (a.name || a.groupID || '');
        if (text) {
            const BR = String.fromCharCode(10);
            text.innerText = (a.reason || '表格里没有对应答案') +
                (a.summary ? (BR + '问题总结：' + a.summary) : '') +
                (list.length > 1 ? (BR + '（还有 ' + (list.length - 1) + ' 条待处理）') : '');
        }
        banner.classList.add('show');
        humanAlertIds = list.map(function (x) { return String(x.id || ''); }).filter(Boolean);
    }

    function ackHumanAlert() {
        const ids = humanAlertIds.slice();
        sendMsg({ action: 'ACK_ALERT', ids: ids });
        const banner = document.getElementById('human-banner');
        if (banner) banner.classList.remove('show');
        humanAlertIds = [];
        stopMobileSiren();
        toast('已确认（继续人工处理）');
    }

    function silenceAlarm() {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        document.getElementById('alarm-overlay').classList.remove('active');
        stopMobileSiren();
        sendMsg({ action: 'SILENCE_ALARM' });
    }

    // ==================== 顶部状态条（中继 / 探针 / 会话数） ====================
    function renderChips() {
        const box = document.getElementById('link-chips');
        if (!box) return;
        const relayOn = !!(ws && ws.readyState === WebSocket.OPEN);
        const probeOn = !!(globalState && globalState.extension_online);
        const convCount = Object.keys(getConvs()).length;
        const ver = (globalState && globalState.probe_version) ? String(globalState.probe_version) : '';
        box.innerHTML =
            '<span class="chip"><span class="dot ' + (relayOn ? 'ok' : 'bad') + '"></span>中继 ' + (relayOn ? '已连接' : '断开') + '</span>' +
            '<span class="chip"><span class="dot ' + (probeOn ? 'ok' : 'bad') + '"></span>电脑探针 ' + (probeOn ? (ver ? 'v' + esc(ver) : '在线') : '未连接') + '</span>' +
            '<span class="chip"><span class="dot ok"></span>' + esc(String(convCount)) + ' 个会话</span>';
        const sub = document.getElementById('brand-sub');
        if (sub) sub.innerText = probeOn ? '电脑端已连接 · 状态同步中' : (relayOn ? '等待电脑端探针连接…' : '中继断开，正在重连…');
    }

    // ==================== WebSocket（中继） ====================
    function initWS() {
      // 清理旧连接，避免句柄泄漏与多路重连
      if (ws) {
        ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
        try { ws.close(); } catch (e) {}
        ws = null;
      }
      ws = new WebSocket((window.location.protocol === 'https:' ? 'wss://' : 'ws://') + window.location.host + '/ws/mobile');
      ws.onopen = () => {
        wsAttempts = 0;                       // 连接成功才重置重连计数
        imStatusRequested = false;
        sendMsg({ action: 'REQUEST_SNAPSHOT' });
        requestStatusFromPC();                // 一进页面就向电脑网页要"真实 IM 状态"，不凭空显示在线
        renderChips();
      };
      ws.onmessage = (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (err) { return; }   // 脏包不打断脚本
        if (!payload) return;
        if (payload.type === 'FULL_SYNC') {
            globalState = payload.data || null;
            // 若当前打开的会话已不存在（如被关单清理），自动退回列表
            if (activeGroupId && !getConv(activeGroupId)) { activeGroupId = null; showList(); }
            renderAll();
            renderIMStatus();
            renderAFK();
            renderChips();
            // 需要人工介入：横幅 + 专属提示音（只对"新出现的告警"响一次）
            const alerts = (globalState && globalState.human_alerts) || [];
            showHumanBanner(alerts);
            const newest = alerts.length ? String(alerts[0].id || '') : '';
            if (newest && newest !== lastAlertSoundId) {
                lastAlertSoundId = newest;
                playHumanAlert();
            }
            const ov = document.getElementById('alarm-overlay');
            if (globalState && globalState.alarm_status) { ov.classList.add('active'); playMobileSiren(); }
            else { ov.classList.remove('active'); stopMobileSiren(); }
        }
        else if (payload.type === 'HUMAN_ALERT') {
            // 中继直接推来的"需要人工"事件（比快照更即时）
            const a = { id: String(payload.groupID || '') + '-' + Date.now(), name: payload.name,
                        reason: payload.message, summary: payload.summary, groupID: payload.groupID };
            lastAlertSoundId = a.id;
            showHumanBanner([a]);
            playHumanAlert();
            toast(String(payload.message || '需要人工介入'));
        }
        else if (payload.type === 'FILL_DRAFT') {
            const input = document.getElementById('chat-input');
            if (input) input.value = payload.content || '';
        }
        else if (payload.type === 'AI_STATUS') {
            if (payload.message) toast(payload.message);   // 关单结果 / AI 状态提示
        }
      };
      ws.onerror = () => { /* 出错后浏览器会触发 onclose，由 onclose 统一调度重连 */ };
      // 指数退避重连（2s→4s→…→30s 上限）：到上限后仍每 30 秒继续重连，绝不放弃
      ws.onclose = () => {
        if (globalState) globalState.extension_online = false;   // 中继断了：别再显示一个"假的在线"
        renderIMStatus();
        renderChips();
        wsAttempts++;
        const delay = Math.min(2000 * wsAttempts, 30000);
        if (wsAttempts === 20) toast('中继连接不稳定，仍在后台持续重连…');
        setTimeout(initWS, delay);
      };
    }

    // ==================== 会话列表 / 聊天流渲染 ====================
    function renderAll() {
      if (!globalState) return;
      const container = document.getElementById('conv-container');
      const convs = getConvs();
      // 排序：① 需要人工/被置顶的排最前 ② 其余按最后活动时间倒序（像微信会话列表）
      const keys = Object.keys(convs).sort((a, b) => {
        const pa = (convs[a] && convs[a].pinned) ? 1 : 0;
        const pb = (convs[b] && convs[b].pinned) ? 1 : 0;
        if (pa !== pb) return pb - pa;
        const ta = (convs[a] && convs[a].updatedAt) || 0;
        const tb = (convs[b] && convs[b].updatedAt) || 0;
        return tb - ta;
      });
      const countEl = document.getElementById('list-count');
      if (countEl) countEl.innerText = keys.length ? (keys.length + ' 个进行中') : '';

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
        const info = convInfo(c);
        const time = fmtTime((last && last.ts) || c.updatedAt);
        // 未读标记：最后一条是玩家发的，且不是当前正在看的会话
        const unread = !!(last && last.sender === 'player' && gid !== activeGroupId);
        // 置顶/需要人工标记（表格里没答案时会置顶）
        const pin = c.pinned ? '<span class="pin-badge">📌</span>' : '';
        const alertTag = c.alert ? '<span class="pin-badge">🙋</span>' : '';
        // 全部走 esc() 转义；gid 改用 data-* 传递，避免内联 onclick 属性逃逸
        return `<div class="conv-card" data-gid="${esc(gid)}">
            <div class="avatar">${esc(String(name).charAt(0) || '玩')}</div>
            <div class="conv-body">
              <div class="conv-top">
                <div class="conv-name">${esc(name)}${pin}${alertTag}${unread ? '<span class="unread-dot"></span>' : ''}</div>
                <div class="conv-time">${esc(time)}</div>
              </div>
              <div class="conv-lastmsg">${esc(preview)}</div>
              ${info ? '<div class="conv-info">' + esc(info) + '</div>' : ''}
            </div>
          </div>`;
      }).join('');

      // 事件委托绑定（不再把数据拼进 onclick）
      container.querySelectorAll('.conv-card').forEach(el => {
        el.addEventListener('click', () => pushChat(el.dataset.gid));
      });

      if (activeGroupId && getConv(activeGroupId)) {
        const c = getConv(activeGroupId);
        renderChatStream(c);
        renderPlayerCard(c);
      }
    }

    function pushChat(gid) {
      const conv = getConv(gid);
      if (!conv) return;
      activeGroupId = gid;
      playerCardOpen = false;
      document.getElementById('chat-player-name').innerText = conv.name || gid;
      const meta = document.getElementById('chat-ticket-id');
      if (meta) {
        const t = String(gid);
        meta.innerText = t.length > 16 ? t.slice(-16) : t;
      }
      renderPlayerCard(conv);
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

    // ==================== 电脑端指令下发 ====================
    function execCommand(cmd) {
      if (!activeGroupId) return;
      if (extensionOffline()) { toast('电脑端未连接，请先在电脑上打开客服工作台'); return; }
      if (!ws || ws.readyState !== WebSocket.OPEN) {
          alert('连接已断开，正在重连，请稍后再试');
          return;
      }
      if (cmd === 'AI_CLOSE') {
          if (!confirm('AI 将自动选择问题分类并生成结束语，然后回复并关单；关单后该会话会从列表移除。确定继续？')) return;
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
          toast('已请求电脑端挂起，结果会再提示…');
      } else if (cmd === 'RESUME') {
          sendMsg({ action: 'EXT_COMMAND', command: 'ACTION_RESUME', groupID: activeGroupId });
          toast('已请求电脑端恢复/接入，结果会再提示…');
      } else {
          const input = document.getElementById('chat-input');
          const text = input && input.value ? input.value.trim() : '';
          if (cmd === 'CLOSE') {
              sendMsg({ action: 'EXT_COMMAND', command: 'ACTION_REPLY_CLOSE', groupID: activeGroupId, content: text, category: "其他" });
          } else if (cmd === 'SEND' && text) {
              sendMsg({ action: 'SEND_REPLY', groupID: activeGroupId, content: text });
          }
          if (input) input.value = '';
      }
    }

    // ==================== 启动 ====================
    renderIMStatus();      // 先渲染成"连接中…"，等拿到电脑网页的真实状态再显示
    renderAFK();
    renderChips();
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


async def api_mode(request):
    """电脑小窗（HUD）切换回复模式。冲突时以手机端为准（见 apply_reply_mode）。

    body: {"mode": "manual|semi|afk", "source": "desktop"}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    mode = str(body.get("mode") or "")
    source = "desktop" if str(body.get("source") or "desktop") == "desktop" else "mobile"
    ok, note = apply_reply_mode(mode, source=source)
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    return web.json_response({"ok": ok, "note": note, "reply_mode": state.get("reply_mode"),
                              "mode_owner": state.get("mode_owner"),
                              "mode_label": MODE_LABEL.get(state.get("reply_mode"), "")})


async def api_alerts(request):
    """需要人工介入的告警队列（桌面 HUD 每 5 秒轮询 /api/diag 就能拿到，这里给手机/脚本用）。"""
    return web.json_response({
        "alerts": state.get("human_alerts", []),
        "count": len(state.get("human_alerts", [])),
        "delay_min_sec": state.get("auto_delay_min"),
        "delay_max_sec": state.get("auto_delay_max"),
    })


async def api_alerts_ack(request):
    """确认已处理的告警（桌面 HUD 播完长报警、手机点"知道了"后调用）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        ids = []
    if ids:
        idset = {str(i) for i in ids}
        state["human_alerts"] = [a for a in state.get("human_alerts", []) if str(a.get("id")) not in idset]
    else:
        state["human_alerts"] = []
    # 置顶标记也一起清掉（客服已经处理过了）
    for c in state["companies"]["main"]["conversations"].values():
        if isinstance(c, dict) and c.get("alert"):
            c.pop("alert", None)
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    return web.json_response({"ok": True, "left": len(state.get("human_alerts", []))})


async def api_fill_draft(request):
    """把文案直接填进**网页上的回复框**（供 F9/F10 免框选使用）。

    旧版 F9/F10 靠 `Ctrl+V` 粘贴，必须先点中回复框、且浏览器焦点在页面上；
    这里改为让探针直接写 DOM，不依赖焦点，也不会被剪贴板里的其他内容污染。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    content = str(body.get("content") or "")
    if not content.strip():
        return web.json_response({"ok": False, "error": "content 为空"}, status=400)
    if not active_clients["extension"]:
        return web.json_response({"ok": False, "error": "电脑端探针未连接（网页没开 / 脚本没跑）"}, status=503)
    for ext in list(active_clients["extension"]):
        await safe_send(ext, {"command": "FILL_DRAFT", "content": content})
    return web.json_response({"ok": True, "clients": len(active_clients["extension"])})

def _kb_stats():
    """知识库概况（诊断页用）。任何异常都不能影响诊断接口本身。"""
    try:
        sheets = getattr(core, "sheet_stats", {}) or {}
        return {
            "static_chars": len(getattr(core, "kb_static", "") or ""),
            "entries": len(getattr(core, "kb_entries", []) or []),
            "sheets": len(sheets),
            "total_rows": sum(sheets.values()),
            "rules_mtime": int(core.rules_mtime() or 0),
        }
    except Exception as e:
        return {"error": str(e)[:80]}


async def api_diag(request):
    """一键自检：中继 / 探针 / 手机端 到底谁没在线（排障第一入口）。"""
    convs = state["companies"]["main"]["conversations"]
    now = time.time()
    last_seen = _PROBE_META.get("last_seen") or 0
    return web.json_response({
        "ok": True,
        "server_ver": SERVER_VER,
        "server": {
            "port": PORT,
            "ip": get_local_ip(),
            "pid": os.getpid(),
            "uptime_sec": int(now - SERVER_START),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(SERVER_START)),
        },
        "probe": {
            "online": bool(active_clients["extension"]),
            "version": _PROBE_META.get("version") or "",
            "version_reported": bool(_PROBE_META.get("version")),
            "page": _PROBE_META.get("page") or "",
            "last_seen_sec": (int(now - last_seen) if last_seen else None),
            "hello_count": _PROBE_META.get("hello_count", 0),
            "expected_version": SERVER_VER,
        },
        "mobile": {"online_clients": len(active_clients["mobile"])},
        "auto_reply": {
            "delay_min_sec": state.get("auto_delay_min"),
            "delay_max_sec": state.get("auto_delay_max"),
            "pending": _pending_tasks_snapshot()["pending"],
            "pending_gids": _pending_tasks_snapshot()["gids"],
            "mode": state.get("reply_mode"),
            "mode_owner": state.get("mode_owner"),
            "mode_ts": state.get("mode_ts"),
            "mode_label": MODE_LABEL.get(state.get("reply_mode"), ""),
            "mobile_priority_sec": MOBILE_PRIORITY_SEC,
            "greeting_from": "话术库 tpl_no_desc（表格）",
        },
        "alerts": state.get("human_alerts", [])[:10],
        "alerts_count": len(state.get("human_alerts", [])),
        "im": {
            "status": state.get("im_status"),
            "status_text": {1: "在线", 2: "忙碌", 3: "离线"}.get(state.get("im_status"), "未知"),
            "known": bool(state.get("im_status_known")),
            "manual": bool(state.get("im_status_manual")),
            "state_file": os.path.basename(IM_STATE_PATH),
            "hint": "known=false 表示本进程还没从电脑网页核实过状态（手机端会显示\"正在获取…\"，不会假装在线）",
        },
        "ticket": {
            "active_gid": _LAST_ACTIVE.get("gid"),
            "active_name": (convs.get(_LAST_ACTIVE.get("gid")) or {}).get("name", ""),
            "conversations": len(convs),
        },
        "kb": _kb_stats(),
        "categories": len(state.get("category_options") or []),
        "config": {
            "rules_auto_sync": bool(config.get("rules_auto_sync", True)),
            "rules_sync_interval_minutes": config.get("rules_sync_interval_minutes", 30),
            "kb_retrieval_enabled": bool(config.get("kb_retrieval_enabled", True)),
            "kb_retrieval_top_k": config.get("kb_retrieval_top_k", 25),
            "local_excel": os.path.basename(str(config.get("local_excel_path", ""))),
            "close_category_path": config.get("close_category_path"),
        },
        "hint": "probe.online=false => 油猴脚本没跑起来；probe.version 落后 => TM 里是旧脚本",
    })


async def probe_js_handler(request):
    """把项目里最新的探针脚本直接发给浏览器。

    用途：打开 `http://IP:8765/probe.js` 即可核对油猴里那版是不是最新；
    加 `?download=1` 直接下载。省去"脚本从哪拷"的沟通成本。
    """
    path = os.path.join(BASE_DIR, "probe.js")
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
    except Exception:
        return web.json_response({"ok": False, "error": "probe.js 不存在于项目目录"}, status=404)
    headers = {"Cache-Control": "no-store"}
    if request.query.get("download"):
        headers["Content-Disposition"] = 'attachment; filename="probe.js"'
    return web.Response(text=body, content_type="application/javascript", headers=headers)


# 诊断页：纯服务端渲染 + meta 自动刷新（不用 JS，iOS 上也不会有缓存/点击问题）
DIAG_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>示例公司 系统自检</title>
<style>
 body{margin:0;padding:16px;background:#0F1115;color:#E6E6E6;
      font:15px/1.6 -apple-system,"Microsoft YaHei UI",system-ui,sans-serif}
 h1{font-size:18px;margin:0 0 12px}
 .card{background:#181B21;border:1px solid #262B34;border-radius:12px;padding:12px 14px;margin-bottom:12px}
 .k{color:#8A93A0;font-size:13px}
 .v{font-weight:600;word-break:break-all}
 .ok{color:#4EC9B0}.bad{color:#F44747}
 .row{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px dashed #232830}
 .row:last-child{border-bottom:none}
 a{color:#63B3ED}
</style></head><body>
<h1>🩺 示例公司 系统自检 <span class="k">（每 5 秒自动刷新）</span></h1>
$CARDS
<p class="k">探针未连接？看手册第 12.2 节：确认 Tampermonkey 里贴的是最新
<a href="/probe.js">probe.js</a>（v$EXPECT_VER）且脚本已启用，然后刷新工作台页面。</p>
</body></html>"""


def _diag_dot(ok, text):
    return '<span class="%s">%s</span>' % ("ok" if ok else "bad", text)


async def diag_page_handler(request):
    """给人看的自检页：中继 / 探针 / 手机端 / 知识库 一屏看懂。"""
    now = time.time()
    last_seen = _PROBE_META.get("last_seen") or 0
    convs = state["companies"]["main"]["conversations"]
    probe_online = bool(active_clients["extension"])
    probe_v = _PROBE_META.get("version") or ""
    probe_old = probe_online and not probe_v      # 连上了却不上报版本 = 油猴里是旧脚本
    if probe_old:
        probe_line = '🟡 已连接 · 未上报版本（旧脚本）'
    elif probe_online:
        probe_line = '🟢 已连接' + ((" · v" + probe_v) if probe_v else "")
    else:
        probe_line = '🔴 未连接'
    kb = _kb_stats()

    def row(label, value):
        return '<div class="row"><span>%s</span><span class="v">%s</span></div>' % (label, value)

    cards = []
    cards.append('<div class="card"><div class="k">中继服务 bridge_server.py</div>' +
                 row("状态", _diag_dot(True, "🟢 运行中 · 已运行 %d 秒" % int(now - SERVER_START))) +
                 row("监听地址", "http://%s:%s · pid %s" % (get_local_ip(), PORT, os.getpid())) +
                 '</div>')

    cards.append('<div class="card"><div class="k">电脑端探针（油猴脚本）</div>' +
                 row("连接状态", _diag_dot(probe_online and not probe_old, probe_line)) +
                 row("最近心跳", ("%d 秒前" % int(now - last_seen)) if last_seen else "无") +
                 row("当前页面", _PROBE_META.get("page") or "-") +
                 row("脚本版本", "%s（期望 v%s）" % (("v" + probe_v) if probe_v else "未上报", SERVER_VER)) +
                 '</div>')

    if probe_old:
        cards.append('<div class="card"><div class="k">⚠️ 需要更新油猴脚本</div>'
                     '<div class="v">探针已连上，但不上报版本号 —— 说明 Tampermonkey 里仍是旧脚本（&lt; v'
                     + SERVER_VER + '）。<br>把 <a href="/probe.js">/probe.js</a> 的内容整段覆盖粘贴 → Ctrl+S → '
                     '刷新工作台页面即可。</div></div>')

    cards.append('<div class="card"><div class="k">手机端</div>' +
                 row("在线连接数", len(active_clients["mobile"])) +
                 '</div>')

    # IM 状态卡：把"手机端到底显示什么状态、这个状态是怎么来的"讲清楚
    im_txt = {1: "🟢 在线", 2: "🟡 忙碌", 3: "🔴 离线"}.get(state.get("im_status"), "未知")
    if not state.get("im_status_known"):
        im_line = "⏳ 未核实（手机端显示「正在获取…」，不会假装在线）"
    elif state.get("im_status_manual"):
        im_line = im_txt + " · 客服手动设置"
    else:
        im_line = im_txt
    _im_st2, _im_man2, _im_ts2 = load_im_state()
    cards.append('<div class="card"><div class="k">IM 状态（手机端顶部下拉框）</div>' +
                 row("当前状态", im_line) +
                 row("状态记忆", ("%s · %s" % ({1: "在线", 2: "忙碌", 3: "离线"}.get(_im_st2, _im_st2),
                                                time.strftime("%m-%d %H:%M", time.localtime(_im_ts2))))
                                if _im_ts2 else "无（还没上报过）") +
                 row("离线守护", "开启（网页自己跳回在线会被改回）"
                                if config.get("keep_manual_offline", True) else "关闭") +
                 '</div>')

    cards.append('<div class="card"><div class="k">工单与知识库</div>' +
                 row("会话数", len(convs)) +
                 row("问题分类数", len(state.get("category_options") or [])) +
                 row("规章条目", "%s 条 / %s 张表" % (kb.get("total_rows", kb.get("error", "-")),
                                                      kb.get("sheets", "-"))) +
                 row("常驻静态区", "%s 字符" % kb.get("static_chars", "-")) +
                 '</div>')

    html = DIAG_HTML.replace("$CARDS", "".join(cards)).replace("$EXPECT_VER", SERVER_VER)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def ws_ext_handler(request):
    # heartbeat=30：定期 ping，及时发现 iOS 退后台/网络抖动造成的死连接
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    active_clients["extension"].add(ws)
    _PROBE_CONNS.setdefault(ws, {})
    state["extension_online"] = True
    # 排障用横幅：一眼看出"电脑端探针到底连上来了没"（含来源页，便于识破多开/错页面）
    print(f"[探针] ✅ 已连接 · 来源: {request.headers.get('Referer', '未知')}")
    # 8 秒内不上报版本 => 油猴里还是旧脚本（< 7.3），主动喊一嗓子
    asyncio.create_task(_probe_hello_watchdog(ws))
    # 探针一连上就请它回报网页上的真实问题分类（供手机端 AI 一键关单选分类）
    # ★ 只在"还没拿到过分类"时才要，避免每次连接都去点开网页上的分类下拉（客服反馈"下拉老是自己跑下来"）
    if not state.get("category_options"):
        await safe_send(ws, {"command": "REQUEST_CATEGORIES"})
    # ★ V7.3：把服务端策略同步给探针（手动离线守护开关），并请它立刻复核一次真实 IM 状态。
    #   这样即使中继刚重启、内存里是默认值，几毫秒内就会被真实状态覆盖。
    await safe_send(ws, {"command": "POLICY",
                         "keepManualOffline": bool(config.get("keep_manual_offline", True))})
    await safe_send(ws, {"command": "REQUEST_IM_STATUS"})
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
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

                # 探针握手 / 心跳：让"脚本加载了没、哪一版"变成后端可查事实
                if ev in ("PROBE_HELLO", "PROBE_HEARTBEAT"):
                    data = pkt.get("data") or {}
                    rec = _PROBE_CONNS.setdefault(ws, {})
                    rec["version"] = (str(data.get("version") or rec.get("version") or "")[:16])
                    if data.get("page"):
                        rec["page"] = str(data["page"])[:200]
                    if data.get("ua"):
                        rec["ua"] = str(data["ua"])[:120]
                    rec["last_seen"] = time.time()
                    if ev == "PROBE_HELLO":
                        rec["hello"] = True
                    _probe_refresh()
                    if ev == "PROBE_HELLO":
                        print(f"[探针] 🚀 握手成功：v{rec['version'] or '未知版本'} · {rec.get('page') or '未提供页面地址'}")
                        print(f"[探针] 提示：手机端为空时请在电脑上打开一个工单（诊断页 http://127.0.0.1:{PORT}/diag）")
                        for m in list(active_clients["mobile"]):
                            await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                # ★ V7.3：探针执行完手机端下的动作（挂起/恢复/关单/发送）后的结果回报。
                #   以前点了没反应是"静默失败"，现在手机端会收到明确提示（含页面上真实的按钮名）。
                if ev == "ACTION_RESULT":
                    data = pkt.get("data") or {}
                    cmd_name = str(data.get("command") or "")
                    ok = bool(data.get("ok"))
                    detail = str(data.get("detail") or "")
                    label = {
                        "ACTION_HANGUP": "挂起",
                        "ACTION_RESUME": "恢复",
                        "ACTION_REPLY_CLOSE": "回复并关单",
                        "SEND_REPLY": "代回复",
                        "SELECT_CATEGORY": "选择分类",
                        "LIST_ACTIONS": "按钮清单",
                    }.get(cmd_name, cmd_name or "操作")
                    print(f"[动作] {'✅' if ok else '❌'} {label}：{detail}")
                    msg = (f"{label}成功 · {detail}" if ok else f"{label}失败 · {detail}")
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok" if ok else "error",
                                            "message": msg[:300]})
                    continue

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
                    manual = bool(data.get("manual", False)) or (st == 3)
                    apply_im_status(st, manual=manual)
                    if data.get("guarded"):
                        print("[状态] 🛡️ 探针已按手动离线设置，把网页自动跳回的在线改回离线")
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                if ev == "ABNORMAL_OFFLINE":
                    # 异常掉线警报闭环（探针在页面上确实读到"离线"才会发这个事件）
                    state["alarm_status"] = True
                    apply_im_status(3, manual=False)
                    # 推送给手机端
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    # 推送 Bark 通知（P0 修复：缺失的 Bark 警报）
                    push_bark("🚨 异常掉线警报", "VPN 或网页网络连接断开，请立即检查！")
                    # 向探针发送确认回执（修复 BUG-002：防止重复上报）
                    await ws.send_json({"command": "ALARM_CONFIRMED"})
                    
                elif ev == "ALARM_RECOVERED":
                    state["alarm_status"] = False
                    apply_im_status(1, manual=False)
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

                    # ★★ 自动回复节奏（V7.4）★★
                    # ① 玩家**第一次**发来消息 -> 立刻发一条开场语（严格取表格话术）
                    # ② 之后不秒回：等 1~3 分钟（随机），期间再来消息就重新计时，确认不说了才回复
                    if not c.get("greeted"):
                        c["greeted"] = True
                        if config.get("auto_send_greeting", True) and (
                                state.get("afk_mode") or state.get("auto_draft", True)):
                            greet = pick_greeting()
                            for ext in list(active_clients["extension"]):
                                await safe_send(ext, {"command": "SEND_REPLY", "content": greet, "groupID": gid})
                            _say_as_agent(gid, greet)
                            print(f"[开场] 已按表格发送开场语给 {name}：{greet[:40]}")
                    schedule_auto_reply(gid)

                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
    finally:
        active_clients["extension"].discard(ws)
        _PROBE_CONNS.pop(ws, None)
        _probe_refresh()
        if not active_clients["extension"]:
            # 电脑端网页关闭/探针掉线：手机端必须能看出来，避免显示过期状态
            state["extension_online"] = False
            # ★ V7.3：探针不在线 = 状态无法核实。把 known 置回 False，
            #   手机端就会显示"电脑未连接"，而不是继续亮着一个可能早已过期的🟢在线。
            state["im_status_known"] = False
            print("[探针] ❌ 已断开（网页被关闭 / 网络抖动 / 油猴脚本被停用）")
            for m in list(active_clients["mobile"]):
                await safe_send(m, {"type": "FULL_SYNC", "data": state})
    return ws


async def _probe_hello_watchdog(ws, delay=8.0):
    """连接 8 秒仍未上报版本 => 说明 Tampermonkey 里是旧脚本（< v7.4）。

    这正是"脚本到底加载了没"最容易误判的场景：连接正常（所以看起来一切 OK），
    但新功能（自检胶囊 / 状态上报 / 问题分类）统统没有。
    """
    try:
        await asyncio.sleep(delay)
    except Exception:
        return
    if ws not in active_clients["extension"]:
        return
    if (_PROBE_CONNS.get(ws) or {}).get("version"):
        return
    print("[探针] ⚠️ 已连接但未上报版本 => Tampermonkey 里仍是旧脚本（< v7.4）")
    print(f"[探针] ⚠️ 请打开 http://127.0.0.1:{PORT}/probe.js 取最新脚本，整段覆盖粘贴后 Ctrl+S 保存")
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})

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
        # 兜底分类：探针在候选里找不到 AI 选的分类时用这个，而不是盲选第一项（选错分类比关不掉更糟）
        "defaultCategory": config.get("close_category_default", "其他"),
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
    # ★ V7.3：手机端一连上就请电脑端探针把"网页上的真实 IM 状态"复核一次。
    #   这样手机打开时看到的绝不是服务端编的默认值，而是电脑网页上的真实状态。
    for ext in list(active_clients["extension"]):
        await safe_send(ext, {"command": "REQUEST_IM_STATUS"})
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

                # ★ V7.3：手机端一打开就来要一次"网页上的真实 IM 状态"
                #   旧版手机端启动时直接显示服务端默认值（1=在线），客服手动挂的离线会被顶掉。
                #   现在改为：先问电脑端探针，拿到真实值再显示（没核实前手机端显示"正在获取…"）。
                if act == "REQUEST_IM_STATUS":
                    await safe_send(ws, {"type": "FULL_SYNC", "data": state})
                    for ext in list(active_clients["extension"]):
                        await safe_send(ext, {"command": "REQUEST_IM_STATUS"})
                    if not active_clients["extension"]:
                        print("[状态] 手机端请求复核状态，但电脑端探针未连接")
                    continue

                # 远程切换 IM 状态：1=在线 2=忙碌 3=离线；切到在线时自动解除异常掉线警报
                if act == "SET_IM_STATUS":
                    try:
                        st = int(pkt.get("status", 1))
                    except Exception:
                        st = 1
                    if st not in (1, 2, 3):
                        st = 1
                    # 手动切到离线 -> 记 manual，网页若自己跳回在线，探针会按离线守护改回来
                    apply_im_status(st, manual=(st == 3))
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

                if act == "SET_AUTO_DELAY":
                    # 调整"玩家说完后等多久再回"（秒）。手机端/调试都可用，范围 0~600。
                    try:
                        lo = max(0.0, min(600.0, float(pkt.get("min", state.get("auto_delay_min", 60)))))
                        hi = max(0.0, min(600.0, float(pkt.get("max", state.get("auto_delay_max", 180)))))
                    except Exception:
                        lo, hi = 60.0, 180.0
                    if hi < lo:
                        lo, hi = hi, lo
                    state["auto_delay_min"] = int(lo)
                    state["auto_delay_max"] = int(hi)
                    print(f"[自动回复] 延迟已调整为 {int(lo)}~{int(hi)} 秒")
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok",
                                            "message": f"回复延迟已设为 {int(lo)}~{int(hi)} 秒"})
                    continue

                if act == "ACK_ALERT":
                    ids = pkt.get("ids")
                    ids = [str(i) for i in ids] if isinstance(ids, list) else []
                    if ids:
                        state["human_alerts"] = [a for a in state.get("human_alerts", [])
                                                 if str(a.get("id")) not in set(ids)]
                    for c in state["companies"]["main"]["conversations"].values():
                        if isinstance(c, dict) and c.get("alert"):
                            c.pop("alert", None)
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                if act == "TOGGLE_AFK":
                    ok, note = apply_reply_mode("afk" if pkt.get("status", False) else "semi", source="mobile")
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                elif act == "SET_MODE":
                    # ★ 三档回复模式：manual=只提醒（不自动起草）/ semi=AI 起草到输入框（不发送）/ afk=AI 直接发送
                    mode = str(pkt.get("mode") or "semi")
                    if mode not in ("manual", "semi", "afk"):
                        mode = "semi"
                    ok, note = apply_reply_mode(mode, source="mobile")
                    label = {"manual": "手动（只提醒，AI 不起草）", "semi": "半自动（AI 起草到输入框，不发送）",
                             "afk": "AFK 全自动（AI 直接回复）"}[mode]
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok" if ok else "error",
                                            "message": ("回复模式：" + label) if ok else note})
                elif act == "SILENCE_ALARM":
                    state["alarm_status"] = False
                    for ext in list(active_clients["extension"]): await safe_send(ext, {"command": "SILENCE_ALARM"})
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                elif act == "TRIGGER_F9":
                    asyncio.create_task(handle_ai_automation(pkt.get("groupID"), source="phone", force=True))
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
app.router.add_get("/diag", diag_page_handler)
app.router.add_get("/probe.js", probe_js_handler)
app.router.add_get("/api/ticket", api_current_ticket)
app.router.add_get("/api/categories", api_categories)
app.router.add_post("/api/fill_draft", api_fill_draft)
app.router.add_get("/api/alerts", api_alerts)
app.router.add_post("/api/alerts/ack", api_alerts_ack)
app.router.add_post("/api/mode", api_mode)
app.router.add_get("/api/diag", api_diag)
app.router.add_get("/ws/extension", ws_ext_handler)
app.router.add_get("/ws/mobile", ws_mobile_handler)

if __name__ == "__main__":
    local_ip = get_local_ip()
    print("=" * 60)
    print("[启动] IM 移动端中继与 AI 自动托管服务已启动！")
    print(f"手机访问: http://{local_ip}:{PORT}")
    print(f"系统自检: http://127.0.0.1:{PORT}/diag   （探针/手机端是否在线一屏看懂）")
    print(f"探针脚本: http://127.0.0.1:{PORT}/probe.js （核对油猴里那版是否最新 v{SERVER_VER}）")
    print("=" * 60)
    web.run_app(app, host="0.0.0.0", port=PORT)