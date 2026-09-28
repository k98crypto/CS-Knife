import os
import re
import sys
import json
import time
import random
import logging
import requests
import pandas as pd
from datetime import datetime, timezone

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_FILE_PATH = os.path.join(CURRENT_DIR, "rules.xlsx")
PATCH_FILE_PATH = os.path.join(CURRENT_DIR, "patch_rules.txt")
CONFIG_PATH = os.path.join(CURRENT_DIR, "config.json")

# ==================== 从 config.json 读取敏感配置（修复 BUG-007） ====================
def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

_config = load_config()
DEEPSEEK_API_KEY = _config.get("deepseek_api_key", "YOUR_DEEPSEEK_API_KEY")  # fallback 到旧值
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# ==================== 疑难单统一标记（前后端唯一约定） ====================
# 契约：只要回复文本中包含该标记，bridge_server / semi_runner 即判定为「规章库未收录」。
# bridge_server.py 与 semi_runner.pyw 均以字符串 "【规章库未收录" 作为判定前缀，切勿改动前缀。
HARD_CASE_MARKER = "【规章库未收录，请上报内部群核实】"

# 容错：AI 历史上可能输出的各种变体（含 sanitize_reply 二次改写后的残形），统一归一化为上面的标记
_HARD_CASE_RE = re.compile(r'【[^】]{0,40}(?:内部群|专人核实|上报群聊|F8)[^】]{0,40}】')

class CustomerServiceCore:
    def __init__(self, api_key=DEEPSEEK_API_KEY):
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        
        self.tpl_no_desc = []
        self.tpl_has_desc = []
        self.tpl_bug_known = []
        self.tpl_guide_info = []
        self.tpl_closing_normal = []
        self.tpl_closing_data = []

        self.kb_context = self.load_rules()

    def get_patch_rules(self) -> str:
        if os.path.exists(PATCH_FILE_PATH):
            try:
                with open(PATCH_FILE_PATH, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    return f"---【个人补丁库与公关铁律】---\n{content}\n"
            except Exception:
                pass
        return "---【公关铁律】---\n严禁承认BUG，严禁承诺补偿与修复时间。玩家统一尊称为【亲爱的玩家】。\n"

    def load_rules(self) -> str:
        if not os.path.exists(RULES_FILE_PATH):
            return "（暂无外部规章库）"

        try:
            excel_file = pd.ExcelFile(RULES_FILE_PATH)
            target_sheet = "常规" if "常规" in excel_file.sheet_names else excel_file.sheet_names[0]
            df = pd.read_excel(RULES_FILE_PATH, sheet_name=target_sheet)
            df.iloc[:, 0] = df.iloc[:, 0].ffill()
            df = df.fillna("")

            rules_list = []
            for index, row in df.iterrows():
                col_a = str(row.iloc[0]).strip()
                col_b = str(row.iloc[1]).strip()
                col_c = str(row.iloc[2]).strip() if len(row) > 2 else ""

                if not col_c:
                    continue

                if "没有描述问题" in col_b or "直接转人工" in col_b:
                    self.tpl_no_desc.append(col_c)
                elif "已经描述问题" in col_b:
                    self.tpl_has_desc.append(col_c)
                elif "反馈已知bug" in col_b or "已知bug" in col_c:
                    self.tpl_bug_known.append(col_c)
                elif "引导提供信息" in col_a or "提供一下录屏" in col_c:
                    self.tpl_guide_info.append(col_c)
                elif "索要玩家资料玩家未回复" in col_b:
                    self.tpl_closing_data.append(col_c)
                elif "长时间未回复" in col_b:
                    self.tpl_closing_normal.append(col_c)
                else:
                    rules_list.append(f"- 条目: {col_a} | {col_b} => 回复: {col_c}")

            if not self.tpl_no_desc:
                self.tpl_no_desc = ["亲爱的玩家，欢迎来到本游戏~ 请问有什么可以帮您？"]
            if not self.tpl_guide_info:
                self.tpl_guide_info = ["辛苦您提供一下录屏/截图，这边帮您进一步确认！"]
            if not self.tpl_closing_normal:
                self.tpl_closing_normal = ["一直没收到您的回复，这边就先不打扰您了，若后续有任何问题欢迎再次联系我们。祝您游戏愉快~"]
            if not self.tpl_closing_data:
                self.tpl_closing_data = ["一直没收到您的回复，这边就先不打扰您了，您可以把问题截图/视频在下次咨询中提供给我们。祝您游戏愉快~"]

            return "\n".join(rules_list)
        except Exception as e:
            return f"（规章库解析异常: {e}）"

    def _call_deepseek(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2
        }
        last_err = ""
        for attempt in range(2):          # 网络抖动时重试 1 次（HTTP 错误不重试）
            try:
                resp = requests.post(DEEPSEEK_API_URL, headers=self.headers, json=payload, timeout=20)
            except Exception as e:
                last_err = f"网络异常: {e}"
                if attempt == 0:
                    time.sleep(1.0)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                except Exception as e:
                    print(f"[agent_core] DeepSeek 响应解析失败: {e}", file=sys.stderr)
                    return ""
            # HTTP 非 200 属接口/鉴权问题，重试无意义
            print(f"[agent_core] DeepSeek 接口错误 HTTP {resp.status_code}", file=sys.stderr)
            return ""
        print(f"[agent_core] DeepSeek 调用失败: {last_err}", file=sys.stderr)
        return ""

    def sanitize_reply(self, text: str) -> str:
        text = re.sub(r'(?i)\bbug\b', '异常情况', text)
        text = re.sub(r'漏洞|程序错误', '当前情况', text)
        text = re.sub(r'补偿您|赔偿您|补发给您', '为您记录并跟进', text)
        # 防呆隔离：严禁把“群内客服/内部群”字眼发给玩家
        text = re.sub(r'群内人工客服|群内客服|群里的人工|内部群', '专人核实处理', text)
        return text

    def _normalize_hard_case(self, text: str) -> str:
        """把 AI 输出的疑难单提示（各种变体）归一化为统一标记 HARD_CASE_MARKER。

        必须在 sanitize_reply 之后调用：sanitize 会把 "内部群" 改写成 "专人核实处理"，
        导致标记变形（如 【需走内部群核实，建议按 F8 上报群聊】 -> 【需走专人核实处理核实，...】），
        进而使 bridge_server / semi_runner 的 "【规章库未收录" 判定永远失败。
        """
        if not text:
            return text
        # 仅当标记出现在开头附近（前 60 字）才认定为疑难单提示，避免误伤正文
        head = text[:60]
        m = _HARD_CASE_RE.search(head)
        if not m:
            return text
        return text[:m.start()] + HARD_CASE_MARKER + text[m.end():]

    # ==================== 本地零 Token 预检器（修复发言人判定） ====================
    def try_local_prefilter(self, chat_history: str) -> tuple[str, str]:
        clean_text = chat_history.strip()
        
        # 1. 玩家简单问候 (在吗/客服/你好)
        greetings_keywords = ["客服", "在吗", "有人吗", "你好", "您好", "在不在", "有人没", "hello", "hi"]
        if len(clean_text) <= 15:
            for kw in greetings_keywords:
                if kw in clean_text.lower():
                    return "GREETING", random.choice(self.tpl_no_desc)

        # 2. 找到最后一个 "客服" 或 "撤回" 标记的位置
        cs_markers = ["客服", "撤回", "√√"]
        last_cs_pos = -1
        for m in cs_markers:
            pos = clean_text.rfind(m)
            if pos > last_cs_pos:
                last_cs_pos = pos

        # ★ 关键修正：如果选区的最后这几行根本找不到客服标，或者客服标后面跟着很长的玩家提问文本，说明最后发言的是玩家！
        if last_cs_pos != -1:
            text_after_cs = clean_text[last_cs_pos:]
            # 如果客服标记之后，出现超过 6 个字符的非时间戳文本，说明玩家在客服之后又发言了！
            # 此时绝不能走本地 WAITING，必须放行给 AI 正常解答
            clean_after_cs = re.sub(r'\d{2}-\d{2}\s+\d{2}:\d{2}|翻译|撤回|√|\s+', '', text_after_cs)
            if len(clean_after_cs) > 3:
                return "NEED_AI", ""

            # 确实是客服最后发言，提取时间戳计算是否超 2 小时
            time_matches = list(re.finditer(r'(\d{2}-\d{2}\s+\d{2}:\d{2})', clean_text[:last_cs_pos + 40]))
            if time_matches:
                last_time_str = time_matches[-1].group(1)
                # 漏单防御：客服最后发的是“查询中/请稍候”，放行让 AI 补答
                if any(x in clean_text[last_cs_pos-50:last_cs_pos+20] for x in ["正在为您查询", "需要一点时间核实", "请稍等片刻"]):
                    return "NEED_AI", ""

                try:
                    now_utc = datetime.now(timezone.utc)
                    msg_time = datetime.strptime(f"{now_utc.year}-{last_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                    diff_hours = (now_utc - msg_time).total_seconds() / 3600.0

                    if diff_hours < 2.0:
                        return "WAITING", ""

                    if any(x in clean_text[last_cs_pos-60:last_cs_pos+20] for x in ["截图", "录屏", "提供一下", "凭证", "区服"]):
                        return "TIMEOUT_CLOSE", random.choice(self.tpl_closing_data)
                    else:
                        return "TIMEOUT_CLOSE", random.choice(self.tpl_closing_normal)
                except Exception:
                    pass

        return "NEED_AI", ""

    # ==================== F9 全能中枢 ====================
    def process_ticket_f9(self, chat_history: str) -> tuple[str, str]:
        # 先经本地预检
        local_tag, local_reply = self.try_local_prefilter(chat_history)
        if local_tag != "NEED_AI":
            return local_tag, local_reply

        current_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        patch = self.get_patch_rules()

        system_prompt = (
            f"你是游戏《本游戏》官方客服核心中枢。玩家统一尊称为【亲爱的玩家】。当前 UTC 时间为：【{current_utc}】。\n\n"
            "【★ 核心铁律：辨别谁是最后发言者】：\n"
            "1. 客服消息特征：带有【客服】字样、带有【撤回】或【√√】标识、或以【亲爱的玩家】开头。\n"
            "2. 玩家消息特征：工单最底部的一句提问或描述，且该句话后面没有【客服】或【撤回】标。\n"
            "3. ★【绝对禁令 - 玩家发言严禁 WAITING】：\n"
            "   只要工单最底下一条消息是【玩家】发送的（无论玩家是刚刚发送还是过了多久），绝对严禁判定为 WAITING 或 TIMEOUT_CLOSE！必须输出标签 [TAG:NORMAL]，并给出具体的业务解答！\n\n"
            "【★ 核心铁律：内部群与玩家严格隔离】：\n"
            "群聊/内部核实是客服自己的工作群，玩家绝对无法接触内部群！\n"
            "严禁在回复中对玩家说'请联系群内人工客服'、'请进群'！\n"
            "若遇到封禁无法登录自助注销、充值争议等特殊问题，回复给玩家的必须是：'该情况需要为您转交专人核实处理，请您提供相关账号信息，由工作人员协助您核实跟进'；若规章库未收录该问题，必须在回复的第一行原样输出【规章库未收录，请上报内部群核实】，第二行起才是给玩家的话术。严禁在话术中提及 F8 等快捷键或内部群字样。\n\n"
            "【分流判定规则】：\n"
            "1. 若工单刚接入/玩家未清晰描述异常（仅发了'客服？'、'在吗'）：输出 [TAG:GREETING_NO_DESC]\n"
            "2. 若玩家提了模糊问题，急需索要录屏/截图凭证：输出 [TAG:NEED_INFO]\n"
            "3. 仅当最底下一条是【客服】发送，且超 2 小时玩家未回复时才触发超时关单：\n"
            "   - 索要信息未回：输出 [TAG:TIMEOUT_CLOSE_DATA]\n"
            "   - BUG已知或解答完毕超2h：输出 [TAG:TIMEOUT_CLOSE_NORMAL]\n"
            "   - 若客服最底下一条仅为'正在查询/核实'（漏单）：绝对严禁关单，输出 [TAG:FORGOTTEN_REPLY]，并根据玩家最初问题给出正解！\n"
            "4. 仅当最底下一条是【客服】发送，且距当前未满 2 小时：输出 [TAG:WAITING]\n"
            "5. 最底下一条是【玩家】发送的具体问题：输出 [TAG:NORMAL]，对照规章库正规解答。\n\n"
            f"{patch}\n"
            f"---【表格官方规章库】---\n{self.kb_context}\n"
        )

        raw = self._call_deepseek(system_prompt, f"工单记录：\n{chat_history}")
        # ★ API 失败时 raw 为空：返回专门的标签，交给调用方提示，绝不产出可发送内容
        if not raw:
            return "API_ERROR", ""
        clean = self._normalize_hard_case(self.sanitize_reply(raw))

        if "[TAG:GREETING_NO_DESC]" in clean:
            return "GREETING", random.choice(self.tpl_no_desc)
        elif "[TAG:NEED_INFO]" in clean:
            return "NEED_INFO", random.choice(self.tpl_guide_info)
        elif "[TAG:TIMEOUT_CLOSE_DATA]" in clean:
            return "TIMEOUT_CLOSE", random.choice(self.tpl_closing_data)
        elif "[TAG:TIMEOUT_CLOSE_NORMAL]" in clean:
            return "TIMEOUT_CLOSE", random.choice(self.tpl_closing_normal)
        elif "[TAG:FORGOTTEN_REPLY]" in clean:
            body = clean.replace("[TAG:FORGOTTEN_REPLY]", "").strip()
            return "FORGOTTEN_REPLY", body
        elif "[TAG:WAITING]" in clean:
            return "WAITING", ""
        else:
            body = clean.replace("[TAG:NORMAL]", "").strip()
            return "NORMAL", body

    def polish_draft_or_instruction(self, chat_context: str, raw_draft: str) -> str:
        patch = self.get_patch_rules()
        system_prompt = (
            "你是游戏《本游戏》资深公关客服。玩家称谓必须统一为【亲爱的玩家】。\n"
            "你的任务是将选中的【客服粗略草稿】或【群内领导粗话批示】，扩充润色为通顺体面、温柔严谨的官方回复。\n"
            "严禁对玩家提及'群内客服'或'内部群'，一律表述为'专人/工作人员为您跟进核实'。\n"
            f"{patch}\n"
            "【公关铁律】\n"
            "1. 严禁出现BUG、漏洞、程序错误，严禁承诺补偿与修复时间。\n"
            "2. 保留原意的同时补齐礼貌问候与官方安抚话术，直接输出最终发给玩家的文字，不带前缀废话。"
        )
        user_content = f"工单背景：\n{chat_context}\n\n待润色草稿/批示：\n{raw_draft}"
        raw = self._call_deepseek(system_prompt, user_content)
        return self.sanitize_reply(raw)

    def summarize_player_issue(self, chat_history: str) -> str:
        system_prompt = "你是资深客服问题提炼助手。用1-2句话概括玩家的核心异常现象与诉求，严禁废话。"
        return self.sanitize_reply(self._call_deepseek(system_prompt, chat_history))