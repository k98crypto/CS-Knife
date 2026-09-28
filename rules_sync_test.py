"""rules_sync 回归测试：监听目录同步 / 直链下载 / 坏文件拒绝 / 热重载"""
import functools
import http.server
import os
import sys
import tempfile
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
import rules_sync

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


def make_xlsx(path, rows, extra=None):
    with pd.ExcelWriter(path) as w:
        pd.DataFrame(rows, columns=["分类", "判定", "回复"]).to_excel(w, sheet_name="常规", index=False)
        for sname, srows in (extra or {}).items():
            pd.DataFrame(srows, columns=["问题", "答案"]).to_excel(w, sheet_name=sname, index=False)


tmp = tempfile.mkdtemp(prefix="rules_sync_test_")
watch = os.path.join(tmp, "watch")
os.makedirs(watch, exist_ok=True)
dest = os.path.join(tmp, "rules.xlsx")

print("=== rules_sync 回归测试 ===\n")

# ---------- 1. 基础校验 ----------
print("[1] 文件校验")
make_xlsx(dest, [["开场", "没有描述问题，直接转人工", "欢迎回来"]])
ok, msg = rules_sync.validate_xlsx(dest)
check("有效 xlsx 校验通过", ok, msg)

bad_file = os.path.join(tmp, "bad.xlsx")
with open(bad_file, "w", encoding="utf-8") as f:
    f.write("this is not an excel file")
ok, msg = rules_sync.validate_xlsx(bad_file)
check("非法内容校验失败", ok is False, msg[:40])

# ---------- 2. 监听目录同步 ----------
print("\n[2] 监听目录同步")
src = os.path.join(watch, "rules.xlsx")
make_xlsx(src, [["开场", "没有描述问题，直接转人工", "欢迎回来"],
                ["充值", "充值未到账", "请提供订单号与截图"]],
          {"新增FAQ": [["如何改名", "请在设置中修改昵称"]]})
os.utime(dest, (time.time() - 300, time.time() - 300))   # 让目标文件更旧
os.utime(src, (time.time() + 5, time.time() + 5))        # 让源文件更新

ok, msg = rules_sync.sync_from_watch_dir(watch, dest)
check("从监听目录同步成功", ok, msg)
check("覆盖前已生成 .bak 备份", os.path.exists(dest + ".bak"))
check("源文件仍在监听目录（只复制不移动）", os.path.exists(src))

ok2, msg2 = rules_sync.sync_from_watch_dir(watch, dest)
check("源不比目标新时不重复同步", ok2 is False, msg2 or "(无变化)")

# ---------- 3. 热重载 ----------
print("\n[3] 热重载（mtime 触发）")
import agent_core as ac
ac.RULES_FILE_PATH = dest
core = ac.CustomerServiceCore()
check("新增工作表已载入", "新增FAQ" in core.sheet_stats, str(list(core.sheet_stats.keys())))

pool = core.kb_static + "\n" + "\n".join(e.get("text", "") for e in core.kb_entries)
check("新规则内容已进入知识库", "请在设置中修改昵称" in pool)

before = core.kb_context
check("无变化时 reload_rules 返回 False", core.reload_rules() is False)

# 再改一次文件，验证 mtime 变化后能自动重载
make_xlsx(dest, [["开场", "没有描述问题，直接转人工", "欢迎回来"],
                 ["充值", "充值未到账", "请提供订单号与截图"],
                 ["活动", "活动奖励未发", "已达上限，次月1日发放"]],
          {"新增FAQ": [["如何改名", "请在设置中修改昵称"]]})
os.utime(dest, (time.time() + 10, time.time() + 10))
check("文件变化后 reload_rules 返回 True", core.reload_rules() is True)
check("新规则生效", "次月1日发放" in (core.kb_static + "\n".join(e["text"] for e in core.kb_entries)))

# ---------- 4. 直链下载同步 ----------
print("\n[4] 直链下载同步（本地 HTTP 服务模拟）")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


handler = functools.partial(QuietHandler, directory=watch)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{port}"

try:
    dl_dest = os.path.join(tmp, "downloaded.xlsx")
    ok, msg = rules_sync.download_rules(f"{base_url}/rules.xlsx", dl_dest)
    check("直链下载并替换成功", ok, msg)
    check("下载结果可正常解析", rules_sync.validate_xlsx(dl_dest)[0])

    ok, msg = rules_sync.download_rules(f"{base_url}/bad.xlsx", os.path.join(tmp, "x.xlsx"))
    check("非表格内容被拒绝（不会污染知识库）", ok is False, msg[:40])

    ok, msg = rules_sync.download_rules("", os.path.join(tmp, "y.xlsx"))
    check("未配置直链时安全跳过", ok is False, msg)
except Exception as e:
    check("直链下载流程未抛异常", False, str(e))
finally:
    srv.shutdown()

# ---------- 5. sync_once 端到端（含热重载） ----------
print("\n[5] sync_once 端到端")
cfg = {"local_excel_path": dest, "rules_watch_dir": "", "rules_sync_url": ""}
changed, message = rules_sync.sync_once(core=core, config=cfg, verbose=False)
check("无变化时 sync_once 返回 False", changed is False, message)

# 往监听目录放一个更新版本，验证 sync_once 会更新文件并热重载
make_xlsx(src, [["开场", "没有描述问题，直接转人工", "欢迎回来"],
                ["公告", "停机维护", "预计 2 小时，请耐心等待"]],
          {"新增FAQ": [["如何改名", "请在设置中修改昵称"]]})
os.utime(src, (time.time() + 60, time.time() + 60))
cfg2 = {"local_excel_path": dest, "rules_watch_dir": watch, "rules_sync_url": ""}
changed, message = rules_sync.sync_once(core=core, config=cfg2, verbose=False)
check("sync_once 检测到监听目录更新", changed is True, message)
check("热重载后新内容已生效",
      "预计 2 小时" in (core.kb_static + "\n".join(e["text"] for e in core.kb_entries)))

print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
sys.exit(0 if failed == 0 else 1)