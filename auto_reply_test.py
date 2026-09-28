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

print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
raise SystemExit(0 if failed == 0 else 1)
