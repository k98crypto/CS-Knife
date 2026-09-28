"""Token 泄露验证：模拟探针注入 Token 后，观察手机端 FULL_SYNC 是否包含 Token

不触发任何 AI 调用（不使用 PLAYER_MESSAGE，避免消耗 DeepSeek 额度）
"""
import asyncio
import json
import aiohttp

BASE = "http://127.0.0.1:8765"
TOKEN_VALUE = "SECRET-TOKEN-ABC123-DO-NOT-LEAK"


async def main():
    ok = True
    async with aiohttp.ClientSession() as s:
        # ---- 1. 探针（extension）连接并注入 Token ----
        ext = await s.ws_connect(BASE + "/ws/extension")
        # 模拟"探针反复重连重发"场景：同一份内容连发 3 次，再发 1 次不同内容
        for _ in range(3):
            await ext.send_json({
                "event": "HEADERS_SYNC",
                "data": {"x-cs-token": TOKEN_VALUE, "authorization": "Bearer " + TOKEN_VALUE},
            })
        await ext.send_json({
            "event": "HEADERS_SYNC",
            "data": {"x-cs-token": "ROTATED-TOKEN-XYZ"},
        })
        await asyncio.sleep(0.4)
        print("[1] 探针已注入 Token:", TOKEN_VALUE)
        print("    (同一内容重复发送 3 次 + 轮换 1 次，用于验证去重)")

        # ---- 2. 手机端连接（服务端会立即回一条 FULL_SYNC）----
        mobile = await s.ws_connect(BASE + "/ws/mobile")
        leaked = False
        raw = ""
        for _ in range(3):
            try:
                msg = await asyncio.wait_for(mobile.receive_json(), timeout=4)
            except asyncio.TimeoutError:
                break
            raw = json.dumps(msg, ensure_ascii=False)
            if TOKEN_VALUE in raw or "ROTATED-TOKEN-XYZ" in raw:
                leaked = True
                break

        print("[2] 手机端收到 FULL_SYNC，长度 =", len(raw))
        if leaked:
            ok = False
            print("    >>> ！！Token 泄露确认！！手机端拿到了探针的 IM 认证 Token")
            idx = raw.find(TOKEN_VALUE)
            if idx < 0:
                idx = raw.find("ROTATED-TOKEN-XYZ")
            print("    >>> 泄露上下文:", raw[max(0, idx - 60): idx + 60])
        else:
            print("    >>> 未发现 Token（安全）")

        # 2b. 确认广播载荷中根本不存在认证头字段
        has_field = "im_auth_headers" in raw
        if has_field:
            ok = False
        print("    >>> 载荷是否含 'im_auth_headers' 字段:", "是（异常）" if has_field else "否（正确）")

        # ---- 3. 检查 state 里是否常驻该字段 ----
        try:
            async with s.get(BASE + "/api/ticket") as r:
                body = await r.text()
            if TOKEN_VALUE in body:
                ok = False
                print("[3] /api/ticket 也返回了 Token  -> 额外泄露点")
            else:
                print("[3] /api/ticket 未返回 Token")
        except Exception as e:
            print("[3] /api/ticket 请求异常:", e)

        await ext.close()
        await mobile.close()

    print("\n=== 结论:", "存在 Token 泄露" if not ok else "无泄露", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
