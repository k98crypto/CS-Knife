"""诊断链路端到端测试：/api/diag、/diag、/probe.js + 探针握手

这套断言回答一个具体的运维问题：**"油猴脚本到底加载了没、跑的是哪一版？"**
旧版只能靠看控制台日志猜，现在有 /api/diag 可以查。

需要先启动 bridge_server.py（占用 8765 端口）；若服务是旧版（无 /api/diag）会自动跳过。
"""
import asyncio
import json
import sys
import urllib.parse

import aiohttp

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:8765"
TEST_VER = "7.8"
TEST_PAGE = "https://ticket.example.com/imChat/workstation"

passed = 0
failed = 0
skipped = 0


def check(name, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


async def fetch(session, path):
    """返回 (status, text)。"""
    try:
        async with session.get(BASE + path) as r:
            return r.status, await r.text()
    except Exception as e:
        return 0, str(e)


async def fetch_json(session, path):
    status, text = await fetch(session, path)
    if status != 200:
        return status, None
    try:
        return status, json.loads(text)
    except Exception:
        return status, None


async def drain(mobile, want="FULL_SYNC", tries=6, settle=1.0):
    for _ in range(tries):
        try:
            m = await asyncio.wait_for(mobile.receive_json(), timeout=settle)
        except asyncio.TimeoutError:
            return None
        if m.get("type") == want:
            return m.get("data") or {}
    return None


async def main():
    global skipped
    async with aiohttp.ClientSession() as s:
        # ---------- 1. /api/diag 结构 ----------
        print("[1] GET /api/diag")
        status, diag = await fetch_json(s, "/api/diag")
        if status == 404:
            skipped += 1
            print("  [SKIP] 服务是旧版（没有 /api/diag）—— 请重启 bridge_server.py 后重跑本测试")
            print(f"\n=== 结果: {passed} 通过 / {failed} 失败 / {skipped} 跳过 ===")
            return 0
        check("接口可访问且返回 JSON", status == 200 and isinstance(diag, dict), f"status={status}")
        if not isinstance(diag, dict):
            print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
            return 1

        for key in ("server", "probe", "mobile", "ticket", "kb", "config"):
            check(f"包含 {key} 区块", isinstance(diag.get(key), dict))
        check("包含 im 区块（手机端状态从哪来，一眼可查）", isinstance(diag.get("im"), dict),
              str(diag.get("im")))
        check("probe.expected_version 提示期望版本", bool((diag.get("probe") or {}).get("expected_version")),
              str((diag.get("probe") or {}).get("expected_version")))
        check("server.ip / port 可读",
              bool((diag.get("server") or {}).get("ip")) and (diag.get("server") or {}).get("port") == 8765,
              f"{(diag.get('server') or {}).get('ip')}:{(diag.get('server') or {}).get('port')}")

        # 安全：诊断接口绝不能带出密钥
        cfg_dump = json.dumps(diag.get("config") or {}, ensure_ascii=False).lower()
        check("配置区块不含任何密钥字段（key/token/secret/bark）",
              all(k not in cfg_dump for k in ("key", "token", "secret", "bark")), cfg_dump)

        # ---------- 2. /diag 给人看的自检页 ----------
        print("\n[2] GET /diag（自检页）")
        st, html = await fetch(s, "/diag")
        check("自检页可打开", st == 200 and "系统自检" in html, f"status={st}")
        check("自检页包含探针区块", "电脑端探针" in html)
        check("自检页给出 probe.js 取用入口", 'href="/probe.js"' in html)

        # ---------- 3. /probe.js 最新脚本可取 ----------
        print("\n[3] GET /probe.js（核对油猴里的版本）")
        st, js = await fetch(s, "/probe.js")
        check("脚本可取得", st == 200 and len(js) > 5000, f"status={st} len={len(js)}")
        check("脚本声明的版本与预期一致", f"@version      {TEST_VER}" in js)
        check("脚本含自检握手 PROBE_HELLO", "PROBE_HELLO" in js)
        check("脚本含页面内状态胶囊", "ensureChip" in js and "探针 v" in js)
        check("声明了 @match 命中域名与 @noframes",
              "// @match" in js and "@noframes" in js)

        # ---------- 4. 探针握手 -> 后端可查 ----------
        print("\n[4] 探针握手 PROBE_HELLO -> /api/diag 可查版本")
        ext = await s.ws_connect(BASE + "/ws/extension")
        await ext.send_json({"event": "PROBE_HELLO",
                             "data": {"version": TEST_VER, "page": TEST_PAGE, "ua": "diag_test"}})
        await asyncio.sleep(0.5)
        st, diag2 = await fetch_json(s, "/api/diag")
        probe = (diag2 or {}).get("probe") or {}
        check("握手后 probe.online=True", probe.get("online") is True, str(probe.get("online")))
        check("握手后 probe.version 可查", probe.get("version") == TEST_VER, str(probe.get("version")))
        check("握手后 version_reported=True", probe.get("version_reported") is True,
              str(probe.get("version_reported")))
        check("握手后 probe.page 可查", probe.get("page") == TEST_PAGE, str(probe.get("page")))
        check("握手后 probe.hello_count 递增", int(probe.get("hello_count") or 0) >= 1,
              str(probe.get("hello_count")))
        check("最近心跳为最近 30 秒内",
              (probe.get("last_seen_sec") is not None) and int(probe["last_seen_sec"]) <= 30,
              str(probe.get("last_seen_sec")))

        # ---------- 5. 手机端快照也带探针版本 ----------
        print("\n[5] 手机端 FULL_SYNC 带 probe_version")
        mobile = await s.ws_connect(BASE + "/ws/mobile")
        snap = await drain(mobile)
        check("快照可拿到", isinstance(snap, dict))
        check("快照含 probe_version", (snap or {}).get("probe_version") == TEST_VER,
              str((snap or {}).get("probe_version")))
        check("快照含 extension_online=True", (snap or {}).get("extension_online") is True)
        await mobile.close()

        # ---------- 6. 断开后接口依然健康 ----------
        print("\n[6] 探针断开后接口依然健康")
        await ext.close()
        await asyncio.sleep(0.5)
        st, diag3 = await fetch_json(s, "/api/diag")
        probe3 = (diag3 or {}).get("probe") or {}
        check("断开后 /api/diag 仍返回 200", st == 200 and isinstance(diag3, dict), f"status={st}")
        check("probe.online 仍为布尔值（无异常）", isinstance(probe3.get("online"), bool),
              str(probe3.get("online")))

    print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
