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

        ext = await s.ws_connect(BASE + "/ws/extension")
        mobile = await s.ws_connect(BASE + "/ws/mobile")
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
        if external_probe:
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
        if external_probe:
            check("另有真实探针在线，跳过「im_status=1 且警报已清除」断言（多客户端场景）", True,
                  f"got im_status={d2.get('im_status')} alarm={d2.get('alarm_status')}")
        else:
            check("服务端 im_status=1 且警报已清除",
                  d2.get("im_status") == 1 and d2.get("alarm_status") is False,
                  f"im_status={d2.get('im_status')} alarm={d2.get('alarm_status')}")

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
        ext2 = await s.ws_connect(BASE + "/ws/extension")
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

        # ---------- 6. 自动回复节奏：首条消息发开场语 + 延迟排队（V7.4） ----------
        print("\n[6] 自动回复节奏（开场语 / 1~3 分钟延迟排队）")

        async def auto_info():
            async with s.get(BASE + "/api/diag") as r:
                return ((await r.json()) or {}).get("auto_reply") or {}

        ext3 = await s.ws_connect(BASE + "/ws/extension")
        await drain(ext3, 0.4, tries=3)
        gid2 = "AUTO-" + RUN
        await ext3.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": gid2, "name": "节奏测试", "playerInfo": "节奏测试 | UID:7777",
            "messages": [{"sender": "player", "text": "客服"}]}})
        await asyncio.sleep(0.6)
        _, ext3_msgs = await drain(ext3, 0.5, tries=4)
        greets = [m for m in ext3_msgs if m.get("command") == "SEND_REPLY"]
        check("玩家第一条消息 -> 立刻发开场语（来自表格）",
              bool(greets) and ("亲" in str(greets[-1].get("content"))), str(greets[:1]))
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
        ext4 = await s.ws_connect(BASE + "/ws/extension")
        await drain(ext4, 0.4, tries=3)
        nasty = "【规章库未收录，请上报内部群核实】我们会补偿您 100 钻石并承诺 48 小时内修复这个 bug，请进群找群内客服。"
        async with s.post(BASE + "/api/fill_draft", json={"content": nasty}) as r:
            body = await r.json() if r.status == 200 else {}
        _, ext4_msgs = await drain(ext4, 0.5, tries=4)
        fills = [m for m in ext4_msgs if m.get("command") == "FILL_DRAFT"]
        got = str(fills[-1].get("content") if fills else "")
        check("直填内容已过安全闸（探针收到的不是原文）", bool(fills) and "内部群" not in got, got[:80])
        check("探针收到的内容零禁词（内部群/补偿/承诺/群聊/上报/bug）",
              all(w not in got for w in ("内部群", "补偿", "承诺", "群聊", "上报", "bug", "BUG")) and "钻石" not in got,
              got[:100])
        # 只有内部提示的内容 -> 直接拦截
        async with s.post(BASE + "/api/fill_draft", json={"content": "【规章库未收录，请上报内部群核实】"}) as r:
            check("纯内部提示的文案被拦截（400）", r.status == 400, f"status={r.status}")
        # 手机端代发同样过闸
        await mobile.send_json({"action": "SEND_REPLY", "groupID": GID,
                                "content": "我们会赔偿您 888 元，请进群找群内客服"})
        await asyncio.sleep(0.5)
        _, ext4_after = await drain(ext4, 0.5, tries=4)
        sent_reply = [m for m in ext4_after if m.get("command") == "SEND_REPLY"]
        payload = str(sent_reply[-1].get("content") if sent_reply else "")
        check("手机端代发也过安全闸（赔偿/群内客服 被改写）",
              "赔偿" not in payload and "群内客服" not in payload and bool(payload), payload[:80])
        await ext4.close()

        # ---------- 收尾：把 IM 状态与告警复位，避免测试给真实使用留下"离线/忙碌" ----------
        if not mobile.closed:
            await mobile.send_json({"action": "SET_IM_STATUS", "status": 1})
            await asyncio.sleep(0.4)
            await drain(mobile, 0.3, tries=2)
            await mobile.close()

    print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))