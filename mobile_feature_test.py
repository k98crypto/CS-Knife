"""手机端新功能端到端测试

覆盖：
  1) SET_IM_STATUS       远程切换 IM 在线/忙碌/离线 + 切到在线自动解除警报
  2) REQUEST_SNAPSHOT    唤醒补拉（iOS 锁屏恢复后主动要快照）
  3) CATEGORY_OPTIONS    + /api/categories 问题分类回报与查询
  4) AI_CLOSE            手机端「AI 回复并关单」→ 探针收到回复并关单指令 → 会话从列表消失
"""
import asyncio
import sys
import time

import aiohttp

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:8765"
RUN = str(int(time.time() * 1000))
GID = "F-" + RUN

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


async def drain(ws, settle=0.5, tries=12):
    """排空推送，返回 (最后一帧 FULL_SYNC, 收集到的全部消息)。"""
    msgs, last_sync = [], None
    for _ in range(tries):
        try:
            m = await asyncio.wait_for(ws.receive_json(), timeout=settle)
        except asyncio.TimeoutError:
            break
        msgs.append(m)
        if m.get("type") == "FULL_SYNC":
            last_sync = m.get("data") or {}
    return last_sync, msgs


def convs(snap):
    return (((snap or {}).get("companies") or {}).get("main") or {}).get("conversations") or {}


async def main():
    async with aiohttp.ClientSession() as s:
        # 先探一次 /api/diag：若已经有一个"真实浏览器页面"的探针在线，
        # 那"我们这条连接断开后 extension_online 必须为 false"就不成立（还有其他探针在），
        # 这种情况要按多客户端场景处理，否则断言会误报（第八轮实测遇到）。
        external_probe = None
        try:
            async with s.get(BASE + "/api/diag") as r:
                if r.status == 200:
                    external_probe = bool(((await r.json()) or {}).get("probe", {}).get("online"))
        except Exception:
            external_probe = None

        ext = await s.ws_connect(BASE + "/ws/extension")
        mobile = await s.ws_connect(BASE + "/ws/mobile")
        await drain(mobile, 0.4)

        # ---------- 1. 远程 IM 状态切换 ----------
        print("\n[1] 远程切换 IM 状态")
        await mobile.send_json({"action": "SET_IM_STATUS", "status": 3})
        await asyncio.sleep(0.5)
        _, ext_msgs = await drain(ext, 0.35)
        _, m_msgs = await drain(mobile, 0.35)
        got = [m for m in ext_msgs if m.get("command") == "CHANGE_STATUS"]
        check("探针收到 CHANGE_STATUS(3 离线)", bool(got) and got[-1].get("status") == 3,
              str(got[-1] if got else ext_msgs))
        snap = [m for m in m_msgs if m.get("type") == "FULL_SYNC"]
        check("服务端记录 im_status=3",
              bool(snap) and (snap[-1].get("data") or {}).get("im_status") == 3,
              str((snap[-1].get("data") or {}).get("im_status") if snap else None))

        await mobile.send_json({"action": "SET_IM_STATUS", "status": 1})
        await asyncio.sleep(0.5)
        _, ext_msgs2 = await drain(ext, 0.35)
        _, m_msgs2 = await drain(mobile, 0.35)
        check("探针收到 CHANGE_STATUS(1 在线)",
              any(m.get("command") == "CHANGE_STATUS" and m.get("status") == 1 for m in ext_msgs2))
        check("切到在线时自动下发 SILENCE_ALARM（解除警报）",
              any(m.get("command") == "SILENCE_ALARM" for m in ext_msgs2))
        snap2 = [m for m in m_msgs2 if m.get("type") == "FULL_SYNC"]
        d2 = (snap2[-1].get("data") if snap2 else {}) or {}
        check("服务端 im_status=1 且警报已清除",
              d2.get("im_status") == 1 and d2.get("alarm_status") is False,
              f"im_status={d2.get('im_status')} alarm={d2.get('alarm_status')}")

        # ---------- 2. 唤醒补拉 ----------
        print("\n[2] 唤醒补拉（REQUEST_SNAPSHOT）")
        await mobile.send_json({"action": "REQUEST_SNAPSHOT"})
        snap3, m_msgs3 = await drain(mobile, 0.5)
        check("主动请求后立即收到 FULL_SYNC 快照",
              snap3 is not None and any(m.get("type") == "FULL_SYNC" for m in m_msgs3))

        # ---------- 3. 问题分类回报与查询 ----------
        print("\n[3] 问题分类回报 + /api/categories")
        await ext.send_json({"event": "CATEGORY_OPTIONS",
                             "data": {"options": ["登录问题", "充值退款", "游戏BUG"]}})
        await asyncio.sleep(0.4)
        await drain(mobile, 0.3)
        async with s.get(BASE + "/api/categories") as r:
            body = await r.json()
        check("分类已透传到 /api/categories",
              body.get("options") == ["登录问题", "充值退款", "游戏BUG"], str(body.get("options")))
        check("接口同时回传分类路径配置", "close_category_path" in body)

        # ---------- 4. AI 回复并关单 ----------
        print("\n[4] AI 回复并关单")
        await ext.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": GID, "name": "测试玩家", "messages": [], "playerInfo": "测试玩家 | UID:9001"}})
        await asyncio.sleep(0.3)
        await drain(mobile, 0.3)
        await mobile.send_json({"action": "SEND_REPLY", "groupID": GID, "content": "您好，正在为您核实"})
        await asyncio.sleep(0.4)
        snap4, _ = await drain(mobile, 0.4)
        check("会话已建立", GID in convs(snap4), str([k for k in convs(snap4) if k == GID]))

        await mobile.send_json({"action": "AI_CLOSE", "groupID": GID})
        status_msg = None
        post_snap = None                         # 关单后的快照（会先于 AI_STATUS 到达）
        for _ in range(30):                      # AI 生成可能耗时，最多等约 15 秒
            try:
                m = await asyncio.wait_for(mobile.receive_json(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "FULL_SYNC":
                post_snap = m.get("data") or {}
            if m.get("type") == "AI_STATUS":
                status_msg = m
                break
        check("收到关单结果回执 AI_STATUS", status_msg is not None,
              str(status_msg) if status_msg else "超时未收到（AI 网络较慢，可重跑）")

        _, ext_msgs4 = await drain(ext, 0.3)
        closes = [m for m in ext_msgs4 if m.get("command") == "ACTION_REPLY_CLOSE"]
        final_snap, _ = await drain(mobile, 0.5)
        snap_for_check = final_snap if final_snap is not None else post_snap
        # DeepSeek 生成结束语偶尔超过上面的等待窗口：再等到"会话真的消失"
        # 或超时为止（最多再等 ~20 秒），避免把"AI 还没返回"误判成"关单功能坏了"。
        for _ in range(10):
            if snap_for_check is not None and GID not in convs(snap_for_check):
                break
            if status_msg is not None:
                break
            try:
                m = await asyncio.wait_for(mobile.receive_json(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "FULL_SYNC":
                snap_for_check = m.get("data") or {}
            elif m.get("type") == "AI_STATUS":
                status_msg = m
                break
        still_there = (GID in convs(snap_for_check)) if snap_for_check else None

        if status_msg and status_msg.get("status") == "closed":
            check("探针收到 ACTION_REPLY_CLOSE 指令", bool(closes), str(closes[:1]))
            if closes:
                c = closes[-1]
                check("关单指令带结束语", bool(c.get("content")), str(c.get("content"))[:40])
                check("关单指令带分类关键字", bool(c.get("category")), str(c.get("category")))
                check("关单指令带分类路径", isinstance(c.get("categoryPath"), list), str(c.get("categoryPath")))
            check("关单后会话已从列表移除", still_there is False,
                  "仍在列表中" if still_there else "已移除")
        else:
            # AI 不可用时必须是"安全失败"：绝不误删会话
            check("AI 不可用时未误删会话（安全失败）", still_there is True,
                  str(status_msg) if status_msg else "")
            check("失败原因有明确提示",
                  bool(status_msg and status_msg.get("message")), str(status_msg))

        # ---------- 5. 探针在线状态 + IM 状态上报 ----------
        print("\n[5] 探针在线状态与 IM 状态上报")
        await mobile.send_json({"action": "REQUEST_SNAPSHOT"})
        snapA, _ = await drain(mobile, 0.4)
        check("探针在线时 extension_online=true",
              (snapA or {}).get("extension_online") is True,
              str((snapA or {}).get("extension_online")))

        await ext.send_json({"event": "IM_STATUS", "data": {"status": 3, "manual": True}})
        await asyncio.sleep(0.4)
        snapB, _ = await drain(mobile, 0.4)
        check("探针上报手动离线 -> im_status=3",
              (snapB or {}).get("im_status") == 3, str((snapB or {}).get("im_status")))

        await ext.send_json({"event": "IM_STATUS", "data": {"status": 2, "manual": False}})
        await asyncio.sleep(0.4)
        snapC, _ = await drain(mobile, 0.4)
        check("探针上报忙碌 -> im_status=2",
              (snapC or {}).get("im_status") == 2, str((snapC or {}).get("im_status")))

        # 关掉电脑端网页 -> 手机必须能看出探针已离线（否则会一直显示过期状态）
        await ext.close()
        await asyncio.sleep(0.9)
        snapD, _ = await drain(mobile, 0.6)
        if external_probe:
            # 另有真实探针在线（例如你自己开着的客服工作台）：extension_online 本就该保持 true
            check("另有真实探针在线，跳过「断开后必须为 false」断言（多客户端场景）", True,
                  f"expected extension_online=true, got {str((snapD or {}).get('extension_online'))}")
            check("多客户端下断开我们这条连接不会误报离线",
                  (snapD is None) or (snapD.get("extension_online") is True),
                  str((snapD or {}).get("extension_online")))
        else:
            check("探针断开后 extension_online=false",
                  (snapD or {}).get("extension_online") is False,
                  str((snapD or {}).get("extension_online")))

        if not mobile.closed:
            await mobile.close()

    print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))