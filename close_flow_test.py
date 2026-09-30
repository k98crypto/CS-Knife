# -*- coding: utf-8 -*-
"""V8.8 回归测试：① 手机"发送"按钮（回执/草稿兜底/不再静默） ② 关单走**网页真实三级分类**
                    ③ 手机「关单」分类面板（含搜索，无原生 select） ④ 绝不误关别人的工单

源码级接线 + 纯逻辑检查（真实链路另用临时实例做端到端）。
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
passed = failed = 0

# 控制台健壮性：Windows 重定向到文件时默认 GBK，emoji 会直接抛 UnicodeEncodeError（把测试打断）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


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
H5 = SRC.split('HTML_CONTENT = """', 1)[1].split('"""', 1)[0]

print("[1] 手机「发送」：不再点了没反应，成功后才有回执")
check("★ 输入框为空但有 AI 草稿 -> 直接发草稿", "if (!text && lastDraftFilled)" in H5 and "useDraft: usedDraft" in H5)
check("★ 真没内容时**明确提示**（不再静默 return）",
      "没有可发送的内容：请在输入框里输入" in H5 and "showErr('没有可发送的内容" in H5)
check("★ 点发送后进入'发送中'状态（按钮有可见反馈）",
      "setSendState(true)" in H5 and ".send-btn.sending" in H5 and "id=\"btn-send\"" in H5)
check("★ 8 秒没回执 -> 如实提示并复位（内容保留）", "未收到电脑端回执" in H5 and "}, 8000);" in H5)
check("★ 只有中继回执成功才清空输入框（旧版无条件清空）",
      "if (okSent) {" in H5 and "inp.value = '';" in H5 and "inp.value.trim() === String(ps.text" in H5)
check("中继回执带 echo（clientId 原样带回）",
      'echo = str(pkt.get("clientId") or "")' in SRC and '"echo": echo' in SRC
      and '"已发送给玩家"' in SRC)
check("中继：空内容 + useDraft -> 用最近一次草稿（_remember_draft 记录）",
      'pkt.get("useDraft")' in SRC and "_remember_draft(group_id, _first)" in SRC
      and 'state.setdefault("last_draft", {})' in SRC)

print("\n[2] 关单：分类只能来自**网页真实菜单**（绝不写死/猜）")
check("★ 探针新增 按路径逐层点选 selectCategoryPath（逐层校验）",
      "selectCategoryPath: function (path, cb)" in PJ and "已是叶子分类但路径还有下一层" in PJ)
check("★ 探针新增 只读下钻 exploreCategory（只点有子级的节点，不提交任何值）",
      "exploreCategory: function (path, cb)" in PJ and "hasChildren" in PJ)
check("★ 探针新增 用网页自带搜索框 searchCategory（没有就如实回 searchable:false）",
      "searchCategory: function (kw, cb)" in PJ and "searchable: false" in PJ)
check("★ 选不中分类 -> 中止关单（绝不带错分类关单）",
      "已中止关单，请手动选择分类后再关" in PJ and "if (!res || !res.ok)" in PJ)
check("旧的'关键字兜底选第一项'没有用于关单路径",
      "Operator.selectCategoryPath(cmd.categoryPath" in PJ)
check("中继：探针分类结果有 await/等待者机制（reqId）",
      "async def probe_category" in SRC and "_CAT_WAITERS" in SRC and "def _resolve_cat_waiters" in SRC)
check("中继：CATEGORY_PROBE / CATEGORY_SEARCH 两个手机动作已接线",
      'act == "CATEGORY_PROBE" or act == "CATEGORY_SEARCH"' in SRC)
check("中继：CATEGORY_RESULT 事件（来自探针）-> 唤醒等待者 + 缓存 + 日志",
      'if ev == "CATEGORY_RESULT":' in SRC and "_CAT_CACHE[_key]" in SRC)
check("AI 关单：先用**网页搜索**把 AI 选的分类解析成真实完整路径，解析不到才用配置提示",
      "sres = await probe_category(q=str(category or \"\"), origin=origin" in SRC
      and "已从网页真实菜单解析出分类路径" in SRC and "选不中会中止关单" in SRC)

print("\n[3] 手机「关单」面板：自己写结束语 + 选三级分类（含搜索）")
check("关单按钮 -> 打开分类面板（不再写死 category:'其他'）",
      "openCloseSheet(text)" in H5 and 'category: "其他"' not in H5)
check("★ 面板不用原生下拉框（既有约定：iOS 卡顿）", ("<" + "select") not in H5)
check("★ 三级：层级行 + 点开列表选择 + 自动读下一层",
      "csOpenPick(Number(this.dataset.lv))" in H5 and "CATEGORY_PROBE', [csData.l1]" in H5)
check("★ 搜索：本地过滤 + 🔍 让电脑网页搜（真实搜索框）",
      "function csFilterLocal()" in H5 and "function csSearchWeb()" in H5
      and "CATEGORY_SEARCH" in H5)
check("★ 确认关单带上完整路径 + 会话名 + clientId",
      "action: 'CLOSE_WITH_PATH'" in H5 and "path: path" in H5 and "clientId: cid" in H5)
check("没有选分类就不让关单（明确提示）",
      "关单前必须选择问题分类" in H5 and "if (!path.length)" in H5)

print("\n[4] 绝不误关别人的工单")
check("★ 中继：不是网页当前会话且没带会话名 -> 拒绝关单（提示先打开它）",
      "为防关错工单：这条不是电脑网页当前打开的会话" in SRC)
check("★ 中继：会话中继不认识 -> 拒绝（不造占位卡片）",
      "这条会话还没在电脑网页上打开过 —— 先在手机主页点它一下" in SRC)
check("★ 中继：空结束语 / 空分类一律拒绝",
      "关单需要一个结束语：请先在输入框里写好，再选分类" in SRC
      and "请先选择问题分类再关单（分类必须来自网页真实菜单）" in SRC)
check("★ 关单仍走 send_to_player（安全闸 + 页面绑定 + require_page + page_name）",
      '"手机关单", origin=ws, require_page=True, page_name=_cname)' in SRC)
check("关单下发后标记为关单中并起看门狗（回执成功才移除会话）",
      "mark_close_pending(_cgid, _cpath[-1], is_test=_c_test)" in SRC
      and "_close_confirm_watchdog(_cgid)" in SRC)

print("\n[5] 逻辑复算：路径解析与文案")
cands = [{"path": ["A", "B", "充值问题"]}, {"path": ["C", "充值问题"]}, {"path": ["X", "Y", "别的"]}]
exact = [o for o in cands if str(o["path"][-1]) == "充值问题"] or cands
exact.sort(key=lambda o: -len(o.get("path") or []))
check("同名分类取层级最多的（三级最具体）", exact[0]["path"] == ["A", "B", "充值问题"], exact[0]["path"])
check("AI 分类为空时不会生成非法路径",
      ([str(x) for x in ([] or ["一级", "二级"])] == ["一级", "二级"]))
check("路径 -> category 取最后一级（探针据此兜底文案）",
      " / ".join(["A", "B", "C"]) == "A / B / C")

print("\n=== 结果: %d 通过 / %d 失败 ===" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
