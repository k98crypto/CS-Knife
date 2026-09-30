# -*- coding: utf-8 -*-
"""V8.7 回归测试：① 不聚焦/后台标签也能感知新消息（事件驱动感知 + 悬浮窗兜底）
                    ② 质检采集结束的主动通知（手机 Bark + 手机端 + 悬浮窗响铃 + 蒸馏命令）

不需要启动服务：源码级接线检查 + 纯逻辑复算（真实行为另用临时实例做端到端验证）。
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
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


PJ = read("probe.js")
SRC = read("bridge_server.py")
QCJ = read("qc_probe.js")
HUD = read("semi_runner.pyw")

print("[1] 探针：不聚焦也能感知新消息（后台标签不再失聪）")
check("聊天扫描抽成具名函数（便于被 DOM 事件直接调用）",
      "function scanChatAndReport()" in PJ and "setInterval(scanChatAndReport, 2000)" in PJ)
check("会话列表扫描也抽成具名函数（顺序未打乱，probe_smoke 另行覆盖）",
      "function bumpConvListReport()" in PJ and "setInterval(bumpConvListReport, 3000)" in PJ)
check("★ 新增 MutationObserver（DOM 事件驱动，不受后台定时器节流影响）",
      "new MutationObserver(" in PJ and 'mo.observe(target, { childList: true, subtree: true, characterData: true })' in PJ)
check("有防抖（同一批 DOM 变化只扫一次）", "function kickScan(why)" in PJ and "lastKickAt" in PJ)
check("切回前台/窗口聚焦会立刻补扫一次（补上后台漏掉的）",
      "visibilitychange" in PJ and 'kickScan("visible")' in PJ and 'kickScan("focus")' in PJ)
check("感知器已在启动处装配", "setupFocusIndependentScan();" in PJ)
check("★ 新消息载荷带 hidden（页面是否在后台）", "hidden: (function () { try { return !!document.hidden; }" in PJ)
check("掉线事件也带 hidden（新消息 + 掉线两处都有）",
      "ABNORMAL_OFFLINE" in PJ and PJ.count("hidden: (function () { try { return !!document.hidden; }") >= 2)
check("★ 后台时网页自己不出声（交给悬浮窗，避免双响）",
      "if (isNewPlayerMsg && !document.hidden) playDingDong();" in PJ)
check("排障入口：__probe.forceScan / __probe.hidden",
      "forceScan: function ()" in PJ and "hidden: function ()" in PJ)

print("\n[2] 中继：页面在后台 -> 桌面悬浮窗兜底（系统声，不依赖浏览器）")
check("新消息：sound_ok=false **或隐藏**都会让悬浮窗响（可用 hud_alert_when_hidden 关掉后台那半）",
      '_hidden = bool(payload.get("hidden"))' in SRC
      and 'if payload.get("sound_ok") is False or (_hidden and _hud_on_hidden):' in SRC
      and 'config.get("hud_alert_when_hidden", True)' in SRC)
check("兜底原因如实标注（页面在后台 / 未授权出声）",
      'why": ("页面在后台（不聚焦）" if _hidden' in SRC and "浏览器未授权出声" in SRC)
check("掉线：隐藏 或 出不了声 -> 悬浮窗兜底报警",
      '_ad_hidden = bool(_ad.get("hidden"))' in SRC
      and 'if _ad.get("sound_ok") is False or _ad_hidden:' in SRC)
check("diag 暴露 desktop_ding / desktop_alarm（悬浮窗据此响铃）",
      '"desktop_ding": state.get("desktop_ding")' in SRC and '"desktop_alarm": state.get("desktop_alarm")' in SRC)

print("\n[3] 质检采集结束 -> 主动通知（不用盯着页面）")
check("★ 中继生成'下一步'可复制蒸馏命令",
      '_d["next_step"] = (f"python distill_rules.py --mode history "' in SRC
      and "--limit {max(1, int(_d.get('done') or 0))} --append" in SRC)
check("★ 结束即推手机 Bark（可在 config 用 notify_qc_done=false 关掉）",
      'config.get("notify_qc_done", True)' in SRC and 'push_bark("✅ 质检采集完成"' in SRC)
check("★ 同时推手机端提示（AI_STATUS）", '"质检采集完成：{_sum}｜已备好蒸馏命令' in SRC)
check("★ 测试来源隔离：?test=1 的采集器不推真实 Bark/手机",
      "_QC_TEST_CONNS" in SRC and 'request.query.get("test")' in SRC
      and "ws not in _QC_TEST_CONNS" in SRC)
check("结束日志带上 成功/跳过/失败/用时/后台节流/累计语料",
      "[QC] ✅ 扫描结束：{_sum}" in SRC and "后台节流" in SRC and "累计语料" in SRC)
check("qc_probe：结束上报带 hidden / stalledMs（可诊断后台节流）",
      "hidden: (function () { try { return !!document.hidden; }" in QCJ and "stalledMs" in QCJ)
check("qc_probe：切回前台时如实说明被节流多久 + 自动继续",
      "function setupVisibilityWatch()" in QCJ and "刚才页面不在前台（被节流/可能被冻结）" in QCJ
      and "setupVisibilityWatch();" in QCJ)
check("qc_probe：开始扫描就告知'可以切走'与冻结风险",
      "可以切到别的标签/窗口去干活" in QCJ and "始终保持活动" in QCJ)
check("悬浮窗：采集完成 -> 响铃 + 暂存区写明结果 + 复制蒸馏命令",
      "def _handle_qc_done" in HUD and "beep_success" in HUD and "pyperclip.copy(cmd)" in HUD
      and "_last_qc_done_ts" in HUD)
check("悬浮窗：轮询 3 秒（兜底响铃/掉线/采集完成通知更快）", "self.root.after(3000, self._poll_links)" in HUD)

print("\n[4] 逻辑复算：通知文案与命令")
_d = {"done": 42, "skipped": 7, "failed": 2, "elapsedMs": 305000, "stalledMs": 0}
_sum = (f"成功 {_d.get('done')} · 跳过(仅机器人) {_d.get('skipped')} · "
        f"失败 {_d.get('failed')} · 用时 {int(int(_d['elapsedMs']) / 1000)} 秒")
check("汇总文案格式正确（成功/跳过/失败/用时）",
      _sum == "成功 42 · 跳过(仅机器人) 7 · 失败 2 · 用时 305 秒", _sum)
check("蒸馏命令格式正确（--mode history --limit 成功数 --append）",
      (f"python distill_rules.py --mode history --limit {max(1, int(_d['done']))} --append")
      == "python distill_rules.py --mode history --limit 42 --append")
check("done=0 时 --limit 兜底为 1（不会生成非法命令）",
      max(1, int({"done": 0}.get("done") or 0)) == 1)

print("\n=== 结果: %d 通过 / %d 失败 ===" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
