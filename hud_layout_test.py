"""悬浮窗几何 / 自检逻辑单元测试（离线可跑，不需要 Tk、不需要启服务）

覆盖第七轮两个真实故障：
  1) hud_pos.json 里存的是上一次（更大屏幕）的坐标，
     从 2560x1440 换回 1440x900 后坐标 (1960,777) 已在屏幕外
     -> 悬浮窗进程明明在跑，人却看不见（"悬浮窗不见了"）
  2) 同一个悬浮窗被启动两次 -> 两套全局热键互相打架
"""
import importlib.machinery
import importlib.util
import os
import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))

passed = 0
failed = 0


def check(name, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


# ---------------- 打桩：把重依赖挡在门外 ----------------
# agent_core 会真的去解析 55 万字节的 Excel；这里只需要导入 semi_runner 模块本身。
_mod = types.ModuleType("agent_core")
_mod.CustomerServiceCore = lambda *a, **k: types.SimpleNamespace(
    kb_static="", kb_entries=[], sheet_stats={}, rules_mtime=lambda: 0)
sys.modules["agent_core"] = _mod

# keyboard 的全局钩子不要真的挂上（避免测试期间抢走真实按键）
_kb = types.ModuleType("keyboard")
_kb.add_hotkey = lambda *a, **k: None
_kb.wait = lambda *a, **k: None
_kb.send = lambda *a, **k: None
sys.modules["keyboard"] = _kb

_ws = types.ModuleType("winsound")
_ws.Beep = lambda *a, **k: None
sys.modules["winsound"] = _ws


def load_runner():
    path = os.path.join(BASE, "semi_runner.pyw")
    loader = importlib.machinery.SourceFileLoader("semi_runner_mod", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeLabel:
    def __init__(self):
        self.text = ""
        self.fg = ""

    def config(self, **kw):
        if "text" in kw:
            self.text = kw["text"]
        if "fg" in kw:
            self.fg = kw["fg"]


class FakeHud:
    """只带 set_links 需要用到的那几个控件。"""

    def __init__(self):
        self.lbl_relay = FakeLabel()
        self.lbl_probe = FakeLabel()
        self.lbl_phone = FakeLabel()
        self.lbl_links = FakeLabel()


def main():
    mod = load_runner()
    print("=== 悬浮窗几何与自检单测 ===\n")

    # ---------- 1. 坐标夹取（"悬浮窗不见了" 的根因） ----------
    print("[1] 坐标夹取 clamp_position")
    check("完整可见的坐标原样保留", mod.clamp_position(100, 100, 470, 300, 1440, 900) == (100, 100))

    x, y = mod.clamp_position(1960, 777, 470, 300, 1440, 900)      # 真实踩坑数据
    check("本机真实坏坐标 (1960,777) 被夹回屏幕内",
          x <= 1440 - 470 - mod.SCREEN_MARGIN and y <= 900 - 300 - mod.SCREEN_MARGIN,
          f"({x},{y})")
    check("夹取后窗口左边缘一定在屏内", x >= mod.SCREEN_MARGIN and y >= mod.SCREEN_MARGIN, f"({x},{y})")

    check("负坐标（多屏拔掉后可能出现的残值）被夹回",
          mod.clamp_position(-500, -500, 470, 300, 1440, 900) == (mod.SCREEN_MARGIN, mod.SCREEN_MARGIN))

    x2, y2 = mod.clamp_position(0, 0, 2000, 2000, 1440, 900)
    check("窗口比屏幕还大时不产生负坐标/崩溃",
          x2 == mod.SCREEN_MARGIN and y2 == mod.SCREEN_MARGIN, f"({x2},{y2})")

    # ---------- 2. 默认位置 ----------
    print("\n[2] 默认位置 default_position")
    dx, dy = mod.default_position(mod.HUD_W, mod.HUD_H, 1440, 900)
    check("默认停靠右下角（不再固定左上角挡视野）",
          dx > 1440 / 2 and dy > 900 / 2, f"({dx},{dy})")
    check("默认位置本身也在屏内",
          dx + mod.HUD_W <= 1440 and dy + mod.HUD_H <= 900, f"({dx + mod.HUD_W},{dy + mod.HUD_H})")

    # ---------- 3. 折叠尺寸 ----------
    print("\n[3] 尺寸常量")
    check("折叠高度 < 展开高度", mod.HUD_H_FOLDED < mod.HUD_H,
          f"{mod.HUD_H_FOLDED} < {mod.HUD_H}")
    check("折叠后只保留标题栏量级（<=80px）", mod.HUD_H_FOLDED <= 80, str(mod.HUD_H_FOLDED))

    # ---------- 4. 单实例互斥 ----------
    print("\n[4] 单实例互斥 acquire_single_instance")
    port = 18766          # 测试专用端口，避开真实实例
    check("第一次占用成功", mod.acquire_single_instance(port))
    check("第二次占用失败（不会再开出第二个悬浮窗）",
          mod.acquire_single_instance(port) is False)
    try:
        mod._INSTANCE_LOCK["sock"].close()
    except Exception:
        pass
    mod._INSTANCE_LOCK["sock"] = None
    check("释放后可以重新占用", mod.acquire_single_instance(port) is True)
    try:
        mod._INSTANCE_LOCK["sock"].close()
    except Exception:
        pass

    # ---------- 5. 端口一致 ----------
    print("\n[5] 中继端口一致")
    check("从 config.json 读到 port(8765)", mod.BRIDGE_PORT == 8765, str(mod.BRIDGE_PORT))
    check("BRIDGE_BASE 拼接正确",
          mod.BRIDGE_BASE == f"http://127.0.0.1:{mod.BRIDGE_PORT}", mod.BRIDGE_BASE)

    # ---------- 6. 连接状态灯 ----------
    print("\n[6] 连接状态灯 set_links")
    hud = FakeHud()
    mod.HUDOverlay.set_links(hud, None)
    check("中继没响应 -> 中继灯变红", hud.lbl_relay.fg == mod.DANGER, hud.lbl_relay.text)
    check("中继没响应 -> 底栏给出启动指引", "中继未响应" in hud.lbl_links.text, hud.lbl_links.text)

    hud = FakeHud()
    mod.HUDOverlay.set_links(hud, {"legacy": True})
    check("旧版中继 -> 中继灯绿但提示需重启",
          hud.lbl_relay.fg == mod.ACCENT and "旧版" in hud.lbl_links.text, hud.lbl_links.text)

    hud = FakeHud()
    mod.HUDOverlay.set_links(hud, {
        "probe": {"online": True, "version": "7.2"},
        "mobile": {"online_clients": 1},
        "ticket": {"conversations": 3},
        "categories": 42,
        "kb": {"total_rows": 843},
    })
    check("探针在线 -> 探针灯绿且带版本号",
          hud.lbl_probe.fg == mod.ACCENT and "7.2" in hud.lbl_probe.text, hud.lbl_probe.text)
    check("手机端在线 -> 手机灯绿且带数量",
          hud.lbl_phone.fg == mod.ACCENT and "1" in hud.lbl_phone.text, hud.lbl_phone.text)
    check("底栏显示会话/分类/规章统计",
          "3 会话" in hud.lbl_links.text and "42 分类" in hud.lbl_links.text
          and "843" in hud.lbl_links.text, hud.lbl_links.text)

    hud = FakeHud()
    mod.HUDOverlay.set_links(hud, {"probe": {"online": False}, "mobile": {}, "ticket": {}, "kb": {}})
    check("探针掉线 -> 探针灯红（一眼看出脚本没跑）", hud.lbl_probe.fg == mod.DANGER, hud.lbl_probe.text)

    hud = FakeHud()
    mod.HUDOverlay.set_links(hud, {"probe": {"online": True, "version": ""},
                                   "mobile": {}, "ticket": {}, "kb": {}})
    check("连上了却不上报版本 -> 黄灯「探针旧版」（不能显示成一切正常）",
          hud.lbl_probe.fg == mod.WARN and "旧版" in hud.lbl_probe.text, hud.lbl_probe.text)
    check("底栏给出更新脚本指引", "旧脚本" in hud.lbl_links.text, hud.lbl_links.text)

    # ---------- 7. 源码级回归（防止后续改动把自救能力删掉） ----------
    print("\n[7] 源码级回归")
    with open(os.path.join(BASE, "semi_runner.pyw"), "r", encoding="utf-8") as f:
        src = f.read()
    check("注册了 Ctrl+Alt+H 召回热键", "ctrl+alt+h" in src)
    check("注册了 Ctrl+Alt+M 折叠热键", "ctrl+alt+m" in src)
    check("启动时启用 DPI 感知", "enable_dpi_awareness()" in src)
    check("启动时做单实例互斥", "acquire_single_instance()" in src)
    check("始终置顶默认开启", '"-topmost"' in src and "self.pinned = True" in src)
    check("界面更新走主线程队列（避免 Tk 跨线程闪退）", "_drain_queue" in src)
    check("保留 F6~F10 全部热键",
          all(k in src for k in ("'f6'", "'f7'", "'f8'", "'f9'", "'f10'")))
    check("保留 update_ui 兼容接口（F9/F10 依赖）",
          "def update_ui(self, status=None, status_color=None, last=None, last_color=None)" in src)

    print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

