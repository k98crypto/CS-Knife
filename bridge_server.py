import os
import sys
import json
import re
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
    from agent_core import CustomerServiceCore, BRAND, HONORIFIC
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
    # ★ V7.7：把"网页真实现状"和"人工意图"分开记（真机 bug：手机显示忙碌、网页其实在线、两边都改不动）
    "im_status_page": 0,            # 探针最后一次读到的网页真实状态（0=还没读到）
    "im_status_manual_at": 0,       # 最近一次"人工设置状态"的时间戳
    "im_status_tries": 0,           # 已把"人工意图"重新下发给探针几次
    "im_status_conflict": "",       # "意图:网页" 不一致组合（内部去重用）
    "im_status_notified": "",       # 已经提示过手机的冲突组合（避免刷屏）
    # ★ V7.7：把"上次状态切换的请求/回执"和"网页下拉的实测选项"留在内存里 —— 中继跑在隐藏窗口时
    #   日志看不见，这些能在 /diag 与 /api/diag 里直接查（"改了没生效"必须有据可依）
    "im_last_request": {},          # {status, ts, source}
    "last_action": {},              # {command, action, ok, detail, ts}
    "status_menu_dump": {},         # {current, items:[{tag,cls,text}], ts}
    "ext_cmd_debug": {},            # 最近一次"下发给探针"的命令：{cmd, conns, sent, ts}
    "event_counts": {},             # 各类探针事件累计次数（V7.7 排障）
    "conv_list": [],                # ★ V8.0：网页左侧会话列表（手机端"全部会话"的数据源）
    "conv_list_test": [],           # ★ V8.0：测试来源的会话列表（只给诊断看，不推给真实手机端）
    "probe_pong": {},               # 探针最近一次 PONG 自报（V7.8 诊断：/api/probe_ping）
    "im_via_relay": {"count": 0, "last_ts": 0},   # 探针"确认收到指令"的次数（V7.8 硬证据）
    "extension_online": False,      # 电脑端探针是否在线（决定手机端能否远程操作）
    "probe_version": "",            # 探针（油猴脚本）版本号，来自 PROBE_HELLO/PROBE_HEARTBEAT
    "probe_last_seen": 0,           # 探针最近一次心跳时间戳（秒），手机端可据此判断新鲜度
    "category_options": [],         # 从网页级联选择器抓到的真实问题分类（供 AI 选分类）
    "companies": { "main": { "name": f"{BRAND['company']} 专线", "status": 1, "conversations": {} } }
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


def feature_flags():
    """三个可独立开关的 AI 动作（客服要求：要能单独关掉，不是一刀切）。

    - ai_close    : 手机/电脑的「AI 回复并关单」
    - auto_draft  : 「AI 自动起草」（半自动模式下 AI 不再自动出手）
    - f10_polish  : 桌面 F10「AI 润色」
    对应 config.json：enable_ai_close / enable_auto_draft / enable_f10_polish（默认都 true）
    """
    return {
        "ai_close": bool(config.get("enable_ai_close", True)),
        "auto_draft": bool(config.get("enable_auto_draft", True)),
        "f10_polish": bool(config.get("enable_f10_polish", True)),
    }


# 让手机端/小窗能拿到这三个开关（随 FULL_SYNC / /api/diag 下发）
state["features"] = feature_flags()


async def ensure_category_options(origin=None, timeout: float = 2.0):
    """按需拿"问题分类"候选：内存里有就用；没有才请探针读一次（避免连接时就嗅探下拉）。

    只有真的要选分类（AI 关单）时才会调用到这里。
    ★ V7.9：origin 为测试客户端时只问测试探针（不然会去点客服真实页面的下拉）。
    """
    opts = state.get("category_options") or []
    if opts or not ext_targets(origin):
        return opts
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _CATEGORY_WAITERS.append(fut)
    for ext in ext_targets(origin):
        await safe_send(ext, {"command": "REQUEST_CATEGORIES"})
    try:
        return (await asyncio.wait_for(fut, timeout=timeout)) or []
    except asyncio.TimeoutError:
        print("[分类] 按需读取超时（页面可能还没渲染出分类选择器）")
        return state.get("category_options") or []


def _notify_category_waiters(options):
    while _CATEGORY_WAITERS:
        fut = _CATEGORY_WAITERS.pop()
        if not fut.done():
            try:
                fut.set_result(list(options or []))
            except Exception:
                pass


def _im_txt(code):
    """状态码 -> 中文（日志/提示统一用词）。"""
    try:
        return {1: "在线", 2: "忙碌", 3: "离线"}.get(int(code or 0), "未知")
    except Exception:
        return "未知"


def _schedule_reassert(target_code):
    """把"人工设定的状态"重新下发给探针（网页自己跳回去 / 切换没生效时兜底）。

    没有运行中的事件循环（例如同步的单元测试）就直接返回，绝不在这里阻塞。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    except Exception:
        return
    try:
        asyncio.ensure_future(_reassert_im_status(int(target_code)))
    except Exception as e:
        print(f"[状态] 重新下发失败：{e}")


async def _reassert_im_status(target_code):
    """重试把人工意图切上去；连续多次仍不生效 -> 如实告诉手机（绝不假装成功）。"""
    tries = int(state.get("im_status_tries") or 0) + 1
    state["im_status_tries"] = tries
    state["im_last_request"] = {"status": int(target_code), "status_text": _im_txt(target_code),
                                "source": f"自动重试 #{tries}", "ts": int(time.time())}
    # ★ V7.9：若这个"人工意图"是测试客户端设的，重试也只打到测试探针（绝不点真实工单）
    for ext in ext_targets("test" if state.get("im_intent_test") else None):
        await safe_send(ext, {"command": "CHANGE_STATUS", "status": int(target_code)})
    want = _im_txt(target_code)
    if tries <= 3:
        print(f"[状态] 🔁 网页状态和你手动设的不一致，已第 {tries} 次把「{want}」重新下发到电脑网页")
        return
    page = state.get("im_status_page")
    key = f"{target_code}:{page}"
    if state.get("im_status_notified") == key:
        return
    state["im_status_notified"] = key
    state["im_status_tries"] = 0
    msg = (f"⚠️ 已重试 {tries} 次切到「{want}」，但网页仍是「{_im_txt(page)}」——"
           f"可能是页面上没有这个选项、或当前是只读/管理员视角。"
           f"请在电脑上确认，或在状态面板里点「以网页为准」")
    print("[状态] " + msg)
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "AI_STATUS", "status": "error", "message": msg})


def reset_im_state(source="", persist=True):
    """♻️ 以"电脑网页的真实现状"为准重置 IM 状态（清掉卡住的手动锁）。返回采用的状态码。

    真机场景：手机切忙碌时页面上没生效（旧脚本标签匹配不上），中继却把"忙碌+手动"锁住了，
    于是手机显示忙碌、网页显示在线，两边都改不动 —— 这个口子就是给这种情况的逃生舱。
    ★ V8.5.1：persist=False = 测试来源，只改内存、不落盘。
    """
    page = state.get("im_status_page") or state.get("im_status") or 1
    try:
        page = int(page)
    except Exception:
        page = 1
    if page not in (1, 2, 3):
        page = 1
    state["im_status_manual"] = False
    state["im_status_manual_at"] = 0
    state["im_status_tries"] = 0
    state["im_status_conflict"] = ""
    state["im_status_notified"] = ""
    apply_im_status(page, manual=False, source="重置（以网页为准）", persist=persist)
    print(f"[状态] ♻️ 已按网页真实现状重置为「{_im_txt(page)}」（来源：{source or '手动'}）")
    return page


def apply_im_status(status, manual=None, source="", from_probe=False, persist=True):
    """IM 状态唯一写入口：内存 + 落盘 + 标记"已核实"，返回是否有变化。

    ★ 客服铁律（第三轮补充）：**任何情况都不得把"离线/忙碌"自动改成"在线"**，除非是人工手动动作。
      这里加一道守卫：当内存里是 2(忙碌)/3(离线) 且带 manual 标记时，
      探针上报的"在线"（网页自己跳回去的）会被拦下并提示 —— 想切在线只能通过手机/小窗手动点。

    ★ V7.7：把「网页事实」(im_status_page) 和「人工意图」(im_status) 分开记。
      from_probe=True 表示这条来自探针读 DOM（网页事实），只更新 im_status_page；
      一旦"事实 ≠ 意图"除了拦住，还会把人工意图**重新下发给探针**（最多 3 次），
      重试无效就如实推手机 —— 不再出现"手机显示忙碌、网页其实在线、两边都改不动"。

    ★ V8.5.1 铁律⑥：persist=False 表示"测试来源"—— 只改内存、**不落盘 im_state.json**，
      否则跑一次回归测试就把客服真实的 IM 状态记忆覆盖掉（"又把我改成在线"）。
    """
    try:
        st = int(status)
    except Exception:
        st = 1
    if st not in (1, 2, 3):
        st = 1
    prev = int(state.get("im_status") or 1)
    manual_held = bool(state.get("im_status_manual"))
    if from_probe:
        state["im_status_page"] = st            # 网页真实现状（不管采不采纳都记下来）
        state["im_status_known"] = True         # 网页刚读过 -> 我们确实知道现状（手机端才不会显示"正在获取…"）
    if st == 1 and prev in (2, 3) and manual_held and not manual:
        _prev_txt = _im_txt(prev)
        state["im_status_conflict"] = f"{prev}:{st}"
        print(f"[状态] 🛡️ 拦截自动上线：你手动设的是「{_prev_txt}」，"
              f"网页想跳回在线（来源：{source or '探针上报'}）—— 只有手动切才会变")
        if from_probe:
            _schedule_reassert(prev)            # 网页没跟上：把人工意图重新发下去
        else:
            for m in list(active_clients["mobile"]):
                try:
                    asyncio.get_event_loop().create_task(safe_send(m, {
                        "type": "AI_STATUS", "status": "error",
                        "message": f"已拦截：网页自动跳回「在线」，你手动设的是「{_prev_txt}」"}))
                except Exception:
                    pass
        return False
    changed = (state.get("im_status") != st) or (not state.get("im_status_known"))
    state["im_status"] = st
    state["im_status_known"] = True
    if manual is not None:
        state["im_status_manual"] = bool(manual)
    if manual:
        state["im_status_manual_at"] = time.time()
    # 一致了（或人工自己改了）-> 清掉冲突与重试计数，下次不一致还能再提示
    if manual or (from_probe and st == prev):
        state["im_status_conflict"] = ""
        state["im_status_tries"] = 0
        state["im_status_notified"] = ""
    if st == 1:
        state["alarm_status"] = False
    if persist:
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


def apply_reply_mode(mode: str, source: str = "mobile", force: bool = False, persist: bool = True):
    """切换回复模式。返回 (ok, note)：手机端优先窗口内拒绝电脑小窗的改动。
    ★ V8.5.1 铁律⑥：persist=False = 测试来源，只改内存、不落盘 mode_state.json。
    """
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
    if persist:
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
HOLD_TEXT = f"{HONORIFIC}，您的问题我已经收到啦，正在为您查询相关信息，请稍等片刻哦~"

_PENDING = {}          # gid -> asyncio.Task：正在等待"玩家说完"的延迟回复


def _pending_tasks_snapshot():
    # 列表放宽到 50：测试/排障时同一进程里可能积压多个等待中的任务，
    # 只取前 10 个会让"新排队的会话"看不到（曾导致测试误判）。
    return {"pending": len(_PENDING), "gids": list(_PENDING.keys())[:50]}


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
    return f"{HONORIFIC}，欢迎来到{BRAND['game']}~ 请问有什么可以帮您？"


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
       ③ ★ V8.2：这条玩家消息得"新鲜"（默认 10 分钟内）—— 翻看旧工单**不许**触发 AI：
          否则客服一打开老会话，过 1~3 分钟 AI 就对着几百年前的消息动手，
          查不到答案还会推一条「🙋 需要人工介入」的 Bark（客服投诉过"选旧对话还收到服务消息"）。
    """
    if not isinstance(conv, dict):
        return False
    msgs = conv.get("msgs")
    if not isinstance(msgs, list) or not msgs:
        return False
    last = msgs[-1] if isinstance(msgs[-1], dict) else {}
    if last.get("sender") != "player":
        return False
    _ts = int(last.get("ts") or 0)
    if _ts and (int(time.time() * 1000) - _ts) > AUTO_ACTIVE_WINDOW_MS:
        return False                    # 旧消息：AI 不插手，交给人工
    return int(conv.get("last_reply_ts") or 0) < _ts


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
# ★ V7.9 血泪教训：探针的 SEND_REPLY / FILL_DRAFT / 回复并关单 永远作用于"页面当前打开的工单"，
#   所以发送前必须核对 gid 是否就是这个工单，否则会发错玩家（测试数据曾因此发进真实工单）。
#   真实探针与测试探针各记一份，互不干扰。
_LAST_PAGE_GID = {"gid": ""}
_LAST_TEST_PAGE_GID = {"gid": ""}
# ★ V8.5.1：上一次"玩家消息"上报的工单（**只**由 PLAYER_MESSAGE 更新，与 CONV_LIST 的 page_gid 解耦）。
#   用途：判断"我当时是不是正看着这条会话"。切会话时探针会先报 CONV_LIST（active 行变了），
#   把 _LAST_PAGE_GID 提前改成新会话 —— 于是 PLAYER_MESSAGE 里 `_prev_page_gid == gid`，
#   把"翻旧会话的历史"误判成新消息，导致 Bark 响铃（客服反馈"切到旧会话还响"的根因）。
_LAST_MSG_GID = {"gid": ""}
_LAST_TEST_MSG_GID = {"gid": ""}
# ★ 分类候选的"按需等待者"（ensure_category_options 用）
_CATEGORY_WAITERS = []
_PONG_WAITERS = []                   # ★ V7.8：/api/probe_ping 等探针 PONG 的地方（诊断用）
# ★ V8.0：探针执行 OPEN_CONV（切会话）后的回执 —— ensure_page_on 靠它"切不动就立刻放弃"，
#   而不是干等满超时（真机上会话名对不上时能 1 秒内就说清楚原因）。
_OPEN_CONV_RESULT = {"ts": 0.0, "ok": None, "gid": "", "detail": "", "test": None}
TEST_WS = set()                      # ★ V7.9：带 ?test=1 连上的"测试客户端"（测试脚本专用）
_DEDUP_SENT = {}                     # ★ V8.0.2：重复发送防抖 {(gid, md5(text)): ts}
# ★ V8.2：AI/通知只对"新鲜消息"动手的活跃窗口（默认 10 分钟）——
#   翻看旧工单不该触发 AI、更不该推 Bark（客服投诉过"选旧对话还收到服务消息"）。
AUTO_ACTIVE_WINDOW_MS = int(config.get("auto_active_window_sec", 600)) * 1000


def ext_targets(origin=None):
    """给电脑端探针下发"会在页面上动手"的指令时，该发给哪些连接。

    ★ 严格隔离（客服投诉过：测试把指令发到了他的真实工作台页面）：
      - 来自**测试客户端**（连接时带 `?test=1`）的指令，**只发给同样是测试连接的客户端**；
      - 真实手机端的指令照旧发给所有探针连接。
      这样"跑测试"永远不可能点到客服的真实工单（哪怕测试里写了挂起/关单/发送）。
    """
    all_ext = list(active_clients["extension"])
    try:
        if origin is not None and (origin in TEST_WS or origin == "test"):
            return [w for w in all_ext if w in TEST_WS]
    except Exception:
        pass
    return all_ext
# ★ 玩家新消息"已提示到手机"的水位线：gid -> 最新已提示的玩家消息 ts
#   用途：只对**新增**的玩家消息提示一次（避免 FULL_SYNC 反复刷新时重复响铃）
_LAST_NOTIFIED = {}

# ==================== 探针自检元数据（排障用，见 GET /api/diag） ====================
# 作用：把"油猴脚本到底加载了没、跑的是哪一版"从猜测变成可查事实。
# _PROBE_CONNS 按"每条连接"记录，避免旧脚本（不上报版本）连接后
# 还继续显示上一次的版本号（第七轮实测到的误导：界面显示 v7.2，实际浏览器里是旧脚本）。
_PROBE_CONNS = {}            # ws -> {"version","page","ua","last_seen","hello"}
_PROBE_META = {"version": "", "page": "", "ua": "", "last_seen": 0.0, "hello_count": 0}
SERVER_START = time.time()
SERVER_VER = "8.4"


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
    ★ V7.7：返回 True/False（"到底发出去了没有"要能查 —— 排查"点了没反应"时这是关键证据）。
    """
    try:
        await client.send_json(payload)
        return True
    except Exception:
        for _group in active_clients.values():
            _group.discard(client)
        return False


# ==================== ★ 出站安全闸（所有"可能发给玩家"的文本的唯一出口） ====================
# 客户铁律：发给玩家的话里**绝不能出现** 内部群 / 群聊 / 上报 / 补偿 / 赔偿 / 承诺 / 保证 /
# 漏洞 / 程序错误 / BUG / 内部快捷键(F6~F10) / 修复时间承诺。
# 这里统一清洗（能改的改写成中性话术，内部提示整块删掉），并复核"零禁词"。
def safe_outbound(text, where=""):
    """清洗即将发给玩家的文本；返回 (清洗后文本, 仍命中的禁词列表)。"""
    raw = "" if text is None else str(text)
    try:
        clean = core.sanitize_outbound(raw)
    except Exception as e:
        print(f"[安全闸] 清洗异常（{where}）：{e}")
        clean = raw
    try:
        left = core.find_forbidden(clean)
    except Exception:
        left = []
    if clean != raw:
        print(f"[安全闸] 已清洗发送内容（{where}）：{len(raw)} -> {len(clean)} 字")
    if left:
        print(f"[安全闸] ⚠️ 清洗后仍命中禁词（{where}）：{left}")
    return clean, left


def _ver_at_least(v, want):
    """粗粒度版本比较（"8.0" >= "7.9"），解析失败一律返回 True（"不确定就不拦"）。"""
    def _p(x):
        out = []
        for part in str(x or "").split("."):
            d = "".join(c for c in part if c.isdigit())
            out.append(int(d) if d else 0)
        return out
    try:
        a, b = _p(v), _p(want)
        while len(a) < len(b):
            a.append(0)
        while len(b) < len(a):
            b.append(0)
        return a >= b
    except Exception:
        return True


async def ensure_page_on(gid, name=None, origin=None, timeout=4.0):
    """确保"电脑网页当前打开的就是 gid 这个工单"；不是就先替客服切过去。返回 (ok, 说明)。

    ★ V8.0：这是"手机远程回复 / AI 自动回复"能真正无人值守工作的关键 ——
      探针的回复永远作用于**页面当前打开的那个工单**，所以要么先切过去，要么就只能等客服
      人在电脑前手动切（那远程和自动回复就失去意义了）。
    """
    if not gid:
        return False, "缺少会话标识"
    if str(page_gid(origin)) == str(gid):
        return True, ""
    conv = state["companies"]["main"]["conversations"].get(str(gid)) or {}
    conv_name = str(name or conv.get("name") or "").strip()
    # ★ 占位名（中继为"还没在网页上打开过的会话"临时起的名字 = 工单号本身）不是网页上的真实会话名，
    #   拿它去点会话列表只会点空 —— 这种情况如实说清楚，绝不瞎点。
    if conv.get("placeholder") and not name:
        conv_name = ""
    if not conv_name or conv_name == str(gid):
        return False, (f"这条会话还没在电脑网页上打开过（中继不知道它在网页列表里的名字），"
                       f"无法自动切换 —— 请先在电脑上打开它一次，或在手机主页点它一下")
    if not config.get("auto_open_conv", True):
        return False, (f"网页当前打开的不是「{conv_name}」，且已关闭自动切换会话"
                       f"（config.json → auto_open_conv=false）")
    targets = ext_targets(origin)
    if not targets:
        return False, "电脑端探针未连接，无法切换会话"
    # ★ V8.0：若连着的探针**全都**是旧版（不认识 OPEN_CONV），别干等到超时 ——
    #   直接告诉客服"油猴里还是旧脚本"，并给出重新粘贴的地址（这是最常见的真实原因）。
    _vers = [str((_PROBE_CONNS.get(e) or {}).get("version") or "") for e in targets]
    if _vers and all(v and not _ver_at_least(v, "8.0") for v in _vers):
        return False, (f"油猴脚本还是旧版（v{_vers[0]}），它不认识「切会话」指令 —— "
                       f"请在电脑上打开 http://127.0.0.1:{PORT}/probe.js 重新复制粘贴一次"
                       f"（v{SERVER_VER}）并刷新工作台")
    msgs = conv.get("msgs") or []
    last_text = str((msgs[-1] or {}).get("text") or "")[:60] if msgs else ""
    for ext in targets:
        await safe_send(ext, {"command": "OPEN_CONV", "name": conv_name,
                              "lastText": last_text, "groupID": str(gid)})
    print(f"[切会话] 已请探针切到「{conv_name}」（目标 {gid}）")
    t0 = time.time()
    deadline = t0 + max(1.5, float(timeout))
    while time.time() < deadline:
        await asyncio.sleep(0.25)
        if str(page_gid(origin)) == str(gid):
            print(f"[切会话] ✅ 网页已切到「{conv_name}」")
            return True, ""
        # 探针明确说"点不到/没这个会话" -> 别干等到超时，立刻如实回报
        if (_OPEN_CONV_RESULT["ts"] > t0 and _OPEN_CONV_RESULT["ok"] is False
                and _OPEN_CONV_RESULT["test"] == _origin_is_test(origin)
                and str(_OPEN_CONV_RESULT["gid"] or gid) == str(gid)):
            return False, "电脑端切不过去：" + (_OPEN_CONV_RESULT["detail"] or "会话没找到")
    return False, (f"已请电脑端切到「{conv_name}」，但网页还没切过去（会话名可能不一致，"
                   f"或该会话在其它分组里）")


def _log_outbound(where, pkt, ok, note="", auto=False, targets=None):
    """审计日志：任何"要进玩家对话框"的指令都记一笔（/diag 可查）。

    ★ 为什么必须要有它：客服原话"你还是能自动给真实玩家发消息！拦截也不管用"。
      口头保证没用 —— 这里把**每一次**出站（谁触发的、什么模式、发给几个探针、成不成）留痕，
      客服自己就能在自检页看到"到底是谁发的、什么时候发的"。
    """
    try:
        pkt = pkt or {}
        entry = {
            "ts": int(time.time()),
            "where": str(where or ""),
            "command": str(pkt.get("command") or ""),
            "groupID": str(pkt.get("groupID") or ""),
            "auto": bool(auto),
            "mode": str(state.get("reply_mode") or state.get("mode") or ""),
            "afk": bool(state.get("afk_mode")),
            "targets": int(targets) if targets is not None else -1,
            "ok": bool(ok),
            "note": str(note or "")[:120],
            "text": " ".join(str(pkt.get("content") or "").split())[:80],
        }
        log = state.setdefault("outbound_log", [])
        log.insert(0, entry)
        del log[40:]
    except Exception as e:
        print(f"[审计] 记录失败（不影响发送）：{e}")


# ==================== ★ V8.6：多段回复"一条一条发" ====================
# 客服痛点：AI 一次给出"两段话"（先安抚 + 再索要信息），整段塞进输入框一次发出，
#   看起来就是一大坨，也不像真人节奏。这里把回复**分段**：
#   · AFK（全自动）：段间随机等 3~8 秒，逐条发出；
#   · 半自动 / F9：**只粘第 1 段**，其余进"待发队列"；客服把这 1 段发出去后，自动粘下一段（按顺序）。
# 🛑 红线不变：**每一段**都走 send_to_player（出站安全闸 + 非 AFK 不许自动发 + 6 秒防重复 + 页面绑定）。
_REPLY_QUEUE = {}          # gid -> {name, total, sent, expect, remaining[], ts, source}


def multi_send_on():
    """总开关（config.json: multi_send_enabled，默认开）。"""
    return bool(config.get("multi_send_enabled", True))


def split_reply(text, max_seg=5, soft_limit=120):
    """把一段回复切成多段（供"一条一条发"）。规则：

      ① 先按**空行**分段（AI 最常见的"两段"写法）；
      ② 段内若超过 soft_limit 字，再按句末标点（。！？!?；;~）切成 ≤soft_limit；
      ③ 最多 max_seg 段，多出来的并进最后一段（防刷屏）；
      ④ 本来就是"一句话、没有空行" -> 原样返回 1 段（**老行为完全不变**）。
    """
    t = str(text or "").strip()
    if not t:
        return []
    try:
        max_seg = max(1, int(max_seg or 5))
        soft_limit = max(40, int(soft_limit or 120))
    except Exception:
        max_seg, soft_limit = 5, 120
    segs = []
    for para in [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]:
        one = " ".join(para.split("\n")).strip()       # 段内换行合成一行（"1. 2. 3." 不被拆散）
        if len(one) <= soft_limit:
            segs.append(one)
            continue
        cur = ""
        for piece in re.split(r"(?<=[。！？!?；;~])", one):
            piece = piece.strip()
            if not piece:
                continue
            if cur and len(cur) + len(piece) > soft_limit:
                segs.append(cur)
                cur = piece
            else:
                cur += piece
        if cur:
            segs.append(cur)
    segs = [s for s in segs if s]
    if not segs:
        return [t]
    if len(segs) > max_seg:
        segs = segs[:max_seg - 1] + [" ".join(segs[max_seg - 1:])]
    return segs


def _text_similar(a, b):
    """粗略相似度（字符集合重叠率）：判断"客服这次发出去的，是不是我们粘的那一段"。"""
    sa = set(re.sub(r"\s+", "", str(a or "")))
    sb = set(re.sub(r"\s+", "", str(b or "")))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(max(1, min(len(sa), len(sb))))


def queue_reply_segments(gid, name, segs, source=""):
    """半自动：第 1 段已粘进输入框，其余排进"待发队列"（客服发出去后自动续粘）。返回段数。"""
    segs = [s for s in (segs or []) if s]
    if not gid or len(segs) < 2:
        _REPLY_QUEUE.pop(gid, None)
        return 0
    _REPLY_QUEUE[gid] = {"name": name or gid, "total": len(segs), "sent": 1,
                         "expect": segs[0], "remaining": list(segs[1:]),
                         "ts": time.time(), "source": source}
    print(f"[分段回复] 共 {len(segs)} 段：第 1 段已粘入输入框，其余 {len(segs) - 1} 段排队"
          f"（客服发出第 1 段后自动粘下一条）")
    return len(segs)


async def advance_reply_queue(gid, human_text, name=""):
    """客服把第 N 段发出去了 -> 自动粘第 N+1 段。三道安全阀：

      ① 没有队列 -> 什么都不做；
      ② 他发的必须**就是我们粘的那一段**（相似度 ≥ 0.6）才续粘 —— 换话题/自己重写了就
         **丢掉队列**，绝不硬塞下一段；
      ③ 队列超过 30 分钟视为过期，丢弃。
    """
    q = _REPLY_QUEUE.get(gid)
    if not q:
        return False
    if time.time() - float(q.get("ts") or 0) > 1800:
        _REPLY_QUEUE.pop(gid, None)
        print("[分段回复] 队列已过期（>30 分钟），丢弃")
        return False
    sim = _text_similar(human_text, q.get("expect"))
    if sim < 0.6:
        _REPLY_QUEUE.pop(gid, None)
        print(f"[分段回复] ⏹ 客服这次发的内容与第 {q.get('sent')} 段不像（相似度 {sim:.2f}），"
              f"已放弃自动续粘（避免硬塞下一段）")
        return False
    rest = list(q.get("remaining") or [])
    if not rest:
        _REPLY_QUEUE.pop(gid, None)          # 最后一段也发出去了 -> 收工
        return False
    seg = rest.pop(0)
    q["sent"] = int(q.get("sent") or 1) + 1
    q["expect"] = seg
    q["remaining"] = rest
    q["ts"] = time.time()
    ok = await send_to_player({"command": "FILL_DRAFT", "content": seg, "groupID": gid,
                               "noOverwrite": True}, "分段草稿（自动续粘）",
                              origin=_auto_origin(gid), require_page=True)
    left = len(rest)
    print(f"[分段回复] ✅ 已自动粘第 {q['sent']}/{q['total']} 段（{'已发出' if ok else '被拦下/未发出'}），"
          f"还剩 {left} 段")
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "AI_STATUS", "status": "ok" if ok else "error",
                            "message": (f"已自动粘第 {q['sent']}/{q['total']} 段，发出后还会继续（剩 {left} 段）"
                                        if left else
                                        f"最后一段（第 {q['sent']}/{q['total']} 段）已粘好，发出即完成")})
    return True


async def send_segments_drip(gid, name, segs, where):
    """AFK 全自动：把回复按段**逐条发出**（段间随机 3~8 秒）。

    每段发出前重新校验"仍在 AFK"：中途切回半自动/手动就停下并如实提示手机（绝不偷偷继续发）。
    """
    segs = [s for s in (segs or []) if s]
    if not segs:
        return False
    total = len(segs)
    if total == 1:
        return await send_to_player({"command": "SEND_REPLY", "content": segs[0], "groupID": gid},
                                    where, origin=_auto_origin(gid), require_page=True, auto=True)
    lo = float(config.get("multi_send_delay_min_sec", 3) or 3)
    hi = max(lo, float(config.get("multi_send_delay_max_sec", 8) or 8))
    print(f"[分段发送] {name or gid}：分 {total} 段逐条发，段间随机 {lo:g}~{hi:g} 秒")
    for i, seg in enumerate(segs):
        if i > 0:
            await asyncio.sleep(random.uniform(lo, hi))
            if not state.get("afk_mode"):
                print(f"[分段发送] ⏹ 模式已切回非 AFK：停止后续 {total - i} 段（已发 {i} 段）")
                for m in list(active_clients["mobile"]):
                    await safe_send(m, {"type": "AI_STATUS", "status": "error",
                                        "message": f"已切回半自动/手动，剩下 {total - i} 段没有自动发，请手动确认"})
                return False
        ok = await send_to_player({"command": "SEND_REPLY", "content": seg, "groupID": gid},
                                  f"{where}（第 {i + 1}/{total} 段）",
                                  origin=_auto_origin(gid), require_page=True, auto=True)
        if not ok:
            print(f"[分段发送] ⏹ 第 {i + 1}/{total} 段被拦下/清洗为空，停止后续")
            return False
        await asyncio.sleep(0.3)
    print(f"[分段发送] ✅ {name or gid}：{total} 段全部发完")
    return True


# ==================== ★ V8.3：人工回复"影子录制"语料（供 distill_rules.py 离线蒸馏） ====================
# 背景：探针 V8.3 会把客服**真人**在工作台上发出的回复 + 当时的工单上下文，静默上报 RECORD_MANUAL_DEMO。
# 这里只做一件事：把它落盘成 JSONL，给离线的 distill_rules.py 去逆向蒸馏话术规则。
#
# 🛑 红线声明：本函数**只落盘、零出站** —— 不填草稿、不发送、不改会话状态、不经过也无需经过 safe_outbound
#   （它是"学习语料"，不是要发给玩家的内容；将来蒸馏出的话术仍会走原有的出站安全闸）。
DEMO_FILE = os.path.join(BASE_DIR, "demonstrations.jsonl")
DEMO_TEST_FILE = os.path.join(BASE_DIR, "demonstrations.test.jsonl")   # 测试探针（?test=1）单独存，不污染真实语料
_DEMO_DEDUP = {}          # (gid, 回复前 80 字, 是否测试) -> 最近一次记录时间戳，用于 6 秒防刷
_DEMO_MAX_MSGS = 40       # 单个样本最多带多少条历史消息（防止异常大包把文件撑爆）


def _append_demo_record(data, is_test=False):
    """把一条"人工真实回复"示范样本追加写入 JSONL。返回 True = 真的写入了一条。

    防刷与健壮性（极简，不做任何多余动作）：
      ① humanReply 去掉空白后 < 2 字 -> 直接忽略（空包/误触）；
      ② 同一 (工单 + 文本) 6 秒内只记一次（防双击、防探针重复派发）；
      ③ 任何异常只打印日志，绝不影响探针连接与消息链路（红线：不得阻塞/打断主流程）。
    """
    try:
        data = data or {}
        reply = " ".join(str(data.get("humanReply") or "").split())
        if len(reply) < 2:
            return False
        gid = str(data.get("groupID") or "")
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            msgs = []
        clean_msgs = []
        for m in msgs[:_DEMO_MAX_MSGS]:
            if not isinstance(m, dict):
                continue
            clean_msgs.append({
                "sender": "agent" if str(m.get("sender")) == "agent" else "player",
                "text": " ".join(str(m.get("text") or "").split())[:2000],
            })
        now = time.time()
        key = (gid, reply[:80], bool(is_test))
        if now - float(_DEMO_DEDUP.get(key) or 0.0) < 6.0:
            return False
        _DEMO_DEDUP[key] = now
        if len(_DEMO_DEDUP) > 500:                     # 轻量清理，长期运行也不涨内存
            for k in sorted(_DEMO_DEDUP, key=lambda k: _DEMO_DEDUP[k])[:250]:
                _DEMO_DEDUP.pop(k, None)
        rec = {
            "timestamp": int(data.get("timestamp") or (now * 1000)),
            "recordedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "groupID": gid,
            "name": str(data.get("name") or "")[:60],
            "playerInfo": " ".join(str(data.get("playerInfo") or "").split())[:300],
            "messages": clean_msgs,
            "humanReply": reply[:2000],
            "trigger": str(data.get("trigger") or "")[:16],
            "probeVersion": str(data.get("probeVersion") or "")[:16],
            "test": bool(is_test),
        }
        path = DEMO_TEST_FILE if is_test else DEMO_FILE
        # 单行 JSON（UTF-8 追加）：一条一行，distill_rules.py 可逐行读、去重、抽样
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print(f"[DemoRecorder] ⚠️ 记录失败（不影响任何发送）：{e}")
        return False


# ==================== ★ QC 历史语料（质检页采集器 qc_probe.js → distill_rules.py --mode history） ====================
# 场景：客服可以在「质检明细」页按"我的会话"回看历史工单。这里把那些**整段历史对话**采集下来，
# 给离线的 distill_rules.py 当蒸馏语料（比实时嗅探的样本量大得多）。
#
# 🛑 红线声明：本文件与这些函数**只落盘、零出站** —— 不填草稿、不发送、不改会话状态，
#   也不进入手机端会话列表 / Bark / ext_targets（采集器走独立通道 /ws/qc，物理隔离）。
HISTORY_FILE = os.path.join(BASE_DIR, "demonstrations_history.jsonl")
_HISTORY_DEDUP = {}          # 会话指纹 -> 已记录（跨本次进程内去重）
_HISTORY_IDS_LOADED = {"done": False}
_HISTORY_MAX_MSGS = 80       # 单条会话最多保留多少条消息（防异常大包）


def _history_known_ids():
    """首次调用时把已落盘的历史会话指纹读进内存 —— 这样"重跑采集/翻页重叠"不会重复记。"""
    if _HISTORY_IDS_LOADED["done"]:
        return
    _HISTORY_IDS_LOADED["done"] = True
    try:
        if not os.path.exists(HISTORY_FILE):
            return
        with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                fp = str((rec or {}).get("fingerprint") or "")
                if fp:
                    _HISTORY_DEDUP[fp] = 0.0
        if _HISTORY_DEDUP:
            print(f"[QCRecorder] 已载入 {len(_HISTORY_DEDUP)} 条历史会话指纹（重跑不会重复记）")
    except Exception as e:
        print(f"[QCRecorder] ⚠️ 读取历史语料指纹失败（不影响继续）：{e}")


def _append_history_record(data):
    """把质检页采集到的一条"整段历史会话"追加写入 JSONL。返回 True = 真的写入了。

    只落盘、零出站；防刷：
      ① 消息少于 2 条、或整段里没有客服发言 -> 忽略（对蒸馏没用）；
      ② 同一会话（会话ID 或消息指纹）只记一次，且**跨重启**也去重（见 _history_known_ids）；
      ③ 任何异常只打印，绝不影响采集器与页面。
    """
    try:
        data = data or {}
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            return False
        clean = []
        for m in msgs[:_HISTORY_MAX_MSGS]:
            if not isinstance(m, dict):
                continue
            txt = " ".join(str(m.get("text") or "").split())[:2000]
            if not txt:
                continue
            s = str(m.get("sender") or "")
            clean.append({"sender": s if s in ("player", "agent", "system") else "unknown", "text": txt})
        if len(clean) < 2:
            return False
        if not any(m["sender"] == "agent" and len(m["text"]) >= 2 for m in clean):
            return False                                   # 没有客服发言 => 对蒸馏没价值
        _history_known_ids()
        sid = str(data.get("sessionId") or "").strip()
        import hashlib
        body = "||".join(m["sender"] + ":" + m["text"] for m in clean)
        fp = sid or hashlib.md5(body.encode("utf-8")).hexdigest()
        if fp in _HISTORY_DEDUP:
            return False                                   # 已记过（重跑/翻页重叠/同一条被点两次）
        _HISTORY_DEDUP[fp] = time.time()
        if len(_HISTORY_DEDUP) > 20000:
            for k in sorted(_HISTORY_DEDUP, key=lambda k: _HISTORY_DEDUP[k])[:10000]:
                _HISTORY_DEDUP.pop(k, None)
        rec = {
            "fingerprint": fp,
            "sessionId": sid,
            "recordedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scannedAt": int(data.get("scannedAt") or int(time.time() * 1000)),
            "category": str(data.get("category") or "")[:80],
            "score": data.get("score") or 0,
            "aiScore": data.get("aiScore") or 0,
            "aiResult": str(data.get("aiResult") or "")[:20],
            "reviewer": str(data.get("reviewer") or "")[:20],
            "source": str(data.get("source") or "")[:20],
            "language": str(data.get("language") or "")[:20],
            "game": str(data.get("game") or "")[:40],
            "agent": str(data.get("agent") or "")[:40],
            "page": int(data.get("page") or 0),
            "row": int(data.get("row") or 0),
            "qcVersion": str(data.get("qcVersion") or "")[:16],
            "messages": clean,
        }
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print(f"[QCRecorder] ⚠️ 记录失败（不影响页面）：{e}")
        return False


async def send_to_player(payload, where="", origin=None, require_page=False, page_name=None, auto=False):
    """把指令发给电脑端探针去执行前，先把"玩家可见文本"过一遍安全闸。

    payload 形如 {"command": "SEND_REPLY"|"FILL_DRAFT"|"ACTION_REPLY_CLOSE", "content": "..."}
    返回 True=已发出；False=清洗后为空 / 被安全策略拦下（调用方需兜底）。

    origin：指令的来源连接。★ V7.9 —— 若来源是"测试客户端"，只发给测试探针（绝不点真实工单）。
    require_page：★ V7.9 —— 发送前核对"目标工单 == 网页当前打开的工单"，
                  对不上就拒绝（探针的发送永远作用于页面当前工单，错了就会发错玩家）。
    """
    pkt = dict(payload or {})
    # ⓪-1 ★ V8.0.2 自动发送硬闸（本轮 P0）：
    #   `auto=True` 表示"系统自己发起的"（开场语/安抚话术/AFK 自动回复/超时关单）。
    #   铁律：**只有 AFK 模式允许系统自己"发送"**；半自动/手动模式下，自动文本一律只能起草。
    #   （FILL_DRAFT = 起草，任何模式都允许 ✓ —— 半自动的 AI 草稿就是靠它）
    #   客服原话："你还是能自动给真实玩家发消息！拦截也不管用" —— 旧版开场语在"半自动"下
    #   也会直接 SEND_REPLY，而且换到旧会话时（重启后没有 greeted 标记）还会再发一次，就是它。
    _is_send_cmd = str(pkt.get("command") or "") in ("SEND_REPLY", "ACTION_REPLY_CLOSE")
    if auto and _is_send_cmd and not state.get("afk_mode"):
        print(f"[自动发送] ⛔ 已拦截（当前模式 {state.get('reply_mode') or '?'}，非 AFK）：{where}")
        _log_outbound(where, pkt, ok=False, note="非 AFK 模式：系统自动发送已拦截", auto=True)
        return False
    # ⓪ 总开关（一键止血）
    if not outbound_enabled():
        print(f"[停发] outbound_enabled=false，已拦截（{where}）")
        _log_outbound(where, pkt, ok=False, note="已开启停发总开关", auto=auto)
        for m in list(active_clients["mobile"]):
            await safe_send(m, {"type": "AI_STATUS", "status": "error",
                                "message": "已开启「停发」：所有会进玩家对话框的内容都被拦住了"
                                           "（config.json → outbound_enabled=false）"})
        return False
    # ⓪-2 ★ 重复发送防抖：同一条会话 + 同一段文本，6 秒内只发一次
    #   （防双击、防手机重发、防"两个工作台标签页同时执行"造成的两条一模一样消息）
    if pkt.get("groupID") and pkt.get("command") in ("SEND_REPLY", "ACTION_REPLY_CLOSE"):
        import hashlib as _hl
        _k = (str(pkt.get("groupID")), _hl.md5(str(pkt.get("content") or "").encode("utf-8")).hexdigest())
        _now = time.time()
        if _now - float(_DEDUP_SENT.get(_k) or 0) < 6.0:
            print(f"[防重] 6 秒内同一条会话的同样内容已发过，忽略重复指令（{where}）")
            _log_outbound(where, pkt, ok=False, note="重复指令（6 秒内同样内容）已忽略", auto=auto)
            return False
        # ★ V8.5：长期运行的清理（60 秒前的记录对 6 秒防抖已无意义，别让字典越涨越大）
        if len(_DEDUP_SENT) > 200:
            for _hk, _hv in list(_DEDUP_SENT.items()):
                if _now - float(_hv or 0) > 60.0:
                    _DEDUP_SENT.pop(_hk, None)
        _DEDUP_SENT[_k] = _now
    # ① 页面绑定核对（防"发错人"）：不对就**自动把网页切过去**，而不是让客服自己切
    if require_page:
        ok_page, note = page_binding_ok(pkt.get("groupID"), origin)
        if not ok_page:
            ok_sw, sw_note = await ensure_page_on(pkt.get("groupID"), name=page_name, origin=origin)
            if ok_sw:
                ok_page, note = page_binding_ok(pkt.get("groupID"), origin)
            else:
                note = note + "；自动切换失败：" + sw_note
        if not ok_page:
            print(f"[页面绑定] 拒绝发送：{note}（{where}）")
            _log_outbound(where, pkt, ok=False, note="页面绑定不符：" + note, auto=auto)
            for m in list(active_clients["mobile"]):
                await safe_send(m, {"type": "AI_STATUS", "status": "error", "message": note,
                                    "groupID": str(pkt.get("groupID") or "")})
            return False
    # ② 出站安全闸（玩家可见文本的唯一出口）
    if pkt.get("content"):
        clean, left = safe_outbound(pkt["content"], where or str(pkt.get("command") or ""))
        if not clean:
            print(f"[安全闸] 内容清洗后为空，已拦截（{where}）")
            _log_outbound(where, pkt, ok=False, note="清洗后为空，已拦截", auto=auto)
            return False
        pkt["content"] = clean
    _targets = ext_targets(origin)
    # ★ V8.0.2：防"多个工作台页面同时执行"造成重复发送 ——
    #   对"必须作用在某条会话上"的指令，只发给**当前真的打开着这条会话**的那个探针连接。
    if require_page and pkt.get("groupID"):
        _want = str(pkt["groupID"])
        _only = [e for e in _targets
                 if str((_PROBE_CONNS.get(e) or {}).get("page_gid") or "") == _want]
        if _only:
            _targets = _only
    sent = 0
    for ext in _targets:
        if await safe_send(ext, pkt):
            sent += 1
    _log_outbound(where, pkt, ok=bool(sent), note=("已发出" if sent else "没有探针连接可执行"),
                  auto=auto, targets=sent)
    return bool(sent)


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
       ① **半自动/手动**：只把安抚话术**起草**到输入框（绝不自动发给玩家）；AFK 才直接发
       ② 长报警（Bark + 手机端专属提示音 + 桌面 HUD 长鸣）——与"新消息提示音""掉线警报"都不同
       ③ 该会话置顶 + 把玩家信息和问题总结复制给客服（复制由桌面 HUD 完成）
    """
    # ① 安抚话术：★ V8.0.2 —— 不再是"无条件直接发给玩家"（那正是"自动给真实玩家发消息"的来源之一）
    hold_text = safe_outbound(HOLD_TEXT, "安抚话术")[0] or HOLD_TEXT
    if state.get("afk_mode"):
        await send_to_player({"command": "SEND_REPLY", "content": hold_text,
                              "groupID": group_id}, "安抚话术",
                             origin=_auto_origin(group_id), require_page=True, auto=True)
        _say_as_agent(group_id, hold_text)
    else:
        await send_to_player({"command": "FILL_DRAFT", "content": hold_text, "groupID": group_id,
                              "noOverwrite": True}, "安抚话术(草稿)",
                             origin=_auto_origin(group_id), require_page=True, auto=True)
        print(f"[人工介入] 半自动：已把安抚话术起草到输入框（未发送），等客服确认：{group_id}")

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
    # ★ V8.2：只有"新鲜消息"才推 Bark —— 翻看旧工单（AI 本不该对旧消息动手）不再打扰手机锁屏
    _lp = last_player_ts(conv)
    # ★ V8.5 铁律⑨：这条告警是不是"测试来源"（测试客户端 / 测试造的会话）——
    #   测试来源只许走站内（/diag / 桌面），**绝不**推客服手机的 Bark，也不弹真机横幅。
    _is_test_alert = (_auto_origin(group_id) == "test")
    if _is_test_alert:
        print("[人工介入] 测试来源：只做站内告警（不推手机、不推 Bark）")
    elif _lp and (int(time.time() * 1000) - _lp) <= AUTO_ACTIVE_WINDOW_MS:
        push_bark("🙋 需要人工介入", f"{name}：表格里没有对应答案，已发安抚话术", group_id)
    else:
        print("[人工介入] 旧会话（消息不新鲜）：只做站内告警，不推 Bark")
    state["human_alerts"] = ([alert] + [a for a in state.get("human_alerts", []) if a.get("groupID") != group_id])[:20]

    print(f"[人工] 🙋 需要人工介入：{name}（{group_id}）· {summary[:60]}")
    for m in _mobile_targets_for(_is_test_alert):
        await safe_send(m, {"type": "HUMAN_ALERT", "groupID": group_id, "name": name,
                            "message": f"{name}：{alert['reason']}", "summary": summary,
                            "playerInfo": alert["playerInfo"]})
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    return alert


# ==================== ★ V7.9 测试隔离 + 页面绑定 + 停发开关 ====================
def _auto_origin(gid=None):
    """自动起草/自动回复的指令来源标记。

    若这条会话是测试脚本（?test=1）造出来的，就返回 "test" —— 后续指令只会发给测试探针，
    绝不填/发客服的真实工单（客服投诉过测试把问候语发进了玩家真实工单）。

    ★ 按**会话**判断（`conv["_test_origin"]`），不依赖全局标记 —— 全局标记会被随后到来的
      真实玩家消息顶掉，那正是会把测试的延迟回复打到真实工单的漏洞。
    """
    try:
        if gid:
            conv = state["companies"]["main"]["conversations"].get(str(gid)) or {}
            if conv.get("_test_origin"):
                return "test"
    except Exception:
        pass
    return "test" if state.get("automation_origin_test") and not gid else None


def _origin_is_test(origin):
    """"测试来源"判定：连接对象带 ?test=1，或用 "test" 哨兵（自动/延迟流程用）。"""
    if origin == "test":
        return True
    try:
        return origin is not None and origin in TEST_WS
    except Exception:
        return False


def _mobile_targets_for(is_test_origin):
    """按"隔离类"挑手机端：测试来源只推测试手机端，真实来源只推真实手机端。

    为什么：测试脚本造的会话/消息如果也推给客服的真机，会变成"手机上莫名弹出假消息"，
    与 v7.9 那次"测试数据发进真实工单"属同一类污染的延伸（通知虽不改页面，但同样会打扰人）。
    """
    out = []
    for m in list(active_clients["mobile"]):
        try:
            if (m in TEST_WS) == bool(is_test_origin):
                out.append(m)
        except Exception:
            continue
    return out


def page_gid(origin=None):
    """电脑网页"此刻打开的那个工单"（由探针的 PLAYER_MESSAGE 上报）。真实/测试各记一份。"""
    if _origin_is_test(origin):
        return _LAST_TEST_PAGE_GID.get("gid") or ""
    return _LAST_PAGE_GID.get("gid") or ""


def page_binding_ok(gid, origin=None):
    """发送前核对：目标工单 == 网页当前打开的工单？返回 (ok, 说明)。

    ★ V7.9 血泪教训（真实事故）：探针的 SEND_REPLY / FILL_DRAFT / 回复并关单 永远作用在
      **页面当前打开的那个工单**上。只要 gid 与页面对不上，这条指令就会发错人
      —— 测试数据曾因此把问候语发进了玩家真实工单并被撤回。
    """
    cur = page_gid(origin)
    is_test = _origin_is_test(origin)
    if not cur:
        if is_test:
            return False, "网页还没上报当前工单，测试模式下拒绝发送"
        return True, "（网页尚未上报当前工单，放行但请留意）"
    if str(cur) == str(gid):
        return True, ""
    return False, (f"电脑网页当前打开的是「{cur}」，不是这条会话「{gid}」"
                   f"（中继会先自动帮你切过去；切不过去才会拒发，避免发错玩家）")


def outbound_enabled():
    """总开关：停发所有"会进玩家对话框"的文本（config.json → outbound_enabled，默认 true）。

    这是给客服的"一键止血"：历史上出现过测试数据把消息发进真实工单的事故。
    """
    return bool(config.get("outbound_enabled", True))


def _purge_test_convs():
    """清掉测试脚本造的会话（形如 F-<数字>/AUTO-<数字>/T-A-<数字>/CLOSEFAIL-<数字>）。

    真实工单的 id 是"页面上真实工单号"或 P+hash，绝不会长成这些形状，所以不会被误删。
    """
    import re as _re
    # ★ V8.0.1：把 NOTOPEN-…（历史版本给"不认识的会话"造的占位卡片）也算进测试脏数据
    pat = _re.compile(r"^(F|AUTO|AUTO2|T-A|T-B|T-C|CLOSEFAIL|NOTOPEN)-\d{6,}$")
    convs = state["companies"]["main"]["conversations"]
    gone = [k for k in list(convs.keys()) if pat.match(str(k))
            or (convs.get(k) or {}).get("placeholder")]
    for k in gone:
        convs.pop(k, None)
    if _LAST_ACTIVE.get("gid") in gone:
        _LAST_ACTIVE["gid"] = None
    for k in gone:
        _PENDING.pop(k, None)
        _PENDING_CLOSE.pop(k, None)
        _LAST_NOTIFIED.pop(k, None)
    return gone


async def handle_ai_automation(group_id: str, source: str = "", force: bool = False):
    conv = state["companies"]["main"]["conversations"].get(group_id)
    if not conv:
        return

    # ★ 「手动模式」只提醒、不自动起草：客服说"AI 自动起草关不掉"，就是这里没有开关。
    #   （force=True 表示"手机上主动点的按钮"，任何时候都放行）
    #   ★ V7.5：手动模式的"有新消息"提示已改成 PLAYER_MESSAGE 里**即时**推 NEW_MESSAGE，
    #     这里不再延迟 1~3 分钟重复弹一条（避免"过一会儿又突然冒出来"）。
    if not force and not state.get("afk_mode") and not state.get("auto_draft", True):
        print(f"[手动模式] {group_id} 有新消息，AI 未自动起草（手机端已即时提示）")
        return

    # ★ 独立开关②（enable_auto_draft=false）：即使是半自动也不自动出手，只提示
    if not force and not feature_flags()["auto_draft"]:
        print(f"[自动起草] 已在设置里关闭（enable_auto_draft=false）：{group_id} 只提示、不起草")
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
        # ★ 安全闸：无论 AFK 直接发送、还是半自动只填草稿，都要先清洗
        #   （草稿也是"可能被客服顺手发出去"的文本，必须同样零禁词）
        body = safe_outbound(reply, "AI 回复")[0]
        if not body:
            print("[安全闸] AI 回复清洗后为空（可能只剩内部提示）-> 转人工：发安抚话术 + 长报警")
            await send_hold_and_alert(group_id, conv, history_str, reply)
            return
        now_str = datetime.now().strftime("%H:%M:%S")
        if state["afk_mode"]:
            conv = state["companies"]["main"]["conversations"].setdefault(
                group_id, {"name": group_id, "msgs": []})
            if not isinstance(conv.get("msgs"), list):
                conv["msgs"] = []
            conv["msgs"].append({"sender": "agent", "text": body, "time": now_str,
                                 "ts": int(time.time() * 1000)})
            conv["updatedAt"] = int(time.time() * 1000)
            conv["last_reply_ts"] = last_player_ts(conv)
            _LAST_ACTIVE["gid"] = group_id
            for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})

            if "TIMEOUT_CLOSE" in tag:
                await send_to_player({"command": "ACTION_REPLY_CLOSE", "category": "其他",
                                      "categoryPath": (config.get("close_category_path") or ["一级分类", "二级分类"]),
                                      "defaultCategory": config.get("close_category_default", "其他"),
                                      "content": body, "groupID": group_id}, "超时关单",
                                     origin=_auto_origin(group_id), require_page=True, auto=True)
            else:
                # ★ V8.6：多段回复 -> 段间随机几秒，**逐条发出**（每段仍各自过安全闸 + 核对页面绑定）
                _segs = (split_reply(body, int(config.get("multi_send_max_seg", 5) or 5))
                         if multi_send_on() else [body])
                if len(_segs) > 1:
                    asyncio.create_task(send_segments_drip(group_id, conv.get("name") or group_id,
                                                           _segs, "AFK 自动回复"))
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok",
                                            "message": f"AI 已分 {len(_segs)} 段回复（段间随机几秒逐条发）"})
                else:
                    await send_to_player({"command": "SEND_REPLY", "content": body,
                                          "groupID": group_id}, "AFK 自动回复",
                                         origin=_auto_origin(group_id), require_page=True, auto=True)
        else:
            # 半自动模式：草稿推到网页与手机输入框（同样已过安全闸）
            #   ★ V8.0.2：带 noOverwrite —— 输入框里如果已经有客服自己写的内容，**绝不覆盖**
            #   ★ V8.6：多段回复**只粘第 1 段**，其余排队 —— 客服发出第 1 段后自动粘下一条
            _segs = (split_reply(body, int(config.get("multi_send_max_seg", 5) or 5))
                     if multi_send_on() else [body])
            _first = _segs[0] if _segs else body
            await send_to_player({"command": "FILL_DRAFT", "content": _first, "category": "其他",
                                  "groupID": group_id, "noOverwrite": True}, "半自动草稿",
                                 origin=_auto_origin(group_id), require_page=True, auto=True)
            for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FILL_DRAFT", "content": _first})
            if len(_segs) > 1:
                queue_reply_segments(group_id, conv.get("name") or group_id, _segs, "半自动草稿")
                for m in list(active_clients["mobile"]):
                    await safe_send(m, {"type": "AI_STATUS", "status": "ok",
                                        "message": f"已起草第 1/{len(_segs)} 段（共 {len(_segs)} 段）"
                                                   f"——你发出后会自动粘下一段"})


# ================= 手机端 H5 界面 =================
HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <meta name="theme-color" content="#0B0C0E">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <title>客服台</title>
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
    .conv-card.page-active{border-color:var(--ok)}   /* ★ V8.0：电脑网页当前打开的那个会话 */
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
    .action-bar{flex:0 0 auto;display:flex;gap:8px;padding:10px 12px;overflow-x:auto;-webkit-overflow-scrolling:touch;
                border-bottom:1px solid var(--line-soft)}
    .action-bar::-webkit-scrollbar{display:none}
    /* ★ V8.4：这些是**动作按钮**（点一下立刻执行），不是开关 —— 做成实心方块按钮，别像胶囊标签 */
    .action-btn{flex:0 0 auto;height:38px;padding:0 15px;border-radius:11px;font-size:13px;font-weight:700;
                white-space:nowrap;background:var(--card-2);border:1px solid var(--line);color:var(--text);
                box-shadow:0 1px 0 rgba(255,255,255,.05) inset}
    .action-btn:active{transform:translateY(1px)}
    .action-btn.ai{color:#EAF6FF;border-color:rgba(124,196,255,.45);
                   background:linear-gradient(135deg, rgba(78,201,176,.30), rgba(124,196,255,.24))}
    .action-btn.primary{color:#04231D;border-color:rgba(78,201,176,.65);font-weight:800;
                        background:linear-gradient(135deg,#4EC9B0,#7CC4FF)}
    .action-btn.locked{opacity:.6}
    .feature-hint{flex:0 0 auto;padding:0 12px 6px;font-size:11.5px;color:#E5C07B}
    .feature-hint:empty{display:none}
    .action-btn:active{background:var(--card-2)}
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
    .chat-text-input{flex:1 1 auto;min-width:0;min-height:40px;max-height:38vh;border-radius:18px;
                     padding:9px 16px;font-size:15px;line-height:1.45;font-family:inherit;resize:none;
                     color:var(--text);background:var(--card);border:1px solid var(--line);outline:none;
                     overflow-y:hidden;display:block;box-sizing:border-box}
    .chat-text-input:focus{border-color:rgba(124,196,255,.5)}
    .send-btn{flex:0 0 auto;width:40px;height:40px;border-radius:50%;border:none;font-size:17px;font-weight:700;
              align-self:flex-end;
              color:#0B0C0E;background:linear-gradient(145deg,#7CC4FF,#4EC9B0)}
    .send-btn:active{opacity:.8}
    /* 失败原因常驻条：toast 一闪而过看不清/复制不了，这里保留到手动关闭 */
    .err-bar{position:fixed;left:10px;right:10px;bottom:calc(var(--safe-bottom) + 76px);z-index:70;display:none;
             background:#3A1D1F;border:1px solid #8B3A3A;color:#FFD9D9;border-radius:12px;padding:9px 12px;
             font-size:13px;line-height:1.45;max-height:32vh;overflow-y:auto;-webkit-overflow-scrolling:touch;
             white-space:pre-wrap;word-break:break-all;box-shadow:0 8px 24px rgba(0,0,0,.45)}
    .err-bar.show{display:block}
    .err-bar b{float:right;margin-left:8px;opacity:.75}

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
            <div class="brand-name">客服台</div>
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
    <div class="err-bar" id="err-bar" onclick="hideErr()"><b>✕</b><span id="err-text"></span></div>

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
        <div class="action-bar" id="action-bar">
          <button class="action-btn primary" id="btn-ai-close" onclick="execCommand('AI_CLOSE')">🤖 AI 回复并关单</button>
          <button class="action-btn ai" id="btn-f9" onclick="execCommand('F9')">✨ AI 立刻起草</button>
          <button class="action-btn" onclick="execCommand('HANGUP')">⏸ 挂起</button>
          <button class="action-btn" onclick="execCommand('RESUME')">▶ 恢复</button>
          <button class="action-btn" onclick="execCommand('CLOSE')">✅ 关单</button>
        </div>
        <div class="feature-hint" id="feature-hint"></div>
        <div class="chat-stream" id="chat-stream"></div>
        <div class="player-card" id="player-card" onclick="togglePlayerCard()"></div>
        <div class="input-bar">
          <textarea class="chat-text-input" id="chat-input" rows="1" placeholder="输入回复内容…"></textarea>
          <button class="send-btn" onclick="execCommand('SEND')">↑</button>
        </div>
      </div>
    </div>
  </div>

  <script>
    let globalState = null; let activeGroupId = null; let ws = null;
    // ★ V8.0.2：手机页面版本号（顶栏胶囊显示）——"刷新了没生效"时第一眼就能确认
    const H5_VER = '8.5.1';
    // ★ V8.0.1：点过"未打开"的会话后，等它出现在中继会话列表里就自动打开聊天页（不用点第二次）
    let pendingOpenName = '';
    let pendingOpenAt = 0;                 // 待打开的登记时间（25 秒后自动作废，避免乱开）
    let lastKnownGids = {};                // 上一次渲染时已知的会话（用于"兜底自动打开"）
    let showOtherConvs = false;            // ★ V8.2："其它会话（不在网页列表里）"默认收起
    let lastDraftFilled = '';              // ★ V8.5：本页自己填进去的最后一份草稿（用来判断"输入框里的字是不是客服写的"）
    let audioCtx = null; let sirenInterval = null;
    let wsAttempts = 0;
    let imStatusRequested = false;      // 已向电脑端索要过真实状态（4 秒内不重复要）

    // HTML 转义：玩家消息与昵称属于不可信输入，直接拼进 innerHTML 会造成存储型 XSS
    function esc(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ★ V8.0.2：会话名归一化 —— 网页列表里的名字和聊天区里的名字可能差空格/大小写/标点，
    //   不归一化就会出现"明明是同一个会话，手机却认为'未打开'、点了打不开"。
    function nameKey(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/[\\s\\u3000]+/g, '')
            .replace(/[（）()【】\\[\\]·、:：;；"'`‘’“”]/g, '')
            .toLowerCase();
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
    // ★ V8.3：再加一层**行内样式**保险 —— 万一某条 CSS 被覆盖/没加载，视图也必须真的切过去
    //   （"点了卡片进不去会话"必须绝迹）。
    function showList() {
        const lv = document.getElementById('list-view');
        const cv = document.getElementById('chat-view');
        if (lv) { lv.classList.add('active'); if (lv.style) lv.style.display = 'flex'; }
        if (cv) { cv.classList.remove('active'); if (cv.style) cv.style.display = 'none'; }
    }
    function showChat() {
        const lv = document.getElementById('list-view');
        const cv = document.getElementById('chat-view');
        if (lv) { lv.classList.remove('active'); if (lv.style) lv.style.display = 'none'; }
        if (cv) { cv.classList.add('active'); if (cv.style) cv.style.display = 'flex'; }
        try { window.scrollTo(0, 0); } catch (e) {}
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
        const page = Number(globalState.im_status_page) || 0;
        // ★ V7.7：网页实际状态和我们的记录不一致时如实标出来（不再"手机显示忙碌、网页其实在线"却不吭声）
        const conflict = !!page && page !== st;
        if (pill) pill.className = 'pill';
        setText('im-label', (IM_STATUS_TEXT[st] || IM_STATUS_TEXT[1])
            + (conflict ? '（网页仍' + String(IM_STATUS_TEXT[page] || '').replace('IM ', '') + '）' : ''));
        setDot('im-dot', conflict ? 'warn' : (IM_DOT_CLASS[st] || 'idle'));
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
                const page = Number(globalState.im_status_page) || 0;
                body.innerHTML = [1, 2, 3].map(function (n) {
                    return '<div class="sheet-item' + (n === cur ? ' cur' : '') + '" data-set="im:' + n + '">' +
                        IM_STATUS_FULL[n] + (n === cur ? '<span class="tick">✓</span>' : '') + '</div>';
                }).join('')
                // ★ V7.7：网页实际与记录不一致 -> 给出"重试 / 以网页为准"两个逃生按钮
                + (page && page !== cur
                    ? '<div class="sheet-item" data-set="im:retry"><span>🔁 重试同步</span>' +
                      '<span class="hint">网页仍是 ' + (IM_STATUS_TEXT[page] || '') + '</span></div>' +
                      '<div class="sheet-item" data-set="im:page"><span>♻️ 以网页为准</span>' +
                      '<span class="hint">清除卡住的状态，跟随网页真实现状</span></div>'
                    : '')
                + '<div class="sheet-item" data-set="im:dump"><span>🧭 读一下网页的状态选项</span>' +
                  '<span class="hint">排障：看页面到底有哪些状态可选</span></div>';
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
                if (v === 'im:retry') retryIMStatus();
                else if (v === 'im:page') resetIMStatus();
                else if (v === 'im:dump') { sendMsg({ action: 'DUMP_STATUS' }); toast('已请电脑网页回报状态选项'); }
                else if (v.indexOf('im:') === 0) setIMStatus(Number(v.slice(3)));
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

    // ★ V7.7：把"人工意图"再切一次（页面没跟上时用）
    function retryIMStatus() {
        const st = Number((globalState && globalState.im_status) || 1);
        if (extensionOffline()) { toast('电脑端未连接，无法重试'); return false; }
        if (!sendMsg({ action: 'SET_IM_STATUS', status: st })) { toast('连接已断开，正在重连'); return false; }
        toast('已重试把状态切到 ' + (IM_STATUS_TEXT[st] || ''));
        return true;
    }

    // ★ V7.7：以网页真实现状为准（清掉卡住的手动锁）——"两边都改不动"时的逃生舱
    function resetIMStatus() {
        if (extensionOffline()) { toast('电脑端未连接，无法重置状态'); return false; }
        if (!sendMsg({ action: 'RESET_IM_STATE' })) { toast('连接已断开，正在重连'); return false; }
        toast('已按电脑网页真实现状重置');
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

    // ==================== 功能开关（三个 AI 动作可独立关闭） ====================
    // 开关来源：中继下发 state.features（config.json → enable_ai_close / enable_auto_draft / enable_f10_polish）
    function featureOn(key) {
        if (!globalState || !globalState.features) return true;      // 拿不到就按"开着"处理
        return globalState.features[key] !== false;
    }
    function renderFeatureButtons() {
        const aiCloseOn = featureOn('ai_close');
        // ★ V8.4：这些是**按钮**（点一下立刻执行），所以"关闭"用 🔒 + 说明文字表达，
        //   不再用 opacity 变灰（那看着像开关被关掉）。
        const b1 = document.getElementById('btn-ai-close');
        if (b1) {
            b1.innerText = '🤖 AI 回复并关单' + (aiCloseOn ? '' : ' 🔒');
            if (b1.classList) aiCloseOn ? b1.classList.remove('locked') : b1.classList.add('locked');
            b1.title = aiCloseOn ? '点击后 AI 立刻选分类、写结束语并关单'
                                 : '已在设置里关闭（enable_ai_close=false）';
        }
        const b2 = document.getElementById('btn-f9');
        if (b2) {
            b2.innerText = '✨ AI 立刻起草';
            b2.title = '点一下立刻让 AI 起草（不会等 1~3 分钟；那 1~3 分钟只用于"自动起草"）';
        }
        const d1 = document.getElementById('feature-hint');
        if (d1) {
            const off = [];
            if (!aiCloseOn) off.push('AI 回复并关单');
            const extra = featureOn('auto_draft') ? '' :
                '（「AI 自动起草」已关：新消息不会自动起草，但点「✨ AI 立刻起草」仍会立即起草）';
            d1.innerText = (off.length ? ('⛔ 已关闭：' + off.join('、') + '（改 config.json 后重启中继）') : '') + extra;
        }
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

    // 失败原因常驻条（toast 一闪而过看不清、也没法复制）
    function showErr(msg) {
        const bar = document.getElementById('err-bar');
        const txt = document.getElementById('err-text');
        if (!bar || !txt) return;
        try { txt.innerText = String(msg || ''); } catch (e) { txt.innerHTML = String(msg || ''); }
        bar.classList.add('show');
    }
    function hideErr() {
        const bar = document.getElementById('err-bar');
        if (bar) bar.classList.remove('show');
    }

    // ==================== 输入框自动长高（有上限，超出滚动） ====================
    // 需求：草稿/长文本要能看全；但高度要有上限，超过上限就滚动翻阅。
    function inputMaxHeight() {
        try {
            const h = (window.innerHeight || 700) * 0.34;
            return Math.max(72, Math.min(h, 240));
        } catch (e) { return 160; }
    }
    function autoGrowInput() {
        const el = document.getElementById('chat-input');
        if (!el || !el.style) return;                 // 老浏览器/测试沙箱没有 style 就直接跳过
        try {
            const max = inputMaxHeight();
            el.style.height = 'auto';
            const full = el.scrollHeight || 0;
            const h = Math.min(full, max);
            el.style.height = h + 'px';
            el.style.overflowY = (full > max) ? 'auto' : 'hidden';   // 超过上限 -> 可滚动翻阅
        } catch (e) {}
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

    // ★ 新消息"叮咚"（与"掉线警笛""需要人工三短一长"都不同）：两个清脆正弦音
    //   需求：玩家发来新消息时，手机端必须**立刻**能听到/看到（旧版手机端完全没有这个提示音）。
    function playNewMsgSound() {
        try {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            const base = audioCtx.currentTime;
            [1046.5, 1318.5].forEach(function (f, i) {          // C6 -> E6，清脆"叮~咚~"
                const t = base + i * 0.16;
                const osc = audioCtx.createOscillator(), gain = audioCtx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(f, t);
                gain.gain.setValueAtTime(0, t);
                gain.gain.linearRampToValueAtTime(0.55, t + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.01, t + 0.35);
                osc.connect(gain); gain.connect(audioCtx.destination);
                osc.start(t); osc.stop(t + 0.4);
            });
        } catch (e) {}
    }

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
    const notifiedMsgTs = {};                 // gid -> 已提示过的玩家消息 ts（同一条只响一次）
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
            '<span class="chip"><span class="dot ok"></span>' + esc(String(convCount)) + ' 个会话</span>' +
            // ★ V8.0.2：把手机页面自己的版本显式标出来 —— 排查"改了没生效"第一眼就有据可查
            '<span class="chip"><span class="dot ok"></span>页面 v' + esc(H5_VER) + '</span>';
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
            renderFeatureButtons();          // 三个开关：把已关闭的功能标出来
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
        else if (payload.type === 'NEW_MESSAGE') {
            // ★ 玩家来新消息：立刻响"叮咚" + 顶部提示 + 轻震动（同一条消息只提示一次）
            const gid = String(payload.groupID || '');
            const ts = Number(payload.ts) || Date.now();
            if (gid && notifiedMsgTs[gid] && ts <= notifiedMsgTs[gid]) return;
            if (gid) notifiedMsgTs[gid] = ts;         // 先记账：同一条只提示一次
            // 客服要求（第十九轮补充）：**统一都要提示**，不再区分"是否正在看这个会话"
            playNewMsgSound();
            try {
                if (typeof navigator !== 'undefined' && navigator.vibrate) navigator.vibrate(40);
            } catch (e) {}
            const nm = String(payload.name || '玩家');
            const pv = String(payload.preview || '').trim();
            toast('💬 ' + nm + '：' + (pv || '新消息') +
                  (payload.mode === 'manual' ? '（手动模式：AI 未自动起草）' : ''));
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
            const draft = String(payload.content || '');
            // ★ V8.5：草稿**绝不冲掉客服正在手机上敲的字**（和探针 fillReplyBox 同一条铁律）——
            //   输入框里已有"不是本页自己填的"内容时只提示、不覆盖；客服清空后再点「✨ AI 立刻起草」即可。
            const cur = input ? String(input.value || '') : '';
            if (input && draft && cur.trim() && cur !== lastDraftFilled) {
                const skipMsg = 'AI 草稿没覆盖你在输入框里写的内容（清空输入框后点「✨ AI 立刻起草」可重新生成）';
                toast('⚠️ ' + skipMsg);
                showErr('未覆盖你正在写的内容 · 草稿：' + draft.slice(0, 120));
                autoGrowInput();                       // 草稿可能很长：自动长高到看得全（有上限，超出滚动）
                return;
            }
            if (input) input.value = draft;
            lastDraftFilled = draft;
            autoGrowInput();                       // 草稿可能很长：自动长高到看得全（有上限，超出滚动）
            if (input && input.focus) { try { input.focus(); } catch (e) {} }
            // ★ V8.4：起草是"动作"，必须给明确反馈（客服点了才知道生效）
            if (draft) toast('✅ AI 草稿已填入输入框（可修改后点 ↑ 发送 · 页面 v' + H5_VER + '）');
        }
        else if (payload.type === 'AI_STATUS') {
            if (payload.message) toast(payload.message);   // 关单结果 / AI 状态提示
            // ★ 失败原因不要一闪而过：常驻显示，可复制（含页面真实按钮名）
            if (payload.status === 'error') {
                showErr((payload.groupID ? '[' + payload.groupID + '] ' : '') + (payload.message || '操作失败'));
            } else {
                hideErr();
            }
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
      if (countEl) {
        const pr = Array.isArray(globalState.conv_list) ? globalState.conv_list : [];
        countEl.innerText = pr.length ? (pr.length + ' 个（电脑网页）')
                                      : (keys.length ? (keys.length + ' 个进行中') : '');
      }

      if (keys.length === 0 && !(Array.isArray(globalState.conv_list) && globalState.conv_list.length)) {
        container.innerHTML = '<div class="conv-empty">暂无会话<br>请在电脑端打开玩家工单</div>';
        return;
      }

      // ★ V8.0.1：会话列表改成**一份**（以"电脑网页上的会话"为准）——
      //   客服反馈："会话"和"电脑网页上的会话"两个分组互相重复，意义不明。
      //   规则：网页列表里的每一行 -> 中继认识就渲染成完整卡片（点开聊天），
      //         中继还不认识就标「未打开」（点一下 = 让电脑切过去，之后自动帮你打开）；
      //         不在网页列表里的会话（旧会话/关单后残留）单独放最后一块，并写明"不在网页列表里"。
      const pageRows = Array.isArray(globalState.conv_list) ? globalState.conv_list : [];
      const gidByName = {};
      keys.forEach(g => {
        const n = nameKey((convs[g] || {}).name);
        if (n) gidByName[n] = g;
      });
      const rendered = {};
      const parts = [];

      function convCardHtml(gid, pageActive) {
        const c = convs[gid] || {};
        const msgs = Array.isArray(c.msgs) ? c.msgs : [];
        const last = msgs.length > 0 ? (msgs[msgs.length - 1] || {}) : null;
        const name = c.name || gid;
        const preview = (last && last.text) ? last.text : '（暂无消息）';
        const info = convInfo(c);
        const time = fmtTime((last && last.ts) || c.updatedAt);
        const unread = !!(last && last.sender === 'player' && gid !== activeGroupId);
        const pin = c.pinned ? '<span class="pin-badge">📌</span>' : '';
        const alertTag = c.alert ? '<span class="pin-badge">🙋</span>' : '';
        const closingTag = c.closing ? '<span class="pin-badge">⏳</span>' : '';
        // pageActive：这条会话就是电脑网页当前打开的那个（来自网页列表的 active 行）
        const openTag = pageActive ? '<span class="pin-badge">🖥 当前</span>' : '';
        return `<div class="conv-card${pageActive ? ' page-active' : ''}" data-gid="${esc(gid)}">
            <div class="avatar">${esc(String(name).charAt(0) || '玩')}</div>
            <div class="conv-body">
              <div class="conv-top">
                <div class="conv-name">${esc(name)}${openTag}${pin}${alertTag}${closingTag}${unread ? '<span class="unread-dot"></span>' : ''}</div>
                <div class="conv-time">${esc(time)}</div>
              </div>
              <div class="conv-lastmsg">${c.closing ? '⏳ 关单中…（等待页面确认）' : esc(preview)}</div>
              ${info ? '<div class="conv-info">' + esc(info) + '</div>' : ''}
            </div>
          </div>`;
      }

      function pageRowHtml(r) {
        const nm = String(r.name || '');
        const isOpen = !!r.active;
        const known = gidByName[nameKey(nm)] || '';
        if (known) {
          rendered[known] = true;
          return convCardHtml(known, isOpen);      // 认识：完整卡片（点开聊天）
        }
        return '<div class="conv-card' + (isOpen ? ' page-active' : '') + '" data-openname="' + esc(nm) + '"' +
               ' data-openlast="' + esc(String(r.last || '')) + '">' +
               '<div class="avatar">' + esc(nm.charAt(0) || '玩') + '</div>' +
               '<div class="conv-body">' +
                 '<div class="conv-top">' +
                   '<div class="conv-name">' + esc(nm) +
                     (isOpen ? '<span class="pin-badge">🖥 当前</span>' : '') +
                     '<span class="pin-badge">未打开</span>' +
                     (r.fresh ? '<span class="unread-dot"></span>' : '') +
                   '</div>' +
                   '<div class="conv-time">' + esc(String(r.time || '')) + '</div>' +
                 '</div>' +
                 '<div class="conv-lastmsg">' + esc(String(r.last || '')) + '</div>' +
               '</div>' +
             '</div>';
      }

      if (pageRows.length) {
        parts.push('<div class="list-title" style="margin:8px 2px">电脑网页上的会话（' + pageRows.length + '）</div>');
        // 单行渲染失败不许连累整张列表（渲染中断 = 后面的点击都没绑定 = "点不进卡片"）
        pageRows.forEach(r => {
          try { parts.push(pageRowHtml(r)); }
          catch (e) { console.warn('[H5] 渲染网页会话行失败（已跳过）', r, e); }
        });
      }
      const rest = keys.filter(g => !rendered[g]);
      // ★ V8.2：客服说「其它会话」很鸡肋 —— 不再一股脑列出来：
      //   只直接显示**需要留意**的（未读 / 需人工🙋 / 关单中⏳），其余收成一行可展开的摘要。
      //   ⚠️ 只有"已经有网页列表"时才这么做；没有列表（旧探针/还没上报）就照旧全列出来。
      function isImportant(g) {
        const c = convs[g] || {};
        const msgs = Array.isArray(c.msgs) ? c.msgs : [];
        const last = msgs.length ? msgs[msgs.length - 1] : null;
        return !!(c.alert || c.closing || (last && last.sender === 'player'));
      }
      const restImportant = pageRows.length ? rest.filter(isImportant) : rest;
      const restHidden = pageRows.length ? rest.filter(g => !isImportant(g)) : [];
      if (restImportant.length) {
        parts.push('<div class="list-title" style="margin:8px 2px">其它会话（不在网页列表里 · 需要留意）</div>');
        restImportant.forEach(g => {
          try { parts.push(convCardHtml(g)); }
          catch (e) { console.warn('[H5] 渲染会话卡片失败（已跳过）', g, e); }
        });
      }
      if (restHidden.length) {
        if (showOtherConvs) {
          parts.push('<div class="list-title" style="margin:8px 2px">其它会话（不在网页列表里）（'
                     + restHidden.length + '）</div>');
          restHidden.forEach(g => {
            try { parts.push(convCardHtml(g)); }
            catch (e) { console.warn('[H5] 渲染会话卡片失败（已跳过）', g, e); }
          });
        } else {
          parts.push('<div class="conv-card other-toggle" data-toggle-other="1">' +
                     '<div class="avatar">⋯</div>' +
                     '<div class="conv-body">' +
                       '<div class="conv-top">' +
                         '<div class="conv-name">其它会话（不在网页列表里） ' + restHidden.length + ' 条</div>' +
                         '<div class="conv-time">点开</div>' +
                       '</div>' +
                       '<div class="conv-lastmsg">一般是被关单/已过滤的历史会话；点一下展开</div>' +
                     '</div>' +
                   '</div>');
        }
      }
      container.innerHTML = parts.join('');

      // 事件委托绑定（不再把数据拼进 onclick）
      //   ★ V8.2：每个处理器都置 ev._h5Handled = true，避免与"启动时的兜底委托"重复处理
      container.querySelectorAll('.conv-card[data-gid]').forEach(el => {
        el.addEventListener('click', (ev) => { if (ev) ev._h5Handled = true; pushChat(el.dataset.gid); });
      });
      container.querySelectorAll('[data-openname]').forEach(el => {
        el.addEventListener('click', (ev) => {
          if (ev) ev._h5Handled = true;
          openPageConv(el.dataset.openname || '', el.dataset.openlast || '');
        });
      });
      container.querySelectorAll('[data-toggle-other]').forEach(el => {
        el.addEventListener('click', (ev) => {
          if (ev) ev._h5Handled = true;
          showOtherConvs = !showOtherConvs;
          renderAll();
        });
      });
      // ★ 点过的"未打开"会话，等它出现在中继会话列表里就自动打开聊天页（不用再点第二次）
      if (pendingOpenName) {
        if (Date.now() - pendingOpenAt > 25000) {
          pendingOpenName = '';                 // 25 秒还没出现就别再挂着（免得以后乱开）
        } else {
          let hit = keys.find(g => nameKey((convs[g] || {}).name) === pendingOpenName);
          if (!hit) {
            // 名字对不上时给一次"兜底"：如果就冒出来一条**以前没见过**的会话，那就是它
            const fresh = keys.filter(g => !lastKnownGids[g]);
            if (fresh.length === 1) hit = fresh[0];
          }
          if (hit) {
            const want = pendingOpenName;
            pendingOpenName = '';
            toast('已打开「' + ((convs[hit] || {}).name || want) + '」');
            pushChat(hit);
          }
        }
      }
      lastKnownGids = {};
      keys.forEach(g => { lastKnownGids[g] = true; });

      if (activeGroupId && getConv(activeGroupId)) {
        const c = getConv(activeGroupId);
        renderChatStream(c);
        renderPlayerCard(c);
      }
    }

    // 点网页列表里的会话：让电脑切过去；中继认识的话同时打开聊天页
    function openPageConv(name, lastText) {
      if (!name) return;
      toast('正在让电脑打开「' + name + '」…（页面 v' + H5_VER + '）');
      const key = nameKey(name);
      const gid = (function () {
        const convs = getConvs();
        return Object.keys(convs).find(g => nameKey((convs[g] || {}).name) === key) || '';
      })();
      // 本地已经认识这条会话：直接打开聊天页（能开就别让客服多点一次）
      if (gid) {
        pushChat(gid);
        if (extensionOffline() || !ws || ws.readyState !== WebSocket.OPEN) return;
        sendMsg({ action: 'OPEN_CONV', name: name, lastText: lastText });   // 顺手让电脑也切过去
        return;
      }
      if (extensionOffline()) { toast('电脑端未连接，无法切换会话'); return; }
      if (!sendMsg({ action: 'OPEN_CONV', name: name, lastText: lastText })) {
        toast('连接已断开，正在重连');
        return;
      }
      pendingOpenName = key;                  // 还没有这条会话：等探针上报后自动打开
      pendingOpenAt = Date.now();
      toast('正在让电脑打开「' + name + '」…');
    }
    function pushChat(gid) {
      const conv = getConv(gid);
      if (!conv) {
        // ★ V8.2：数据还没到也要给反馈，不能"点了没反应"
        toast('这条会话的数据还没到，稍等一下再点…（页面 v' + H5_VER + '）');
        return;
      }
      activeGroupId = gid;
      playerCardOpen = false;
      // ★ V8.3：点卡片一定给反馈（带页面版本）——"点了有没有反应"从此可自证
      toast('打开「' + String(conv.name || gid) + '」…（页面 v' + H5_VER + '）');
      try {
        document.getElementById('chat-player-name').innerText = conv.name || gid;
        const meta = document.getElementById('chat-ticket-id');
        if (meta) {
          const t = String(gid);
          meta.innerText = t.length > 16 ? t.slice(-16) : t;
        }
      } catch (e) { console.warn('[H5] 设置会话标题失败', e); }
      // ★ V8.2：**先切视图**再渲染 —— 以前渲染里任何一处抛异常，showChat 就永远不会执行，
      //   表现就是"点了卡片没反应 / 进不去会话"（客服反馈过两次）。
      showChat();
      try { renderPlayerCard(conv); } catch (e) { console.warn('[H5] 玩家卡渲染失败', e); }
      try { renderChatStream(conv); } catch (e) { console.warn('[H5] 聊天流渲染失败', e); }
      try { renderAll(); } catch (e) { console.warn('[H5] 列表刷新失败', e); }   // 清掉该会话的未读点
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
          if (!featureOn('ai_close')) {
              const m = '「AI 回复并关单」已在设置里关闭（config.json → enable_ai_close=false）';
              toast(m); showErr(m);
              return;
          }
          if (!confirm('AI 将自动选择问题分类并生成结束语，然后回复并关单；关单后该会话会从列表移除。确定继续？')) return;
          if (!sendMsg({ action: 'AI_CLOSE', groupID: activeGroupId })) {
              toast('连接已断开，正在重连');
              return;
          }
          toast('AI 正在生成结束语…（页面 v' + H5_VER + '）');
          return;
      }
      if (cmd === 'F9') {
          // ★ V8.4：这是**动作按钮** —— 点一下立刻起草（force=True，不走 1~3 分钟的自动节奏）
          if (!sendMsg({ action: 'TRIGGER_F9', groupID: activeGroupId })) {
              toast('连接已断开，正在重连');
              return;
          }
          toast('AI 正在起草，马上填入输入框…（页面 v' + H5_VER + '）');
          return;
      } else if (cmd === 'HANGUP') {
          sendMsg({ action: 'EXT_COMMAND', command: 'ACTION_HANGUP', groupID: activeGroupId });
          toast('已请求电脑端挂起，结果会再提示…（页面 v' + H5_VER + '）');
      } else if (cmd === 'RESUME') {
          sendMsg({ action: 'EXT_COMMAND', command: 'ACTION_RESUME', groupID: activeGroupId });
          toast('已请求电脑端恢复/接入，结果会再提示…（页面 v' + H5_VER + '）');
      } else {
          const input = document.getElementById('chat-input');
          const text = input && input.value ? input.value.trim() : '';
          if (cmd === 'CLOSE') {
              sendMsg({ action: 'EXT_COMMAND', command: 'ACTION_REPLY_CLOSE', groupID: activeGroupId, content: text, category: "其他" });
              toast('已请求回复并关单…（页面 v' + H5_VER + '）');
          } else if (cmd === 'SEND' && text) {
              const c = getConv(activeGroupId);
              if (!sendMsg({ action: 'SEND_REPLY', groupID: activeGroupId, content: text,
                             name: (c && c.name && String(c.name) !== String(activeGroupId)) ? c.name : '' })) {
                  toast('连接已断开，正在重连');
                  return;
              }
              toast('已发送（页面 v' + H5_VER + '）');
          }
          if (input) { input.value = ''; lastDraftFilled = ''; }   // ★ V8.5：清空后草稿可重新填入（不再被"已有内容"挡住）
          autoGrowInput();                       // 清空后把高度收回去
      }
    }

    // ==================== 启动 ====================
    (function bindListFallback() {
        // ★ V8.2/V8.3：列表点击的**兜底委托** —— 万一某次渲染没把点击绑上（或将来改坏了绑定），
        //   点卡片依然能进会话，不会再出现"点了没反应"。卡片上的直接绑定会先跑并置 ev._h5Handled，
        //   所以这里只做兜底，不会重复处理（不会重复发 OPEN_CONV）。
        //   同时绑在容器与 document 上（两处都带 _h5Handled 去重），容错到底。
        function onTap(ev) {
            if (!ev || ev._h5Handled) return;
            const t = ev.target || {};
            if (typeof t.closest !== 'function') return;
            const card = t.closest('.conv-card[data-gid]');
            if (card && card.dataset && card.dataset.gid) {
                ev._h5Handled = true;
                pushChat(card.dataset.gid);
                return;
            }
            const row = t.closest('[data-openname]');
            if (row && row.dataset) {
                ev._h5Handled = true;
                openPageConv(row.dataset.openname || '', row.dataset.openlast || '');
                return;
            }
            const tg = t.closest('[data-toggle-other]');
            if (tg) {
                ev._h5Handled = true;
                showOtherConvs = !showOtherConvs;
                renderAll();
            }
        }
        const box = document.getElementById('conv-container');
        if (box && box.addEventListener) box.addEventListener('click', onTap);
        try { document.addEventListener('click', onTap); } catch (e) {}
    })();
    // 输入框：输入/聚焦时自动长高（有上限，超出滚动翻阅）；草稿被填进来时也会自动长高
    (function bindInputGrow() {
        const el = document.getElementById('chat-input');
        if (!el || !el.addEventListener) return;
        ['input', 'focus', 'change'].forEach(ev => el.addEventListener(ev, autoGrowInput));
        el.addEventListener('keydown', function (e) {           // 回车换行后重新量高度
            setTimeout(autoGrowInput, 0);
        });
        autoGrowInput();
    })();
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
    # ★ V7.7：手机页面必须 no-store —— 否则 iOS 会把旧版 H5 缓存下来，
    #   新加的按钮/提示（如「以网页为准」「网页仍在线」）在手机上根本看不到。
    # ★ V8.5.1：再加 Expires:0，进一步压住 iOS Safari 对 no-store 偶发无视的缓存（"改了没生效/打不开卡片"第一排查点）。
    return web.Response(text=HTML_CONTENT, content_type="text/html",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                                 "Pragma": "no-cache", "Expires": "0"})


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
    # ★ 安全闸：F9/F10 的文案是"可能被直接发出去"的，同样必须零禁词
    content, left = safe_outbound(content, "F9/F10 直填")
    if not content.strip():
        return web.json_response({"ok": False, "error": "内容清洗后为空（只含内部提示），已拦截"}, status=400)
    if not active_clients["extension"]:
        return web.json_response({"ok": False, "error": "电脑端探针未连接（网页没开 / 脚本没跑）"}, status=503)
    # ★ V8.0：`?test=1` 时只发给**测试探针** —— 测试脚本调用本接口不许往客服真实页面的回复框里填字
    # ★ V8.6：多段回复只填第 1 段，其余排队（用"网页当前工单"当键，客服发出去后自动续粘）
    _gid = page_gid()
    _segs = (split_reply(content, int(config.get("multi_send_max_seg", 5) or 5))
             if (multi_send_on() and _gid) else [content])
    _first = _segs[0] if _segs else content
    await send_to_player({"command": "FILL_DRAFT", "content": _first}, "F9/F10 直填",
                         origin=("test" if request.query.get("test") == "1" else None))
    if len(_segs) > 1:
        _qname = str((state["companies"]["main"]["conversations"].get(_gid) or {}).get("name") or _gid)
        queue_reply_segments(_gid, _qname, _segs, "F9/F10 直填")
    return web.json_response({"ok": True, "clients": len(active_clients["extension"]),
                              "test": request.query.get("test") == "1",
                              "filtered": left,
                              "segments": len(_segs),
                              "queue_gid": _gid if len(_segs) > 1 else ""})

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


async def api_im_reset(request):
    """♻️ 以电脑网页的真实现状为准重置 IM 状态（清掉卡住的手动锁）—— V7.7 逃生舱。

    真机场景：手机切"忙碌"时页面上没生效，中继却把"忙碌 + 手动"锁死了，
    于是手机显示忙碌、网页显示在线、两边都改不动。浏览器直接打开
    http://127.0.0.1:8765/api/im_reset 即可恢复成"跟随网页真实状态"。
    """
    st = reset_im_state(source="接口 /api/im_reset")
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    for ext in list(active_clients["extension"]):
        await safe_send(ext, {"command": "REQUEST_IM_STATUS"})
    return web.json_response({"ok": True, "im_status": st, "im_status_text": _im_txt(st),
                              "note": "已重置为跟随网页真实现状（手动锁已清除）"})


async def api_probe_ping(request):
    """🏓 诊断：给电脑端探针发一条 PING，等它"自报家门"后原样返回（只读，不改任何状态）。

    能一次性回答两个问题：
      ① 中继发的指令**到底有没有到探针**（3 秒内没有 PONG = 探针没处理指令）；
      ② 浏览器里跑的**到底是哪一版代码**（逐个 typeof 检查 v7.7/v7.8 的新函数）。
    用法：浏览器打开 http://127.0.0.1:8765/api/probe_ping
    """
    conns = list(active_clients["extension"])
    if not conns:
        return web.json_response({"ok": False, "error": "电脑端探针未连接（工作台页面没开/脚本没跑）"}, status=503)
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _PONG_WAITERS.append(fut)
    sent = 0
    for ext in conns:
        if await safe_send(ext, {"command": "PING"}):
            sent += 1
    state["ext_cmd_debug"] = {"cmd": "PING", "conns": len(conns), "sent": sent, "ts": int(time.time())}
    try:
        data = await asyncio.wait_for(fut, timeout=3.0)
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "conns": len(conns), "sent": sent,
                                  "error": "探针 3 秒内没回 PONG：说明它没处理这条指令 —— "
                                           "请确认工作台页面里是**最新脚本**（左下角胶囊显示 v7.8），"
                                           "或看页面 Console 有没有报错"})
    return web.json_response({"ok": True, "conns": len(conns), "sent": sent, "pong": data})


async def api_outbound(request):
    """🛑 一键止血开关：停发 / 恢复所有"会进玩家对话框"的内容。

    用法：`/api/outbound?on=0` 立即停发（SEND_REPLY / FILL_DRAFT / 回复并关单 全被拦下），
          `/api/outbound?on=1` 恢复。对应 config.json 的 `outbound_enabled`（落盘生效）。
    """
    on = str(request.query.get("on", "1")).lower() not in ("0", "false", "off", "no")
    config["outbound_enabled"] = bool(on)
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[停发] 写入 config.json 失败（内存里已生效）：{e}")
    print(f"[停发] {'已恢复发送' if on else '🛑 已停发所有出站内容'}")
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
        await safe_send(m, {"type": "AI_STATUS", "status": "ok" if on else "error",
                            "message": ("已恢复发送" if on else "🛑 已停发：所有会进玩家对话框的内容都被拦住")})
    return web.json_response({"ok": True, "outbound_enabled": bool(on)})


async def api_purge_test_data(request):
    """🧹 清掉测试脚本造成的脏数据（形如 F-<数字>/AUTO-<数字>/T-A-<数字>/CLOSEFAIL-<数字> 的会话）。

    真实工单 id 是"页面上真实工单号"或 P+hash，不会被误删。
    """
    gone = _purge_test_convs()
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    print(f"[清理] 已移除 {len(gone)} 个测试会话：{', '.join(gone[:10])}")
    return web.json_response({"ok": True, "removed": gone, "removed_count": len(gone)})


async def api_diag(request):
    # 顺手刷新一次三个开关（配置文件被改过也能反映出来）
    state["features"] = feature_flags()
    """一键自检：中继 / 探针 / 手机端 到底谁没在线（排障第一入口）。"""
    convs = state["companies"]["main"]["conversations"]
    now = time.time()
    last_seen = _PROBE_META.get("last_seen") or 0
    return web.json_response({
        "ok": True,
        "server_ver": SERVER_VER,
        # ★ V8.3：已捕获的人工回复样本数（distill_rules.py 的语料；0 说明探针还没记到，不是坏了）
        "demo_records": int(state.get("demo_records") or 0),
        # ★ V8.6：半自动"待发段"队列（客服发出上一段后会自动粘下一段；HUD 会显示它）
        "reply_queue": {_g: {"name": _q.get("name"), "total": _q.get("total"),
                             "sent": _q.get("sent"), "remaining": len(_q.get("remaining") or []),
                             "next": ((_q.get("remaining") or [""])[0] or "")[:60],
                             "source": _q.get("source"),
                             "age_sec": int(time.time() - float(_q.get("ts") or 0))}
                        for _g, _q in list(_REPLY_QUEUE.items())[:5]},
        # ★ V8.6：电脑端提醒兜底（探针出不了声时，由桌面悬浮窗响铃）
        "sound_ok": state.get("probe_sound_ok", True),
        "desktop_ding": state.get("desktop_ding") or {},
        "desktop_alarm": state.get("desktop_alarm") or {},
        "sound_blocked": state.get("sound_blocked") or {},
        # ★ QC：质检页历史语料采集（走独立通道 /ws/qc，与工作台探针/出站链路完全无关）
        "history_records": int(state.get("history_records") or 0),
        "qc_clients": int(state.get("qc_clients") or 0),
        "qc_sweep": state.get("qc_sweep") or {},
        "qc_sweep_done": state.get("qc_sweep_done") or {},
        "features": state.get("features") or feature_flags(),
        # ★ V7.9 安全状态：出站总开关 + "网页当前打开的工单" + 测试客户端数（一键止血/防发错人）
        "safety": {
            "outbound_enabled": outbound_enabled(),
            "page_gid": page_gid(),
            "test_clients": len(TEST_WS),
            "pause_url": "/api/outbound?on=0",
            "purge_test_url": "/api/purge_test_data",
            "note": "outbound_enabled=false 时所有会进玩家对话框的内容都会被拦下；"
                    "page_gid 与目标会话不一致时会拒绝发送（探针永远发到页面当前打开的工单）",
        },
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
            # ★ V7.7："探针连接数" —— 排查"指令发不出去 / 发了没反应"的第一现场
            "connections": len(active_clients["extension"]),
            # ★ V7.7：各类探针事件的累计次数（判断"探针到底发没发"用）
            "events": state.get("event_counts") or {},
            # ★ V7.8：探针最近一次"自报家门"（GET /api/probe_ping 触发）
            "pong": state.get("probe_pong") or {},
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
            "status_text": _im_txt(state.get("im_status")),
            "known": bool(state.get("im_status_known")),
            "manual": bool(state.get("im_status_manual")),
            # ★ V7.7：网页真实现状 / 是否与记录不一致 / 已重试几次（专治"手机改不了、网页也不对"）
            "page_status": state.get("im_status_page") or 0,
            "page_status_text": _im_txt(state.get("im_status_page")) if state.get("im_status_page") else "",
            "conflict": bool(state.get("im_status_page")
                             and int(state.get("im_status_page")) != int(state.get("im_status") or 1)),
            "tries": int(state.get("im_status_tries") or 0),
            "last_request": state.get("im_last_request") or {},
            "last_action": state.get("last_action") or {},
            "status_menu_dump": state.get("status_menu_dump") or {},
            "ext_cmd_debug": state.get("ext_cmd_debug") or {},
            # ★ V7.8：探针"确认收到指令"的次数（= 指令确实到达并被处理过的硬证据）
            "via_relay_confirm": state.get("im_via_relay") or {"count": 0, "last_ts": 0},
            "reset_url": "/api/im_reset",
            "state_file": os.path.basename(IM_STATE_PATH),
            "hint": "known=false 表示本进程还没从电脑网页核实过状态（手机端会显示\"正在获取…\"，不会假装在线）；"
                    "conflict=true 时手机上可点「以网页为准」，或直接访问 /api/im_reset",
        },
        "ticket": {
            "active_gid": _LAST_ACTIVE.get("gid"),
            "active_name": (convs.get(_LAST_ACTIVE.get("gid")) or {}).get("name", ""),
            "conversations": len(convs),
            # ★ V8.0：网页左侧会话列表（手机端"全部会话"就是它）
            "page_list_count": len(state.get("conv_list") or []),
            "page_list": [str(r.get("name") or "") for r in (state.get("conv_list") or [])][:20],
            "page_list_test_count": len(state.get("conv_list_test") or []),
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
            # ★ V8.0：回复前是否自动把电脑网页切到目标会话（手机远程/AI 自动回复靠它）
            "auto_open_conv": bool(config.get("auto_open_conv", True)),
            "outbound_enabled": outbound_enabled(),
        },
        "hint": "probe.online=false => 油猴脚本没跑起来；probe.version 落后 => TM 里是旧脚本",
        # ★ V8.0.2：出站审计 —— 最近 10 次"要进玩家对话框"的指令（谁触发、什么模式、发给几个探针、成不成）
        #   客服可以据此确认"到底是不是系统自己发的"
        "outbound_log": (state.get("outbound_log") or [])[:10],
        "warn": (f"检测到 {len(active_clients['extension'])} 个电脑端探针连接 —— "
                 f"多开工作台标签页会导致同一条消息被发两遍，建议只留一个"
                 if len(active_clients["extension"]) > 1 else ""),
    })


# ==================== 探针命中域名注入（仓库里只放占位域名） ====================
# ★ 为什么需要它：油猴脚本的 @match 必须是**精确的工作台域名**才能注入页面；
#   但仓库是公开的，不该写内部域名。于是：
#     · probe.js（入库）里放占位域名 `*://ticket.example.com/*`
#     · 真实域名写在本地 config.json 的 workbench_domains
#     · 中继在 /probe.js 响应时把占位行替换成真实域名 —— 你从
#       http://127.0.0.1:8765/probe.js 复制出来的一定是能用的版本。
PLACEHOLDER_HOST = "ticket.example.com"


def _workbench_domains():
    d = config.get("workbench_domains")
    if isinstance(d, str):
        d = [d]
    out = []
    for x in (d or []):
        x = str(x).strip().lstrip("/").rstrip("/")
        if x and x not in out:
            out.append(x)
    if not out:
        x = str(config.get("workbench_domain") or "").strip()
        if x:
            out.append(x)
    return out


def render_probe_js(body: str) -> str:
    """把脚本里的占位 @match 域名替换成本机配置的真实域名（没有配置就原样返回）。"""
    hosts = _workbench_domains()
    if not hosts or PLACEHOLDER_HOST not in body:
        return body
    lines = []
    for line in body.split("\n"):
        if ("@match" in line or "@include" in line) and PLACEHOLDER_HOST in line:
            lines.append(line.replace(PLACEHOLDER_HOST, hosts[0]))
            for h in hosts[1:]:
                lines.append(line.replace(PLACEHOLDER_HOST, h))
        else:
            lines.append(line)
    return "\n".join(lines)


async def probe_js_handler(request):
    """把项目里最新的探针脚本直接发给浏览器。

    用途：打开 `http://IP:8765/probe.js` 即可核对油猴里那版是不是最新；
    加 `?download=1` 直接下载。省去"脚本从哪拷"的沟通成本。
    ★ 会把 @match 里的占位域名替换成 config.json → workbench_domains 配置的真实域名。
    """
    path = os.path.join(BASE_DIR, "probe.js")
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = render_probe_js(f.read())
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
<title>系统自检</title>
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
<h1>🩺 系统自检 <span class="k">（每 5 秒自动刷新）</span></h1>
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
                 row("探针连接数", len(active_clients["extension"])) +
                 row("指令自检（🏓）", (lambda _p: (
                     ("收到指令 ✅ · 自报 v%s · 连接 %s 个" % (_p.get("version") or "?", len(active_clients["extension"])))
                     if _p else
                     '<a href="/api/probe_ping">点这里测一发</a>（3 秒内回 PONG = 探针在正常收指令）'
                 ))(state.get("probe_pong") or {})) +
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
    _page2 = state.get("im_status_page") or 0
    cards.append('<div class="card"><div class="k">IM 状态（手机端顶部下拉框）</div>' +
                 row("当前状态", im_line) +
                 row("网页实际（探针读到的）", {1: "🟢 在线", 2: "🟡 忙碌", 3: "🔴 离线"}.get(_page2, "未知（还没读到）")) +
                 row("同步状态", ("⚠️ 不一致：记录是「%s」、网页是「%s」—— 中继会自动重试，手机上也能点「以网页为准」"
                                  % (_im_txt(state.get("im_status")), _im_txt(_page2)))
                                 if (_page2 and int(_page2) != int(state.get("im_status") or 1))
                                 else "✅ 一致（或网页还没上报）") +
                 row("重试次数", int(state.get("im_status_tries") or 0)) +
                 row("状态记忆", ("%s · %s" % ({1: "在线", 2: "忙碌", 3: "离线"}.get(_im_st2, _im_st2),
                                                time.strftime("%m-%d %H:%M", time.localtime(_im_ts2))))
                                if _im_ts2 else "无（还没上报过）") +
                 row("离线守护", "开启（网页自己跳回在线会被改回）"
                                if config.get("keep_manual_offline", True) else "关闭") +
                 row("状态卡住时", '<a href="/api/im_reset">以网页为准重置</a>（清掉手动锁，跟随网页真实状态）') +
                 row("上次切换请求", (("切到「%s」· %s · %s"
                                      % (_im_txt((state.get("im_last_request") or {}).get("status")),
                                         (state.get("im_last_request") or {}).get("source", ""),
                                         time.strftime("%H:%M:%S",
                                                       time.localtime((state.get("im_last_request") or {}).get("ts") or 0))))
                                     if state.get("im_last_request") else "无")) +
                 row("上次动作回执", (("✅ " if (state.get("last_action") or {}).get("ok") else "❌ ")
                                      + str((state.get("last_action") or {}).get("action") or "") + " · "
                                      + str((state.get("last_action") or {}).get("detail") or "")
                                      + " · "
                                      + time.strftime("%H:%M:%S",
                                                      time.localtime((state.get("last_action") or {}).get("ts") or 0)))
                                     if state.get("last_action") else "无") +
                 row("网页下拉实测", (("当前 %s；可见选项：%s"
                                       % ((state.get("status_menu_dump") or {}).get("current") or "-",
                                          " / ".join([str(i.get("tag", "")) + ":" + str(i.get("text", ""))
                                                      for i in ((state.get("status_menu_dump") or {}).get("items") or [])])
                                          or "（一个都没读到）"))
                                      if state.get("status_menu_dump") else
                                      "还没读过（手机状态面板点「🧭 读一下网页的状态选项」）")) +
                 row("探针连接数", len(active_clients["extension"])) +
                 row("最近一次下发", (("%s · 连接 %s 个 · 成功 %s 个 · %s"
                                      % ((state.get("ext_cmd_debug") or {}).get("cmd", "-"),
                                         (state.get("ext_cmd_debug") or {}).get("conns", 0),
                                         (state.get("ext_cmd_debug") or {}).get("sent", 0),
                                         time.strftime("%H:%M:%S",
                                                       time.localtime((state.get("ext_cmd_debug") or {}).get("ts") or 0))))
                                     if state.get("ext_cmd_debug") else "无（还没下发过指令）")) +
                 '</div>')

    cards.append('<div class="card"><div class="k">出站安全（V7.9）</div>' +
                 row("是否允许发送", "✅ 允许" if outbound_enabled() else
                     "🛑 已停发（<a href=\"/api/outbound?on=1\">点这里恢复</a>）") +
                 row("网页当前工单", page_gid() or "（探针还没上报）") +
                 row("测试客户端连接数", len(TEST_WS)) +
                 row("一键停发", '<a href="/api/outbound?on=0">立即停发所有出站内容</a>') +
                 row("清理测试脏数据", '<a href="/api/purge_test_data">清掉测试造的会话</a>') +
                 '</div>')

    cards.append('<div class="card"><div class="k">工单与知识库</div>' +
                 row("会话数", len(convs)) +
                 row("问题分类数", len(state.get("category_options") or [])) +
                 row("人工样本（蒸馏语料）", "%s 条 · 见 demonstrations.jsonl（离线跑 distill_rules.py 提炼话术）"
                     % int(state.get("demo_records") or 0)) +
                 row("历史会话样本（质检页采集）", "%s 条 · 见 demonstrations_history.jsonl"
                     "（distill_rules.py --mode history）"
                     % int(state.get("history_records") or 0)) +
                 row("质检页采集器", ("已连接 %d 个" % int(state.get("qc_clients") or 0)) +
                     (" · 最近进度 已采 %s / 跳过 %s / 失败 %s"
                      % ((state.get("qc_sweep") or {}).get("done", 0),
                         (state.get("qc_sweep") or {}).get("skipped", 0),
                         (state.get("qc_sweep") or {}).get("failed", 0))
                      if state.get("qc_sweep") else
                      ' · 未开始（<a href="/api/qc_sweep?on=1">点这里启动采集</a>）')) +
                 row("规章条目", "%s 条 / %s 张表" % (kb.get("total_rows", kb.get("error", "-")),
                                                      kb.get("sheets", "-"))) +
                 row("常驻静态区", "%s 字符" % kb.get("static_chars", "-")) +
                 '</div>')

    html = DIAG_HTML.replace("$CARDS", "".join(cards)).replace("$EXPECT_VER", SERVER_VER)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


# ==================== ★ QC 采集器：独立通道 /ws/qc + 启停接口（与工作台探针物理隔离） ====================
# 为什么单独开一条通道：质检页采集器只做"读历史 + 落盘"，它**绝不能**被当成工作台探针——
#   不进 ext_targets（不会被下发 FILL_DRAFT / SEND_REPLY 之类的发送指令）、
#   不进手机端会话列表、不推 Bark。分开一条 WS 通道 = 从物理上杜绝这类事故。
_QC_CONNS = set()
_QC_TEST_CONNS = set()      # ★ V8.7：测试来源的采集器（完成通知不推真实 Bark/手机）


async def qc_probe_js_handler(request):
    """把质检页采集器脚本发给浏览器（`http://IP:8765/qc_probe.js`）。

    与 /probe.js 同款：把 @match 里的占位域名替换成本机 config.json → workbench_domains 的真实域名。
    安装方法见 README 7.9；★ 请从这个地址复制，别直接复制项目目录里的文件（域名是占位符）。
    """
    path = os.path.join(BASE_DIR, "qc_probe.js")
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = render_probe_js(f.read())
    except Exception:
        return web.json_response({"ok": False, "error": "qc_probe.js 不存在于项目目录"}, status=404)
    headers = {"Cache-Control": "no-store"}
    if request.query.get("download"):
        headers["Content-Disposition"] = 'attachment; filename="qc_probe.js"'
    return web.Response(text=body, content_type="application/javascript", headers=headers)


async def api_qc_status(request):
    """质检页采集器的状态：连没连上、上一次扫描进度、已采到多少条历史语料。"""
    return web.json_response({
        "ok": True,
        "clients": len(_QC_CONNS),
        "hello": state.get("qc_hello") or {},
        "progress": state.get("qc_sweep") or {},
        "last_done": state.get("qc_sweep_done") or {},
        "history_records": int(state.get("history_records") or 0),
        "history_file": os.path.basename(HISTORY_FILE),
        "dump": state.get("qc_dump") or {},          # ★ 质检页结构回报（/api/qc_sweep?dump=1 触发后看这里）
        "script_url": "/qc_probe.js",
        "hint": "clients=0 说明质检页没装/没启用采集器脚本：打开 http://127.0.0.1:8765/qc_probe.js "
                "复制到油猴（详见 README 7.9）",
    })


async def api_qc_sweep(request):
    """启动/停止质检页历史采集（**只发指令给质检采集器**，绝不碰工作台探针）。

    用法：
      `/api/qc_sweep?on=1&max_rows=60&min_score=90&pages=4&row_delay_ms=2500`
      `/api/qc_sweep?on=0`  停止（当前这一条读完就停）
    """
    on = str(request.query.get("on", "1")).lower() not in ("0", "false", "no")
    # ?dump=1：让采集器把"详情面板的真实结构"回报过来（只读排障，用来校准正文节点/关闭键）
    if str(request.query.get("dump", "")).lower() in ("1", "true", "yes"):
        payload = {"command": "QC_DUMP"}
    else:
        payload = {"command": "QC_SWEEP", "on": on}
    if payload["command"] == "QC_SWEEP":
        for key, cast in (("max_rows", int), ("pages", int), ("row_delay_ms", int), ("min_score", float)):
            raw = request.query.get(key)
            if raw is None:
                continue
            try:
                payload[key] = cast(float(raw))
            except Exception:
                pass
    sent = 0
    for w in list(_QC_CONNS):
        try:
            await safe_send(w, payload)
            sent += 1
        except Exception:
            pass
    if sent:
        print(f"[QC] 📤 已{'启动' if on else '停止'}历史采集：{json.dumps(payload, ensure_ascii=False)}")
    return web.json_response({"ok": bool(sent), "targets": sent, "payload": payload,
                              "hint": "没有采集器在线？先在质检页装好 /qc_probe.js（README 7.9）；"
                                      "dump=1 的结果看 /api/qc_status 的 dump 字段"})


async def ws_qc_handler(request):
    """质检页采集器专用 WebSocket（qc_probe.js 连这里）。

    ★ 与 /ws/extension（工作台探针）完全隔离：
      · 收到的事件只做两件事 —— 落盘历史语料、更新诊断进度；
      · 不广播给手机端、不写 state["companies"]、不推 Bark；
      · 不在 ext_targets 里，因此**永远不会**被下发发送/填草稿类指令。
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _QC_CONNS.add(ws)
    # ★ V8.7：`ws://.../ws/qc?test=1` = 测试来源：进度照记，但**完成通知不推真实 Bark/手机**
    if str(request.query.get("test") or "") == "1":
        _QC_TEST_CONNS.add(ws)
    state["qc_clients"] = len(_QC_CONNS)
    print(f"[QC] ✅ 采集器已连接（当前 {len(_QC_CONNS)} 个）· 来源: {request.headers.get('Referer', '未知')}")
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                pkt = json.loads(msg.data)
            except Exception:
                continue
            if not isinstance(pkt, dict):
                continue
            ev = str(pkt.get("event") or "")
            data = pkt.get("data") or {}
            if ev in ("QC_HELLO", "QC_HEARTBEAT"):
                state["qc_hello"] = {"version": str(data.get("version") or "")[:16],
                                     "page": str(data.get("page") or "")[:200],
                                     "ua": str(data.get("ua") or "")[:120],
                                     "running": bool(data.get("running")),
                                     "last_seen": int(time.time())}
                if ev == "QC_HELLO":
                    print(f"[QC] 🚀 采集器握手：v{state['qc_hello']['version'] or '?'} · "
                          f"{state['qc_hello']['page'] or '未提供页面地址'}")
                continue
            if ev == "RECORD_HISTORY_SESSION":
                try:
                    added = await asyncio.get_event_loop().run_in_executor(
                        None, _append_history_record, data)
                except Exception as e:
                    print(f"[QCRecorder] ⚠️ 落盘失败（不影响页面）：{e}")
                    added = False
                if added:
                    state["history_records"] = int(state.get("history_records") or 0) + 1
                    print(f"[QCRecorder] 成功捕获 1 条历史会话样本（累计 {state['history_records']}）"
                          f" · 分类：{str(data.get('category') or '')[:28]}"
                          f" · 评分：{data.get('score') or '-'}"
                          f" · 消息：{len(data.get('messages') or [])} 条")
                continue
            if ev == "QC_SWEEP_PROGRESS":
                state["qc_sweep"] = dict(data)
                state["qc_sweep"]["ts"] = int(time.time())
                if int(data.get("done") or 0) % 5 == 0:
                    print(f"[QC] 进度：已采 {data.get('done')} / 跳过 {data.get('skipped')} / "
                          f"失败 {data.get('failed')} · 第 {data.get('page')} 页"
                          + (f" · {data.get('lastError')}" if data.get("lastError") else ""))
                continue
            if ev == "QC_SWEEP_DONE":
                state["qc_sweep_done"] = dict(data)
                state["qc_sweep_done"]["ts"] = int(time.time())
                _d = state["qc_sweep_done"]
                # ★ V8.7：给出"下一步"的可复制命令 —— 采集完就该蒸馏，不用人再想
                _d["next_step"] = (f"python distill_rules.py --mode history "
                                   f"--limit {max(1, int(_d.get('done') or 0))} --append")
                _secs = ""
                try:
                    if _d.get("elapsedMs"):
                        _secs = f" · 用时 {int(int(_d['elapsedMs']) / 1000)} 秒"
                except Exception:
                    pass
                _stall = (" · 后台节流 " + str(int(int(_d.get('stalledMs') or 0) / 1000)) + " 秒"
                          if int(_d.get("stalledMs") or 0) > 0 else "")
                _sum = (f"成功 {_d.get('done')} · 跳过(仅机器人) {_d.get('skipped')} · "
                        f"失败 {_d.get('failed')}{_secs}{_stall}（累计语料 {state.get('history_records') or 0} 条）")
                print(f"[QC] ✅ 扫描结束：{_sum} · 原因：{_d.get('reason') or '完成'}")
                print(f"[QC] 👉 现在可以蒸馏了：{_d['next_step']}")
                # ★ V8.7：**主动通知**（不用人盯着质检页）—— 手机 Bark + 手机端提示 + 桌面悬浮窗响铃
                if bool(config.get("notify_qc_done", True)) and ws not in _QC_TEST_CONNS:
                    push_bark("✅ 质检采集完成", f"{_sum}\n下一步：{_d['next_step']}")
                if ws not in _QC_TEST_CONNS:
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok",
                                            "message": f"质检采集完成：{_sum}｜已备好蒸馏命令（看悬浮窗/日志）"})
                continue
            if ev == "QC_DEBUG":
                state["qc_dump"] = dict(data)
                print(f"[QC] 🧭 结构回报：{json.dumps(data, ensure_ascii=False)[:600]}")
                continue
    finally:
        _QC_CONNS.discard(ws)
        _QC_TEST_CONNS.discard(ws)
        state["qc_clients"] = len(_QC_CONNS)
        print(f"[QC] 采集器已断开（剩 {len(_QC_CONNS)} 个）")
    return ws


async def ws_ext_handler(request):
    # heartbeat=30：定期 ping，及时发现 iOS 退后台/网络抖动造成的死连接
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    # ★ V7.9：测试脚本用 ?test=1 连上；这类连接只会收到"来自测试客户端"的指令（见 ext_targets）
    if request.query.get("test") == "1":
        TEST_WS.add(ws)
    active_clients["extension"].add(ws)
    _PROBE_CONNS.setdefault(ws, {})
    state["extension_online"] = True
    # 排障用横幅：一眼看出"电脑端探针到底连上来了没"（含来源页，便于识破多开/错页面）
    # ★ 客服要求（第十九轮）：**不要老是嗅探分类下拉**。
    #   旧版探针一连上就去点开"问题分类"读选项 —— 页面上的下拉会自己弹出来，很烦。
    #   现在改为**按需**：只有当真的要选分类（AI 关单）却还没有候选时，才让探针去读一次（见 ensure_category_options）。
    print(f"[探针] ✅ 已连接 · 来源: {request.headers.get('Referer', '未知')}")
    # 8 秒内不上报版本 => 油猴里还是旧脚本（< 7.3），主动喊一嗓子
    asyncio.create_task(_probe_hello_watchdog(ws))
    # 探针一连上**不再**主动去读分类（避免"下拉自己弹出来"）；真要选分类时按需请求（见 ensure_category_options）
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
                # ★ V7.7：事件计数（/api/diag 可查）—— 判断"探针到底发没发这条事件"的硬证据
                #   （排查"指令发了没反应"：到底是发给探针失败、探针没处理、还是中继丢了事件）
                if ev:
                    _cnt = state.setdefault("event_counts", {})
                    _cnt[str(ev)] = int(_cnt.get(str(ev)) or 0) + 1
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
                    # ★ V8.0：切会话的成败单独记一份（ensure_page_on 等它，能提前收工）
                    if cmd_name == "OPEN_CONV":
                        _cvgid = str(data.get("groupID") or "")
                        _OPEN_CONV_RESULT.update({"ts": time.time(), "ok": ok, "gid": _cvgid,
                                                  "detail": detail[:120], "test": ws in TEST_WS})
                        # 探针已确认"页面当前就是目标会话" -> 直接把页面绑定更新过去，
                        # 不必等下一次会话列表扫描（真机上切完能立刻发，不用干等）
                        if ok and _cvgid:
                            if ws in TEST_WS:
                                _LAST_TEST_PAGE_GID["gid"] = _cvgid
                            else:
                                _LAST_PAGE_GID["gid"] = _cvgid
                            # 按连接记：执行切换的这个探针，此刻打开的就是目标会话
                            try:
                                _PROBE_CONNS.setdefault(ws, {})["page_gid"] = _cvgid
                            except Exception:
                                pass
                    # ★ V7.7：最近一次动作请求与结果留存（/diag 可查，便于定位"点了没反应"）
                    state["last_action"] = {"command": cmd_name, "action": label, "ok": ok,
                                            "detail": detail, "ts": int(time.time()),
                                            "groupID": str(data.get("groupID") or "")}
                    msg = (f"{label}成功 · {detail}" if ok else f"{label}失败 · {detail}")
                    # ★ AI 关单：只有探针回报"真的点到关单按钮"才把会话从列表移除（否则保留，绝不误删）
                    if cmd_name == "ACTION_REPLY_CLOSE":
                        gid = str(data.get("groupID") or "")
                        if not gid and len(_PENDING_CLOSE) == 1:
                            gid = next(iter(_PENDING_CLOSE))       # 兼容老探针（回执不带工单号）
                        if gid:
                            removed, note = resolve_close(gid, ok, detail)
                            if ok and removed:
                                msg = f"已回复并关单 · {detail}"
                            elif not ok:
                                msg = f"关单未完成（会话已保留）· {detail}"
                            for m in list(active_clients["mobile"]):
                                await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok" if ok else "error",
                                            "message": msg[:300], "groupID": data.get("groupID") or ""})
                    continue

                # 探针回报的问题分类（用于手机端 AI 自动选分类关单）
                if ev == "CATEGORY_OPTIONS":
                    opts = (pkt.get("data") or {}).get("options", [])
                    if isinstance(opts, list) and opts:
                        state["category_options"] = [str(o)[:40] for o in opts][:300]
                        print(f"[分类] 已获取 {len(state['category_options'])} 个问题分类")
                        _notify_category_waiters(state["category_options"])   # 唤醒按需等待者
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
                    # ★ V7.7：manual 只认"人手在网页上点的"（探针新字段 manual）。
                    #   旧版是 `or (st in (2, 3))` —— 探针上报的忙碌/离线一律被当"手动"，
                    #   于是探针自己就把中继的手动锁绕过了（网页真实状态反过来盖掉客服的选择）。
                    manual = bool(data.get("manual", False))
                    via_relay = bool(data.get("via_relay", False))
                    if via_relay:
                        # ★ V7.8：收到"中继指令触发的上报" = 硬证据：探针确实处理了下行指令
                        _vr = state.setdefault("im_via_relay", {"count": 0, "last_ts": 0})
                        _vr["count"] = int(_vr.get("count") or 0) + 1
                        _vr["last_ts"] = int(time.time())
                        print(f"[状态] 📲 探针确认收到指令并回读：{_im_txt(st)}"
                              f"（累计 {_vr['count']} 次）")
                    _want = int(state.get("im_status") or 1)
                    # ★ V8.5.1：测试探针（?test=1）上报的 IM_STATUS 也只改内存、不落盘，别污染真实记忆
                    apply_im_status(st, manual=manual, source="探针上报", from_probe=True,
                                    persist=(ws not in TEST_WS))
                    if data.get("guarded"):
                        print("[状态] 🛡️ 探针已按手动离线设置，把网页自动跳回的在线改回离线")
                    if via_relay and st != _want:
                        # 探针对"中继指令"的回读：和我们要的不一致就记一笔（_reassert 会重试/如实提示）
                        print(f"[状态] ⚠️ 探针回读不一致：要求「{_im_txt(_want)}」，网页实际「{_im_txt(st)}」")
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                # 探针回报的"状态下拉可见选项"（V7.7 排障：实机校准状态匹配，绝不靠猜标签）
                if ev == "STATUS_MENU_DUMP":
                    data = pkt.get("data") or {}
                    items = data.get("items") or []
                    lines = [f"{it.get('tag', '')}:{it.get('text', '')}" for it in items if isinstance(it, dict)]
                    msg = ("网页状态下拉选项：" + (" / ".join(lines) if lines else "（没读到可见选项）")
                           + f"（当前显示：{data.get('current') or '未知'}）")
                    print("[状态] 🧭 " + msg)
                    state["status_menu_dump"] = {"current": data.get("current") or "",
                                                 "items": [it for it in items if isinstance(it, dict)][:20],
                                                 "trigger": data.get("trigger") or {},
                                                 "visible_status_nodes": data.get("visible_status_nodes") or [],
                                                 "ts": int(time.time())}
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok", "message": msg})
                    continue

                # ★ V7.7：探针处理指令出错（整段 onmessage 已加兜底）—— 让"点了没反应"不再无声无息
                if ev == "PROBE_ERROR":
                    data = pkt.get("data") or {}
                    emsg = f"探针处理指令出错（{data.get('where') or '?'}）：{data.get('error') or ''}"
                    print("[探针] ⚠️ " + emsg)
                    state["last_action"] = {"command": "PROBE_ERROR", "action": "探针错误", "ok": False,
                                            "detail": emsg, "ts": int(time.time())}
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "AI_STATUS", "status": "error", "message": emsg})
                    continue

                # ★ V8.6：探针报"浏览器不给出声"（需先点/按一下页面）——只记录+提示，不改任何状态
                if ev == "SOUND_BLOCKED":
                    data = pkt.get("data") or {}
                    state["sound_blocked"] = {"what": str(data.get("what") or "")[:16],
                                              "ts": int(time.time()),
                                              "hint": str(data.get("hint") or "")[:80]}
                    if ws not in TEST_WS:
                        state["probe_sound_ok"] = False
                    print(f"[提醒] 🔇 探针出不了声（{state['sound_blocked']['what']}）：请在工作台页面"
                          f"点一下或按一下键；在那之前由桌面悬浮窗兜底提醒")
                    continue

                # ★ V8.3：人工回复"影子录制" —— 客服真人发出的回复 + 当时的工单上下文，落盘给离线蒸馏用。
                #   铁律：
                #     ① 这条分支**只落盘、零出站**（不填草稿/不发送/不改状态），所以不需要也不允许走 safe_outbound
                #        —— 它记录的是"学习语料"，不是要发给玩家的内容；
                #     ② 落盘走线程池（run_in_executor），绝不在事件循环里做同步 IO（红线③）；
                #     ③ 测试来源（?test=1 的假探针）单独写 demonstrations.test.jsonl，不污染真实语料（红线⑥）。
                if ev == "RECORD_MANUAL_DEMO":
                    try:
                        added = await asyncio.get_event_loop().run_in_executor(
                            None, _append_demo_record, pkt.get("data") or {}, (ws in TEST_WS))
                    except Exception as e:
                        print(f"[DemoRecorder] ⚠️ 落盘失败（不影响任何发送）：{e}")
                        added = False
                    if added:
                        state["demo_records"] = int(state.get("demo_records") or 0) + 1
                        print("[DemoRecorder] 成功捕获 1 条人工标准回复样本")
                    # ★ V8.6：半自动"分段回复"——客服把上一条发出去后，自动把下一段粘进输入框
                    #   （安全阀见 advance_reply_queue：不像他发的那段就丢弃队列，绝不硬塞）
                    try:
                        _d = pkt.get("data") or {}
                        _mgid = str(_d.get("groupID") or "")
                        _mtxt = str(_d.get("humanReply") or "")
                        if _mgid and _mtxt and ws not in TEST_WS and _mgid in _REPLY_QUEUE:
                            await advance_reply_queue(_mgid, _mtxt)
                    except Exception as e:
                        print(f"[分段回复] 续粘失败（不影响发送）：{e}")
                    continue

                # ★ V8.0：探针上报"网页左侧会话列表"（手机端"全部会话"的数据源 + 切会话的依据）
                if ev == "CONV_LIST":
                    rows = (pkt.get("data") or {}).get("rows") or []
                    rows = [r for r in rows if isinstance(r, dict)][:40]
                    if rows:
                        # ★ 隔离：测试来源的会话列表只进诊断键，真实手机端不显示（和上次事故同一类问题）
                        is_test_ws = ws in TEST_WS
                        # ★ V8.0.1：列表本身的**变化**就是"网页来新会话/新消息"的唯一线索 ——
                        #   探针只在**当前打开的工单**里读聊天区，别的会话来消息（还没点开）根本不会
                        #   走 PLAYER_MESSAGE，只靠那条链路注定漏通知（客服反馈：网页来新会话时不响）。
                        _prev_rows = (state.get("conv_list_test") if is_test_ws else state.get("conv_list")) or []
                        _prev = {str(x.get("name") or "").strip(): str(x.get("last") or "")
                                 for x in _prev_rows if isinstance(x, dict)}
                        _first_sight = not bool(_prev)      # 第一次拿到列表：只建立基线，不刷一屏通知
                        fresh = []
                        for r in rows:
                            _nm = str(r.get("name") or "").strip()
                            if not _nm:
                                continue
                            _last = str(r.get("last") or "")
                            if r.get("active"):
                                r.pop("fresh", None)        # 已经被打开（人手或我们切的）-> 不再是"新"
                                continue
                            if _first_sight:
                                continue
                            if _nm not in _prev:
                                _kind = "new"
                            elif _prev.get(_nm) != _last:
                                _kind = "msg"
                            else:
                                continue
                            # ★ V8.3：别把自己刚回的也当成"新内容"来提醒自己
                            _own_last = ""
                            for _cg, _cc in state["companies"]["main"]["conversations"].items():
                                if str((_cc or {}).get("name") or "").strip() == _nm:
                                    _cm = _cc.get("msgs") or []
                                    if _cm and isinstance(_cm[-1], dict) and _cm[-1].get("sender") == "agent":
                                        _own_last = " ".join(str(_cm[-1].get("text") or "").split())[:60]
                                    break
                            if _own_last and _own_last == " ".join(str(_last).split())[:60]:
                                r.pop("fresh", None)
                                continue
                            r["fresh"] = True
                            fresh.append((_nm, _last, _kind))
                        if is_test_ws:
                            state["conv_list_test"] = rows
                        else:
                            state["conv_list"] = rows
                        # 用"当前高亮的那一行"对齐"网页当前打开的工单"（按会话名匹配已知会话）
                        for r in rows:
                            if not r.get("active"):
                                continue
                            _n = str(r.get("name") or "")
                            for _gid, _c in state["companies"]["main"]["conversations"].items():
                                if str((_c or {}).get("name") or "") == _n:
                                    if is_test_ws:
                                        _LAST_TEST_PAGE_GID["gid"] = str(_gid)
                                    else:
                                        _LAST_PAGE_GID["gid"] = str(_gid)
                                    try:
                                        _PROBE_CONNS.setdefault(ws, {})["page_gid"] = str(_gid)
                                    except Exception:
                                        pass
                                    break
                            break
                        for m in list(active_clients["mobile"]):
                            await safe_send(m, {"type": "FULL_SYNC", "data": state})
                        print(f"[会话列表] 已更新 {len(rows)} 个会话"
                              + ("（当前：" + str([r.get('name') for r in rows if r.get('active')][:1]) + "）"
                                 if any(r.get("active") for r in rows) else ""))
                        # 通知（只推给同一隔离类的手机端：真实行只推真实手机，测试行只推测试手机）
                        for _nm, _txt, _kind in fresh[:5]:
                            _preview = " ".join(str(_txt).split())[:60]
                            for m in _mobile_targets_for(is_test_ws):
                                await safe_send(m, {"type": "NEW_MESSAGE", "groupID": "page:" + _nm,
                                                    "name": _nm, "preview": _preview,
                                                    "mode": "page", "ts": int(time.time() * 1000),
                                                    "from_page_list": True, "kind": _kind})
                            print(f"[新消息] 🖥 网页列表{'新会话' if _kind == 'new' else '有新内容'}："
                                  f"{_nm} — {_preview[:30]}")
                            # ★ V8.3：**只有"冒出一条全新会话"才推 Bark**。
                            #   列表里只是"某行内容变了"分不清是谁发的（可能是我们自己刚回的、
                            #   也可能是页面把预览截断方式变了），拿它推锁屏通知 = 客服投诉的"切换工单也 Bark"。
                            if _kind == "new" and not is_test_ws and config.get("bark_on_new_message", True):
                                push_bark("🆕 新会话 " + _nm, _preview or "（电脑网页上出现了一条新会话）")
                    continue

                # ★ V7.8：探针自报家门（PONG）—— 诊断"指令到底有没有到、浏览器里是哪版代码"
                if ev == "PONG":
                    data = pkt.get("data") or {}
                    print("[探针] 🏓 PONG：" + json.dumps(data, ensure_ascii=False)[:400])
                    state["probe_pong"] = dict(data)
                    while _PONG_WAITERS:
                        _fut = _PONG_WAITERS.pop()
                        if not _fut.done():
                            try:
                                _fut.set_result(data)
                            except Exception:
                                pass
                    continue

                if ev == "ABNORMAL_OFFLINE":
                    # 异常掉线警报闭环（探针在页面上确实读到"离线"才会发这个事件）
                    state["alarm_status"] = True
                    # ★ V8.6/V8.7：探针若报"出不了声"或"页面在后台"，让桌面悬浮窗兜底报警（否则电脑静悄悄）
                    _ad = pkt.get("data") or {}
                    if "sound_ok" in _ad:
                        state["probe_sound_ok"] = bool(_ad.get("sound_ok"))
                    _ad_hidden = bool(_ad.get("hidden"))
                    if _ad.get("sound_ok") is False or _ad_hidden:
                        if ws not in TEST_WS:
                            state["desktop_alarm"] = {"ts": int(time.time() * 1000),
                                                      "why": ("页面在后台（不聚焦）" if _ad_hidden
                                                              else "探针无法出声（掉线警笛）")}
                            print(f"[提醒] {'🖥 页面在后台' if _ad_hidden else '🚨 探针出不了声'}"
                                  f" -> 已请桌面悬浮窗兜底报警")
                    apply_im_status(3, manual=False, source="异常掉线", persist=(ws not in TEST_WS))
                    # 推送给手机端
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    # 推送 Bark 通知（P0 修复：缺失的 Bark 警报）
                    # ★ V8.5 铁律⑨：测试探针发来的掉线事件只做站内告警，绝不推客服手机
                    if ws in TEST_WS:
                        print("[掉线] 测试来源：只做站内告警，不推 Bark")
                    else:
                        push_bark("🚨 异常掉线警报", "VPN 或网页网络连接断开，请立即检查！")
                    # 向探针发送确认回执（修复 BUG-002：防止重复上报）
                    await ws.send_json({"command": "ALARM_CONFIRMED"})
                    
                elif ev == "ALARM_RECOVERED":
                    state["alarm_status"] = False
                    # 掉线恢复 = 自动把状态改回在线；若你手动设过 忙碌/离线，apply_im_status 会拦下（
                    # 客服铁律：只有人工手动切才能变回在线）
                    apply_im_status(1, manual=False, source="掉线恢复", persist=(ws not in TEST_WS))
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    # 向探针发送确认回执
                    await ws.send_json({"command": "RECOVERY_CONFIRMED"})
                    
                elif ev == "PLAYER_MESSAGE":
                    payload = pkt.get("data", {})
                    # ★ V7.9：记下"这批玩家消息是不是测试脚本发来的"（同时落到会话上，见下），
                    #   后续自动起草/自动回复只打到测试探针 —— 绝不填/发客服的真实工单。
                    _is_test_origin = (ws in TEST_WS)
                    state["automation_origin_test"] = _is_test_origin
                    # ★ V8.6：记住"浏览器到底能不能出声"（探针上报的 sound_ok）——
                    #   出不了声时就由桌面悬浮窗兜底响铃（见下面的 desktop_ding / desktop_alarm）
                    if not _is_test_origin and "sound_ok" in payload:
                        state["probe_sound_ok"] = bool(payload.get("sound_ok"))
                    gid = payload.get("groupID")
                    # ★ V8.3：这次上报**之前**网页打开着哪条会话 —— 用来区分
                    #   "我正在看的会话来了新消息"（该提示）与"我刚切到另一条工单"（不该提示/不该推 Bark）。
                    # ★ V8.5.1：用"上一次 PLAYER_MESSAGE 的工单"判断，而不是 page_gid ——
                    #   page_gid 会被 CONV_LIST 提前改成新会话，把翻旧会话的历史误判成新消息（Bark 根因）。
                    _prev_page_gid = str((_LAST_TEST_MSG_GID if _is_test_origin else _LAST_MSG_GID)["gid"] or "")
                    # ★ V7.9 血泪教训：探针的"发送/填写"永远作用于**页面当前打开的那个工单**。
                    #   这里记下"网页上此刻打开的工单"，发送前必须核对，避免发错人。
                    #   真实探针与测试探针各记一份（测试的假探针不该污染真实页面的绑定）。
                    if gid:
                        if _is_test_origin:
                            _LAST_TEST_PAGE_GID["gid"] = str(gid)
                            _LAST_TEST_MSG_GID["gid"] = str(gid)
                        else:
                            _LAST_PAGE_GID["gid"] = str(gid)
                            _LAST_MSG_GID["gid"] = str(gid)
                        # ★ V8.0.2：按连接记"这个探针此刻打开的是哪条会话" ——
                        #   多开工作台标签页时，只让"真的打开着目标会话"的那个执行发送（防重复发送）
                        try:
                            _PROBE_CONNS.setdefault(ws, {})["page_gid"] = str(gid)
                        except Exception:
                            pass
                    
                    target = state["companies"]["main"]
                    # ★ V8.0.2：这条会话对中继来说是不是"第一次见"（决定要不要开场语/要不要提示手机）
                    _was_known = gid in target["conversations"]
                    _prev_msgs = list((target["conversations"].get(gid) or {}).get("msgs") or [])
                    _prev_last_pl = 0
                    for _pm in reversed(_prev_msgs):
                        if isinstance(_pm, dict) and _pm.get("sender") == "player":
                            _prev_last_pl = int(_pm.get("ts") or 0)
                            break
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
                    c.pop("placeholder", None)              # ★ V8.0：网页上报的真实会话名到手 -> 不再是"占位会话"
                    # ★ V7.9：把"这条会话的数据来自测试客户端"记在会话上（比全局标记可靠：
                    #   全局标记会被随后到来的真实消息顶掉，导致测试的延迟回复打到真实工单）
                    if _is_test_origin:
                        c["_test_origin"] = True
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
                            # ★ V8.2：优先沿用已知时间戳；其次用探针给的时间戳（若有）；
                            #   都没有才当"现在" —— 这样"翻看旧工单"才不会被误判成新消息。
                            try:
                                _given = int(m.get("ts") or 0)
                            except Exception:
                                _given = 0
                            cleaned.append({"sender": sender, "text": m.get("text"),
                                            "ts": prev_ts.get(key) or _given or now_ms})
                        c["msgs"] = cleaned
                    else:
                        c["msgs"] = []
                    c["playerInfo"] = payload.get("playerInfo", "")
                    c["updatedAt"] = now_ms
                    _LAST_ACTIVE["gid"] = gid

                    # ★★ 新消息即时通知（V7.5 / V8.0.2 收紧）★★
                    # 旧行为：只要"末尾有玩家消息且 ts 比上次提示的新"就提示 —— 于是**翻看旧会话**
                    #   或重启后重看老工单也会叮咚（客服反馈："电脑切换到其他旧的会话时，手机不要弹消息提示"）。
                    # 现在：只提示"中继本来就认识这条会话、且玩家消息时间戳真的往后走了"的情况。
                    #   真正的新会话/新消息由"会话列表变化"那条链路负责（CONV_LIST 的 diff）。
                    try:
                        new_ts, new_txt = 0, ""
                        for pm in reversed(c.get("msgs") or []):
                            if isinstance(pm, dict) and pm.get("sender") == "player":
                                new_ts = int(pm.get("ts") or 0)
                                new_txt = str(pm.get("text") or "")
                                break
                        # ★ V8.3：新增"我当时是否正看着这条会话"这个条件 ——
                        #   切到另一条工单时，探针会把那条工单的**整段历史**读一遍，
                        #   若它末尾有系统没见过的玩家消息（时间戳只能补成 now），就会被误判成新消息。
                        _was_watching = bool(_prev_page_gid) and str(_prev_page_gid) == str(gid)
                        if (_was_known and _was_watching and _prev_last_pl
                                and new_ts > _prev_last_pl
                                and (now_ms - new_ts) <= AUTO_ACTIVE_WINDOW_MS
                                and new_ts > int(_LAST_NOTIFIED.get(gid) or 0)):
                            _LAST_NOTIFIED[gid] = new_ts
                            if state.get("afk_mode"):
                                mode_now = "afk"
                            elif state.get("auto_draft", True):
                                mode_now = "semi"
                            else:
                                mode_now = "manual"
                            preview = " ".join(new_txt.split())[:60]
                            for m in _mobile_targets_for(_is_test_origin):
                                await safe_send(m, {"type": "NEW_MESSAGE", "groupID": gid, "name": name,
                                                    "preview": preview, "mode": mode_now, "ts": new_ts})
                            print(f"[新消息] {name}：{preview[:30]}（已即时提示手机端 · 模式 {mode_now}）")
                            # ★ ③ 同时推一条 Bark（手机锁屏/退后台也能收到；可在 config.json 用
                            #   "bark_on_new_message": false 关掉）。push_bark 内部走线程池，不阻塞事件循环。
                            if config.get("bark_on_new_message", True) and not _is_test_origin:
                                push_bark(f"💬 {name} 新消息", preview or "（玩家发来新消息）", gid)
                            # ★ V8.7 兜底：探针报"浏览器出不了声"**或"页面在后台"**（不聚焦）时，
                            #   让**桌面悬浮窗**用系统声（winsound）响一声 —— 后台标签的网页音频不可靠，
                            #   而客服的电脑一定要能听见提醒。前台且能出声时不走这里（不会双响）。
                            _hidden = bool(payload.get("hidden"))
                            _hud_on_hidden = bool(config.get("hud_alert_when_hidden", True))
                            if payload.get("sound_ok") is False or (_hidden and _hud_on_hidden):
                                state["desktop_ding"] = {"ts": int(new_ts or (time.time() * 1000)),
                                                         "name": name, "preview": preview[:40],
                                                         "why": ("页面在后台（不聚焦）" if _hidden
                                                                 else "浏览器未授权出声（需先点/按一下工作台页面）")}
                                print(f"[提醒] {'🖥 页面在后台' if _hidden else '🔊 探针出不了声'}"
                                      f" -> 已请桌面悬浮窗兜底响铃")
                    except Exception as e:
                        print(f"[新消息] 通知失败（不影响主流程）：{e}")

                    # ★★ 开场语（V8.0.2 收紧：只对"真正的新会话"、且半自动只起草不发送）★★
                    # 旧版漏洞（客服反馈"你还是能自动给真实玩家发消息"）：
                    #   ① 只要"会话里有玩家消息且没 greeted 过"就发 —— 换到旧会话、或中继重启后重看老工单
                    #      （greeted 标记丢了）都会**真的给玩家发一条开场语**；
                    #   ② 半自动模式也在 `or state.get("auto_draft", True)` 下直接 SEND_REPLY；
                    #   ③ 没有页面绑定（require_page），页面停在别的工单时也会打到当前工单里。
                    # 现在：只有"这条会话对中继是全新的 + 玩家刚说话（≤3 分钟）+ 还没有客服发过言"才处理；
                    #   AFK 才发送，半自动只**起草**到输入框（并提示手机），手动模式什么都不做。
                    has_player_msg = any(m.get("sender") == "player" for m in (c.get("msgs") or []))
                    _last_pl = last_player_ts(c)
                    _fresh = bool(_last_pl) and (now_ms - _last_pl) <= 180000
                    _no_agent_yet = not any(m.get("sender") == "agent" for m in (c.get("msgs") or []))
                    if (has_player_msg and not c.get("greeted") and not _was_known
                            and _fresh and _no_agent_yet and config.get("auto_send_greeting", True)):
                        c["greeted"] = True
                        greet = safe_outbound(pick_greeting(), "开场语")[0]
                        if greet:
                            if state.get("afk_mode"):
                                _sent = await send_to_player(
                                    {"command": "SEND_REPLY", "content": greet, "groupID": gid}, "开场语",
                                    origin=ws, require_page=True, page_name=name, auto=True)
                                if _sent:
                                    _say_as_agent(gid, greet)
                                    print(f"[开场] AFK：已按表格发送开场语给 {name}：{greet[:40]}")
                            elif state.get("auto_draft", True):
                                # 半自动：只起草（绝不自动发给玩家）
                                await send_to_player(
                                    {"command": "FILL_DRAFT", "content": greet, "groupID": gid,
                                     "noOverwrite": True}, "开场语(草稿)",
                                    origin=ws, require_page=True, page_name=name, auto=True)
                                for m in _mobile_targets_for(_is_test_origin):
                                    await safe_send(m, {"type": "AI_STATUS", "status": "ok",
                                                        "groupID": gid,
                                                        "message": f"已为「{name}」起草开场语（半自动：等你确认发送）"})
                                print(f"[开场] 半自动：已起草开场语（未发送）给 {name}")
                    schedule_auto_reply(gid)

                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
    finally:
        active_clients["extension"].discard(ws)
        TEST_WS.discard(ws)                  # ★ V7.9：断开就摘掉，别让计数/隔离判断留在脏数据上
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
    """连接 8 秒仍未上报版本 => 说明 Tampermonkey 里是旧脚本（低于期望版本）。

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
    print(f"[探针] ⚠️ 已连接但未上报版本 => Tampermonkey 里仍是旧脚本（< v{SERVER_VER}）")
    print(f"[探针] ⚠️ 请打开 http://127.0.0.1:{PORT}/probe.js 取最新脚本，整段覆盖粘贴后 Ctrl+S 保存")
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})

# ==================== AI 关单确认（防止"页面没点成功，卡片却消失了"） ====================
# 旧实现：AI 生成结束语后**立刻**把会话从列表移除 —— 但探针点「回复并关单」可能失败，
# 结果手机端卡片没了、工单其实还挂着（客服以为已关单）。
# 现在：先标记 pending -> 等探针 ACTION_RESULT 回执 -> 成功才移除；失败/超时一律保留会话。
CLOSE_CONFIRM_TIMEOUT = 20.0          # 秒：等探针回执的最长时间
_PENDING_CLOSE = {}                   # gid -> {"category":..., "name":..., "ts":...}


def mark_close_pending(gid: str, category: str, is_test: bool = False) -> bool:
    """把会话标记为"关单中"（不删除），并记住待确认信息。

    ★ V8.5 铁律⑨：is_test=True 表示"这次关单来自测试来源"（测试客户端 / 测试造的会话），
      关单成功也**不许**推客服手机的 Bark —— 否则跑一次回归测试就给客服推一条假关单通知。
    """
    conv = state["companies"]["main"]["conversations"].get(gid)
    if not isinstance(conv, dict):
        return False
    conv["closing"] = True
    _PENDING_CLOSE[gid] = {"category": category, "name": conv.get("name") or gid,
                           "ts": time.time(), "test": bool(is_test or conv.get("_test_origin"))}
    return True


def resolve_close(gid: str, ok: bool, detail: str = ""):
    """探针回执处理：成功才真正把会话从列表移除；失败则保留（返回 (是否移除, 说明)）。"""
    convs = state["companies"]["main"]["conversations"]
    conv = convs.get(gid)
    if isinstance(conv, dict):
        conv.pop("closing", None)
    info = _PENDING_CLOSE.pop(gid, None)
    if info is None:
        return False, "没有待确认的关单（可能已处理或已超时）"
    name = info.get("name") or gid
    if ok:
        convs.pop(gid, None)
        if _LAST_ACTIVE.get("gid") == gid:
            _LAST_ACTIVE["gid"] = None
        # ★ V8.5 铁律⑨：测试来源的关单**绝不推 Bark**（实测过：跑一次回归测试就会给客服手机推一条假关单）
        if info.get("test"):
            print("[关单] （测试来源：只记站内，不推 Bark）")
        else:
            push_bark("已回复并关单", f"{name}　分类：{info.get('category')}　{(detail or '')[:40]}", gid)
        print(f"[关单] ✅ {name} -> 分类「{info.get('category')}」（页面确认：{detail}）")
        return True, "已关单"
    print(f"[关单] ❌ {name} 未关单，已保留会话：{detail}")
    return False, (detail or "页面未确认关单")


async def _close_confirm_watchdog(gid: str):
    """超时保护：页面迟迟不回报就保留会话并明确告诉手机端（绝不静默消失）。"""
    await asyncio.sleep(CLOSE_CONFIRM_TIMEOUT)
    if gid not in _PENDING_CLOSE:
        return
    info = _PENDING_CLOSE.pop(gid, None)
    conv = state["companies"]["main"]["conversations"].get(gid)
    if isinstance(conv, dict):
        conv.pop("closing", None)
    msg = ("页面未回报「回复并关单」结果，已保留该会话（未确认成功不会移除）；"
           "请在电脑上确认工单状态")
    print(f"[关单] ⚠️ {info.get('name') if info else gid}：{msg}")
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "AI_STATUS", "groupID": gid, "status": "error", "message": msg})
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})


async def handle_ai_close(group_id: str, origin=None):
    """手机端「AI 回复并关单」。

    流程：AI 选问题分类 + 生成结束语 -> 通知探针执行「回复并关单」-> 会话从列表移除。
    任一步失败都会明确返回错误，绝不误删会话。
    ★ V7.9：origin 为"测试客户端"时只发给测试探针（测试绝不点客服的真实工单）。
    """
    conv = state["companies"]["main"]["conversations"].get(group_id)
    if not conv:
        return False, "会话不存在（可能已关单）"

    history_str = build_chat_history_str(group_id) or str(conv.get("playerInfo") or "") or "（无聊天记录）"
    options = await ensure_category_options(origin) or config.get("close_category_options") or []

    try:
        category, content = await asyncio.get_event_loop().run_in_executor(
            None, core.generate_closing, history_str, options)
    except Exception as e:
        return False, f"AI 生成失败: {e}"

    if not content:
        return False, "AI 未生成结束语，已取消关单"
    # ★ 安全闸：结束语会直接进玩家对话框
    content, left = safe_outbound(content, "关单结束语")
    if not content.strip():
        return False, "结束语清洗后为空（只含内部提示），已取消关单"
    if not ext_targets(origin):
        return False, "电脑端探针未连接（或测试客户端没有测试探针），无法关单"

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
    await send_to_player(payload, "关单结束语", origin=origin, require_page=True)

    # ★ 关键改动：不再立刻移除会话 —— 先标记"关单中"，等探针回执确认成功后才移除。
    #   （旧实现删早了：页面点失败会导致"卡片消失但工单还挂着"）
    name = conv.get("name") or group_id
    # ★ V8.5 铁律⑨：关单来源是测试客户端 / 测试会话 -> 回执成功也不推客服手机 Bark
    mark_close_pending(group_id, category,
                       is_test=(_origin_is_test(origin) or bool(conv.get("_test_origin"))))
    asyncio.create_task(_close_confirm_watchdog(group_id))
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "FULL_SYNC", "data": state})

    print(f"[关单] ⏳ {name} 已下发结束语（分类「{category}」），等待页面确认…")
    return True, category


async def _ai_close_and_notify(gid: str, origin=None):
    try:
        ok, info = await handle_ai_close(gid, origin=origin)
    except Exception as e:
        ok, info = False, f"关单异常: {e}"
    msg = (f"结束语已下发，等待页面执行「回复并关单」（分类：{info}）" if ok else str(info))
    for m in list(active_clients["mobile"]):
        await safe_send(m, {"type": "AI_STATUS", "groupID": gid,
                            "status": "closing" if ok else "error", "message": msg})


async def ws_mobile_handler(request):
    # heartbeat=30：手机退后台/锁屏时能尽快探活，配合前端重连即补拉快照
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    # ★ V7.9：测试脚本用 ?test=1 连上；这类连接发的指令只会发给"测试探针"，绝不碰真实工作台页面
    if request.query.get("test") == "1":
        TEST_WS.add(ws)
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
                    for ext in ext_targets(ws):                     # ★ V7.9：测试客户端不会打扰真实探针
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
                    # ★ 手动切到 离线/忙碌 -> 记 manual（网页若自己跳回在线，探针会按守护改回来；
                    #   中继侧 apply_im_status 也会拦住"自动上线"）
                    # ★ V8.5.1：测试来源只改内存、不落盘（绝不覆盖客服真实的 im_state.json）
                    apply_im_status(st, manual=(st in (2, 3)), source="手机/小窗手动",
                                    persist=(ws not in TEST_WS))
                    state["im_last_request"] = {"status": st, "status_text": _im_txt(st),
                                                "source": "手机/小窗手动", "ts": int(time.time())}
                    _conns = ext_targets(ws)                        # ★ V7.9：测试客户端只打到测试探针
                    _sent = 0
                    state["im_intent_test"] = (ws in TEST_WS)       # 给"自动重试"用的隔离标记
                    for ext in _conns:
                        if await safe_send(ext, {"command": "CHANGE_STATUS", "status": st}):
                            _sent += 1
                        if st == 1:
                            await safe_send(ext, {"command": "SILENCE_ALARM"})
                    # ★ V7.7：把"下发给了几个探针连接、成功几个"记下来（/diag 可查）
                    state["ext_cmd_debug"] = {"cmd": f"CHANGE_STATUS({st}/{_im_txt(st)})",
                                              "conns": len(_conns), "sent": _sent,
                                              "ts": int(time.time())}
                    if _sent == 0:
                        await safe_send(ws, {"type": "AI_STATUS", "status": "error",
                                             "message": "切换指令没能发到电脑网页（探针连接数 0）——"
                                                        "请确认工作台页面开着、油猴脚本在跑"})
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    continue

                # ♻️ 以网页真实现状为准重置 IM 状态（清掉卡住的手动锁）—— V7.7 逃生舱
                if act == "RESET_IM_STATE":
                    st = reset_im_state(source="手机端", persist=(ws not in TEST_WS))
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    await safe_send(ws, {"type": "AI_STATUS", "status": "ok",
                                         "message": f"已按电脑网页真实现状重置为「{_im_txt(st)}」"})
                    continue

                # 🧭 请探针把"状态下拉的可见选项"回报过来（实机校准用，只读诊断）
                if act == "DUMP_STATUS":
                    _dconns = ext_targets(ws)                       # ★ V7.9：测试客户端只打到测试探针
                    _dsent = 0
                    for ext in _dconns:
                        if await safe_send(ext, {"command": "DUMP_STATUS_MENU"}):
                            _dsent += 1
                    state["ext_cmd_debug"] = {"cmd": "DUMP_STATUS_MENU", "conns": len(_dconns),
                                              "sent": _dsent, "ts": int(time.time())}
                    if not _dconns:
                        await safe_send(ws, {"type": "AI_STATUS", "status": "error",
                                             "message": "电脑端探针未连接，读不到状态下拉选项"})
                    else:
                        await safe_send(ws, {"type": "AI_STATUS", "status": "ok",
                                             "message": f"已请电脑网页回报状态下拉选项（探针连接 {_dsent}/{len(_dconns)}，马上返回）"})
                    continue

                # ★ V8.0：手机端点某个会话 -> 让电脑网页切过去（手机主页"全部会话"可点）
                if act == "OPEN_CONV":
                    _gid = str(pkt.get("groupID") or "")
                    _nm = str(pkt.get("name") or "")
                    _targets = ext_targets(ws)
                    if not _targets:
                        await safe_send(ws, {"type": "AI_STATUS", "status": "error",
                                             "message": "电脑端探针未连接，无法切换会话"})
                        continue
                    for ext in _targets:
                        await safe_send(ext, {"command": "OPEN_CONV", "name": _nm,
                                              "lastText": str(pkt.get("lastText") or ""),
                                              "groupID": _gid})
                    state["ext_cmd_debug"] = {"cmd": "OPEN_CONV(" + (_nm or _gid) + ")",
                                              "conns": len(_targets), "sent": len(_targets),
                                              "ts": int(time.time())}
                    await safe_send(ws, {"type": "AI_STATUS", "status": "ok",
                                         "message": "已请电脑网页切到「" + (_nm or _gid) + "」"})
                    continue

                # AI 一键回复并关单（AI 选问题分类 + 生成结束语，关单后会话从列表消失）
                if act == "AI_CLOSE":
                    if not feature_flags()["ai_close"]:
                        await safe_send(ws, {"type": "AI_STATUS", "status": "error",
                                             "message": "「AI 回复并关单」已在设置里关闭"
                                                        "（config.json → enable_ai_close=false）"})
                        continue
                    gid = pkt.get("groupID")
                    if gid:
                        # ★ V7.9：测试客户端不许真的关单（AI_CLOSE 是后台任务，得单独拦）
                        if ws in TEST_WS and not ext_targets(ws):
                            await safe_send(ws, {"type": "AI_STATUS", "status": "ok",
                                                 "message": "（测试客户端：已跳过真实 AI 关单，未触碰工作台）"})
                            continue
                        asyncio.create_task(_ai_close_and_notify(gid, origin=ws))
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
                    ok, note = apply_reply_mode("afk" if pkt.get("status", False) else "semi", source="mobile",
                                                persist=(ws not in TEST_WS))
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                elif act == "SET_MODE":
                    # ★ 三档回复模式：manual=只提醒（不自动起草）/ semi=AI 起草到输入框（不发送）/ afk=AI 直接发送
                    mode = str(pkt.get("mode") or "semi")
                    if mode not in ("manual", "semi", "afk"):
                        mode = "semi"
                    ok, note = apply_reply_mode(mode, source="mobile", persist=(ws not in TEST_WS))
                    label = {"manual": "手动（只提醒，AI 不起草）", "semi": "半自动（AI 起草到输入框，不发送）",
                             "afk": "AFK 全自动（AI 直接回复）"}[mode]
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
                        await safe_send(m, {"type": "AI_STATUS", "status": "ok" if ok else "error",
                                            "message": ("回复模式：" + label) if ok else note})
                elif act == "SILENCE_ALARM":
                    state["alarm_status"] = False
                    for ext in ext_targets(ws): await safe_send(ext, {"command": "SILENCE_ALARM"})
                    for m in list(active_clients["mobile"]): await safe_send(m, {"type": "FULL_SYNC", "data": state})
                elif act == "TRIGGER_F9":
                    # ★ V7.9：测试客户端不许真的去草稿/回复（会打到客服的真实工单）
                    if ws in TEST_WS and not ext_targets(ws):
                        await safe_send(ws, {"type": "AI_STATUS", "status": "ok",
                                             "message": "（测试客户端：已跳过真实 AI 起草，未触碰工作台）"})
                    else:
                        asyncio.create_task(handle_ai_automation(pkt.get("groupID"), source="phone", force=True))
                elif act == "EXT_COMMAND":
                    # ★ 安全闸：挂起/恢复等动作可能带 content（如手机端"关单"会把输入框内容一起发），
                    #   凡是"要进玩家对话框"的文本都必须清洗
                    fwd = dict(pkt)
                    if fwd.get("content"):
                        clean, left = safe_outbound(fwd["content"], "手机端指令")
                        if clean.strip():
                            fwd["content"] = clean
                        else:
                            fwd.pop("content", None)
                            await safe_send(ws, {"type": "AI_STATUS", "status": "error",
                                                 "message": "内容清洗后为空（只含内部提示），已拦截"})
                            continue
                    # ★ 手机端「✅ 关单」= 同一条 ACTION_REPLY_CLOSE 链路：先标记待确认，
                    #   等探针回执成功才移除会话（避免"卡片没了、工单还挂着"）
                    if str(fwd.get("command") or "") == "ACTION_REPLY_CLOSE":
                        _cgid = str(fwd.get("groupID") or "")
                        if _cgid and state["companies"]["main"]["conversations"].get(_cgid):
                            # ★ V8.5 铁律⑨：测试客户端 / 测试会话的关单回执成功也不推客服手机 Bark
                            _c_test = (ws in TEST_WS) or bool((state["companies"]["main"]["conversations"].get(_cgid) or {}).get("_test_origin"))
                            mark_close_pending(_cgid, str(fwd.get("category") or "其他"), is_test=_c_test)
                            asyncio.create_task(_close_confirm_watchdog(_cgid))
                            for m in list(active_clients["mobile"]):
                                await safe_send(m, {"type": "FULL_SYNC", "data": state})
                    for ext in ext_targets(ws):                 # ★ V7.9：测试客户端只打到测试探针
                        await safe_send(ext, fwd)
                elif act == "SEND_REPLY":
                    gid = pkt.get("groupID")
                    text = pkt.get("content")
                    if not gid or not text:
                        continue
                    gid = str(gid)
                    # ★ V8.0.1：不再给"中继不认识的会话"造占位卡片（名字=工单号）。
                    #   那正是手机上出现 NOTOPEN-… / 长得像工单号的假会话的来源（客服看到就是垃圾）。
                    #   不认识的会话直接拒绝，并告诉客服正确动作：先在手机主页点它一下（电脑会自动打开）。
                    if not (state["companies"]["main"]["conversations"].get(gid) or {}):
                        await safe_send(ws, {"type": "AI_STATUS", "status": "error",
                                             "message": "这条会话还没在电脑网页上打开过 —— "
                                                        "先在手机主页点它一下（电脑会自动切过去），再发消息"})
                        continue
                    # ★ 安全闸：手机端代发的内容也会进玩家对话框（可能是从草稿复制来的）
                    text, left = safe_outbound(text, "手机端代发")
                    if not text.strip():
                        await safe_send(ws, {"type": "AI_STATUS", "status": "error",
                                             "message": "内容清洗后为空（只含内部提示），未发送"})
                        continue
                    conv = state["companies"]["main"]["conversations"][gid]
                    if not isinstance(conv.get("msgs"), list):
                        conv["msgs"] = []
                    # 修复 BUG-011：过滤 action 字段，仅转发必要字段
                    # ★ V8.0.1：先发（必要时中继会先自动切会话），**发送成功才把这条消息记进会话**，
                    #   否则手机上会显示一条"其实没发出去"的假消息（诚实优先）。
                    ok_sent = await send_to_player({"command": "SEND_REPLY", "groupID": gid,
                                                    "content": text}, "手机端代发", origin=ws,
                                                   require_page=True,
                                                   page_name=str(pkt.get("name") or ""))
                    if not ok_sent:
                        continue                      # 拒发原因已由 send_to_player 推给手机端
                    conv["msgs"].append({"sender": "agent", "text": text,
                                         "time": datetime.now().strftime("%H:%M:%S"),
                                         "ts": int(time.time() * 1000)})
                    conv["updatedAt"] = int(time.time() * 1000)
                    _LAST_ACTIVE["gid"] = gid
                    for m in list(active_clients["mobile"]):
                        await safe_send(m, {"type": "FULL_SYNC", "data": state})
    finally:
        active_clients["mobile"].discard(ws)
        TEST_WS.discard(ws)                  # ★ V7.9：断开就摘掉，别让计数/隔离判断留在脏数据上
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
app.router.add_get("/api/im_reset", api_im_reset)
app.router.add_get("/api/probe_ping", api_probe_ping)
app.router.add_get("/api/outbound", api_outbound)
app.router.add_get("/api/purge_test_data", api_purge_test_data)
app.router.add_get("/ws/extension", ws_ext_handler)
app.router.add_get("/ws/mobile", ws_mobile_handler)
# ★ QC：质检页历史会话采集（独立通道 + 独立脚本，与工作台探针互不影响）
app.router.add_get("/qc_probe.js", qc_probe_js_handler)
app.router.add_get("/api/qc_status", api_qc_status)
app.router.add_get("/api/qc_sweep", api_qc_sweep)
app.router.add_get("/ws/qc", ws_qc_handler)

if __name__ == "__main__":
    local_ip = get_local_ip()
    print("=" * 60)
    print("[启动] IM 移动端中继与 AI 自动托管服务已启动！")
    print(f"手机访问: http://{local_ip}:{PORT}")
    print(f"系统自检: http://127.0.0.1:{PORT}/diag   （探针/手机端是否在线一屏看懂）")
    print(f"探针脚本: http://127.0.0.1:{PORT}/probe.js （核对油猴里那版是否最新 v{SERVER_VER}）")
    print("=" * 60)
    web.run_app(app, host="0.0.0.0", port=PORT)