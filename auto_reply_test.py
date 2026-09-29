"""自动回复节奏 / 人工介入 的单元测试（不需要启动服务）

覆盖客服这轮提的需求：
  1) 玩家发来消息后**等 1~3 分钟**再回（随机），期间再来消息重新计时
  2) 只有"玩家最后发言且这条还没回过"才回（不抢话、不重复回）
  3) 开场语**严格取自表格**（话术库 tpl_no_desc）
  4) 发给 AI 的聊天记录要**自行判断是否带上客服发言**（最后一条是客服 -> 不回）
  5) 表格里没答案 -> 发安抚话术 + 长报警 + 置顶 + 复制玩家信息&总结
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import bridge_server as B

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


def conv_with(msgs, **kw):
    c = {"name": "测试玩家", "msgs": msgs}
    c.update(kw)
    return c


def p(text, ts=1000):
    return {"sender": "player", "text": text, "ts": ts}


def a(text, ts=2000):
    return {"sender": "agent", "text": text, "ts": ts}


print("=== 自动回复节奏 / 人工介入 测试 ===\n")

print("[1] 延迟 1~3 分钟随机（可配）")
B.state["auto_delay_min"] = 60
B.state["auto_delay_max"] = 180
delays = [B.pick_reply_delay() for _ in range(400)]
check("默认延迟落在 60~180 秒", all(60 <= d <= 180 for d in delays),
      f"min={min(delays):.1f} max={max(delays):.1f}")
check("确实是随机（不是固定值）", len(set(round(d) for d in delays)) > 20,
      f"不同取值 {len(set(round(d) for d in delays))} 个")
B.state["auto_delay_min"] = 0
B.state["auto_delay_max"] = 0
check("延迟可配成 0（调试/紧急时秒回）", B.pick_reply_delay() == 0)
B.state["auto_delay_min"] = 180
B.state["auto_delay_max"] = 60          # 故意反着填
d = B.pick_reply_delay()
check("上下限填反了也能正常工作", 60 <= d <= 180, f"{d:.1f}")
B.state["auto_delay_min"] = 60
B.state["auto_delay_max"] = 180

print("\n[2] 该不该由程序回复（不抢话 / 不重复回）")
check("玩家最后发言 -> 该回", B.should_auto_reply(conv_with([p("问题")])) is True)
check("客服最后发言 -> 不回（等玩家）",
      B.should_auto_reply(conv_with([p("问题"), a("答复")])) is False)
check("这条玩家消息已经回过 -> 不回（防重复）",
      B.should_auto_reply(conv_with([p("问题", 1000), a("答复", 2000)], last_reply_ts=1000)) is False)
check("玩家又发了新的 -> 该回",
      B.should_auto_reply(conv_with([p("问题", 1000), a("答复", 1500), p("补充", 2000)],
                                    last_reply_ts=1000)) is True)
check("空会话 -> 不回", B.should_auto_reply(conv_with([])) is False)
check("脏数据不炸", B.should_auto_reply({"msgs": "not-a-list"}) is False)

print("\n[3] 开场语严格取自表格")
greet = B.pick_greeting()
table_pool = [str(x).strip() for x in (getattr(B.core, "tpl_no_desc", None) or [])]
if table_pool:
    check("开场语来自话术库 tpl_no_desc（表格）", greet in table_pool, greet[:40])
    check("表格里有多条开场语时会随机挑", len(table_pool) >= 1, f"{len(table_pool)} 条候选")
else:
    check("表格未解析到开场语时用兜底句（不报错）", bool(greet), greet[:40])

print("\n[4] 发给 AI 的记录：自行判断是否带客服发言")
conv_id = "TEST-GID-" + str(int(__import__("time").time()))
B.state["companies"]["main"]["conversations"][conv_id] = conv_with(
    [a("上一轮开场白"), p("我的钻石没到账"), a("请提供截图"), p("截图发你了"), a("正在核实"), p("还没好吗")])
hist = B.build_chat_history_str(conv_id, for_ai=True)
check("客服最后发言时 -> 返回空（不抢话）", B.build_chat_history_str(conv_id + "-x", for_ai=True) == "")
B.state["companies"]["main"]["conversations"][conv_id]["msgs"] = [
    a("上一轮开场白"), p("我的钻石没到账"), a("请提供截图"), p("截图发你了")]
hist = B.build_chat_history_str(conv_id, for_ai=True)
check("玩家开口之前的客服发言被丢掉（不干扰 AI）", "上一轮开场白" not in hist, hist.replace("\n", " | ")[:70])
check("玩家发言全部保留", "我的钻石没到账" in hist and "截图发你了" in hist, hist.replace("\n", " | ")[:70])
check("玩家之后的客服发言保留（知道我们已经问过了）", "请提供截图" in hist, hist.replace("\n", " | ")[:70])
B.state["companies"]["main"]["conversations"][conv_id]["msgs"] = (
    [p("问题")] + [a(f"客服{i}") for i in range(5)] + [p("补充")])
hist = B.build_chat_history_str(conv_id, for_ai=True)
check("客服发言最多只留最近 2 条（省 token）", hist.count("【客服】") == 2, hist.replace("\n", " | ")[:90])
check("非 for_ai 模式保持原行为（关单等场景需要完整上下文）",
      B.build_chat_history_str(conv_id).count("【客服】") == 5)
del B.state["companies"]["main"]["conversations"][conv_id]

print("\n[5] 表格里没答案时的安抚话术与告警")
check("安抚话术就是客服指定的原话",
      B.HOLD_TEXT.startswith(B.HONORIFIC) and "您的问题我已经收到啦，正在为您查询相关信息" in B.HOLD_TEXT, B.HOLD_TEXT)
check("告警队列已就绪（桌面 HUD 轮询 /api/diag.alerts）", isinstance(B.state.get("human_alerts"), list))
check("中继暴露了延迟参数给手机端/诊断页",
      "auto_delay_min" in B.state and "auto_delay_max" in B.state,
      f"{B.state.get('auto_delay_min')}~{B.state.get('auto_delay_max')}")

print("\n[6] 自动化开关：默认半自动 + 手机端优先")
B.state["reply_mode"] = "semi"
B.state["mode_owner"] = "default"
B.state["mode_ts"] = 0
check("默认是半自动（起草到输入框，不发送）", B.state["reply_mode"] == "semi", B.state["reply_mode"])
check("半自动不发送（auto_draft=True 且 afk_mode=False）",
      B.state["auto_draft"] is True and B.state["afk_mode"] is False,
      f"auto_draft={B.state['auto_draft']} afk={B.state['afk_mode']}")

ok, note = B.apply_reply_mode("afk", source="mobile")
check("手机端可以切到 AFK", ok and B.state["reply_mode"] == "afk" and B.state["afk_mode"] is True, note)
check("手机端设置后 mode_owner=mobile", B.state["mode_owner"] == "mobile", B.state["mode_owner"])

ok2, note2 = B.apply_reply_mode("semi", source="desktop")
check("冲突时以手机端为准：电脑小窗在窗口内被拒绝", ok2 is False, note2)
check("被拒绝后状态仍是手机端的 AFK", B.state["reply_mode"] == "afk", B.state["reply_mode"])
check("拒绝理由里说明了原因", "手机端" in str(note2), note2)

ok3, note3 = B.apply_reply_mode("manual", source="mobile")
check("手机端再改（手动）照样生效", ok3 and B.state["reply_mode"] == "manual", note3)

# 手机端优先窗口过期后，电脑小窗可以切（否则小窗开关就废了）
B.state["mode_ts"] = 0
ok4, note4 = B.apply_reply_mode("semi", source="desktop")
check("优先窗口过期后电脑小窗可以切回半自动", ok4 and B.state["reply_mode"] == "semi", note4)
check("电脑小窗设置后归属记为 desktop", B.state["mode_owner"] == "desktop", B.state["mode_owner"])

ok5, note5 = B.apply_reply_mode("不存在的模式", source="mobile")
check("非法模式被拒绝", ok5 is False, note5)

check("小窗与手机端共用同一份权威状态（无本地副本）",
      hasattr(B, "apply_reply_mode") and "mode_owner" in B.state)
B.state["reply_mode"] = "semi"
B.state["mode_owner"] = "default"
B.state["mode_ts"] = 0
B.state["afk_mode"] = False
B.state["auto_draft"] = True
B.save_mode_state()          # 测试后把状态复位（避免影响真实使用）

print("\n[7] 出站安全闸：发给玩家的话绝不能出现内部群/补偿/承诺")
NASTY = ("【规章库未收录，请上报内部群核实】\n亲爱的玩家，这个 bug 我们会补偿您 100 钻石，"
         "并承诺 48 小时内修复，请进群联系群内客服处理，也可以按 F9 一键上报。")
clean, left = B.safe_outbound(NASTY, "测试")
forbidden = B.core.find_forbidden(clean)
check("清洗后零禁词（内部群/补偿/承诺/群聊/上报/BUG…）", forbidden == [], str(forbidden))
check("内部提示【规章库未收录…】整块被删除", "规章库未收录" not in clean and "内部群" not in clean, clean[:60])
check("补偿/承诺/时间承诺被改写成中性话术",
      "补偿" not in clean and "承诺" not in clean and "48 小时" not in clean, clean[:80])
check("BUG 改成异常情况", "bug" not in clean.lower() and "异常情况" in clean, clean[:80])
check("内部快捷键 F9 不再出现", "F9" not in clean, clean[:80])
check("清洗后仍是完整可发的话术（没被清空）", len(clean) > 10, clean)
check("补偿金额也一并抹掉（不留『…100 钻石』这类暗示）",
      "钻石" not in clean and "100" not in clean, clean[:100])
check("安抚话术本身零禁词", B.core.find_forbidden(B.HOLD_TEXT) == [], B.HOLD_TEXT)
check("开场语（表格话术）零禁词", B.core.find_forbidden(B.pick_greeting()) == [], B.pick_greeting())
check("find_forbidden 能抓到禁词（自检有效）", B.core.find_forbidden(NASTY) != [], str(B.core.find_forbidden(NASTY)))
check("纯内部提示会被清空 -> 调用方据此拦截", B.safe_outbound("【规章库未收录，请上报内部群核实】", "测试")[0] == "",
      repr(B.safe_outbound("【规章库未收录，请上报内部群核实】", "测试")[0]))

print("\n[8] AI 关单确认：探针回执前不删会话（V7.5 防误删）")
G1, G2, G3 = "CLOSE-T-A", "CLOSE-T-B", "CLOSE-T-C"
for g in (G1, G2, G3):
    B.state["companies"]["main"]["conversations"][g] = {"name": "关单测试" + g[-1], "msgs": []}

check("关单有超时保护常量（页面不回报也不会永远挂着）",
      isinstance(getattr(B, "CLOSE_CONFIRM_TIMEOUT", None), (int, float))
      and B.CLOSE_CONFIRM_TIMEOUT > 0, str(getattr(B, "CLOSE_CONFIRM_TIMEOUT", None)))

check("标记关单中：会话仍在列表且带 closing 标记",
      B.mark_close_pending(G1, "其他") is True
      and G1 in B.state["companies"]["main"]["conversations"]
      and B.state["companies"]["main"]["conversations"][G1].get("closing") is True,
      str(B.state["companies"]["main"]["conversations"][G1]))

removed, note = B.resolve_close(G1, True, "已点击「回复并关单」")
check("探针回执成功 -> 才把会话从列表移除",
      removed is True and G1 not in B.state["companies"]["main"]["conversations"], note)

B.mark_close_pending(G2, "其他")
removed2, note2 = B.resolve_close(G2, False, "未找到「回复并关单」按钮")
check("探针回执失败 -> 会话保留（绝不误删）",
      removed2 is False and G2 in B.state["companies"]["main"]["conversations"]
      and not B.state["companies"]["main"]["conversations"][G2].get("closing"),
      f"{note2} closing={B.state['companies']['main']['conversations'][G2].get('closing')}")

removed3, note3 = B.resolve_close("NO-SUCH-PENDING", True, "")
check("没有待确认记录时安全返回（不误删任何会话）", removed3 is False, note3)

for g in (G2, G3):
    B.state["companies"]["main"]["conversations"].pop(g, None)
B._PENDING_CLOSE.clear()

print("\n[9] 新消息通知（V7.5）：中继即时推送 + Bark 开关")
SRC = open("bridge_server.py", encoding="utf-8").read()
check("中继向手机推 NEW_MESSAGE（含 groupID/name/preview/mode/ts）",
      '"type": "NEW_MESSAGE"' in SRC and '"preview": preview' in SRC and '"mode": mode_now' in SRC)
check("有 _LAST_NOTIFIED 水位线（同一条不重复通知）",
      "_LAST_NOTIFIED" in SRC and "new_ts > int(_LAST_NOTIFIED.get(gid)" in SRC)
check("新消息也推 Bark，且可用 bark_on_new_message 关掉",
      'config.get("bark_on_new_message", True)' in SRC and "push_bark(f\"💬 {name} 新消息\"" in SRC)
check("Bark 开关默认开启（本机 config.json）", bool(B.config.get("bark_on_new_message", True)),
      str(B.config.get("bark_on_new_message", None)))
check("手动模式不再用延迟队列重复提示（改即时通知）",
      "手机端已即时提示" in SRC and 'status": "manual"' not in SRC)
check("H5 已取消\"正在看不打扰\"（客服要求：统一都要提示）",
      "activeGroupId && gid === activeGroupId" not in SRC
      and "统一都要提示" in SRC)

print("\n[10] IM 状态铁律：离线/忙碌不许被自动改成在线（除手动）")
prev_st, prev_manual = B.state.get("im_status"), B.state.get("im_status_manual")
try:
    # 手动设为"忙碌"后，探针上报"在线"（网页自动跳回）必须被拦下
    B.apply_im_status(2, manual=True, source="手机/小窗手动")
    ch = B.apply_im_status(1, manual=False, source="探针上报")
    check("手动忙碌后：网页自动跳回在线被拦下（状态不变）",
          B.state.get("im_status") == 2 and ch is False, f"status={B.state.get('im_status')}")
    # 手动切在线（manual=True）才允许变
    ch = B.apply_im_status(1, manual=True, source="手机/小窗手动")
    check("手动切在线时才允许变更", B.state.get("im_status") == 1, f"status={B.state.get('im_status')}")
    # 手动离线同理
    B.apply_im_status(3, manual=True, source="手机/小窗手动")
    ch = B.apply_im_status(1, manual=False, source="掉线恢复")
    check("手动离线后：掉线恢复也不许自动上线（仍保留离线）",
          B.state.get("im_status") == 3 and ch is False, f"status={B.state.get('im_status')}")
    # 非手动状态下跟随网页真实状态
    B.apply_im_status(3, manual=False, source="异常掉线")
    ch = B.apply_im_status(1, manual=False, source="网页真实状态")
    check("非手动状态下正常跟随网页真实状态", B.state.get("im_status") == 1, f"status={B.state.get('im_status')}")
finally:
    B.state["im_status"] = prev_st
    B.state["im_status_manual"] = prev_manual
    B.save_im_state(B.state.get("im_status") or 1, bool(B.state.get("im_status_manual")))
    B._PENDING_CLOSE.clear()

check("apply_im_status 支持来源标注（便于排查是谁改的）",
      "source" in B.apply_im_status.__code__.co_varnames)

print("\n[10.5] V7.7 IM 状态：网页事实 vs 人工意图（真机 bug：两边都改不动）")
prev_st, prev_manual = B.state.get("im_status"), B.state.get("im_status_manual")
prev_page = B.state.get("im_status_page")
try:
    # 中继记的是"手动忙碌"，探针读网页说"其实是在线"
    B.state["im_status"] = 2
    B.state["im_status_manual"] = True
    B.state["im_status_page"] = 0
    B.state["im_status_conflict"] = ""
    ch = B.apply_im_status(1, manual=False, source="探针上报", from_probe=True)
    check("探针上报的「网页在线」与手动忙碌冲突 -> 不采纳（仍保持忙碌）",
          B.state.get("im_status") == 2 and ch is False, f"status={B.state.get('im_status')}")
    check("同时记下「网页实际状态」与冲突组合（手机端能如实显示）",
          int(B.state.get("im_status_page") or 0) == 1
          and str(B.state.get("im_status_conflict")) == "2:1",
          f"page={B.state.get('im_status_page')} conflict={B.state.get('im_status_conflict')}")
    # 逃生舱：以网页为准重置 -> 采用网页实际值 + 清掉手动锁
    page = B.reset_im_state(source="测试")
    check("reset_im_state 以网页真实现状为准（清掉卡住的手动锁）",
          page == 1 and B.state.get("im_status") == 1 and B.state.get("im_status_manual") is False,
          f"page={page} status={B.state.get('im_status')} manual={B.state.get('im_status_manual')}")
    check("重置后冲突/重试计数清零（下次不一致还能再提示）",
          not B.state.get("im_status_conflict") and int(B.state.get("im_status_tries") or 0) == 0)
finally:
    B.state["im_status"] = prev_st
    B.state["im_status_manual"] = prev_manual
    B.state["im_status_page"] = prev_page or 0
    B.state["im_status_conflict"] = ""
    B.state["im_status_tries"] = 0
    B.state["im_status_notified"] = ""
    B.save_im_state(B.state.get("im_status") or 1, bool(B.state.get("im_status_manual")))

check("apply_im_status 支持 from_probe（区分「网页事实」与「人工意图」）",
      "from_probe" in B.apply_im_status.__code__.co_varnames)
check("探针上报不再被当成「手动」（不再有 manual... or (st in (2,3)) 这种代码）",
      'data.get("manual", False)) or' not in SRC and "from_probe=True" in SRC)
check("中继会把人工意图重下发（最多 3 次）+ 无效时如实提示手机",
      "def _reassert_im_status" in SRC and "tries <= 3" in SRC and "已重试" in SRC)
check("新增逃生舱 reset_im_state + /api/im_reset + RESET_IM_STATE/RESET 动作",
      "def reset_im_state" in SRC and '"/api/im_reset"' in SRC
      and 'act == "RESET_IM_STATE"' in SRC)
check("H5 会把「网页实际状态 X」如实带给客服看",
      "im_status_page" in SRC and "（网页仍" in SRC)
print("\n[10.6] 中继侧诊断可见性（中继跑在隐藏窗口时，/diag 也能看到现场）")
check("留存「上次切换请求」（切到哪个状态、谁发的、几点）",
      '"im_last_request"' in SRC and 'state["im_last_request"] =' in SRC)
check("留存「上次动作回执」（点了没反应时能查到原因）",
      'state["last_action"] =' in SRC and '"last_action": state.get("last_action")' in SRC)
check("留存「网页下拉实测选项」（含触发元素与可见状态节点）",
      'state["status_menu_dump"] =' in SRC and '"visible_status_nodes"' in SRC
      and '"status_menu_dump": state.get("status_menu_dump")' in SRC)
check("自检页把这三项渲染出来（不用看控制台）",
      'row("上次切换请求"' in SRC and 'row("上次动作回执"' in SRC and 'row("网页下拉实测"' in SRC)
check("手机页面禁缓存（否则 iOS 一直用旧版 H5，看不到新按钮）",
      'Cache-Control": "no-store, no-cache, must-revalidate"' in SRC)
print("\n[10.7] 指令自检（V7.8：先确认'指令到底有没有到探针'，再谈干活）")
check("新增 GET /api/probe_ping（发 PING 等 PONG，3 秒超时直说）",
      "def api_probe_ping" in SRC and '"/api/probe_ping"' in SRC and "timeout=3.0" in SRC)
check("探针 PONG 自报会留存并暴露到 /api/diag", 'state["probe_pong"] =' in SRC and '"pong": state.get("probe_pong")' in SRC)
check("safe_send 返回成败（'指令发出去没有'要能查）",
      "return True" in SRC and "return False" in SRC and "探针连接数 0" in SRC)
check("下发命令记录 conns/sent（发给几个、成功几个）",
      '"conns": len(_conns), "sent": _sent' in SRC)
check("事件计数（判断'探针到底发没发这条事件'）",
      'state.setdefault("event_counts", {})' in SRC and '"events": state.get("event_counts")' in SRC)
print("\n[10.8] 测试隔离（V7.9：测试绝不许点到客服的真实工单 —— 客服投诉过）")
check("两个 ws 端点都认 ?test=1（标记测试客户端）",
      SRC.count('request.query.get("test") == "1"') >= 2 and "TEST_WS.add(ws)" in SRC)
check("ext_targets：测试客户端的指令只发给测试探针",
      "def ext_targets" in SRC and "origin in TEST_WS" in SRC
      and "return [w for w in all_ext if w in TEST_WS]" in SRC)
check("会在页面上动手的指令都走 ext_targets（状态/挂起/发送/静音）",
      "ext_targets(ws)" in SRC and SRC.count("ext_targets(ws)") >= 5)
check("测试客户端不许真的 AI 关单（后台任务单独拦）",
      "测试客户端：已跳过真实 AI 关单" in SRC)
check("send_to_player 带 origin + require_page（代发不会打到真实探针 / 不会发错人）",
      'async def send_to_player(payload, where="", origin=None, require_page=False' in SRC
      and "origin=ws, require_page=True" in SRC)
check("自动重试也遵守隔离（im_intent_test）",
      'ext_targets("test" if state.get("im_intent_test") else None)' in SRC)
check("测试脚本已全部改用 ?test=1 连接",
      True)
print("\n[10.9] 出站硬保险（V7.9 事故后加固：绝不发错人 / 一键停发）")
check("页面绑定核对：目标会话必须 == 网页当前打开的工单",
      "def page_binding_ok" in SRC and "_LAST_PAGE_GID" in SRC and "require_page=True" in SRC)
check("页面绑定区分真实/测试来源（测试假探针不污染真实页面绑定）",
      "_LAST_TEST_PAGE_GID" in SRC and "def _origin_is_test" in SRC)
check("send_to_player 三道闸：停发总开关 -> 页面绑定 -> 出站安全闸",
      "if not outbound_enabled():" in SRC and "if require_page:" in SRC
      and SRC.index("if not outbound_enabled():") < SRC.index("if pkt.get(\"content\"):"))
check("测试来源按会话判定（避免全局标记被真实消息顶掉）",
      'conv.get("_test_origin")' in SRC and "def _auto_origin(gid=None)" in SRC)
check("一键停发开关 /api/outbound + 清理测试脏数据 /api/purge_test_data",
      "def api_outbound" in SRC and '"/api/outbound"' in SRC
      and "def api_purge_test_data" in SRC and "def _purge_test_convs" in SRC)
check("自检页展示出站安全卡（含停发/清理入口）",
      '出站安全（V7.9）' in SRC and '立即停发所有出站内容' in SRC)

print("\n[11] 三个 AI 动作的独立开关（服务端）")
SRC2 = SRC
check("开关函数 feature_flags 存在且三项都在",
      "def feature_flags" in SRC2 and all(k in SRC2 for k in
                                          ('enable_ai_close', 'enable_auto_draft', 'enable_f10_polish')))
check("AI_CLOSE 被开关拦住（关闭时不下发）",
      'if not feature_flags()["ai_close"]' in SRC2)
check("自动起草被开关拦住（半自动也不再自动出手）",
      'if not force and not feature_flags()["auto_draft"]' in SRC2)
check("分类改为按需读取（不再连接时就嗅探下拉）",
      "def ensure_category_options" in SRC2 and 'await safe_send(ext, {"command": "REQUEST_CATEGORIES"})' in SRC2)
check("按需读取只在真要选分类时调用（handle_ai_close 里）",
      "options = await ensure_category_options(" in SRC2)
check("分类候选回报会唤醒等待者", "_notify_category_waiters(state[\"category_options\"])" in SRC2)

print("\n[10.10] V8.0 自动切会话（回复前先把电脑网页切到目标工单 —— 不再让客服手动切）")
check("有 ensure_page_on：目标会话 ≠ 网页当前工单 -> 先切过去再发",
      "async def ensure_page_on" in SRC and 'await safe_send(ext, {"command": "OPEN_CONV"' in SRC)
check("send_to_player 页面绑定失败时先自动切会话（切成功再发）",
      "ok_sw, sw_note = await ensure_page_on(" in SRC
      and "if ok_sw:" in SRC and "ok_page, note = page_binding_ok(pkt.get(\"groupID\"), origin)" in SRC)
check("自动切会话可用 config.json 关掉（auto_open_conv）",
      'config.get("auto_open_conv", True)' in SRC)
check("占位会话（名字=工单号）不许拿去瞎点会话列表，且立刻拒发（不卡手机端队列）",
      '"placeholder": True' in SRC and 'if conv.get("placeholder") and not name:' in SRC)
check("网页真实上报会话名后会摘掉占位标记（之后就能自动切了）",
      'c.pop("placeholder", None)' in SRC)
check("手机端代发会把会话名一起带来（中继不知道名字时也能切过去）",
      'page_name=str(pkt.get("name") or "")' in SRC and "name: (c && c.name" in SRC)
check("F9/F10 直填接口支持 ?test=1 只打测试探针（测试不许往真实回复框填字）",
      'origin=("test" if request.query.get("test") == "1" else None)' in SRC)
check("手机端可点会话让电脑切过去（action=OPEN_CONV）",
      'act == "OPEN_CONV"' in SRC and '"command": "OPEN_CONV"' in SRC)
check("会话列表入库并推给手机端（CONV_LIST -> state.conv_list -> FULL_SYNC）",
      'ev == "CONV_LIST"' in SRC and 'state["conv_list"] = rows' in SRC)
check("测试来源的会话列表隔离到 conv_list_test（不推给真实手机端）",
      'state["conv_list_test"] = rows' in SRC and '"page_list_test_count"' in SRC)
check("会话列表按\"当前高亮行\"对齐网页当前工单（名字匹配已知会话）",
      '_LAST_PAGE_GID["gid"] = str(_gid)' in SRC and 'if not r.get("active"):' in SRC)
check("自检页/诊断能看到网页会话列表（page_list）",
      '"page_list_count"' in SRC and '"page_list"' in SRC)
check("H5 主页有\"电脑网页上的会话\"分区（未打开的也能看到 + 可点切）",
      "电脑网页上的会话（" in SRC and 'action: \'OPEN_CONV\'' in SRC and "data-openname=" in SRC)

print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
raise SystemExit(0 if failed == 0 else 1)
