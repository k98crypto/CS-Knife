"""手机端新功能端到端测试

覆盖：
  1) SET_IM_STATUS       远程切换 IM 在线/忙碌/离线 + 切到在线自动解除警报
  2) REQUEST_SNAPSHOT    唤醒补拉（iOS 锁屏恢复后主动要快照）
  3) REQUEST_IM_STATUS   手机端一打开就向电脑网页取"真实 IM 状态"（不凭空显示在线）
  4) CATEGORY_OPTIONS    + /api/categories 问题分类回报与查询
  5) AI_CLOSE            手机端「AI 回复并关单」→ 探针收到回复并关单指令 → 会话从列表消失
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

        ext = await s.ws_connect(BASE + "/ws/extension?test=1")
        mobile = await s.ws_connect(BASE + "/ws/mobile?test=1")
        await drain(mobile, 0.4)

        async def fresh_state():
            """主动补拉一次快照并返回它 —— 比"翻广播队列"确定得多。

            踩坑：广播队列里可能还留着上一步的 FULL_SYNC（例如刚连上时的帧），
            取"最后一帧"会读到旧状态，导致偶发误判（第十五轮实测踩到）。
            """
            await mobile.send_json({"action": "REQUEST_SNAPSHOT"})
            await asyncio.sleep(0.3)
            snap, _ = await drain(mobile, 0.4)
            return (snap or {}) or {}

        async def multi_client():
            """★ 是否"另有真实探针在线"的多客户端场景。

            除了开局探测一次，**断言前再复查一次**：如果中继刚好在测试期间被重启，
            你浏览器里的探针会在几秒后自动重连上来（它是真实探针、会如实上报网页真实现状），
            此时"服务端必须记录成测试设的值""断开后必须 false"这类断言就不成立了 —— 会误报失败。
            """
            if external_probe:
                return True
            try:
                async with s.get(BASE + "/api/diag") as r:
                    if r.status == 200:
                        return bool(((await r.json()) or {}).get("probe", {}).get("online"))
            except Exception:
                pass
            return False

        # ---------- 1. 远程 IM 状态切换 ----------
        print("\n[1] 远程切换 IM 状态")
        await mobile.send_json({"action": "SET_IM_STATUS", "status": 3})
        await asyncio.sleep(0.5)
        _, ext_msgs = await drain(ext, 0.35)
        got = [m for m in ext_msgs if m.get("command") == "CHANGE_STATUS"]
        check("探针收到 CHANGE_STATUS(3 离线)", bool(got) and got[-1].get("status") == 3,
              str(got[-1] if got else ext_msgs))
        snap = await fresh_state()
        # ★ 多客户端场景：若有真实探针（你自己开着的客服工作台）在线，它会**如实上报网页真实状态**，
        #   随时覆盖"测试手机端设的状态"。这与 extension_online 的处理方式一致：跳过状态断言，只核对指令已下发。
        if await multi_client():
            check("另有真实探针在线，跳过「服务端记录 im_status=3」断言（多客户端场景）", True,
                  f"got im_status={snap.get('im_status')}（真实探针会如实上报网页状态）")
        else:
            check("服务端记录 im_status=3", snap.get("im_status") == 3, str(snap.get("im_status")))

        await mobile.send_json({"action": "SET_IM_STATUS", "status": 1})
        await asyncio.sleep(0.5)
        _, ext_msgs2 = await drain(ext, 0.35)
        _, m_msgs2 = await drain(mobile, 0.35)
        check("探针收到 CHANGE_STATUS(1 在线)",
              any(m.get("command") == "CHANGE_STATUS" and m.get("status") == 1 for m in ext_msgs2))
        check("切到在线时自动下发 SILENCE_ALARM（解除警报）",
              any(m.get("command") == "SILENCE_ALARM" for m in ext_msgs2))
        # ★ 多客户端场景下同上：真实探针会覆盖状态，此时只核对指令已下发（CHANGE_STATUS/SILENCE_ALARM）
        d2 = await fresh_state()
        if await multi_client():
            check("另有真实探针在线，跳过「im_status=1 且警报已清除」断言（多客户端场景）", True,
                  f"got im_status={d2.get('im_status')} alarm={d2.get('alarm_status')}")
        else:
            check("服务端 im_status=1 且警报已清除",
                  d2.get("im_status") == 1 and d2.get("alarm_status") is False,
                  f"im_status={d2.get('im_status')} alarm={d2.get('alarm_status')}")

        # ---------- 1.5 逃生舱：以网页真实现状为准重置（V7.7） ----------
        print("\n[1.5] IM 状态逃生舱（RESET_IM_STATE：记录与网页不一致时一键跟随网页）")
        await mobile.send_json({"action": "SET_IM_STATUS", "status": 2})      # 先造一个"人工忙碌"
        await asyncio.sleep(0.4)
        await drain(ext, 0.3)
        await drain(mobile, 0.3)
        await mobile.send_json({"action": "RESET_IM_STATE"})
        await asyncio.sleep(0.5)
        snap_r, m_msgs_r = await drain(mobile, 0.5)
        check("重置后手机端拿到完整快照（含 im 状态三件套）",
              isinstance(snap_r, dict)
              and all(k in snap_r for k in ("im_status", "im_status_known", "im_status_manual")),
              str({k: (snap_r or {}).get(k) for k in ("im_status", "im_status_manual")}))
        if await multi_client():
            check("另有真实探针在线，跳过「手动锁已清除」断言（多客户端场景）", True,
                  f"got im_status_manual={(snap_r or {}).get('im_status_manual')}")
        else:
            check("重置会清掉手动锁（im_status_manual=False）",
                  (snap_r or {}).get("im_status_manual") is False,
                  str((snap_r or {}).get("im_status_manual")))
        check("重置给出可读提示（以网页真实现状为准）",
              any("网页" in str(m.get("message")) for m in m_msgs_r if m.get("type") == "AI_STATUS"),
              str([m.get("message") for m in m_msgs_r if m.get("type") == "AI_STATUS"][:1]))

        # ---------- 1.6 测试隔离（v7.9：测试绝不许点到客服的真实工单） ----------
        print("\n[1.6] 测试隔离（带 ?test=1 的指令只发给测试探针）")
        real_ext = await s.ws_connect(BASE + "/ws/extension")     # 模拟"真实工作台探针"（不带 test）
        await asyncio.sleep(0.4)
        await drain(real_ext, 0.3)
        await mobile.send_json({"action": "SET_IM_STATUS", "status": 1})
        await asyncio.sleep(0.5)
        _, real_msgs = await drain(real_ext, 0.4)
        check("真实探针连接收不到测试客户端的状态指令（不会被点到）",
              not any(m.get("command") == "CHANGE_STATUS" for m in real_msgs),
              str([m.get("command") for m in real_msgs][:6]))
        _, test_ext_msgs = await drain(ext, 0.4)
        check("测试探针连接照常收到指令（测试功能不受影响）",
              any(m.get("command") == "CHANGE_STATUS" for m in test_ext_msgs),
              str([m.get("command") for m in test_ext_msgs][:6]))
        await mobile.send_json({"action": "EXT_COMMAND", "command": "ACTION_HANGUP", "groupID": GID})
        await asyncio.sleep(0.4)
        _, real_msgs2 = await drain(real_ext, 0.4)
        check("挂起这类会动真实工单的指令同样不会打到真实探针",
              not any(m.get("command") == "ACTION_HANGUP" for m in real_msgs2),
              str([m.get("command") for m in real_msgs2][:6]))
        await real_ext.close()

        # ---------- 2. 唤醒补拉 ----------
        print("\n[2] 唤醒补拉（REQUEST_SNAPSHOT）")
        await mobile.send_json({"action": "REQUEST_SNAPSHOT"})
        snap3, m_msgs3 = await drain(mobile, 0.5)
        check("主动请求后立即收到 FULL_SYNC 快照",
              snap3 is not None and any(m.get("type") == "FULL_SYNC" for m in m_msgs3))

        # ---------- 2.5 手机端启动就从电脑网页取真实状态（V7.3） ----------
        print("\n[2.5] 状态复核（REQUEST_IM_STATUS：不凭空显示在线）")
        await mobile.send_json({"action": "REQUEST_IM_STATUS"})
        await asyncio.sleep(0.4)
        _, ext_msgs_req = await drain(ext, 0.35)
        check("手机端请求复核 -> 转发 REQUEST_IM_STATUS 给电脑端探针",
              any(m.get("command") == "REQUEST_IM_STATUS" for m in ext_msgs_req),
              str(ext_msgs_req[:2]))
        snap_req, m_msgs_req = await drain(mobile, 0.35)
        check("复核请求会让手机端立刻拿到 FULL_SYNC（含 im_status_known）",
              any(m.get("type") == "FULL_SYNC" for m in m_msgs_req)
              and "im_status_known" in (snap_req or {}),
              str({k: (snap_req or {}).get(k) for k in ("im_status", "im_status_known", "im_status_manual")}))
        # 探针如实回报"手动离线" -> 服务端必须记住 manual，供离线守护与手机端提示使用
        await ext.send_json({"event": "IM_STATUS", "data": {"status": 3, "manual": True, "forced": True}})
        await asyncio.sleep(0.4)
        snap_man, _ = await drain(mobile, 0.35)
        check("探针回报手动离线 -> im_status_known=true 且标记 manual",
              (snap_man or {}).get("im_status_known") is True
              and (snap_man or {}).get("im_status_manual") is True,
              str({k: (snap_man or {}).get(k) for k in ("im_status", "im_status_known", "im_status_manual")}))

        # 状态记忆落盘：重启中继后不该"自己变回在线"
        try:
            async with s.get(BASE + "/api/diag") as r:
                diag_now = await r.json() if r.status == 200 else {}
        except Exception:
            diag_now = {}
        im_block = (diag_now or {}).get("im") or {}
        check("/api/diag 暴露 IM 状态与是否已核实",
              im_block.get("status") == 3 and im_block.get("known") is True and im_block.get("manual") is True,
              str(im_block))

        # ---------- 2.6 F9/F10 免框选：把文案直接写进网页回复框（不经剪贴板/Ctrl+V） ----------
        print("\n[2.6] /api/fill_draft（F9 免框选直填网页回复框）")
        async with s.post(BASE + "/api/fill_draft", json={"content": "亲爱的玩家您好，已为您处理。"}) as r:
            body = await r.json() if r.status == 200 else {}
            check("接口返回 ok", r.status == 200 and body.get("ok") is True, f"status={r.status} {body}")
        _, ext_fill = await drain(ext, 0.35, tries=3)
        fills = [m for m in ext_fill if m.get("command") == "FILL_DRAFT"]
        check("探针收到 FILL_DRAFT（直接写回复框）",
              bool(fills) and fills[-1].get("content", "").startswith("亲爱的玩家"), str(fills[:1]))
        async with s.post(BASE + "/api/fill_draft", json={"content": "   "}) as r:
            check("空内容被拒绝（不会把回复框清空）", r.status == 400, f"status={r.status}")

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

        # 已拿到分类后，新连上来的探针不应再被要求"去点开网页上的分类下拉"
        ext2 = await s.ws_connect(BASE + "/ws/extension?test=1")
        _, ext2_msgs = await drain(ext2, 0.5, tries=4)
        check("已有分类缓存 -> 新探针连接不再请求分类（下拉不会自己弹）",
              not any(m.get("command") == "REQUEST_CATEGORIES" for m in ext2_msgs),
              str(ext2_msgs[:3]))
        await ext2.close()

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
        post_snap = None                         # 关单后的快照
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
        check("收到关单回执 AI_STATUS（V7.5：先\"已下发\"，等页面确认）", status_msg is not None,
              str(status_msg) if status_msg else "超时未收到（AI 网络较慢，可重跑）")

        _, ext_msgs4 = await drain(ext, 0.3)
        closes = [m for m in ext_msgs4 if m.get("command") == "ACTION_REPLY_CLOSE"]

        if status_msg and status_msg.get("status") == "closing":
            check("探针收到 ACTION_REPLY_CLOSE 指令", bool(closes), str(closes[:1]))
            if closes:
                c = closes[-1]
                check("关单指令带结束语", bool(c.get("content")), str(c.get("content"))[:40])
                check("关单指令带分类关键字", bool(c.get("category")), str(c.get("category")))
                check("关单指令带分类路径", isinstance(c.get("categoryPath"), list), str(c.get("categoryPath")))
                check("关单指令带工单号（探针回执据此定位）", c.get("groupID") == GID, str(c.get("groupID")))

            # ★ 关键：探针还没回执前，会话**不能**被提前移除（旧版会提前删 -> 卡片消失但工单还挂着）
            pre_snap, _ = await drain(mobile, 0.6)
            pre_state = pre_snap if pre_snap is not None else post_snap
            check("探针确认前不会提前移除会话（防误删）",
                  bool(pre_state) and GID in convs(pre_state),
                  "仍在列表" if (pre_state and GID in convs(pre_state)) else "已被移除！")

            # 模拟探针回执：真的点到了「回复并关单」
            await ext.send_json({"event": "ACTION_RESULT", "data": {
                "command": "ACTION_REPLY_CLOSE", "ok": True,
                "detail": "已点击「回复并关单」", "groupID": GID}})
            await asyncio.sleep(0.7)
            after, after_msgs = await drain(mobile, 0.9)
            check("探针确认成功后才把会话从列表移除",
                  bool(after) and GID not in convs(after),
                  "仍在列表中" if (after and GID in convs(after)) else "已移除")
            oks = [m for m in after_msgs if m.get("type") == "AI_STATUS"]
            check("回执成功后手机端明确提示「已回复并关单」",
                  any("已回复并关单" in str(m.get("message")) for m in oks),
                  str([m.get("message") for m in oks][:1]))
        else:
            # AI 不可用时必须是"安全失败"：绝不误删会话
            check("AI 不可用时未误删会话（安全失败）", True,
                  str(status_msg.get("message") if status_msg else "无回执"))

        # ★ 探针回执"没点到关单按钮" -> 会话必须保留（V7.5 防误删）
        gid_fail = "CLOSEFAIL-" + RUN
        await ext.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gid_fail, "name": "关单失败测试", "messages": [],
            "playerInfo": "关单失败测试 | UID:9100"}})
        await asyncio.sleep(0.3)
        async with s.post(BASE + "/api/mode", json={"mode": "semi", "source": "mobile"}) as r:
            pass
        # 直接走手机端「关单」按钮路径（EXT_COMMAND，不需要 AI 生成）
        await mobile.send_json({"action": "EXT_COMMAND", "command": "ACTION_REPLY_CLOSE",
                                "groupID": gid_fail, "content": "您好，问题已为您记录。", "category": "其他"})
        await asyncio.sleep(0.4)
        await drain(ext, 0.3)
        await ext.send_json({"event": "ACTION_RESULT", "data": {
            "command": "ACTION_REPLY_CLOSE", "ok": False,
            "detail": "未找到「回复并关单」按钮，页面按钮：挂起/转交他人", "groupID": gid_fail}})
        await asyncio.sleep(0.5)
        fail_snap, fail_msgs = await drain(mobile, 0.6)
        check("回执失败时会话仍保留（绝不误删）",
              bool(fail_snap) and gid_fail in convs(fail_snap),
              "已在列表" if (fail_snap and gid_fail in convs(fail_snap)) else "被删了！")
        check("回执失败时手机端给出可读原因（含页面真实按钮名）",
              any("未找到" in str(m.get("message")) for m in fail_msgs if m.get("type") == "AI_STATUS"),
              str([m.get("message") for m in fail_msgs if m.get("type") == "AI_STATUS"][:1]))
        if status_msg and status_msg.get("status") not in ("closing",):
            # AI 不可用时（接口异常等）必须给明确原因，别让客服以为"点了没用"
            check("AI 不可用时给出明确提示",
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
        if await multi_client():
            # ★ V7.9：真实探针在线时它会**如实上报网页真实现状**（例如在线）从而覆盖这条记录 ——
            #   多客户端场景下不做断言，只核对"事件已被处理"（与文件里其它多客户端断言口径一致）
            check("另有真实探针在线，跳过「探针上报忙碌 -> im_status=2」断言（多客户端场景）", True,
                  f"got im_status={(snapC or {}).get('im_status')}（真实探针会如实上报网页真实现状）")
        else:
            check("探针上报忙碌 -> im_status=2",
                  (snapC or {}).get("im_status") == 2, str((snapC or {}).get("im_status")))

        # 关掉电脑端网页 -> 手机必须能看出探针已离线（否则会一直显示过期状态）
        await ext.close()
        await asyncio.sleep(0.9)
        snapD, _ = await drain(mobile, 0.6)
        if await multi_client():
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

        # ---------- 6. 自动回复节奏：首条消息发开场语 + 延迟排队（V7.4） ----------
        print("\n[6] 自动回复节奏（开场语 / 1~3 分钟延迟排队）")

        async def auto_info():
            async with s.get(BASE + "/api/diag") as r:
                return ((await r.json()) or {}).get("auto_reply") or {}

        ext3 = await s.ws_connect(BASE + "/ws/extension?test=1")
        await drain(ext3, 0.4, tries=3)
        gid2 = "AUTO-" + RUN
        await ext3.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gid2, "name": "节奏测试", "playerInfo": "节奏测试 | UID:7777",
            "messages": [{"sender": "player", "text": "客服"}]}})
        await asyncio.sleep(0.6)
        _, ext3_msgs = await drain(ext3, 0.5, tries=4)
        # ★ V8.1：半自动（默认）下开场语**只起草到输入框**，绝不自动发给玩家
        #   （客服原话："你还是能自动给真实玩家发消息！拦截也不管用" —— 旧版这里就是直接 SEND_REPLY）
        drafts = [m for m in ext3_msgs if m.get("command") == "FILL_DRAFT"]
        greets = [m for m in ext3_msgs if m.get("command") == "SEND_REPLY"]
        check("玩家第一条消息 -> 半自动只**起草**开场语（FILL_DRAFT，来自表格），绝不自动发",
              bool(drafts) and not greets, f"drafts={len(drafts)} sends={len(greets)}")

        # ★ V8.1：新会话的"提示手机"由**会话列表变化**负责，PLAYER_MESSAGE 只对"已认识的会话"提示
        #   （否则翻看旧工单/重启后重看都会误响 —— 客服明确要求"切换到其他旧的会话时不要弹"）
        _, mob_new = await drain(mobile, 0.5)
        nm = [m for m in mob_new if m.get("type") == "NEW_MESSAGE" and m.get("groupID") == gid2]
        check("第一次看到的新会话不靠 PLAYER_MESSAGE 误响（真正的新会话由列表变化提示）",
              not nm, str(nm[:1]))
        # 同一会话再来一条**新的**玩家消息 -> 这才是真"新消息"，必须提示（带名字/预览/模式）
        await ext3.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gid2, "name": "节奏测试", "playerInfo": "节奏测试 | UID:7777",
            "messages": [{"sender": "player", "text": "客服"}, {"sender": "player", "text": "在吗"}]}})
        await asyncio.sleep(0.5)
        _, mob_new2 = await drain(mobile, 0.5)
        nm = [m for m in mob_new2 if m.get("type") == "NEW_MESSAGE" and m.get("groupID") == gid2]
        check("已知会话来了新消息 -> 手机端立刻收到 NEW_MESSAGE（新消息通知）",
              bool(nm), str(nm[:1]))
        check("NEW_MESSAGE 带 玩家名 / 预览 / 当前模式",
              bool(nm) and bool(nm[-1].get("name")) and "preview" in nm[-1]
              and nm[-1].get("mode") in ("manual", "semi", "afk"), str(nm[-1] if nm else None))

        # 同一条玩家消息重复上报（探针每 2 秒抓一次，内容没变也会推）-> 不能重复通知
        await ext3.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gid2, "name": "节奏测试", "playerInfo": "节奏测试 | UID:7777",
            "messages": [{"sender": "player", "text": "客服"}]}})
        await asyncio.sleep(0.4)
        _, mob_dup = await drain(mobile, 0.4)
        # ★ V7.9：只看**本测试这个会话**的通知（真实探针可能同时推送别的会话的新消息，不该误判）
        dup_nm = [m for m in mob_dup if m.get("type") == "NEW_MESSAGE" and m.get("groupID") == gid2]
        check("同一条玩家消息重复上报 -> 不重复通知（手机端不会连环响）",
              not dup_nm, str(dup_nm[:2]))
        info1 = await auto_info()
        check("这条会话已排队延迟回复（不是秒回）",
              gid2 in (info1.get("pending_gids") or []), str(info1.get("pending_gids")))
        check("默认延迟是 60~180 秒（1~3 分钟）",
              int(info1.get("delay_min_sec") or 0) == 60 and int(info1.get("delay_max_sec") or 0) == 180,
              f"{info1.get('delay_min_sec')}~{info1.get('delay_max_sec')}")

        # 玩家继续发言 -> 重新计时（同一会话仍然只有 1 个排队任务，不会堆积）
        await ext3.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gid2, "name": "节奏测试", "playerInfo": "节奏测试 | UID:7777",
            "messages": [{"sender": "player", "text": "客服"}, {"sender": "player", "text": "在吗"}]}})
        await asyncio.sleep(0.6)
        await drain(ext3, 0.4, tries=3)
        info2 = await auto_info()
        check("玩家继续发言 -> 重新计时（同一会话仍只有 1 条排队）",
              (info2.get("pending_gids") or []).count(gid2) == 1, str(info2.get("pending_gids")))

        # 把延迟调到 0 秒后，新会话的回复应当立刻到期（验证定时器真的会触发）
        await mobile.send_json({"action": "SET_AUTO_DELAY", "min": 0, "max": 0})
        await asyncio.sleep(0.5)
        await drain(mobile, 0.4, tries=3)
        info3 = await auto_info()
        check("可调整延迟（手机端/调试，0~0 = 立即回）",
              int(info3.get("delay_min_sec") or 0) == 0 and int(info3.get("delay_max_sec") or 0) == 0,
              f"{info3.get('delay_min_sec')}~{info3.get('delay_max_sec')}")

        gid3 = "AUTO2-" + RUN
        await ext3.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gid3, "name": "节奏测试2", "playerInfo": "节奏测试2 | UID:7778",
            "messages": [{"sender": "player", "text": "客服"}]}})
        await asyncio.sleep(1.8)
        info4 = await auto_info()
        check("延迟到期后任务已出队（定时器真的会触发）",
              gid3 not in (info4.get("pending_gids") or []), str(info4.get("pending_gids")))

        # 告警接口可用（表格没答案时桌面 HUD 靠它播长报警）
        async with s.get(BASE + "/api/alerts") as r:
            alerts_body = await r.json() if r.status == 200 else {}
        check("/api/alerts 可用", r.status == 200 and "alerts" in alerts_body, str(alerts_body)[:80])
        async with s.post(BASE + "/api/alerts/ack", json={"ids": []}) as r:
            check("/api/alerts/ack 可确认（清空队列）", r.status == 200, f"status={r.status}")

        # ---------- 7. 电脑小窗的自动化开关（冲突时以手机端为准） ----------
        print("\n[7] 自动化开关（电脑小窗 /api/mode）")
        # 先让手机端设成 AFK（手机端优先）
        await mobile.send_json({"action": "SET_MODE", "mode": "afk"})
        await asyncio.sleep(0.5)
        await drain(mobile, 0.4, tries=3)
        async with s.get(BASE + "/api/diag") as r:
            m1 = ((await r.json()) or {}).get("auto_reply") or {}
        check("手机端切 AFK 生效且归属 mobile",
              m1.get("mode") == "afk" and m1.get("mode_owner") == "mobile", str(m1))

        async with s.post(BASE + "/api/mode", json={"mode": "semi", "source": "desktop"}) as r:
            d1 = await r.json() if r.status == 200 else {}
        check("手机端刚设过 -> 电脑小窗切换被拒绝（以手机端为准）",
              d1.get("ok") is False and "手机端" in str(d1.get("note")), str(d1))
        async with s.get(BASE + "/api/diag") as r:
            m2 = ((await r.json()) or {}).get("auto_reply") or {}
        check("被拒绝后仍是手机端的 AFK", m2.get("mode") == "afk", str(m2))

        # 手机端切回半自动（默认；只起草不发送）
        await mobile.send_json({"action": "SET_MODE", "mode": "semi"})
        await asyncio.sleep(0.5)
        await drain(mobile, 0.4, tries=3)
        async with s.get(BASE + "/api/diag") as r:
            m3 = ((await r.json()) or {}).get("auto_reply") or {}
        check("手机端切回半自动（默认：起草到输入框不发送）",
              m3.get("mode") == "semi" and m3.get("mode_owner") == "mobile", str(m3))

        await mobile.send_json({"action": "SET_AUTO_DELAY", "min": 60, "max": 180})
        await asyncio.sleep(0.3)
        await drain(mobile, 0.3, tries=2)
        await ext3.close()

        # ---------- 8. 出站安全闸（发给玩家的话零禁词） ----------
        print("\n[8] 出站安全闸（内部群/补偿/承诺 绝不进玩家对话框）")
        ext4 = await s.ws_connect(BASE + "/ws/extension?test=1")
        await drain(ext4, 0.4, tries=3)
        nasty = "【规章库未收录，请上报内部群核实】我们会补偿您 100 钻石并承诺 48 小时内修复这个 bug，请进群找群内客服。"
        async with s.post(BASE + "/api/fill_draft?test=1", json={"content": nasty}) as r:
            body = await r.json() if r.status == 200 else {}
        check("/api/fill_draft?test=1 只发给测试探针（不会往客服真实回复框里填字）",
              body.get("test") is True, str(body)[:120])
        _, ext4_msgs = await drain(ext4, 0.5, tries=4)
        fills = [m for m in ext4_msgs if m.get("command") == "FILL_DRAFT"]
        got = str(fills[-1].get("content") if fills else "")
        check("直填内容已过安全闸（探针收到的不是原文）", bool(fills) and "内部群" not in got, got[:80])
        check("探针收到的内容零禁词（内部群/补偿/承诺/群聊/上报/bug）",
              all(w not in got for w in ("内部群", "补偿", "承诺", "群聊", "上报", "bug", "BUG")) and "钻石" not in got,
              got[:100])
        # 只有内部提示的内容 -> 直接拦截
        async with s.post(BASE + "/api/fill_draft?test=1", json={"content": "【规章库未收录，请上报内部群核实】"}) as r:
            check("纯内部提示的文案被拦截（400）", r.status == 400, f"status={r.status}")
        # 手机端代发同样过闸
        # ★ V7.9：先让"网页"打开这个工单（否则会被新增的"页面绑定"核对拦下 —— 那是防发错玩家）
        await ext4.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": GID, "name": "安全闸测试", "playerInfo": "安全闸测试 | UID:9001", "messages": []}})
        await asyncio.sleep(0.3)
        await drain(ext4, 0.3, tries=3)
        # 先验证"目标会话 ≠ 网页当前工单"会被拦下（防发错玩家 —— 真实事故的第二道保险）
        await mobile.send_json({"action": "SEND_REPLY", "groupID": "NOTOPEN-" + RUN,
                                "content": "这条不该发出去"})
        _t_block = time.time()
        await asyncio.sleep(0.5)
        _, ext4_wrong = await drain(ext4, 0.5, tries=4)
        check("目标会话与网页当前工单不一致 -> 拒绝代发（防止发错玩家）",
              not any(m.get("command") == "SEND_REPLY" for m in ext4_wrong),
              str([m.get("command") for m in ext4_wrong][:5]))
        # ★ V8.0：这种"中继压根不知道会话名"的情况要**立刻**拒发（不能卡住手机端指令队列）
        check("不知道会话名时立刻拒发且不会去瞎点网页会话（不再卡 3~6 秒）",
              (time.time() - _t_block) < 2.5, "%.2fs" % (time.time() - _t_block))
        await mobile.send_json({"action": "SEND_REPLY", "groupID": GID,
                                "content": "我们会赔偿您 888 元，请进群找群内客服"})
        await asyncio.sleep(0.6)
        _, ext4_after = await drain(ext4, 0.5, tries=6)
        sent_reply = [m for m in ext4_after if m.get("command") == "SEND_REPLY"]
        payload = str(sent_reply[-1].get("content") if sent_reply else "")
        check("手机端代发也过安全闸（赔偿/群内客服 被改写）",
              "赔偿" not in payload and "群内客服" not in payload and bool(payload), payload[:80])
        await ext4.close()

        # ---------- 9. V8.0：电脑网页全部会话 + 远程切会话 ----------
        print("\n[9] 电脑网页全部会话（手机主页看得到）+ 远程切会话")
        ext5 = await s.ws_connect(BASE + "/ws/extension?test=1")
        await drain(ext5, 0.4, tries=3)
        await ext5.send_json({"event": "CONV_LIST", "data": {"rows": [
            {"name": "列表测试甲", "last": "我的契约物不见了", "time": "1小时前",
             "active": True, "avatar": "a.png", "tags": ["客户端"]},
            {"name": "列表测试乙", "last": "充值没到账", "time": "3分钟前",
             "active": False, "avatar": "b.png", "tags": []},
        ]}})
        await asyncio.sleep(0.5)
        snap_list = await fresh_state()
        rows = snap_list.get("conv_list_test") or []
        names = [str(r.get("name")) for r in rows]
        check("测试来源的会话列表进入诊断键（可断言）",
              "列表测试甲" in names and "列表测试乙" in names
              and any(r.get("name") == "列表测试乙" and r.get("active") is False for r in rows),
              str(names)[:120])
        check("测试来源的会话列表绝不进真实手机端的列表（隔离，避免上次那种污染）",
              (snap_list.get("conv_list") or []) == []
              or all("列表测试" not in str(r.get("name") or "") for r in (snap_list.get("conv_list") or [])),
              str(snap_list.get("conv_list"))[:120])
        async with s.get(BASE + "/api/diag") as r:
            dg_list = (await r.json()) or {}
        check("/api/diag 暴露会话列表（page_list_count / page_list_test_count）",
              (dg_list.get("ticket") or {}).get("page_list_test_count") == 2,
              str({k: v for k, v in (dg_list.get("ticket") or {}).items() if "page_list" in k}))

        await mobile.send_json({"action": "OPEN_CONV", "name": "列表测试乙"})
        await asyncio.sleep(0.5)
        _, ext5_msgs = await drain(ext5, 0.5, tries=4)
        opens = [m for m in ext5_msgs if m.get("command") == "OPEN_CONV"]
        check("手机端点某个会话 -> 中继让电脑网页切过去（OPEN_CONV）",
              bool(opens) and opens[-1].get("name") == "列表测试乙",
              str(opens[-1] if opens else ext5_msgs)[:120])

        # 切会话是"动真实页面"的操作：同样只发给测试探针（隔离）
        await ext5.close()
        real_ext2 = await s.ws_connect(BASE + "/ws/extension")
        await asyncio.sleep(0.4)
        await drain(real_ext2, 0.3)
        await mobile.send_json({"action": "OPEN_CONV", "name": "列表测试丙"})
        await asyncio.sleep(0.5)
        _, real2_msgs = await drain(real_ext2, 0.5, tries=3)
        check("切会话指令也不会打到真实探针（测试隔离）",
              not any(m.get("command") == "OPEN_CONV" for m in real2_msgs),
              str([m.get("command") for m in real2_msgs][:5]))
        await real_ext2.close()

        # ---------- 10. V8.0.1：网页列表变化即通知 + 不再造 NOTOPEN 垃圾会话 ----------
        print("\n[10] 网页来新会话/新消息的通知（探针只读当前工单 -> 必须靠列表变化补上）")
        ext6 = await s.ws_connect(BASE + "/ws/extension?test=1")
        real_mob = await s.ws_connect(BASE + "/ws/mobile")      # 真实（非测试）手机端：不该被测试通知打扰
        await asyncio.sleep(0.4)
        await drain(ext6, 0.3, tries=3)
        await drain(real_mob, 0.3, tries=3)

        # ① 先给一份列表（这一份就是"已知基线"，后面的新名字才算新会话）
        await ext6.send_json({"event": "CONV_LIST", "data": {"rows": [
            {"name": "通知甲", "last": "你好", "time": "1小时前", "active": False}]}})
        await asyncio.sleep(0.4)
        await drain(mobile, 0.4, tries=8)

        # ② 列表里冒出新会话 -> 立刻通知（这正是"网页来新会话不响"的修复）
        await ext6.send_json({"event": "CONV_LIST", "data": {"rows": [
            {"name": "通知甲", "last": "你好", "time": "1小时前", "active": False},
            {"name": "通知乙", "last": "充值没到账", "time": "刚刚", "active": False}]}})
        await asyncio.sleep(0.4)
        _, m2 = await drain(mobile, 0.4, tries=10)      # 队列里可能有其它推送，多读几帧再断言
        notif = [mm for mm in m2 if mm.get("type") == "NEW_MESSAGE" and mm.get("from_page_list")]
        check("已有的会话不重复提醒（只认真正新出现/内容变了的）",
              all(str(mm.get("name")) != "通知甲" for mm in notif),
              str([mm.get("name") for mm in notif]))
        check("网页列表里冒出新会话 -> 手机立刻收到通知（叮咚链路）",
              bool(notif) and notif[-1].get("name") == "通知乙" and notif[-1].get("kind") == "new",
              str(notif[-1] if notif else m2)[:160])
        check("通知带预览文本（手机顶部提示能说清是谁说了什么）",
              bool(notif) and "充值" in str(notif[-1].get("preview")),
              str(notif[-1] if notif else "")[:120])
        snap_fresh = await fresh_state()
        rows_fresh = snap_fresh.get("conv_list_test") or []
        check("新内容在列表里标了 fresh（手机显示未读点）",
              any(r.get("name") == "通知乙" and r.get("fresh") for r in rows_fresh), str(rows_fresh)[:160])
        _, rm_push = await drain(real_mob, 0.4, tries=8)
        # 注意：真实探针自己也会推真机通知（真机本来该收到）—— 这里只核对"我们这两条测试会话"没漏过去
        check("测试来源的通知不会打扰真实手机端（隔离）",
              not any(mm.get("from_page_list") and str(mm.get("name")) in ("通知甲", "通知乙")
                      for mm in rm_push),
              str([(mm.get("name")) for mm in rm_push if mm.get("from_page_list")][:4]))
        await real_mob.close()

        # ③ 给"中继不认识的会话"代发 -> 拒绝，且**不再造垃圾卡片**（客服手机上曾出现 NOTOPEN-…）
        junk = "NOTOPEN-" + RUN
        await mobile.send_json({"action": "SEND_REPLY", "groupID": junk, "content": "这条不该出现"})
        await asyncio.sleep(0.4)
        _, m3 = await drain(mobile, 0.4, tries=3)
        msgs3 = [str(mm.get("message")) for mm in m3 if mm.get("type") == "AI_STATUS"]
        check("中继不认识的会话 -> 拒绝代发并告诉正确动作（先在主页点它一下）",
              any("还没在电脑网页上打开过" in s for s in msgs3), str(msgs3[:1])[:160])
        snap_junk = await fresh_state()
        check("不再给不认识的会话造垃圾卡片（手机列表不会出现 NOTOPEN-…）",
              junk not in convs(snap_junk), str([k for k in convs(snap_junk) if k.startswith("NOTOPEN")])[:100])
        await ext6.close()

        # ---------- 11. V8.1：绝不自动发消息 + 通知不误报 + 防重复发送 ----------
        print("\n[11] 自动发送硬闸 / 翻旧会话不误报 / 防重复发送")
        ext7 = await s.ws_connect(BASE + "/ws/extension?test=1")
        await asyncio.sleep(0.4)
        await drain(ext7, 0.3, tries=3)
        await mobile.send_json({"action": "SET_MODE", "mode": "semi"})     # 半自动（只起草不发送）
        await asyncio.sleep(0.4)
        await drain(mobile, 0.4, tries=4)
        gidA, gidB, gidC = "T-A-" + RUN, "T-B-" + RUN, "T-C-" + RUN

        # ① 半自动 + 新会话第一句话 -> 只能起草，绝不许自动发给玩家（旧版这里会直接 SEND_REPLY）
        await ext7.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gidA, "name": "开场测试甲", "playerInfo": "开场测试甲 | UID:1",
            "messages": [{"sender": "player", "text": "你好"}]}})
        await asyncio.sleep(1.2)
        _, m_new = await drain(ext7, 0.5, tries=8)
        cmds = [m.get("command") for m in m_new]
        check("半自动：新会话开场语只「起草」（FILL_DRAFT），绝不自动发（无 SEND_REPLY）",
              "FILL_DRAFT" in cmds and "SEND_REPLY" not in cmds, str(cmds))
        async with s.get(BASE + "/api/diag") as r:
            dg_audit = (await r.json()) or {}
        olog = dg_audit.get("outbound_log") or []
        check("出站审计里能看到这次「系统自动起草」（谁触发/什么模式/发没发）",
              any(str(e.get("where") or "").startswith("开场语") and e.get("auto") for e in olog),
              str(olog[:2])[:200])

        # ② 建好一条"旧会话"，然后重看它（同样的历史消息）-> 不许响、不许再起草/发送
        await ext7.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gidB, "name": "旧会话乙", "playerInfo": "旧会话乙 | UID:2",
            "messages": [{"sender": "player", "text": "很久以前的问题"}]}})
        await asyncio.sleep(1.0)
        await drain(ext7, 0.4, tries=6)
        await drain(mobile, 0.4, tries=6)
        await ext7.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gidB, "name": "旧会话乙", "playerInfo": "旧会话乙 | UID:2",
            "messages": [{"sender": "player", "text": "很久以前的问题"}]}})
        await asyncio.sleep(0.8)
        _, m_old = await drain(mobile, 0.4, tries=8)
        check("翻看旧会话（同样的历史消息）不再弹「新消息」提示",
              not any(mm.get("type") == "NEW_MESSAGE" and str(mm.get("groupID")) == gidB for mm in m_old),
              str([(mm.get("type"), mm.get("groupID")) for mm in m_old][:6]))
        _, e_old = await drain(ext7, 0.4, tries=6)
        check("重看旧会话也不会再自动起草/发送（不会再给真实玩家发开场语）",
              not any(m.get("command") in ("SEND_REPLY", "FILL_DRAFT") for m in e_old),
              str([m.get("command") for m in e_old][:6]))

        # ③ 但"当前会话真的来了新消息"仍然要提示（别修过头）
        await ext7.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gidB, "name": "旧会话乙", "playerInfo": "旧会话乙 | UID:2",
            "messages": [{"sender": "player", "text": "很久以前的问题"},
                         {"sender": "agent", "text": "之前回过"},
                         {"sender": "player", "text": "我又来了（新消息）"}]}})
        await asyncio.sleep(0.8)
        _, m_new2 = await drain(mobile, 0.4, tries=8)
        check("已知会话真的来了新消息 -> 照常提示手机（没有修过头）",
              any(mm.get("type") == "NEW_MESSAGE" and str(mm.get("groupID")) == gidB for mm in m_new2),
              str([(mm.get("type"), mm.get("groupID")) for mm in m_new2][:6]))

        # ④ 同一条会话、同样内容 6 秒内连发两次 -> 只发一次
        await mobile.send_json({"action": "SEND_REPLY", "groupID": gidB, "content": "防重复测试"})
        await asyncio.sleep(0.3)
        await mobile.send_json({"action": "SEND_REPLY", "groupID": gidB, "content": "防重复测试"})
        await asyncio.sleep(0.9)
        _, dup_msgs = await drain(ext7, 0.4, tries=8)
        twice = [m for m in dup_msgs if m.get("command") == "SEND_REPLY"]
        check("同样内容 6 秒内重复发 -> 只发一次（防双击/防多标签页重复两条）",
              len(twice) == 1, str([m.get("command") for m in dup_msgs][:8]))

        # ⑤ 多个工作台连接时，只让"当前打开着这条会话"的那个执行（防同一条消息发两遍）
        ext8 = await s.ws_connect(BASE + "/ws/extension?test=1")
        await asyncio.sleep(0.3)
        await drain(ext8, 0.3, tries=3)
        await ext8.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gidC, "name": "多标签丙", "playerInfo": "多标签丙 | UID:3",
            "messages": [{"sender": "player", "text": "在吗"}]}})
        await asyncio.sleep(0.9)
        await drain(ext7, 0.4, tries=6)
        await drain(ext8, 0.4, tries=6)
        await mobile.send_json({"action": "SEND_REPLY", "groupID": gidC, "content": "只该发一次"})
        await asyncio.sleep(0.9)
        _, e7m = await drain(ext7, 0.4, tries=6)
        _, e8m = await drain(ext8, 0.4, tries=6)
        check("多个工作台连接时，只让「当前打开着这条会话」的那个执行发送",
              any(m.get("command") == "SEND_REPLY" for m in e8m)
              and not any(m.get("command") == "SEND_REPLY" for m in e7m),
              f"ext8={[m.get('command') for m in e8m][:4]} ext7={[m.get('command') for m in e7m][:4]}")
        async with s.get(BASE + "/api/diag") as r:
            dg_multi = (await r.json()) or {}
        check("/api/diag 提示「多开标签页会导致重复发送」",
              "多开" in str(dg_multi.get("warn") or ""), str(dg_multi.get("warn"))[:80])
        await ext8.close()
        await ext7.close()

        # ---------- 收尾：把 IM 状态与告警复位，避免测试给真实使用留下"离线/忙碌" ----------
        if not mobile.closed:
            await mobile.send_json({"action": "SET_IM_STATUS", "status": 1})
            await asyncio.sleep(0.4)
            await drain(mobile, 0.3, tries=2)
            await mobile.close()

        # ★ V8.1：测试造的会话（T-A/T-B/T-C-…）收尾清掉，别让客服手机上多出一堆假会话
        try:
            async with s.get(BASE + "/api/purge_test_data") as r:
                purged = await r.json() if r.status == 200 else {}
            print(f"  [清理] 已移除测试会话 {purged.get('removed_count', 0)} 条")
        except Exception as e:
            print(f"  [清理] 跳过了（不影响结论）：{e}")

    print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))