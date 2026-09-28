"""agent_core P0 修复验证：疑难单标记归一化 + AI 报错不外发"""
import sys
import io

# 强制 UTF-8 输出，避免 GBK 控制台下打印中文/符号报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from agent_core import CustomerServiceCore, HARD_CASE_MARKER

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


print("=== agent_core P0 修复验证 ===\n")
core = CustomerServiceCore()

# ---------- 1. 疑难单标记归一化 ----------
print("[1] 疑难单标记归一化")

# 1a. AI 原始输出（prompt 里教的新写法）
raw_a = "【需走内部群核实，建议按 F8 上报群聊】\n亲爱的玩家，该情况需要为您转交专人核实处理。"
norm_a = core._normalize_hard_case(core.sanitize_reply(raw_a))
check("旧变体(内部群+F8) 归一化后含 '【规章库未收录'",
      "【规章库未收录" in norm_a, repr(norm_a.split("\n")[0]))

# 1b. 走 sanitize 后变形的残形（本期真实 bug 场景）
san_b = core.sanitize_reply("【需走内部群核实，建议按 F8 上报群聊】\n正文")
check("sanitize 确实会改写 '内部群'（复现原 bug 成因）",
      "内部群" not in san_b, repr(san_b.split("\n")[0]))
norm_b = core._normalize_hard_case(san_b)
check("变形残形也能被归一化",
      "【规章库未收录" in norm_b, repr(norm_b.split("\n")[0]))

# 1c. 新写法（prompt 已改为直接输出标准标记）
raw_c = HARD_CASE_MARKER + "\n亲爱的玩家，您好。"
norm_c = core._normalize_hard_case(raw_c)
check("标准标记保持不变", norm_c == raw_c, repr(norm_c.split("\n")[0]))

# 1d. 普通回复不得被误伤
raw_d = "【亲爱的玩家】您好，您反馈的充值问题我们已收到，正在为您核实，请您耐心等待。"
norm_d = core._normalize_hard_case(raw_d)
check("普通回复不被误改", "规章库未收录" not in norm_d)

# 1e. 标记不在开头时不得误伤（防止正文里出现 F8 字样被改）
raw_e = "亲爱的玩家您好，关于您的问题我们已记录，稍后会有专人联系您。（请勿按 F8）"
norm_e = core._normalize_hard_case(raw_e)
check("标记不在开头时不误改", "规章库未收录" not in norm_e)

# ---------- 2. AI 报错不外发 ----------
print("\n[2] AI 报错不外发")

scenario = "【客服】10-01 12:00\n亲爱的玩家您好\n【玩家】10-01 12:05\n我充值了648元但钻石没到账，请帮我查一下订单号 12345"
# 强制走 AI 分支
core.try_local_prefilter = lambda t: ("NEED_AI", "")
# 模拟接口失败（修复后 _call_deepseek 返回空串）
core._call_deepseek = lambda s, u: ""
tag, reply = core.process_ticket_f9(scenario)
check("接口失败 -> tag=API_ERROR", tag == "API_ERROR", tag)
check("接口失败 -> reply 为空（不含报错占位符）", reply == "", repr(reply))

# 2b. 确认旧的报错占位符已不再作为返回值（注释中提及不算）
src = open("agent_core.py", "r", encoding="utf-8").read()
check("源码中已无 return '[接口错误:' 分支", 'return f"[接口错误:' not in src)
check("源码中已无 return '[网络异常:' 分支", 'return f"[网络异常:' not in src)

# 2c. 正常回复仍然可用（回归）
core._call_deepseek = lambda s, u: "[TAG:NORMAL]\n亲爱的玩家，您的问题已受理。"
tag2, reply2 = core.process_ticket_f9(scenario)
check("正常路径未被破坏", tag2 == "NORMAL" and "已受理" in reply2, f"{tag2} / {reply2[:20]}")

# ---------- 3. 前后端契约一致性 ----------
print("\n[3] 前后端契约一致性")
bridge = open("bridge_server.py", "r", encoding="utf-8").read()
runner = open("semi_runner.pyw", "r", encoding="utf-8").read()
check("bridge_server 判定前缀为 '【规章库未收录'", 'if "【规章库未收录" in reply:' in bridge)
check("semi_runner 判定前缀为 '【规章库未收录'", 'if "【规章库未收录" in reply:' in runner)
check("HARD_CASE_MARKER 以该前缀开头", HARD_CASE_MARKER.startswith("【规章库未收录"), HARD_CASE_MARKER)
check("marker 经 sanitize 后前缀仍完好（关键前提）",
      core.sanitize_reply(HARD_CASE_MARKER).startswith("【规章库未收录"),
      repr(core.sanitize_reply(HARD_CASE_MARKER)))
check("prompt 不再指导客服按 F8", "在第一行给客服提示【需走内部群核实" not in src)

print(f"\n=== 结果: {passed} 通过 / {failed} 失败 ===")
sys.exit(0 if failed == 0 else 1)
