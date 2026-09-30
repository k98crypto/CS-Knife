# -*- coding: utf-8 -*-
"""V8.6 回归测试：① 多段回复分句（split_reply）② 半自动"发完自动粘下一条"队列
                    ③ AFK 分段发送的接线 ④ 电脑端提醒兜底（探针出不了声 -> 悬浮窗响）

不需要启动服务：直接 import bridge_server 测纯逻辑 + 源码级接线检查。
"""
import asyncio
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

passed = failed = 0


def check(name, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print("  [PASS] " + name + (("  -> " + str(extra)) if extra else ""))
    else:
        failed += 1
        print("  [FAIL] " + name + (("  -> " + str(extra)) if extra else ""))


def read(p):
    with io.open(os.path.join(BASE, p), encoding="utf-8-sig") as f:
        return f.read()


print("[1] 分段规则 split_reply()")
import bridge_server as B   # noqa: E402  （导入即读 config.json，与其他测试一致）

one = "亲爱的联结者，已为您记录，请耐心等待哦~"
check("本来是一句话 -> 仍然 1 段（老行为不变）", B.split_reply(one) == [one])

two = ("亲爱的联结者，非常理解您的心情，辛苦您提供一下以下信息，我们会帮您进行记录核实~\n\n"
       "这可能需要一定时间，还请您耐心等待一下，感谢您的理解！")
segs = B.split_reply(two)
check("客服给的两段样例 -> 正好切成 2 段", len(segs) == 2, len(segs))
check("第 1 段内容正确",
      segs[0] == "亲爱的联结者，非常理解您的心情，辛苦您提供一下以下信息，我们会帮您进行记录核实~",
      segs[0][:30])
check("第 2 段内容正确", segs[1] == "这可能需要一定时间，还请您耐心等待一下，感谢您的理解！", segs[1][:30])

long_one = "第一句话。" * 30
seg = B.split_reply(long_one, soft_limit=100)
check("超长单段 -> 按句号再切，且每段不超过软上限",
      len(seg) > 1 and all(len(s) <= 100 for s in seg), [len(s) for s in seg])

many = "\n\n".join(["第%d段内容" % i for i in range(1, 9)])
capped = B.split_reply(many, max_seg=3)
check("段数上限生效（8 段 -> 最多 3 段，多出的并进最后一段）",
      len(capped) == 3 and capped[2].endswith("第8段内容"), [len(s) for s in capped])

check("空内容 -> 空列表", B.split_reply("   \n\n  ") == [])
check("列表型内容（1. 2. 3.）不被拆散",
      B.split_reply("1. 截图或录屏 2. 具体模式 3. 丢失道具") == ["1. 截图或录屏 2. 具体模式 3. 丢失道具"])

print("\n[2] 相似度（决定要不要续粘）")
check("同一段 -> 1.0", B._text_similar(segs[0], segs[0]) == 1.0)
check("客服只改了几个字 -> 仍算同一段（>=0.6）",
      B._text_similar(segs[1], segs[1].replace("时间", "工夫")) >= 0.6)
check("换了话题 -> 判为不像（<0.6）",
      B._text_similar(segs[1], "我这边帮您重新查一下账号状态，稍等") < 0.6)
check("空串 -> 0", B._text_similar("", "abc") == 0.0)

print("\n[3] 半自动队列：发出上一段 -> 自动粘下一段")
calls = []


async def _fake_send(payload, where="", origin=None, require_page=False, page_name=None, auto=False):
    calls.append({"cmd": payload.get("command"), "content": payload.get("content"),
                  "gid": payload.get("groupID"), "noOverwrite": payload.get("noOverwrite"),
                  "require_page": require_page, "auto": auto, "where": where})
    return True


_real_send = B.send_to_player
B.send_to_player = _fake_send

three = ["第一段：先安抚您的心情~", "第二段：麻烦提供订单号和截图", "第三段：已记录，请耐心等待"]
GID = "TEST-QUEUE-1"
B._REPLY_QUEUE.pop(GID, None)
n = B.queue_reply_segments(GID, "测试玩家", three, "单测")
check("排队：记 3 段、sent=1、剩 2 段",
      n == 3 and B._REPLY_QUEUE[GID]["sent"] == 1 and len(B._REPLY_QUEUE[GID]["remaining"]) == 2)

loop = asyncio.new_event_loop()
run = loop.run_until_complete

calls.clear()
run(B.advance_reply_queue(GID, three[0]))
check("客服发出第 1 段 -> 自动粘第 2 段（FILL_DRAFT + noOverwrite + 页面绑定）",
      len(calls) == 1 and calls[0]["cmd"] == "FILL_DRAFT" and calls[0]["content"] == three[1]
      and calls[0]["noOverwrite"] is True and calls[0]["require_page"] is True, calls)
check("队列推进：sent=2、剩 1 段",
      B._REPLY_QUEUE[GID]["sent"] == 2 and len(B._REPLY_QUEUE[GID]["remaining"]) == 1)

calls.clear()
run(B.advance_reply_queue(GID, three[1]))
check("再发出第 2 段 -> 自动粘第 3 段（最后一段）",
      len(calls) == 1 and calls[0]["content"] == three[2], calls)

calls.clear()
run(B.advance_reply_queue(GID, three[2]))
check("最后一段发出后 -> 队列清空、不再粘", len(calls) == 0 and GID not in B._REPLY_QUEUE)

print("\n[4] 安全阀：客服换话题就不许硬塞")
B._REPLY_QUEUE.pop(GID, None)
B.queue_reply_segments(GID, "测试玩家", three, "单测")
calls.clear()
run(B.advance_reply_queue(GID, "我给您重新查一下别的"))
check("发的内容与排队的段不像 -> 丢弃队列、不粘下一段",
      len(calls) == 0 and GID not in B._REPLY_QUEUE)
check("单段回复不排队（len<2）",
      B.queue_reply_segments("G2", "x", [one]) == 0 and "G2" not in B._REPLY_QUEUE)

B.send_to_player = _real_send

print("\n[5] AFK 分段发送接线")
SRC = read("bridge_server.py")
check("有 send_segments_drip（段间随机延时）", "async def send_segments_drip" in SRC)
check("段间随机用的是 config 的 multi_send_delay_min/max_sec",
      "multi_send_delay_min_sec" in SRC and "multi_send_delay_max_sec" in SRC
      and "random.uniform(lo, hi)" in SRC)
check("★ 每一段发出前重新校验仍在 AFK（中途切回就停）",
      'if not state.get("afk_mode"):' in SRC and "已切回非 AFK：停止后续" in SRC)
check("AFK 路径已接线（多段走 drip、单段走原逻辑）",
      "asyncio.create_task(send_segments_drip(" in SRC and "AFK 自动回复" in SRC)
check("★ 每一段都走 send_to_player（安全闸/防重复/页面绑定不绕过）",
      '"command": "SEND_REPLY", "content": seg' in SRC and "require_page=True, auto=True" in SRC)
check("半自动路径：只填第 1 段 + 排队",
      "queue_reply_segments(group_id" in SRC and "半自动草稿" in SRC)
check("F9/F10 直填路径：只填第 1 段 + 排队", "queue_reply_segments(_gid" in SRC)
check("人发出去后触发续粘（挂在 RECORD_MANUAL_DEMO 上）",
      "advance_reply_queue(_mgid, _mtxt)" in SRC and 'if ev == "RECORD_MANUAL_DEMO":' in SRC)
check("总开关 multi_send_enabled（默认开）",
      "multi_send_on()" in SRC and 'config.get("multi_send_enabled", True)' in SRC)
check("diag 暴露 reply_queue（HUD/排障可见）", '"reply_queue"' in SRC)

print("\n[6] 电脑端提醒兜底（探针出不了声 -> 悬浮窗响）")
PJ = read("probe.js")
HUD = read("semi_runner.pyw")
check("探针：initAudio 返回真实可否出声（audioReady）",
      "audioReady = (audioCtx && audioCtx.state === 'running')" in PJ and "return audioReady" in PJ)
check("探针：按键/聚焦/切回本页都会解锁提示音（不再依赖必须点击）",
      "function setupAudioUnlock" in PJ and "addEventListener('keydown'" in PJ
      and "visibilitychange" in PJ and "setupAudioUnlock();" in PJ)
check("探针：出不了声时如实上报（SOUND_BLOCKED / sound_ok）",
      "SOUND_BLOCKED" in PJ and "sound_ok: audioReady" in PJ and "reportSoundBlocked" in PJ)
check("探针：胶囊提示“点一下才能出声”", "点一下工作台页面才能出声" in PJ)
check("中继：探针出不了声 -> 让悬浮窗兜底响铃/报警",
      '"desktop_ding"' in SRC and '"desktop_alarm"' in SRC and 'state["probe_sound_ok"]' in SRC)
check("中继：diag 暴露 sound_ok", '"sound_ok": state.get("probe_sound_ok"' in SRC)
check("悬浮窗：兜底提示音 + 去重 + 显示待发段队列",
      "def beep_ding" in HUD and "def beep_offline" in HUD and "_last_ding_ts" in HUD
      and "_handle_desktop_fallback" in HUD and "_handle_reply_queue" in HUD)

print("\n=== 结果: %d 通过 / %d 失败 ===" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
