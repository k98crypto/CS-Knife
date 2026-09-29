import os
import sys
import json
import time
import socket
import queue
import threading
import tkinter as tk
import winsound
import keyboard
import pyperclip
import requests
import urllib.request

# ==================== 控制台输出健壮性（防止编码问题导致进程崩溃） ====================
# pythonw 运行时 sys.stdout 为 None；被重定向时若遇到非 BMP 字符会抛 UnicodeEncodeError。
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

CURRENT_DIR = get_real_base_dir()
os.chdir(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

POS_FILE_PATH = os.path.join(CURRENT_DIR, "hud_pos.json")

try:
    from agent_core import CustomerServiceCore
    core = CustomerServiceCore()
except Exception as e:
    import ctypes
    import traceback
    ctypes.windll.user32.MessageBoxW(0, f"agent_core 加载失败：\n\n{traceback.format_exc()}", "错误", 0x10)
    sys.exit(1)

current_ticket_context = ""
is_summarizing = False

# ==================== 中继服务地址 ====================
def load_bridge_port(default=8765):
    """读 config.json 里的 port，保证与 bridge_server.py 一致。"""
    try:
        with open(os.path.join(CURRENT_DIR, "config.json"), "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return int(cfg.get("port", default) or default)
    except Exception:
        return default

BRIDGE_PORT = load_bridge_port()
BRIDGE_BASE = f"http://127.0.0.1:{BRIDGE_PORT}"


# ==================== 高 DPI 感知（防止缩放导致窗口被摆到屏幕外） ====================
def enable_dpi_awareness():
    """让 Tk 使用物理像素坐标：本机 125%~150% 缩放时坐标不再错乱。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE_V2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ==================== 单实例互斥（旧版能开出两个悬浮窗互相抢热键） ====================
_INSTANCE_LOCK = {"sock": None}

def acquire_single_instance(port=8766):
    """用本地端口占用做互斥：成功 True，已有实例在跑 False。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _INSTANCE_LOCK["sock"] = s
        return True
    except OSError:
        return False


def fetch_diag_info(timeout=2.0):
    """读取中继的 /api/diag。返回 dict（含 legacy=True 表示中继是旧版代码）或 None（没连上）。"""
    try:
        r = requests.get(f"{BRIDGE_BASE}/api/diag", timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else None
        if r.status_code == 404:
            return {"legacy": True}
        return None
    except Exception:
        return None


# ==================== 悬浮窗几何（纯粹函数，便于单测） ====================
HUD_W, HUD_H = 470, 300          # 展开尺寸
HUD_H_FOLDED = 58                # 折叠后只留标题栏 + 底栏
SCREEN_MARGIN = 8
DEFAULT_MARGIN = 24
TASKBAR_RESERVE = 48

BG_ROOT = "#12141A"
BG_BAR = "#1C2029"
BORDER = "#2B3140"
FG_MAIN = "#E6E6E6"
FG_DIM = "#8A93A0"
FG_HINT = "#6B7280"
ACCENT = "#4EC9B0"
WARN = "#E5C07B"
DANGER = "#F44747"


def clamp_position(x, y, w, h, sw, sh, margin=SCREEN_MARGIN):
    """把窗口坐标夹回屏幕可见区域。

    不夹取的话，一旦换显示器 / 改分辨率（例如 2560 宽换回 1440x900），
    从 hud_pos.json 读回的旧坐标就会把窗口放到屏幕外 —— 表现就是"悬浮窗不见了"。
    """
    max_x = max(margin, sw - w - margin)
    max_y = max(margin, sh - h - margin)
    x = min(max(int(x), margin), max_x)
    y = min(max(int(y), margin), max_y)
    return x, y


def default_position(w, h, sw, sh, margin=DEFAULT_MARGIN, taskbar=TASKBAR_RESERVE):
    """默认停靠右下角（旧版固定左上角，容易被浏览器工具栏/地址栏压住）。"""
    return clamp_position(sw - w - margin, sh - h - margin - taskbar, w, h, sw, sh, margin)


def beep_start(): winsound.Beep(1000, 100)
def beep_success(): winsound.Beep(1600, 150)
def beep_item(): winsound.Beep(1300, 80)
def beep_error():
    winsound.Beep(400, 150)
    time.sleep(0.05)
    winsound.Beep(400, 150)

# ★ 人工介入专属长报警：三短（700Hz）一长（500Hz），
#   与"新消息/成功/失败"提示音、与"掉线警报"（探针的连续警笛）都明显不同。
ALERT_SEEN = set()
def long_human_alarm():
    try:
        for _ in range(3):
            winsound.Beep(700, 320)
            time.sleep(0.10)
        time.sleep(0.18)
        winsound.Beep(500, 700)
    except Exception:
        pass

# ==================== 功能开关（三个 AI 动作可独立关闭） ====================
# 值来自中继 /api/diag.features（config.json → enable_ai_close / enable_auto_draft / enable_f10_polish）
FEATURES = {}

# ==================== ESC：清空小窗"暂存内容" ====================
# 「暂存内容」= 小窗中"最近提炼 / 框选预览 / F9·F10 结果"那一块（双击可复制）。
# ESC 一键清空它，并**无条件**把剪贴板也清掉（客服要求：防止玩家信息/草稿留在剪贴板里）。
# 暂存与剪贴板都空时才什么都不做（避免平时按 ESC 刷出无意义的提示）。
STAGED_PLACEHOLDER = "（尚无，按 F7/F8 提炼后在此核验）"


def staged_clear_plan(staged_text: str, clip_text: str):
    """纯逻辑（不依赖 Tk，便于单测）：返回 (新文案, 是否清剪贴板, 是否有暂存内容)。

    - 剪贴板：**无条件清**（只要里面有东西）—— 这是客服明确要求的"ESC 连剪贴板一起清"；
    - 暂存内容：有就清回占位，没有就只清剪贴板。
    """
    txt = (staged_text or "").strip()
    had = bool(txt) and not txt.startswith("（尚无")
    clear_clip = bool((clip_text or "").strip())
    return (STAGED_PLACEHOLDER if had else ""), clear_clip, had

class HUDOverlay:
    """桌面悬浮窗（始终置顶 + 可折叠 + 可召回 + 连接状态灯）。

    第七轮重构要点：
      1. 始终保持置顶：📌 默认开启（可手动取消，召回时自动重新置顶）
      2. 标题栏三盏灯一眼看出「中继 / 探针 / 手机端」是否在线
      3. 位置绝不会再跑到屏幕外：启动时坐标夹取 + Ctrl+Alt+H 一键召回
      4. 所有 UI 更新走线程安全队列（旧版从工作线程直接操作 Tk，偶发闪退）
    """

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("工单助手 HUD")
        self.pinned = True
        self.folded = False
        self.mode = "semi"            # 当前回复模式（由中继同步，本窗口不维护独立副本）
        self.mode_owner = "default"   # 谁设的：mobile=手机端优先
        self._q = queue.Queue()
        self._pos_fixed = False
        self._staged_text = ""        # 小窗暂存内容（最近提炼/框选预览/F9·F10 结果）——供 ESC 清空
        # 记录"我们想要的坐标"。不要依赖 winfo_x()：窗口还没映射时它返回 0，
        # 会把 (962,592) 这种正确位置写成 (0,0)（第七轮踩过的坑）。
        self._target = (DEFAULT_MARGIN, DEFAULT_MARGIN)
        # 高 DPI 缩放：本机 2880x1800 / 200% 时，若只按物理像素写 470x300，
        # 字号会被 tk scaling 放大 2 倍 -> 文字溢出、界面挤成一团。
        # 这里按 DPI 比例放大窗口与内边距，视觉尺寸与 100% 缩放时保持一致。
        self.scale = self._detect_scale()
        self.w = int(HUD_W * self.scale)
        self.h = int(HUD_H * self.scale)
        self.h_folded = int(HUD_H_FOLDED * self.scale)
        self._build_ui()
        self._apply_geometry(initial=True)
        self.root.attributes("-topmost", bool(self.pinned))
        self.root.attributes("-alpha", 0.95)
        self.root.overrideredirect(True)
        self.root.after(80, self._drain_queue)
        self.root.after(400, self._poll_links)

    def _detect_scale(self):
        """DPI 缩放比（100% -> 1.0，200% -> 2.0）。"""
        try:
            scaling = float(self.root.tk.call("tk", "scaling"))
            return max(1.0, min(3.0, round(scaling / (96.0 / 72.0), 2)))
        except Exception:
            return 1.0

    def _px(self, n):
        """把设计稿上的 100% 尺寸换算成本机物理像素。"""
        return int(round(n * self.scale))

    # ---------------- 界面构建 ----------------
    def _mk_btn(self, parent, text, cmd, fg=FG_DIM, hover="#39414F", width=3):
        return tk.Button(parent, text=text, command=cmd, font=("Microsoft YaHei UI", 9),
                         fg=fg, bg=BG_BAR, activeforeground="#FFFFFF", activebackground=hover,
                         bd=0, relief="flat", cursor="hand2", padx=self._px(4), pady=0, width=width,
                         highlightthickness=0, takefocus=0)

    def _build_ui(self):
        pad = self._px(12)
        wrap = self.w - self._px(44)

        self.outer = tk.Frame(self.root, bg=BORDER)      # 1px 描边，替代旧版纯色方块
        self.outer.pack(fill="both", expand=True)

        self.bar = tk.Frame(self.outer, bg=BG_BAR)
        self.bar.pack(fill="x")

        tk.Label(self.bar, text="⚡ 工单助手", font=("Microsoft YaHei UI", 9, "bold"),
                 fg=ACCENT, bg=BG_BAR).pack(side="left", padx=(self._px(8), self._px(6)), pady=self._px(5))
        self.lbl_relay = tk.Label(self.bar, text="⚪中继", font=("Microsoft YaHei UI", 8), fg=FG_DIM, bg=BG_BAR)
        self.lbl_relay.pack(side="left", padx=(0, self._px(7)))
        self.lbl_probe = tk.Label(self.bar, text="⚪探针", font=("Microsoft YaHei UI", 8), fg=FG_DIM, bg=BG_BAR)
        self.lbl_probe.pack(side="left", padx=(0, self._px(7)))
        self.lbl_phone = tk.Label(self.bar, text="⚪手机", font=("Microsoft YaHei UI", 8), fg=FG_DIM, bg=BG_BAR)
        self.lbl_phone.pack(side="left")

        self.btn_close = self._mk_btn(self.bar, "✕", self.close_app, hover="#E81123")
        self.btn_close.pack(side="right", padx=(self._px(2), self._px(6)), pady=self._px(3))
        self.btn_fold = self._mk_btn(self.bar, "➖", self.toggle_fold)
        self.btn_fold.pack(side="right", padx=self._px(2), pady=self._px(3))
        self.btn_pin = self._mk_btn(self.bar, "📌", self.toggle_pin, fg=ACCENT)
        self.btn_pin.pack(side="right", padx=self._px(2), pady=self._px(3))
        self.btn_home = self._mk_btn(self.bar, "🧭", self.recall)
        self.btn_home.pack(side="right", padx=self._px(2), pady=self._px(3))
        # ★ 自动化开关（默认半自动=只把草稿填进输入框，不发送）
        #   点一下循环：🤖半自动 → 🚀AFK → ✋手动 → 🤖半自动
        #   显示内容始终来自中继（手机端改了这里 5 秒内跟着变；冲突时以手机端为准）
        self.btn_mode = self._mk_btn(self.bar, "🤖半自动", self.cycle_mode, fg=ACCENT, width=9)
        self.btn_mode.pack(side="right", padx=self._px(2), pady=self._px(3))

        self.body = tk.Frame(self.outer, bg=BG_ROOT)
        self.body.pack(fill="both", expand=True)

        self.lbl_status = tk.Label(self.body, text="⚡ 状态: 监听就绪",
                                   font=("Microsoft YaHei UI", 10, "bold"), fg=ACCENT, bg=BG_ROOT,
                                   anchor="w", justify="left", wraplength=wrap)
        self.lbl_status.pack(fill="x", padx=pad, pady=(self._px(9), self._px(2)))

        self.lbl_profile = tk.Label(self.body, text="最近提炼 / 框选预览（双击复制）：",
                                    font=("Microsoft YaHei UI", 8), fg=FG_DIM, bg=BG_ROOT, anchor="w")
        self.lbl_profile.pack(fill="x", padx=pad, pady=(self._px(5), 0))

        self.lbl_last = tk.Label(self.body, text="（尚无，按 F7/F8 提炼后在此核验）",
                                 font=("Microsoft YaHei UI", 9), fg="#DCDCAA", bg=BG_ROOT,
                                 anchor="nw", justify="left", wraplength=wrap, height=3)
        self.lbl_last.pack(fill="x", padx=pad, pady=(0, self._px(8)))
        self.lbl_last.bind("<Double-Button-1>", self._copy_last)
        # ESC 一键清空小窗暂存内容（窗口聚焦时；浏览器聚焦时由全局热键兜底）
        try:
            self.root.bind("<Escape>", self.clear_staged)
        except Exception:
            pass

        self.footer = tk.Frame(self.outer, bg=BG_BAR)
        self.footer.pack(fill="x")
        self.lbl_links = tk.Label(self.footer, text="正在检测中继服务…",
                                  font=("Microsoft YaHei UI", 8), fg=FG_DIM, bg=BG_BAR, anchor="w")
        self.lbl_links.pack(fill="x", padx=self._px(10), pady=(self._px(4), self._px(1)))
        self.lbl_hint = tk.Label(self.footer, text="拖动移动 · Ctrl+Alt+H 召回 · Ctrl+Alt+M 折叠 · ESC 清除暂存",
                                 font=("Microsoft YaHei UI", 7), fg=FG_HINT, bg=BG_BAR, anchor="w")
        self.lbl_hint.pack(fill="x", padx=self._px(10), pady=(0, self._px(4)))

        for w in (self.outer, self.bar, self.body, self.footer, self.lbl_status,
                  self.lbl_profile, self.lbl_last, self.lbl_links, self.lbl_hint,
                  self.lbl_relay, self.lbl_probe, self.lbl_phone):
            self._bind_drag(w)

    def _bind_drag(self, widget):
        widget.bind("<Button-1>", self._start_move)
        widget.bind("<B1-Motion>", self._do_move)
        widget.bind("<ButtonRelease-1>", lambda e: self.save_position())

    # ---------------- 位置与几何 ----------------
    def _screen(self):
        return self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _hud_size(self):
        return self.w, (self.h_folded if self.folded else self.h)

    def _default_pos(self, sw, sh):
        """默认右下角（内边距与任务栏预留也按 DPI 缩放）。"""
        w, h = self._hud_size()
        return default_position(w, h, sw, sh, self._px(DEFAULT_MARGIN), self._px(TASKBAR_RESERVE))

    def _load_pos(self):
        """读回上次位置；没有/损坏/越界都会自动回到右下角。"""
        sw, sh = self._screen()
        w, h = self._hud_size()
        if os.path.exists(POS_FILE_PATH):
            try:
                # utf-8-sig：hud_pos.json 可能被记事本/PowerShell 写成带 BOM
                with open(POS_FILE_PATH, "r", encoding="utf-8-sig") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "x" in d and "y" in d:
                    self.pinned = bool(d.get("pinned", True))
                    self.folded = bool(d.get("folded", False))
                    w, h = self._hud_size()
                    return int(d["x"]), int(d["y"])
            except Exception:
                pass
        return self._default_pos(sw, sh)

    def _apply_geometry(self, initial=False):
        """设置窗口尺寸与坐标（坐标一律夹取，永不出屏）。"""
        sw, sh = self._screen()
        w, h = self._hud_size()
        if initial:
            x, y = self._load_pos()
            w, h = self._hud_size()
        else:
            x, y = self._target
        nx, ny = clamp_position(x, y, w, h, sw, sh)
        if initial and (nx, ny) != (int(x), int(y)):
            self._pos_fixed = True
            print(f"[HUD] 原坐标 ({x},{y}) 超出屏幕 {sw}x{sh}，已自动移回 ({nx},{ny})")
        self._target = (nx, ny)
        self.root.geometry(f"{w}x{h}+{nx}+{ny}")
        if initial:
            self.save_position()

    def _start_move(self, event):
        self.x = event.x
        self.y = event.y

    def _do_move(self, event):
        x = self._target[0] + (event.x - self.x)
        y = self._target[1] + (event.y - self.y)
        sw, sh = self._screen()
        w, h = self._hud_size()
        nx, ny = clamp_position(x, y, w, h, sw, sh)
        self._target = (nx, ny)
        self.root.geometry(f"{w}x{h}+{nx}+{ny}")

    def save_position(self):
        try:
            with open(POS_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump({"x": self._target[0], "y": self._target[1],
                           "folded": self.folded, "pinned": self.pinned}, f)
        except Exception:
            pass

    def close_app(self):
        """✕：保存位置后退出（只关悬浮窗，不影响中继服务）。"""
        self.save_position()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    # ---------------- 折叠 / 置顶 / 召回 ----------------
    def toggle_fold(self):
        """折叠/展开（按钮与 Ctrl+Alt+M 都走这里，线程安全）。"""
        self._q.put(self._do_toggle_fold)

    def _do_toggle_fold(self):
        self.folded = not self.folded
        if self.folded:
            self.body.pack_forget()
            self.footer.pack_forget()
            self.btn_fold.config(text="➕")
        else:
            self.body.pack(fill="both", expand=True)
            self.footer.pack(fill="x")
            self.btn_fold.config(text="➖")
        self._apply_geometry(initial=False)
        self.save_position()

    def toggle_pin(self):
        self._q.put(self._do_toggle_pin)

    def _do_toggle_pin(self):
        self.pinned = not self.pinned
        self.root.attributes("-topmost", bool(self.pinned))
        self.btn_pin.config(fg=ACCENT if self.pinned else FG_HINT)
        if self.pinned:
            self.root.lift()
        self.save_position()

    def recall(self):
        """召回：拉回屏幕内 + 重新置顶（按钮 🧭 与 Ctrl+Alt+H 都走这里）。"""
        self._q.put(self._do_recall)

    def _do_recall(self):
        sw, sh = self._screen()
        w, h = self._hud_size()
        x, y = self._default_pos(sw, sh)
        self._target = (x, y)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.pinned = True
        self.root.attributes("-topmost", True)
        self.btn_pin.config(fg=ACCENT)
        try:
            self.root.lift()
        except Exception:
            pass
        self.save_position()
        self._apply_ui("🧭 悬浮窗已召回并置顶", ACCENT, None, None)
        beep_start()

    # ---------------- 线程安全 UI 队列 ----------------
    def _drain_queue(self):
        """把工作线程塞进来的界面更新在主线程执行（Tk 不是线程安全的）。"""
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            self.root.after(80, self._drain_queue)
        except Exception:
            pass

    def update_ui(self, status=None, status_color=None, last=None, last_color=None):
        """对外接口保持不变（F6~F10 都在用），但改为排队到主线程执行。"""
        self._q.put(lambda: self._apply_ui(status, status_color, last, last_color))

    def _apply_ui(self, status=None, status_color=None, last=None, last_color=None):
        try:
            if status is not None:
                self.lbl_status.config(text=status)
            if status_color is not None:
                self.lbl_status.config(fg=status_color)
            if last is not None:
                self.lbl_last.config(text=last)
                self._staged_text = str(last)      # 记住暂存内容，ESC 清空时用（不依赖读 Tk 控件）
            if last_color is not None:
                self.lbl_last.config(fg=last_color)
        except Exception:
            pass

    def clear_staged(self, event=None):
        """ESC：清空小窗"暂存内容"（最近提炼 / 框选预览 / F9·F10 结果）**并清空剪贴板**。

        线程安全：只读实例变量 + 通过队列更新 UI（全局热键回调在别的线程里执行）。
        """
        try:
            cur = self._staged_text
            try:
                clip = pyperclip.paste()
            except Exception:
                clip = ""
            new_text, clear_clip, had = staged_clear_plan(cur, clip)
            if not had and not clear_clip:
                return                            # 暂存与剪贴板都是空的：不做任何事、不弹提示
            if had and clear_clip:
                what = "小窗暂存内容与剪贴板"
            elif had:
                what = "小窗暂存内容"
            else:
                what = "剪贴板"
            if had:
                self._apply_ui(status=f"🧹 已清除{what}（ESC）", status_color=FG_DIM,
                               last=new_text, last_color=FG_DIM)
            else:
                self._apply_ui(status=f"🧹 已清除{what}（ESC）", status_color=FG_DIM)
            if clear_clip:
                try:
                    pyperclip.copy("")            # 无条件清剪贴板（客服要求）
                except Exception:
                    pass
        except Exception:
            pass

    def _copy_last(self, event=None):
        txt = ""
        try:
            txt = self.lbl_last.cget("text") or ""
        except Exception:
            pass
        if not txt or txt.startswith("（尚无"):
            return
        try:
            pyperclip.copy(txt)
            self._apply_ui("📋 已复制「最近提炼」内容到剪贴板", ACCENT, None, None)
        except Exception:
            pass

    # ---------------- 连接状态灯（中继 / 探针 / 手机端） ----------------
    def _poll_links(self):
        def work():
            info = fetch_diag_info()
            self._q.put(lambda: self.set_links(info))
        threading.Thread(target=work, daemon=True).start()
        try:
            self.root.after(5000, self._poll_links)
        except Exception:
            pass

    def set_links(self, info):
        """info=None 表示中继没响应；{'legacy': True} 表示中继是旧版代码。"""
        try:
            relay_ok = isinstance(info, dict)
            legacy = bool(relay_ok and info.get("legacy"))
            # ★ 三个 AI 动作的开关（来自中继 /api/diag.features）：本地热键据此拒绝执行
            if relay_ok and isinstance(info.get("features"), dict):
                FEATURES.clear()
                FEATURES.update(info["features"])
            probe = (info or {}).get("probe") or {}
            probe_ok = bool(relay_ok and not legacy and probe.get("online"))
            probe_v = probe.get("version") or ""
            # 连上了却不上报版本 = Tampermonkey 里还是旧脚本：必须黄灯提示，不能显示成正常
            probe_old = probe_ok and not probe_v
            phone_n = 0
            if relay_ok and not legacy:
                phone_n = int(((info.get("mobile") or {}).get("online_clients") or 0))

            self.lbl_relay.config(text="🟢中继" if relay_ok else "🔴中继",
                                  fg=ACCENT if relay_ok else DANGER)
            if probe_old:
                self.lbl_probe.config(text="🟡探针旧版", fg=WARN)
            else:
                self.lbl_probe.config(
                    text=(f"🟢探针v{probe_v}" if probe_v else "🟢探针") if probe_ok else "🔴探针",
                    fg=ACCENT if probe_ok else DANGER)
            self.lbl_phone.config(text=(f"🟢手机{phone_n}" if phone_n else "⚪手机"),
                                  fg=ACCENT if phone_n else FG_DIM)

            if not relay_ok:
                self.lbl_links.config(text=f"⚠️ 中继未响应：请启动 bridge_server.py（{BRIDGE_PORT} 端口）", fg=WARN)
            elif legacy:
                self.lbl_links.config(text="⚠️ 中继是旧版代码（无 /api/diag）：请重启 bridge_server.py", fg=WARN)
            elif probe_old:
                self.lbl_links.config(text="⚠️ 油猴里是旧脚本：请用 /probe.js 覆盖粘贴并 Ctrl+S", fg=WARN)
            else:
                tk_info = info.get("ticket") or {}
                kb = info.get("kb") or {}
                self.lbl_links.config(
                    text=(f"{tk_info.get('conversations', 0)} 会话 · {info.get('categories', 0)} 分类 · "
                          f"{kb.get('total_rows', '-')} 规章 | F6下载 F7提炼 F9解答 F10润色"),
                    fg=FG_DIM)
                self._handle_human_alerts(info.get("alerts") or [])
                self._sync_mode_ui(info)
        except Exception:
            pass

    # ---------------- 自动化开关（回复模式） ----------------
    def cycle_mode(self):
        """循环切换：🤖半自动（起草到输入框，不发送）→ 🚀AFK（直接发）→ ✋手动（只提醒）。"""
        order = ["semi", "afk", "manual"]
        cur = self.mode if self.mode in order else "semi"
        nxt = order[(order.index(cur) + 1) % len(order)]
        threading.Thread(target=self._push_mode, args=(nxt,), daemon=True).start()

    def _push_mode(self, mode):
        """把选择推给中继。手机端刚设过时会被拒绝（以手机端为准），这里如实提示。"""
        try:
            r = requests.post(f"{BRIDGE_BASE}/api/mode",
                              json={"mode": mode, "source": "desktop"}, timeout=2)
            d = r.json() or {}
        except Exception as e:
            self.update_ui(status=f"❌ 切换失败：{str(e)[:24]}", status_color=DANGER)
            return
        if d.get("ok"):
            self.update_ui(status=f"✅ 回复模式：{d.get('mode_label') or mode}", status_color=ACCENT)
        else:
            self.update_ui(status=f"⚠️ {d.get('note') or '切换被拒绝（以手机端为准）'}", status_color=WARN)

    def _sync_mode_ui(self, info):
        """从中继同步模式显示（本窗口不自己记状态，手机端改了这里会跟着变）。"""
        ar = ((info or {}).get("auto_reply") or {})
        m = str(ar.get("mode") or "semi")
        if m not in ("manual", "semi", "afk"):
            m = "semi"
        self.mode = m
        self.mode_owner = str(ar.get("mode_owner") or "default")
        short = {"manual": "✋手动", "semi": "🤖半自动", "afk": "🚀AFK"}[m]
        suffix = "·手机" if self.mode_owner == "mobile" else ""
        try:
            self.btn_mode.config(text=short + suffix, fg=ACCENT if m == "afk" else FG_DIM)
        except Exception:
            pass

    def _handle_human_alerts(self, alerts):
        """人工介入告警（表格里没有答案时）：长报警 + 自动复制玩家信息&问题总结。

        与"新消息叮咚""功能成功/失败""掉线警报"都不同的一种声音：三短一长。
        """
        if not alerts:
            return
        fresh = [a for a in alerts if isinstance(a, dict) and str(a.get("id")) not in ALERT_SEEN]
        if not fresh:
            return
        a = fresh[0]
        for x in fresh:
            ALERT_SEEN.add(str(x.get("id")))
        # 复制给客服（直接可粘到群里/做成工单备注）
        text = (f"玩家信息：{a.get('playerInfo') or a.get('name') or ''}\n"
                f"问题总结：{a.get('summary') or ''}\n"
                f"处理建议：{a.get('reason') or '表格里没有对应答案'}\n"
                f"工单：{a.get('groupID') or ''}")
        try:
            pyperclip.copy(text)
            copied = "已复制到剪贴板"
        except Exception:
            copied = "复制失败"
        self.update_ui(status=f"🙋 需要人工：{a.get('name') or a.get('groupID')}",
                       status_color=WARN,
                       last=f"[人工] 玩家信息+问题总结{copied}：{' '.join(str(a.get('summary') or '').split())[:100]}",
                       last_color="#E5C07B")
        # 长报警放到线程里，别卡住 5 秒轮询
        threading.Thread(target=long_human_alarm, daemon=True).start()
        # 告诉中继"已经提醒过"，避免重复鸣笛
        try:
            requests.post(f"{BRIDGE_BASE}/api/alerts/ack", json={"ids": [a.get("id")]}, timeout=2)
        except Exception:
            pass

hud = None

def safe_capture_selection(min_len=2, quiet=False) -> str:
    """取当前选中文本。quiet=True 时不弹提示/不响警报（供"免框选"回退流程使用）。"""
    try: pyperclip.copy("")
    except Exception: pass
    time.sleep(0.04)
    keyboard.send('ctrl+c')
    raw = ""
    for _ in range(6):
        time.sleep(0.04)
        raw = pyperclip.paste().strip()
        if raw: break
    if not raw or len(raw) < min_len:
        if not quiet:
            if hud: hud.update_ui(status="⚠️ 拦截: 未选中文本或选区为空", status_color="#CE9178")
            beep_error()
        return ""
    if "sk-" in raw or raw.startswith("sk-"):
        if hud: hud.update_ui(status="🛑 危险熔断: 检测到 API Key，已拦截！", status_color="#F44747")
        beep_error()
        return ""
    return raw


def fetch_ticket_history(limit=8) -> str:
    """从当前工单抓聊天记录，作为 F9 的"免框选"输入（手册一直写的免框选，之前其实要选字）。"""
    try:
        data = requests.get(f'{BRIDGE_BASE}/api/ticket', timeout=2).json()
    except Exception:
        return ""
    msgs = [m for m in (data.get("msgs") or []) if isinstance(m, dict) and m.get("text")]
    if not msgs:
        return ""
    return "\n".join([f"{'【玩家】' if m.get('sender') == 'player' else '【客服】'}: {m.get('text')}"
                      for m in msgs[-limit:]])


def fill_into_page(reply: str) -> bool:
    """把文案直接填进网页回复框（不依赖光标焦点）。失败返回 False，调用方再退回 Ctrl+V。"""
    try:
        r = requests.post(f'{BRIDGE_BASE}/api/fill_draft', json={"content": reply}, timeout=2)
        return r.status_code == 200 and bool((r.json() or {}).get("ok"))
    except Exception:
        return False

def on_f7():
    """一键免框选抓取提炼出库"""
    global is_summarizing
    if is_summarizing: return
    is_summarizing = True
    if hud: hud.update_ui(status="⚡ 正在后台抓取玩家数据并提炼...", status_color="#DCDCAA")
    beep_start()
    
    try:
        resp = requests.get(f'{BRIDGE_BASE}/api/ticket', timeout=2)
        data = resp.json()
        if not data or not data.get("playerInfo"):
            if hud: hud.update_ui(status="⚠️ 获取失败: 当前无活跃工单数据", status_color="#CE9178")
            beep_error()
            return
            
        raw_info = data.get("playerInfo", "")
        formatted_info = raw_info.replace(" | ", "\n").replace(": ", "\n")
        
        msgs = [m for m in data.get("msgs", []) if isinstance(m, dict) and m.get("text")]
        chat_text = "\n".join([f"{'【玩家】' if m.get('sender')=='player' else '【客服】'}: {m.get('text')}" for m in msgs[-6:]])
        summary = core.summarize_player_issue(chat_text)

        # ★ 安全闸：提炼失败时不输出半成品（避免把空内容粘给客服）
        if not summary or not summary.strip():
            if hud: hud.update_ui(status="❌ 提炼失败: AI 无返回", status_color="#F44747")
            beep_error()
            return
        
        final_report = f"{formatted_info}\n玩家问题：{summary}"
        pyperclip.copy(final_report)
        
        if hud:
            hud.update_ui(status="✅ 汇报已组装至剪贴板！可直接粘贴", status_color="#4EC9B0",
                          last=f"[提炼] {' '.join(str(summary).split())[:120]}", last_color="#9CDCFE")
        beep_success()
    except Exception as e:
        if hud: hud.update_ui(status=f"❌ 自动提取异常: {str(e)[:20]}", status_color="#F44747")
        beep_error()
    finally:
        is_summarizing = False

def on_f6():
    """下载附件到桌面"""
    url = pyperclip.paste().strip()
    if not url.startswith("http"):
        if hud: hud.update_ui(status="⚠️ 剪贴板未包含有效 URL", status_color="#CE9178")
        beep_error()
        return
    if hud: hud.update_ui(status="⬇️ 正在下载媒体文件...", status_color="#DCDCAA")
    beep_start()
    try:
        desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop", "CS_Media")
        os.makedirs(desktop_dir, exist_ok=True)
        file_name = url.split("/")[-1].split("?")[0]
        if not file_name or "." not in file_name: file_name = f"media_{int(time.time())}.jpg"
        save_path = os.path.join(desktop_dir, file_name)
        urllib.request.urlretrieve(url, save_path)
        if hud: hud.update_ui(status=f"✅ 已保存至桌面 CS_Media", status_color="#4EC9B0")
        beep_success()
    except Exception as e:
        if hud: hud.update_ui(status=f"❌ 下载失败: {str(e)[:20]}", status_color="#F44747")
        beep_error()

def on_f9():
    global current_ticket_context
    text = safe_capture_selection(quiet=True)
    source = "框选内容"
    if not text:
        # ★ 免框选：没选中文字时，直接用当前工单的聊天记录（手册一直是这么写的）
        text = fetch_ticket_history()
        source = "当前工单聊天记录"
    if not text:
        if hud: hud.update_ui(status="⚠️ 没选中文字，也没抓到工单记录：请先在网页打开工单", status_color="#CE9178")
        beep_error()
        return
    beep_start()
    current_ticket_context = text
    if hud: hud.update_ui(status=f"⚡ 正在智能分析工单状态…（{source}）", status_color="#DCDCAA")

    try:
        tag, reply = core.process_ticket_f9(text)
        if tag == "WAITING":
            if hud: hud.update_ui(status="⏸️ 等待玩家回复中，暂不处理", status_color="#CE9178")
            beep_item()
            return

        # ★ 安全闸：AI 接口异常 / 无内容时，严禁把报错占位符或空串粘贴进回复框
        if tag == "API_ERROR" or not reply or not reply.strip():
            if hud: hud.update_ui(status="❌ AI 未返回内容(接口异常)，已阻止粘贴", status_color="#F44747")
            beep_error()
            return

        pyperclip.copy(reply)
        # 优先让探针直接写网页回复框（不依赖光标焦点，避免 Ctrl+V 粘到别处）
        into_page = fill_into_page(reply)
        if not into_page:
            time.sleep(0.1)
            keyboard.send('ctrl+v')
        if hud: hud.update_ui(last=f"[F9·{tag}] {' '.join(str(reply).split())[:120]}", last_color="#4EC9B0")
        where = "网页回复框" if into_page else "当前光标处(剪贴板粘贴)"

        if tag == "GREETING":
            if hud: hud.update_ui(status=f"👋【开场】已填入{where}", status_color="#4EC9B0"); beep_success()
        elif tag == "NEED_INFO":
            if hud: hud.update_ui(status=f"📸【要信息】已填入{where}", status_color="#4EC9B0"); beep_success()
        elif tag == "TIMEOUT_CLOSE":
            if hud: hud.update_ui(status="🚪【超时关单】玩家超2h未回可关单", status_color="#FFA500")
            winsound.Beep(1800, 200)
        elif tag == "FORGOTTEN_REPLY":
            if hud: hud.update_ui(status="🚨【漏单补救】已补业务正解", status_color="#E5C07B")
            winsound.Beep(900, 150); time.sleep(0.05); winsound.Beep(1200, 200)
        else:
            if "【规章库未收录" in reply:
                if hud: hud.update_ui(status="⚠️ 规章未收录！建议上报群聊", status_color="#CE9178")
            else:
                if hud: hud.update_ui(status=f"✅ 官方解答已填入{where}", status_color="#4EC9B0")
            beep_success()
    except Exception as e:
        if hud: hud.update_ui(status=f"❌ 处理异常: {str(e)[:25]}", status_color="#F44747"); beep_error()

def on_f10():
    # ★ 独立开关③：enable_f10_polish=false 时 F10 不再调用 AI 润色（只提示，不动你的回复框）
    if FEATURES and FEATURES.get("f10_polish") is False:
        if hud:
            hud.update_ui(status="⛔「AI 润色」已在设置里关闭（enable_f10_polish=false）",
                          status_color="#E5C07B")
        beep_error()
        return
    text = safe_capture_selection()
    if not text: return
    beep_start()
    if hud: hud.update_ui(status="✨ 正在公关润色转译...", status_color="#DCDCAA")
    try:
        final_reply = core.polish_draft_or_instruction(current_ticket_context, text)
        # ★ 安全闸：润色失败时不得用空内容覆盖客服的回复框
        if not final_reply or not final_reply.strip():
            if hud: hud.update_ui(status="❌ 润色失败: AI 无返回，已阻止粘贴", status_color="#F44747")
            beep_error()
            return
        pyperclip.copy(final_reply)
        into_page = fill_into_page(final_reply)          # 同 F9：优先直接写网页回复框
        if not into_page:
            time.sleep(0.1)
            keyboard.send('ctrl+v')
        if hud: hud.update_ui(last=f"[F10·润色] {' '.join(str(final_reply).split())[:120]}", last_color="#C586C0")
        if hud: hud.update_ui(status=("✅ 话术已公关润色并替换" + ("（网页回复框）" if into_page else "（光标处）")),
                              status_color="#4EC9B0")
        beep_success()
    except Exception as e:
        if hud: hud.update_ui(status=f"❌ 润色异常: {str(e)[:25]}", status_color="#F44747"); beep_error()

def register_hotkeys():
    keyboard.add_hotkey('f6', lambda: threading.Thread(target=on_f6, daemon=True).start())
    keyboard.add_hotkey('f7', lambda: threading.Thread(target=on_f7, daemon=True).start())
    keyboard.add_hotkey('f8', lambda: threading.Thread(target=on_f7, daemon=True).start())   # F8 = F7 别名（历史文档一直提到 F8）
    keyboard.add_hotkey('f9', lambda: threading.Thread(target=on_f9, daemon=True).start(), suppress=True)
    keyboard.add_hotkey('f10', lambda: threading.Thread(target=on_f10, daemon=True).start(), suppress=True)
    # 悬浮窗自救热键：任何情况下都能把窗找回来（F 键被别的软件抢也不怕）
    keyboard.add_hotkey('ctrl+alt+h', recall_hud)
    keyboard.add_hotkey('ctrl+alt+m', toggle_hud_fold)
    # ESC：清空小窗暂存内容（suppress 默认 False -> 不吞按键，浏览器/弹窗里的 ESC 照常生效）
    keyboard.add_hotkey('esc', clear_staged_hud)
    keyboard.wait()


def recall_hud():
    """Ctrl+Alt+H：把悬浮窗拉回屏幕内并重新置顶。"""
    if hud:
        hud.recall()
    else:
        beep_error()


def toggle_hud_fold():
    """Ctrl+Alt+M：折叠 / 展开悬浮窗。"""
    if hud:
        hud.toggle_fold()


def clear_staged_hud():
    """ESC：清空小窗暂存内容（全局热键兜底：浏览器聚焦时也能清）。

    注意：没有暂存内容时 clear_staged() 什么都不做 —— 所以平时按 ESC 不会有副作用。
    """
    if hud:
        hud.clear_staged()


if __name__ == "__main__":
    enable_dpi_awareness()            # 先把 DPI 定下来，坐标才不会错乱
    if not acquire_single_instance():
        # 已有实例在跑：不要再开第二个（否则两套热键互相打架）
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "工单助手悬浮窗已经在运行了。\n\n"
                "如果屏幕上看不到它，请按 Ctrl+Alt+H 召回；\n"
                "或先在任务管理器结束 pythonw.exe，再重新启动。",
                "工单助手", 0x40)
        except Exception:
            pass
        sys.exit(0)

    threading.Thread(target=register_hotkeys, daemon=True).start()
    hud = HUDOverlay()
    if getattr(hud, "_pos_fixed", False):
        hud.root.after(200, lambda: hud.update_ui(
            status="🛠 悬浮窗原坐标在屏幕外，已自动移回", status_color=WARN))
    hud.root.mainloop()