import os
import sys
import json
import time
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

def beep_start(): winsound.Beep(1000, 100)
def beep_success(): winsound.Beep(1600, 150)
def beep_item(): winsound.Beep(1300, 80)
def beep_error():
    winsound.Beep(400, 150)
    time.sleep(0.05)
    winsound.Beep(400, 150)

class HUDOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("工单助手 HUD")
        init_pos = "+25+25"
        if os.path.exists(POS_FILE_PATH):
            try:
                with open(POS_FILE_PATH, "r", encoding="utf-8") as f:
                    pos_data = json.load(f)
                    init_pos = f"+{pos_data.get('x', 25)}+{pos_data.get('y', 25)}"
            except Exception: pass

        self.root.geometry(f"460x276{init_pos}")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.overrideredirect(True)
        self.root.configure(bg="#1E1E1E")

        self.root.bind("<Button-1>", self._start_move)
        self.root.bind("<B1-Motion>", self._do_move)
        self.root.bind("<ButtonRelease-1>", lambda e: self.save_position())

        top_frame = tk.Frame(self.root, bg="#252526")
        top_frame.pack(fill="x")
        top_frame.bind("<Button-1>", self._start_move)
        top_frame.bind("<B1-Motion>", self._do_move)

        cheat_sheet = (
            "【操作指南】F9: 智能解答(免操作) | F10: 润色选中草稿\n"
            "• F7/F8: 一键后台提取当前工单并提炼 (免框选)\n"
            "• F6: 将选中的图片/视频链接直接下载到桌面\n"
        )
        lbl_guide = tk.Label(top_frame, text=cheat_sheet, font=("Microsoft YaHei UI", 8),
                             fg="#9CDCFE", bg="#252526", justify="left", padx=8, pady=4)
        lbl_guide.pack(side="left", fill="both", expand=True)
        lbl_guide.bind("<Button-1>", self._start_move)
        lbl_guide.bind("<B1-Motion>", self._do_move)

        btn_close = tk.Button(top_frame, text=" ✕ ", font=("Microsoft YaHei UI", 9, "bold"), fg="#CCCCCC", bg="#252526", activebackground="#E81123", activeforeground="white", bd=0, relief="flat", cursor="hand2", command=self.close_app)
        btn_close.pack(side="right", anchor="ne", padx=4, pady=4)

        self.lbl_profile = tk.Label(self.root, text="最近提炼 / 框选预览：", font=("Microsoft YaHei UI", 8), fg="#808080", bg="#1E1E1E", anchor="w")
        self.lbl_profile.pack(fill="x", padx=10, pady=(6, 0))

        self.lbl_last = tk.Label(self.root, text="（尚无，按 F7/F8 提炼后在此核验）", font=("Microsoft YaHei UI", 9),
                                 fg="#DCDCAA", bg="#1E1E1E", anchor="w", justify="left", wraplength=432)
        self.lbl_last.pack(fill="x", padx=10, pady=(0, 4))

        self.lbl_status = tk.Label(self.root, text="⚡ 状态: 监听就绪", font=("Microsoft YaHei UI", 9, "bold"), fg="#4EC9B0", bg="#1E1E1E", anchor="w")
        self.lbl_status.pack(fill="x", padx=10, pady=(2, 6))

    def _start_move(self, event): self.x = event.x; self.y = event.y
    def _do_move(self, event):
        x = self.root.winfo_x() + (event.x - self.x)
        y = self.root.winfo_y() + (event.y - self.y)
        self.root.geometry(f"+{x}+{y}")
    def save_position(self):
        try:
            with open(POS_FILE_PATH, "w", encoding="utf-8") as f: json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()}, f)
        except Exception: pass
    def close_app(self):
        self.save_position(); self.root.destroy(); os._exit(0)
    def update_ui(self, status=None, status_color=None, last=None, last_color=None):
        if status is not None: self.lbl_status.config(text=status)
        if status_color is not None: self.lbl_status.config(fg=status_color)
        if last is not None: self.lbl_last.config(text=last)
        if last_color is not None: self.lbl_last.config(fg=last_color)

hud = None

def safe_capture_selection(min_len=2) -> str:
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
        if hud: hud.update_ui(status="⚠️ 拦截: 未选中文本或选区为空", status_color="#CE9178")
        beep_error()
        return ""
    if "sk-" in raw or raw.startswith("sk-"):
        if hud: hud.update_ui(status="🛑 危险熔断: 检测到 API Key，已拦截！", status_color="#F44747")
        beep_error()
        return ""
    return raw

def on_f7():
    """一键免框选抓取提炼出库"""
    global is_summarizing
    if is_summarizing: return
    is_summarizing = True
    if hud: hud.update_ui(status="⚡ 正在后台抓取玩家数据并提炼...", status_color="#DCDCAA")
    beep_start()
    
    try:
        resp = requests.get('http://127.0.0.1:8765/api/ticket', timeout=2)
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
    text = safe_capture_selection()
    if not text: return
    beep_start()
    current_ticket_context = text
    if hud: hud.update_ui(status="⚡ 正在智能分析工单状态...", status_color="#DCDCAA")

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
        if hud: hud.update_ui(last=f"[F9·{tag}] {' '.join(str(reply).split())[:120]}", last_color="#4EC9B0")
        time.sleep(0.1)
        keyboard.send('ctrl+v')

        if tag == "GREETING":
            if hud: hud.update_ui(status="👋【开场】已填入开场白", status_color="#4EC9B0"); beep_success()
        elif tag == "NEED_INFO":
            if hud: hud.update_ui(status="📸【要信息】已填入截图追问", status_color="#4EC9B0"); beep_success()
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
                if hud: hud.update_ui(status="✅ 官方解答已填入", status_color="#4EC9B0")
            beep_success()
    except Exception as e:
        if hud: hud.update_ui(status=f"❌ 处理异常: {str(e)[:25]}", status_color="#F44747"); beep_error()

def on_f10():
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
        if hud: hud.update_ui(last=f"[F10·润色] {' '.join(str(final_reply).split())[:120]}", last_color="#C586C0")
        time.sleep(0.1)
        keyboard.send('ctrl+v')
        if hud: hud.update_ui(status="✅ 话术已公关润色并自动替换", status_color="#4EC9B0")
        beep_success()
    except Exception as e:
        if hud: hud.update_ui(status=f"❌ 润色异常: {str(e)[:25]}", status_color="#F44747"); beep_error()

def register_hotkeys():
    keyboard.add_hotkey('f6', lambda: threading.Thread(target=on_f6, daemon=True).start())
    keyboard.add_hotkey('f7', lambda: threading.Thread(target=on_f7, daemon=True).start())
    keyboard.add_hotkey('f8', lambda: threading.Thread(target=on_f7, daemon=True).start())   # F8 = F7 别名（历史文档一直提到 F8）
    keyboard.add_hotkey('f9', lambda: threading.Thread(target=on_f9, daemon=True).start(), suppress=True)
    keyboard.add_hotkey('f10', lambda: threading.Thread(target=on_f10, daemon=True).start(), suppress=True)
    keyboard.wait()

if __name__ == "__main__":
    threading.Thread(target=register_hotkeys, daemon=True).start()
    hud = HUDOverlay()
    hud.root.mainloop()