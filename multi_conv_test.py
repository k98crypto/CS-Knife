"""多工单隔离验证（端到端，真实启服）

验证用户反馈的核心问题已修复：
  以前 probe.js 把所有工单都硬编码成 groupID="当前工单"，
  导致手机端永远只有一个会话、新玩家一上来就把旧玩家的记录覆盖掉。

本测试不使用 PLAYER_MESSAGE 携带聊天内容（避免触发真实 DeepSeek 调用），
而是用 PLAYER_MESSAGE 建立会话（messages 为空 -> 不触发 AI），
再用手机端 SEND_REPLY 往两个会话分别写消息，验证互不干扰。
"""
import asyncio
import json
import sys
import time

import aiohttp

# 防止 GBK 控制台 / stdout 重定向时打印 emoji 崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:8765"

# 每次运行使用唯一 ID：服务端会累积历史会话，写死 ID 会导致重复运行时断言失败
RUN = str(int(time.time() * 1000))
GID_A, GID_B, GID_C = "T-A-" + RUN, "T-B-" + RUN, "T-C-" + RUN


async def wait_full_sync(mobile, settle=0.5, tries=10):
    """排空当前所有推送，返回最后一帧 FULL_SYNC。

    SEND_REPLY / PLAYER_MESSAGE 都会立刻广播 FULL_SYNC，
    若只读第一帧会拿到过期快照（SEND_REPLY 是异步广播的）。
    """
    last = None
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(mobile.receive_json(), timeout=settle)
        except asyncio.TimeoutError:
            break
        if isinstance(msg, dict) and msg.get("type") == "FULL_SYNC":
            last = msg.get("data") or {}
    return last


def convs_of(snap):
    return (((snap or {}).get("companies") or {}).get("main") or {}).get("conversations") or {}


async def main():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        if cond:
            print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
        else:
            ok = False
            print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))

    async with aiohttp.ClientSession() as s:
        ext = await s.ws_connect(BASE + "/ws/extension")

        # ---- 建立两个不同玩家的会话（messages 为空，不触发 AI）----
        await ext.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": GID_A, "name": "玩家甲", "messages": [], "playerInfo": "玩家甲 | UID:1001"}})
        await asyncio.sleep(0.25)
        await ext.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": GID_B, "name": "玩家乙", "messages": [], "playerInfo": "玩家乙 | UID:2002"}})
        await asyncio.sleep(0.4)

        mobile = await s.ws_connect(BASE + "/ws/mobile")
        snap = await wait_full_sync(mobile)
        convs = convs_of(snap)

        print("\n[1] 会话隔离")
        check("两个玩家的会话同时存在（不再只剩一个）",
              GID_A in convs and GID_B in convs,
              "本次运行键=%s" % [k for k in convs.keys() if k.endswith(RUN)])
        check("本次只有这两个新会话（未与其它玩家混淆）",
              len([k for k in convs.keys() if k.endswith(RUN)]) == 2)
        a, b = convs.get(GID_A, {}), convs.get(GID_B, {})
        check("会话名分别对应玩家甲/玩家乙",
              a.get("name") == "玩家甲" and b.get("name") == "玩家乙",
              "%r / %r" % (a.get("name"), b.get("name")))

        print("\n[2] 消息互不覆盖")
        await mobile.send_json({"action": "SEND_REPLY", "groupID": GID_A, "content": "回复给甲"})
        await asyncio.sleep(0.25)
        await mobile.send_json({"action": "SEND_REPLY", "groupID": GID_B, "content": "回复给乙"})
        await asyncio.sleep(0.4)

        snap2 = await wait_full_sync(mobile)
        c2 = convs_of(snap2)
        a2, b2 = c2.get(GID_A, {}), c2.get(GID_B, {})
        a_msgs = [m.get("text") for m in (a2.get("msgs") or [])]
        b_msgs = [m.get("text") for m in (b2.get("msgs") or [])]
        check("甲会话只含给甲的消息", a_msgs == ["回复给甲"], "甲=%s" % a_msgs)
        check("乙会话只含给乙的消息", b_msgs == ["回复给乙"], "乙=%s" % b_msgs)
        check("旧会话没有被新会话覆盖（关键修复点）",
              len(a2.get("msgs") or []) == 1 and len(b2.get("msgs") or []) == 1)

        print("\n[3] 排序与时间戳")
        ua, ub = a2.get("updatedAt") or 0, b2.get("updatedAt") or 0
        check("两个会话都有 updatedAt", ua > 0 and ub > 0, "甲=%s 乙=%s" % (ua, ub))
        check("最后操作的会话 updatedAt 更大（手机端可倒序）", ub >= ua, "乙-甲=%d ms" % (ub - ua))
        ts_ok = all(isinstance(m.get("ts"), int) and m["ts"] > 0
                    for m in (a2.get("msgs") or []) + (b2.get("msgs") or []))
        check("每条消息都带 ts（供手机端显示时间）", ts_ok)

        print("\n[4] 脏数据不打断连接")
        await ext.send_str("not-a-json")
        await asyncio.sleep(0.2)
        check("非法 JSON 后扩展端仍存活", not ext.closed)

        await ext.send_json({"event": "PLAYER_MESSAGE", "data": {
            "groupID": GID_C, "name": "玩家🚀C", "messages": [], "playerInfo": "玩家C"}})
        await asyncio.sleep(0.3)
        snap3 = await wait_full_sync(mobile)
        c3 = convs_of(snap3)
        check("emoji 会话名不导致异常", GID_C in c3 and c3[GID_C].get("name") == "玩家🚀C",
              repr(c3.get(GID_C, {}).get("name")))

        await ext.close()
        await mobile.close()

    print("\n=== 结论:", "通过" if ok else "失败", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
