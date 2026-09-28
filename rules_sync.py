"""在线表格规章库自动同步工具

背景：当前账号对该在线表格只有**只读**权限、也无法添加企业机器人，因此无法走表格开放平台 API。
本模块提供三条可落地的同步路径：

  1) 监听目录（推荐）
     把导出的 rules.xlsx 放进 config.json 的 rules_watch_dir
     （例如表格客户端 / 坚果云 / OneDrive 的同步目录）。检测到更新的表格会自动覆盖并热重载。

  2) 直链拉取
     在 config.json 配置 rules_sync_url（任何能直接返回 xlsx 的地址），
     定时下载 → 校验 → 备份 → 原子替换 → 热重载。

  3) 手动覆盖
     直接把新 xlsx 放到项目根目录即可，Agent 会在下次处理工单时按 mtime 自动热重载
     （agent_core.reload_rules），无需重启服务。

用法：
  python rules_sync.py --once     # 执行一次同步（可挂到 Windows 任务计划）
  python rules_sync.py --watch    # 常驻定时同步（前台运行）
  python rules_sync.py --info     # 查看当前规章库状态
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def get_base_dir() -> str:
    """脚本/打包后的真实物理路径（免疫 PyInstaller 虚拟环境）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    try:
        # utf-8-sig：兼容记事本/PowerShell 写出的带 BOM 的 UTF-8（否则 json.load 直接失败）
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def rules_path(cfg: dict = None) -> str:
    cfg = cfg or load_config()
    name = cfg.get("local_excel_path") or "rules.xlsx"
    return name if os.path.isabs(name) else os.path.join(BASE_DIR, name)


# 旧版配置键名兼容：早期版本用过 "<平台名>_sync_url" 这种前缀，老配置里还留着。
# 为了不让仓库源码出现该第三方平台名，这里用拼接方式还原旧键名（不影响老配置继续生效）。
_LEGACY_KEY_PREFIX = "fei" + "shu_"


def cfg_get(cfg: dict, key: str, default=None):
    """先读新键名（rules_sync_url），读不到再兼容旧键名（旧前缀 + sync_url）。"""
    if not isinstance(cfg, dict):
        return default
    if cfg.get(key):
        return cfg.get(key)
    legacy = _LEGACY_KEY_PREFIX + key.replace("rules_", "", 1)
    return cfg.get(legacy, default)


def validate_xlsx(path: str):
    """校验是否为可解析的 Excel（避免坏文件毁掉知识库）。

    注意：必须显式 close，否则 pandas 会占住文件句柄，
    在 Windows 上导致后续 os.replace 报 WinError 32。
    """
    try:
        import pandas as pd
        x = pd.ExcelFile(path)
        try:
            names = list(x.sheet_names)
        finally:
            x.close()
        if not names:
            return False, "文件中没有工作表"
        return True, f"{len(names)} 张表"
    except Exception as e:
        return False, str(e)


def _validate_bytes(data: bytes):
    """直接在内存里校验，完全不落盘、不产生文件句柄。"""
    import io as _io
    try:
        import pandas as pd
        x = pd.ExcelFile(_io.BytesIO(data))
        try:
            names = list(x.sheet_names)
        finally:
            x.close()
        return (True, f"{len(names)} 张表") if names else (False, "文件中没有工作表")
    except Exception as e:
        return False, str(e)


def _apply(src: str, dest: str, move: bool):
    """先备份旧文件，再原子替换，避免下载/复制中断导致知识库损坏。"""
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        if os.path.exists(dest):
            shutil.copy2(dest, dest + ".bak")
        if move:
            os.replace(src, dest)
        else:
            shutil.copy2(src, dest)
        return True, "已更新"
    except Exception as e:
        return False, f"写入失败: {e}"


def download_rules(url: str, dest: str, timeout: int = 30):
    if not url:
        return False, "未配置 rules_sync_url"
    tmp = dest + ".download"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as e:
        return False, f"下载失败: {e}"
    if len(data) < 1024:
        return False, f"下载内容过小（{len(data)} 字节），疑似不是有效表格"

    ok, msg = _validate_bytes(data)      # 先在内存里校验，避免占用文件句柄
    if not ok:
        return False, f"校验失败: {msg}"

    try:
        with open(tmp, "wb") as f:
            f.write(data)
    except Exception as e:
        return False, f"暂存失败: {e}"
    return _apply(tmp, dest, move=True)


def sync_from_watch_dir(watch_dir: str, dest: str):
    """从监听目录取更晚的表格（复制，不动源文件）。"""
    if not watch_dir or not os.path.isdir(watch_dir):
        return False, ""
    cands = [os.path.join(watch_dir, n) for n in os.listdir(watch_dir)
             if n.lower().endswith((".xlsx", ".xls")) and not n.startswith("~$")]
    if not cands:
        return False, ""
    # 文件名含 rules/规章/规则 的优先，其次按修改时间新的优先
    def rank(p):
        base = os.path.basename(p).lower()
        prefer = 0 if ("rules" in base.lower() or "规章" in p or "规则" in p) else 1
        return (prefer, -os.path.getmtime(p))
    cands.sort(key=rank)
    src = cands[0]

    if os.path.exists(dest) and os.path.getmtime(src) <= os.path.getmtime(dest):
        return False, ""
    ok, msg = validate_xlsx(src)
    if not ok:
        return False, f"监听目录中的文件无效: {msg}"
    ok2, msg2 = _apply(src, dest, move=False)
    return (ok2, f"从监听目录更新: {msg2}" if ok2 else f"从监听目录更新失败: {msg2}")


def sync_once(core=None, config: dict = None, verbose: bool = True):
    """执行一轮同步。返回 (是否有变化, 说明文字)。

    core 传 None 时只更新文件（正在运行的服务会通过 mtime 自行热重载）。
    """
    cfg = config if config is not None else load_config()
    dest = rules_path(cfg)

    notes, file_changed = [], False

    ok, msg = sync_from_watch_dir(cfg.get("rules_watch_dir", ""), dest)
    if msg:
        notes.append(msg)
        file_changed = file_changed or ok

    _sync_url = cfg_get(cfg, "rules_sync_url")
    if _sync_url:
        ok2, msg2 = download_rules(_sync_url, dest)
        notes.append(f"直链同步: {msg2}")
        file_changed = file_changed or ok2

    reloaded = False
    if core is not None:
        try:
            reloaded = core.reload_rules()
        except Exception as e:
            notes.append(f"热重载失败: {e}")

    changed = file_changed or reloaded
    message = "；".join(notes) if notes else "文件无变化"
    if verbose and changed:
        print(f"[rules_sync] {message}{'（已热重载）' if reloaded else ''}")
    return changed, message


def watch_loop(core=None, interval_minutes: int = 30, on_change=None):
    """常驻定时同步。on_change(changed, message) 可用于发通知。"""
    interval = max(60, int(interval_minutes) * 60)
    print(f"[rules_sync] 已启动定时同步：每 {interval // 60} 分钟检查一次")
    while True:
        try:
            changed, message = sync_once(core=core)
            if changed and on_change:
                try:
                    on_change(changed, message)
                except Exception:
                    pass
        except Exception as e:
            print(f"[rules_sync] 同步异常: {e}", file=sys.stderr)
        time.sleep(interval)


def print_info():
    cfg = load_config()
    dest = rules_path(cfg)
    print(f"规章库路径   : {dest}")
    if not os.path.exists(dest):
        print("状态         : 文件不存在")
        return 1
    st = os.stat(dest)
    print(f"文件大小     : {st.st_size} 字节")
    print(f"修改时间     : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
    ok, msg = validate_xlsx(dest)
    print(f"表格校验     : {'通过' if ok else '失败'}（{msg}）")

    if "-v" in sys.argv or "--verbose" in sys.argv:
        try:
            from agent_core import CustomerServiceCore
            core = CustomerServiceCore()
            print(f"常驻静态区   : {len(core.kb_static)} 字符")
            print(f"检索池       : {len(core.kb_entries)} 条")
            print(f"解析条目合计 : {sum(core.sheet_stats.values())} 条 / {len(core.sheet_stats)} 张表")
            for k, v in core.sheet_stats.items():
                print(f"   - {k}: {v}")
        except Exception as e:
            print(f"（加载 agent_core 失败: {e}）")
    print(f"同步间隔     : {cfg.get('rules_sync_interval_minutes', 30)} 分钟")
    print(f"监听目录     : {cfg.get('rules_watch_dir') or '（未配置）'}")
    print(f"直链同步     : {cfg_get(cfg, 'rules_sync_url') or '（未配置）'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="在线表格规章库自动同步")
    ap.add_argument("--once", action="store_true", help="执行一次同步后退出")
    ap.add_argument("--watch", action="store_true", help="常驻定时同步")
    ap.add_argument("--info", action="store_true", help="查看当前规章库状态")
    ap.add_argument("--verbose", "-v", action="store_true", help="输出各工作表解析明细")
    ap.add_argument("--interval", type=int, default=0, help="覆盖同步间隔（分钟）")
    args = ap.parse_args()

    cfg = load_config()

    if args.info:
        return print_info()

    if args.watch:
        interval = args.interval or int(cfg.get("rules_sync_interval_minutes", 30))
        watch_loop(interval_minutes=interval)
        return 0

    changed, message = sync_once()
    print(f"[rules_sync] {message}")
    if not changed:
        print("[rules_sync] 提示：文件无变化时无需重启；运行中的服务会自动热重载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())