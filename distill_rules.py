#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""distill_rules.py —— 人工客服操作"规则逆向蒸馏"离线工具（V8.3）

它做什么（只读语料 + 只写本地文件，**完全不参与线上链路**）：
    1. 读取项目根目录的 demonstrations.jsonl（由 bridge_server.py 的 DemoRecorder 落盘，
       数据源是 probe.js 的"人工回复静默嗅探"事件 RECORD_MANUAL_DEMO）；
    2. 取最近的 15~30 条真实"玩家问题 → 客服实际回复"样本（默认 20 条，可用 --limit 调）；
    3. 复用项目现有的 DeepSeek 配置（config.json → 环境变量 DEEPSEEK_API_KEY → agent_core.py），
       把大模型当成"规则逆向工程师"，对比客服的输入（对话上下文）与输出（客服真人发出的原话）；
    4. 提炼：① 固定句式库 ② 避坑策略 ③ 索要信息边界 ④ 3 组最精炼的 Few-Shot 范例；
    5. 结果写入 distilled_patch.txt（供人工查看），可用 --append 追加进补丁库。

用法：
    python distill_rules.py                        # 最近 20 条 -> distilled_patch.txt
    python distill_rules.py --limit 30 --show      # 顺便把结果打印到控制台
    python distill_rules.py --dry-run              # 只打印会喂给模型的提示词，不花一分钱
    python distill_rules.py --file demonstrations.test.jsonl --limit 15
    python distill_rules.py --append               # 追加到 patch_rules.local.txt（人工确认后再执行！）
    python distill_rules.py --append patch_rules.txt

🛑 安全声明（对齐 .clinerules 红线）：
    * 本脚本只读语料、只写本地文件，**不会给玩家发送任何消息**，也不接触 bridge_server 的 WS 链路；
    * 蒸馏出来的只是"提示词补丁"，将来真正生成回复时**仍然必须过出站安全闸**（safe_outbound / sanitize_*）；
    * 脚本会对产物做一次"禁词体检"（只提示、绝不改写），提醒哪些字眼不能出现在给玩家的话术里。
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime

try:                                    # Windows 控制台/重定向下输出中文更稳（与 bridge_server 同款做法）
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        if _stream is not None:
            _stream.reconfigure(errors="replace")
except Exception:
    pass

try:
    import requests
except Exception:                       # 没装 requests 时不至于连 --dry-run 都用不了
    requests = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_FILE = os.path.join(BASE_DIR, "demonstrations.jsonl")
OUT_FILE = os.path.join(BASE_DIR, "distilled_patch.txt")
DEFAULT_APPEND_TARGET = os.path.join(BASE_DIR, "patch_rules.local.txt")   # 本地私有补丁库（优先加载、不入库）
FALLBACK_APPEND_TARGET = os.path.join(BASE_DIR, "patch_rules.txt")        # 仓库里的通用示例

DEFAULT_LIMIT = 20          # 默认样本条数（要求区间 15~30；太少没规律、太多烧 token）
MIN_LIMIT, MAX_LIMIT = 5, 60
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"

# 仅用于"体检提示"的禁词（**不是**过滤逻辑，绝不修改任何线上过滤函数）
ADVISORY_FORBIDDEN = ("BUG", "bug", "漏洞", "程序错误", "内部群", "群内客服", "补偿", "赔偿", "补发", "承诺")


def load_deepseek_conf():
    """复用项目现有配置：config.json → 环境变量 DEEPSEEK_API_KEY →（可选）agent_core.py 的常量。"""
    key, url, model = "", DEFAULT_API_URL, DEFAULT_MODEL
    cfg = {}
    cfg_path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:      # utf-8-sig：兼容带 BOM 的 config.json
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    key = str(cfg.get("deepseek_api_key") or "").strip()
    url = str(cfg.get("deepseek_api_url") or "").strip() or DEFAULT_API_URL
    model = str(cfg.get("deepseek_model") or "").strip() or DEFAULT_MODEL
    if not key:
        key = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key or url == DEFAULT_API_URL:                        # 与 agent_core 保持一致（端点以它为准）
        try:
            import agent_core
            key = key or str(getattr(agent_core, "DEEPSEEK_API_KEY", "") or "").strip()
            url = str(getattr(agent_core, "DEEPSEEK_API_URL", "") or "").strip() or url
        except Exception:
            pass
    return key, url, model


def read_demos(path, limit):
    """读 JSONL，返回最近 limit 条有效样本（文件是追加写的，天然"旧 -> 新"）。"""
    if not os.path.exists(path):
        print(f"[提示] 没有找到语料文件：{path}", file=sys.stderr)
        return []
    recs, bad = [], 0
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    bad += 1
                    continue
                if isinstance(r, dict) and str(r.get("humanReply") or "").strip():
                    recs.append(r)
    except Exception as e:
        print(f"[读取失败] {path}: {e}", file=sys.stderr)
        return []
    if bad:
        print(f"[提示] 跳过 {bad} 行无法解析的记录（不影响其余样本）")
    # 去重：同一"最后一条玩家消息 + 客服回复"只留一份（防重复派发/重复点击白烧 token）
    seen, uniq = set(), []
    for r in reversed(recs):
        tail = ""
        for m in reversed(r.get("messages") or []):
            if isinstance(m, dict) and str(m.get("sender")) == "player" and str(m.get("text") or "").strip():
                tail = " ".join(str(m.get("text")).split())
                break
        fp = hashlib.md5((tail + "||" + " ".join(str(r.get("humanReply") or "").split())).encode("utf-8")).hexdigest()
        if fp in seen:
            continue
        seen.add(fp)
        uniq.append(r)
    uniq.reverse()
    picked = uniq[-limit:] if limit > 0 else uniq
    if len(uniq) > len(picked):
        print(f"[提示] 语料共 {len(recs)} 条 → 去重后 {len(uniq)} 条 → 本次取最近 {len(picked)} 条")
    return picked


# ==================== 把样本渲染成"给模型看的材料" ====================
def render_case(idx, rec):
    msgs = rec.get("messages") or []
    lines = []
    for m in msgs[-12:]:                            # 单样本最多带 12 句上下文，防止 token 撑爆
        if not isinstance(m, dict):
            continue
        who = "客服" if str(m.get("sender")) == "agent" else "玩家"
        txt = " ".join(str(m.get("text") or "").split())
        if txt:
            lines.append(f"{who}：{txt[:400]}")
    try:
        when = datetime.fromtimestamp(int(rec.get("timestamp") or 0) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        when = "时间未知"
    return "\n".join([
        f"### 样本 {idx}（{when} · 工单 {(rec.get('name') or rec.get('groupID') or '未知')}）",
        "【玩家资料】" + (" ".join(str(rec.get("playerInfo") or "").split()) or "（未采集到）"),
        "【对话记录（末尾为最新）】",
        *(lines or ["（本次没抓到历史消息，只看客服回复）"]),
        "【客服本次实际发出的回复（人工标准答案，原文照录）】",
        str(rec.get("humanReply") or "").strip(),
        "",
    ])


SYSTEM_PROMPT = """你是一名资深的客服话术"逆向工程师"。用户会给你若干条**真实客服**处理工单的实际回复样本（含当时的对话上下文）。
你的任务：从这些真人回复里逆向蒸馏出可复用的回复规则与话术模板，供 AI 客服学习使用。

必须遵守的铁律：
1. 只做归纳，不做创作 —— 每条规则/每个模板都必须能在样本里找到依据；样本没体现的、你不确定的，宁可少写也不要编。
2. 重点抓"说得既礼貌又稳"的技巧：称呼与开场、安抚节奏、拆解问题、引导玩家操作、收尾话术。
3. 特别留意"避坑"：客服是怎么回避时间承诺与补偿的、怎么不承认异常/不暴露内部流程、怎么把责任问题转成"已为您记录并核实跟进"。
4. 输出必须是**可直接粘贴进提示词文件的纯文本**：用【】做小节标题，不要 Markdown 表格、不要代码块、不要解释你的分析过程。
5. 出现在"可对玩家说的话术"里时，绝不能包含内部流程字眼（例如内部群/专人之外的内部说法、承诺修复时间、补偿或赔偿性表述）。
6. 不要复述样本里的玩家昵称、UID、订单号等隐私信息；模板里需要身份信息时一律用 {昵称} 这类占位符。"""


def build_user_prompt(cases):
    """按"固定句式 / 避坑策略 / 索要信息边界 / 3 组 Few-Shot"四段式要求写成提示词。"""
    body = "\n".join(render_case(i + 1, r) for i, r in enumerate(cases))
    return f"""下面是从真实工作台上静默录制的 {len(cases)} 条人工客服回复样本（客服真人原话，未做任何改写）：

{body}
---
请基于以上样本，输出一份**可直接粘贴的规则补丁**，严格按下面 4 个小节的顺序与标题输出（标题必须一模一样）：

【固定句式库】
按场景分组写（例如：开场/回应模糊描述/索要凭证/引导自助操作/安抚情绪/收尾结束语）。
每条写成一行模板，用 {{占位符}} 标出可变部分，可直接套用；语言风格必须贴近样本里客服的真实口吻。
每场景 1~3 条，总共不超过 18 条；每条不超过 60 字。

【避坑策略】
逐条写"不要说什么 → 换成什么说"，并在括号里注明依据（哪条样本体现了这一点）。
只写样本里真的体现出来的坑，不要凭空补常识；每条不超过 60 字，总共不超过 12 条。

【索要信息边界】
写清：什么情况下必须先索要什么信息（截图/录屏/订单号/区服角色名/时间点）、索要时必须怎么说、
什么情况下**不该**再索要、以及玩家反复不给时怎么推进。每条不超过 60 字，总共不超过 10 条。

【Few-Shot 范例（3 组）】
恰好 3 组，最精炼、最能代表客服水准的问答对；格式严格为两行：
玩家：<问题>（≤40 字，可含 {{占位符}}）
客服：<标准回复>（≤80 字）
不要编号之外的任何额外说明文字。

最后再补一节：
【样本观察备注】
用 1~5 条说明这批样本的共同风格特征，以及样本里尚未覆盖（AI 还学不到）的场景，供人工补录样本时参考。
"""


# ==================== 调用大模型（复用项目现有 DeepSeek 配置） ====================
def call_deepseek(key, url, model, system_prompt, user_prompt, timeout=90):
    if not requests:
        print("[错误] 没装 requests（requirements.txt 里有它），无法调用接口。可先用 --dry-run 看提示词。",
              file=sys.stderr)
        return ""
    if not key:
        print("[错误] 未配置 DeepSeek 密钥：请在 config.json 填 deepseek_api_key，"
              "或设置环境变量 DEEPSEEK_API_KEY。", file=sys.stderr)
        return ""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_prompt}],
        "temperature": 0.3,          # 蒸馏要稳定复现，不需要发散
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last_err = ""
    for attempt in range(2):         # 与 agent_core 一致：网络抖动重试 1 次（HTTP 错误不重试）
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as e:
            last_err = f"网络异常: {e}"
            if attempt == 0:
                print("[提示] 请求失败，1 秒后重试一次…")
                time.sleep(1.0)
            continue
        if resp.status_code == 200:
            try:
                return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"[错误] 响应解析失败: {e}", file=sys.stderr)
                return ""
        print(f"[错误] 接口返回 HTTP {resp.status_code}：{str(resp.text)[:300]}", file=sys.stderr)
        return ""
    print(f"[错误] 调用失败：{last_err}", file=sys.stderr)
    return ""


def advisory_scan(text):
    """产物禁词体检（只提示、绝不改写）—— 这些字眼不能出现在给玩家的最终话术里。"""
    return sorted({w for w in ADVISORY_FORBIDDEN if w and w in text})


def build_header(src_file, count, model):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return ("# 由 distill_rules.py 自动生成（V8.3 · 离线工具，不参与线上链路）\n"
            f"# 生成时间：{stamp}\n"
            f"# 语料文件：{os.path.basename(src_file)}\n"
            f"# 样本条数：{count}    模型：{model}\n"
            "# ⚠️ 仅供人工查看：确认无误后再并入补丁库（python distill_rules.py --append）；\n"
            "#    这些话术将来生成回复时仍会经过出站安全闸（safe_outbound / sanitize_*），\n"
            "#    但不要人工往里塞禁词。\n")


def append_to(target, body):
    """把蒸馏结果追加到补丁库（agent_core 会把它注入提示词）。文件不存在则创建。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(target, "a", encoding="utf-8") as f:
        f.write(f"\n\n【人工示范逆向蒸馏 · {stamp}】\n" + body.rstrip() + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 demonstrations.jsonl 逆向蒸馏客服话术规则（离线工具，不给玩家发任何消息）")
    ap.add_argument("--file", default=DEMO_FILE, help="语料文件（默认 demonstrations.jsonl）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"取最近多少条样本（{MIN_LIMIT}~{MAX_LIMIT}，默认 {DEFAULT_LIMIT}）")
    ap.add_argument("--out", default=OUT_FILE, help="输出文件（默认 distilled_patch.txt）")
    ap.add_argument("--model", default="", help=f"模型名（默认取配置，兜底 {DEFAULT_MODEL}）")
    ap.add_argument("--dry-run", action="store_true", help="只打印会喂给模型的提示词，不调用接口、不花额度")
    ap.add_argument("--show", action="store_true", help="把蒸馏结果同时打印到控制台")
    ap.add_argument("--append", nargs="?", const=DEFAULT_APPEND_TARGET, default="",
                    help=f"把结果追加进补丁库（默认 {os.path.basename(DEFAULT_APPEND_TARGET)}）；"
                         f"也可写 --append patch_rules.txt")
    args = ap.parse_args(argv)

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(args.limit or DEFAULT_LIMIT)))
    cases = read_demos(args.file, limit)
    if len(cases) < 3:
        print(f"[停止] 有效样本只有 {len(cases)} 条，太少 —— 先让客服在网页上正常回复一段时间"
              f"（探针会静默记录；条数可在 /api/diag 的 demo_records 看）。")
        return 3

    key, url, model = load_deepseek_conf()
    model = str(args.model or model or DEFAULT_MODEL).strip()
    user_prompt = build_user_prompt(cases)
    print(f"[语料] {os.path.basename(args.file)} → 取用 {len(cases)} 条样本")
    print(f"[模型] {model} @ {url}（密钥：{'已配置' if key else '未配置'}）")
    print(f"[提示词] 系统 {len(SYSTEM_PROMPT)} 字 + 用户 {len(user_prompt)} 字")

    if args.dry_run:
        print("\n========== DRY-RUN：下面这些内容本应发给大模型 ==========\n")
        print("[system]\n" + SYSTEM_PROMPT + "\n")
        print("[user]\n" + user_prompt)
        print("========== DRY-RUN 结束（未调用接口、未写任何文件） ==========")
        return 0

    body = call_deepseek(key, url, model, SYSTEM_PROMPT, user_prompt)
    if not body:
        print("[停止] 模型没有返回内容（检查密钥/余额/网络后重试）。", file=sys.stderr)
        return 2

    hits = advisory_scan(body)
    warn = ""
    if hits:
        warn = ("# ⚠️ 禁词体检：产物里出现了 " + "、".join(hits)
                + " —— 这些字眼不能出现在最终发给玩家的话术里（线上会被出站安全闸改写或删除），"
                  "请人工审阅时改成中性说法。\n")
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(build_header(args.file, len(cases), model) + warn + body + "\n")
    except Exception as e:
        print(f"[错误] 写文件失败 {args.out}: {e}", file=sys.stderr)
        return 4
    print(f"[完成] 已写入 {args.out}（正文 {len(body)} 字）")
    if hits:
        print("[警告] 禁词体检命中：" + "、".join(hits) + "（仅提醒，请人工改写后再并入补丁库）")
    if args.show:
        print("\n" + "-" * 60 + "\n" + body + "\n" + "-" * 60)

    if args.append:
        target = args.append
        if not os.path.isabs(target):
            target = os.path.join(BASE_DIR, os.path.basename(target))
        try:
            append_to(target, body)
        except Exception as e:
            print(f"[错误] 追加失败 {target}: {e}", file=sys.stderr)
            return 5
        print(f"[完成] 已追加到 {target}")
        print("        提示：该文件会被注入 AI 提示词（patch_rules.local.txt 优先加载、且不入库）；"
              "改完想撤回，删掉文件里对应那段即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

