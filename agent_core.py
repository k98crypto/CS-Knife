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
# ★ 安全：**绝不把真实密钥写进代码**（仓库会推到 GitHub 上）。
#   取值顺序：config.json -> 环境变量 DEEPSEEK_API_KEY；都没有就给空串，
#   调用时由 _call_deepseek 给出"未配置"的明确提示，不会静默失败。
DEEPSEEK_API_KEY = (_config.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
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

        self.sheet_stats = {}      # 各工作表条目数（供日志与诊断）
        self.kb_static = ""        # 常驻小表（始终进 prompt）
        self.kb_entries = []       # 大表条目（按工单内容检索后注入）
        self._kb_grams = []        # 检索索引，惰性构建
        self._kb_mtime = None      # 规章库文件修改时间，用于热重载判断
        self.kb_context = self.load_rules()
        self._kb_mtime = self.rules_mtime()

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

    # ==================== 规章库解析（多工作表自适应） ====================
    _QA_KEYS = ("答案", "纯文本答案")

    def _cell_str(self, v) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in ("nan", "none", "nat"):
            return ""
        return s

    def _clip(self, s: str, n: int) -> str:
        s = self._cell_str(s)
        return s if len(s) <= n else s[:n] + "…"

    def _detect_sheet_mode(self, sheet: str, headers, ncols: int) -> str:
        """按表头关键字 + 有效列数判断这张表该怎么读。"""
        h = " ".join(headers)
        if "处理方式" in h:
            return "priority"
        if "话术" in h:
            return "script"
        if "进度" in h or "已知" in sheet or "bug" in sheet.lower():
            return "bug"
        if any(k in h for k in self._QA_KEYS):
            return "qa"
        if ncols == 1:
            return "block"
        if ncols == 2:
            return "qa"
        return "generic"

    def load_rules(self) -> str:
        """解析规章库 Excel 的**全部工作表**，自适应多种表结构，生成 AI 上下文。

        自动识别的排版：
          1) 话术库  「分类 | 情形 | 话术」          -> 开场/引导/关单类进模板池，其余进上下文
          2) 问答库  「问题 | 答案 / 纯文本答案」    -> Q => A
          3) 已知BUG 「问题 | 进度 / 说明」           -> 【已知问题】
          4) 优先级  「重要级别 | 问题类型 | 处理方式」-> 【优先级 T0】
          5) 长文本  「单列 / 仅首列有内容」          -> 整块说明（公告、SOP、长 FAQ）
          6) 其它                                    -> 带列名的通用拼接

        体积保护：
          · 总字数 <= kb_max_chars（默认 30000）
          · 每张表按份数均分预算（kb_sheet_budget_chars，0=自动），避免后面的表被长表挤掉
          · 单条内容 <= kb_entry_max_chars（默认 400）
        """
        self.tpl_no_desc, self.tpl_has_desc, self.tpl_bug_known = [], [], []
        self.tpl_guide_info, self.tpl_closing_normal, self.tpl_closing_data = [], [], []
        self.sheet_stats = {}

        if not os.path.exists(RULES_FILE_PATH):
            self._fill_default_templates()
            return "（暂无外部规章库）"

        try:
            # 一次读入全部工作表（比逐表 read_excel 快很多）
            all_sheets = pd.read_excel(RULES_FILE_PATH, sheet_name=None)
            entry_max = int(_config.get("kb_entry_max_chars", 400))

            sheet_entries = {}      # {sheet: [(question, answer), ...]}
            for sheet, raw_df in all_sheets.items():
                if raw_df is None or raw_df.empty:
                    self.sheet_stats[sheet] = 0
                    continue

                df = raw_df.dropna(how="all").dropna(axis=1, how="all").fillna("")
                if df.shape[1] == 0:
                    self.sheet_stats[sheet] = 0
                    continue

                headers = [str(c).strip() for c in df.columns]
                mode = self._detect_sheet_mode(sheet, headers, df.shape[1])

                # 长文本表：整块作为一个检索单元（公告 / SOP / 长 FAQ）
                if mode == "block":
                    texts = [self._cell_str(v) for v in df.iloc[:, 0].tolist()]
                    joined = "\n".join(t for t in texts if t)
                    sheet_entries[sheet] = [(f"【{sheet}】说明文档", joined)] if joined else []
                    self.sheet_stats[sheet] = 1 if joined else 0
                    continue

                if mode in ("script", "priority"):
                    df.iloc[:, 0] = df.iloc[:, 0].ffill()   # 分类 / 优先级 常是合并单元格

                entries = []
                for row in df.itertuples(index=False):
                    cells = [self._cell_str(v) for v in row]
                    if not any(cells):
                        continue

                    if mode == "script":
                        col_a = cells[0]
                        col_b = cells[1] if len(cells) > 1 else ""
                        col_c = cells[2] if len(cells) > 2 else ""
                        if not col_c:
                            continue
                        if self._classify_template(col_a, col_b, col_c):
                            continue
                        entries.append((f"{col_a} {col_b}".strip(), col_c))
                    elif mode == "qa":
                        q = cells[0]
                        a = cells[1] if len(cells) > 1 else ""
                        if not q or not a:
                            continue
                        entries.append((q, a))
                    elif mode == "bug":
                        q = cells[0]
                        st = cells[1] if len(cells) > 1 else ""
                        if not q:
                            continue
                        entries.append((f"[已知问题] {q}", st or "已记录"))
                    elif mode == "priority":
                        if not (cells[0] or (len(cells) > 1 and cells[1])):
                            continue
                        q = f"[优先级 {cells[0]}] {cells[1] if len(cells) > 1 else ''}".strip()
                        entries.append((q, cells[2] if len(cells) > 2 else ""))
                    else:  # generic：带列名拼接，避免错标
                        parts = [f"{headers[i]}: {c}" for i, c in enumerate(cells) if c]
                        if not parts:
                            continue
                        entries.append((parts[0], " | ".join(parts[1:]) or parts[0]))

                sheet_entries[sheet] = entries
                self.sheet_stats[sheet] = len(entries)

            # ---------- 分区：小表常驻静态上下文，大表进检索池 ----------
            static_lines, retrieval = [], []
            static_cap = int(_config.get("kb_static_max_chars", 12000))
            static_used = 0
            by_size = sorted(sheet_entries.items(),
                             key=lambda kv: sum(len(q) + len(a) + 8 for q, a in kv[1]))
            for sheet, ents in by_size:
                if not ents:
                    continue
                size = sum(len(q) + len(a) + 8 for q, a in ents)
                as_static = (static_used + size) <= static_cap
                for q, a in ents:
                    text = f"[{sheet}] {self._clip(q, entry_max)} => {self._clip(a, entry_max)}"
                    if as_static:
                        static_lines.append("- " + text)
                    else:
                        retrieval.append({"sheet": sheet, "text": text})
                if as_static:
                    static_used += size

            self.kb_entries = retrieval          # 供按需检索（大表）
            self._kb_grams = []                  # 检索索引，首次检索时惰性构建
            self._fill_default_templates()
            self.kb_static = "\n".join(static_lines)
            return self.kb_static
        except Exception as e:
            self._fill_default_templates()
            self.kb_static = ""
            self.kb_entries = []
            self._kb_grams = []
            print(f"[规章库] 解析失败：{e}", file=sys.stderr)
            return f"（规章库解析异常: {e}）"

    def _fill_default_templates(self):
        """模板池保底：Excel 缺失/解析失败时仍能正常开场与关单。"""
        if not self.tpl_no_desc:
            self.tpl_no_desc = ["亲爱的玩家，欢迎来到本游戏~ 请问有什么可以帮您？"]
        if not self.tpl_guide_info:
            self.tpl_guide_info = ["辛苦您提供一下录屏/截图，这边帮您进一步确认！"]
        if not self.tpl_closing_normal:
            self.tpl_closing_normal = ["一直没收到您的回复，这边就先不打扰您了，若后续有任何问题欢迎再次联系我们。祝您游戏愉快~"]
        if not self.tpl_closing_data:
            self.tpl_closing_data = ["一直没收到您的回复，这边就先不打扰您了，您可以把问题截图/视频在下次咨询中提供给我们。祝您游戏愉快~"]

    def _classify_template(self, col_a: str, col_b: str, col_c: str) -> bool:
        """把「开场 / 引导 / 关单」类固定话术归入模板池（自动去重）。

        返回 True 表示已归类（该条不必再进 AI 上下文，省 token）。
        """
        target = None
        if "没有描述问题" in col_b or "直接转人工" in col_b:
            target = self.tpl_no_desc
        elif "已经描述问题" in col_b:
            target = self.tpl_has_desc
        elif "反馈已知bug" in col_b or "已知bug" in col_c:
            target = self.tpl_bug_known
        elif "引导提供信息" in col_a or "提供一下录屏" in col_c:
            target = self.tpl_guide_info
        elif "索要玩家资料玩家未回复" in col_b:
            target = self.tpl_closing_data
        elif "长时间未回复" in col_b:
            target = self.tpl_closing_normal
        if target is None:
            return False
        if col_c not in target:
            target.append(col_c)
        return True

    # ==================== 规章库热重载（配合定时同步） ====================
    def rules_mtime(self) -> float:
        try:
            return os.path.getmtime(RULES_FILE_PATH)
        except Exception:
            return 0.0

    def reload_rules(self, force: bool = False) -> bool:
        """重载规章库；force=False 时只在文件 mtime 变化后真正重载。

        一次 stat 调用开销极低，因此可在每次处理工单前无脑调用，
        实现「改完 Excel 立即生效，无需重启服务」。
        """
        cur = self.rules_mtime()
        if not force and cur == self._kb_mtime:
            return False
        self.kb_context = self.load_rules()
        self._kb_mtime = self.rules_mtime()
        total = sum((self.sheet_stats or {}).values())
        print(f"[规章库] 已重载：常驻 {len(self.kb_static)} 字符 / 检索池 {len(self.kb_entries)} 条 "
              f"/ 共 {total} 条 / {len(self.sheet_stats)} 张表", file=sys.stderr)
        return True

    # ==================== 按需检索（RAG 简化版，零外部依赖） ====================
    @staticmethod
    def _grams(text: str) -> set:
        """中文字符 2/3-gram 集合，用于轻量关键词匹配。"""
        s = re.sub(r'[\s\-_/|,，。；;：:（）()【】\[\]"]+', '', str(text or ""))
        g = set()
        for k in (2, 3):
            for i in range(len(s) - k + 1):
                g.add(s[i:i + k])
        return g

    def search_kb(self, query: str, top_k: int = 25) -> list:
        """按与本工单的字符重合度，从检索池挑出最相关的若干条规则。"""
        if not self.kb_entries or not query:
            return []
        qg = self._grams(query)
        if not qg:
            return []
        if len(self._kb_grams) != len(self.kb_entries):
            self._kb_grams = [self._grams(e.get("text", "")) for e in self.kb_entries]

        scored = []
        for entry, grams in zip(self.kb_entries, self._kb_grams):
            if not grams:
                continue
            inter = len(qg & grams)
            if inter:
                scored.append((inter / (len(grams) ** 0.5), entry.get("text", "")))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:max(0, int(top_k))]]

    def build_kb_context(self, query: str = "") -> str:
        """组装本次请求的规章上下文：常驻小表 + 按工单内容检索命中的相关条目。

        这样既能覆盖全部 13 张表 / 数千条规则，又不会把 token 撑爆。
        """
        parts = []
        if self.kb_static:
            parts.append("---【常驻规则】---\n" + self.kb_static)

        top_k = int(_config.get("kb_retrieval_top_k", 25))
        if query and top_k > 0 and _config.get("kb_retrieval_enabled", True):
            hits = self.search_kb(query, top_k)
            if hits:
                parts.append("---【与本工单最相关的规章】---\n" + "\n".join("- " + h for h in hits))

        if not parts:
            return self.kb_static or "（暂无外部规章库）"
        return "\n".join(parts)

    def _call_deepseek(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            print("[agent_core] 未配置 DeepSeek 密钥：请在 config.json 填 deepseek_api_key"
                  "（或设置环境变量 DEEPSEEK_API_KEY）", file=sys.stderr)
            return ""
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

    # ==================== ★ 出站安全闸（所有"可能发给玩家"的文本的唯一出口） ====================
    # 客服铁律：**绝不能出现** 内部群 / 补偿 / 赔偿 / 承诺 / 保证 / 漏洞 / BUG / 修复时间承诺 等字样，
    # 也不能把内部提示（【规章库未收录…】）甩给玩家。
    # 这里做"检测 + 改写"：能改的就改成中性话术，整块内部提示直接删掉；
    # 改完之后用 find_forbidden() 复核，确保真的一个都不剩（调用方可据此兜底）。
    OUTBOUND_RULES = [
        # 1) 内部提示整块删除（【规章库未收录，请上报内部群核实】这类）
        (r"【[^】]{0,60}(?:规章库未收录|内部群|上报群聊|专人核实|按\s*F\d{1,2}|F\d{1,2}\s*上报)[^】]{0,60}】", ""),
        (r"规章库未收录[，,]?\s*请上报[^。；;\n]{0,12}", ""),
        # 2) 内部称呼 / 群聊 / 上报 -> 中性说法
        (r"群内人工客服|群内客服|群里的人工|内部群聊|内部群|上报群聊|报备群|上报群里|上报", "专人核实处理"),
        (r"请进群[^。；;\n]{0,10}", "由专人协助您核实跟进"),
        # 3) 补偿 / 赔偿 / 补发（任何形式，连同紧跟的金额/道具一起抹掉）
        (r"补偿(您|你|玩家)?|赔偿(您|你|玩家)?|补发(给)?(您|你|玩家)?", "为您记录并跟进"),
        (r"(为您记录并跟进|为您跟进|记录并跟进)\s*\d+\s*(钻石|宝石|金币|元|点券|道具|奖励|QB|现金)",
         "为您记录并跟进"),
        # 4) 承诺 / 保证 / 时间承诺
        (r"承诺[^。；;，,\n]{0,16}", "为您持续跟进"),
        (r"保证[^。；;，,\n]{0,16}", "会为您跟进"),
        (r"一定(会)?(为您)?(修复|解决|处理|到账|发放)", "会为您跟进处理"),
        (r"(\d+\s*(小时|天|工作日|分钟)内?)(修复|解决|处理|到账|发放|回复)", "尽快为您跟进处理"),
        (r"(马上|立刻|立即|尽快)(为您)?(修复|解决|到账|发放)", "会尽快为您跟进"),
        # 5) 技术黑话 / 内部快捷键
        (r"(?i)\bbug\b|漏洞|程序错误", "异常情况"),
        (r"(?<![A-Za-z0-9])F([6-9]|10)(\s*快捷键)?(?![A-Za-z0-9])", "对应功能"),
    ]

    # 复核用禁词表（可被测试直接引用）
    FORBIDDEN_OUTBOUND = ["内部群", "群内客服", "群里的人工", "群聊", "上报", "补偿", "赔偿",
                          "补发", "承诺", "保证", "漏洞", "程序错误", "BUG", "bug"]

    def sanitize_outbound(self, text: str) -> str:
        """把一段即将发给玩家的文本清洗到"零禁词"。返回清洗后的文本（可能为空串）。"""
        if text is None:
            return ""
        t = str(text)
        for pat, rep in self.OUTBOUND_RULES:
            try:
                t = re.sub(pat, rep, t)
            except Exception:
                continue
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        return t

    @classmethod
    def find_forbidden(cls, text: str) -> list:
        """返回文本里命中的禁词列表（空列表 = 安全）。"""
        t = str(text or "")
        return [w for w in cls.FORBIDDEN_OUTBOUND if w in t]

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
        self.reload_rules()          # 规章库热重载：mtime 变化才真的读盘，开销极低
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
            f"---【表格官方规章库】---\n{self.build_kb_context(chat_history)}\n"
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

    # ==================== 手机端「AI 回复并关单」 ====================
    def generate_closing(self, chat_history: str, category_options=None) -> tuple:
        """生成「回复并关单」所需的 (问题分类, 结束语)。

        一次 AI 调用同时产出分类与话术；任何失败都会退回本地关单模板，
        保证手机端一键关单永远有内容可用。
        """
        self.reload_rules()
        options = category_options or _config.get("close_category_options") or []
        options = [str(x) for x in options]
        default_cat = str(_config.get("close_category_default", "其他"))
        fallback_msg = random.choice(self.tpl_closing_normal)

        if options:
            cat_hint = "必须从以下分类中【原样选择一个】作为 category：" + "、".join(options)
        else:
            cat_hint = f'若无法判断分类，category 直接输出 "{default_cat}"。'

        patch = self.get_patch_rules()
        system_prompt = (
            "你是游戏《本游戏》官方客服。玩家统一尊称为【亲爱的玩家】。\n"
            "现在需要为这通工单**收尾关单**。请严格只输出一行 JSON，"
            "不要任何额外说明、不要 Markdown 代码块围栏：\n"
            '{"category": "问题分类", "content": "给玩家的结束语"}\n'
            "要求：\n"
            "1. 结束语礼貌收尾：说明问题已受理/已记录，欢迎再次联系；1~3 句，不要分点。\n"
            "2. 严禁出现 BUG、漏洞、程序错误等字样；严禁承诺补偿金额与修复时间。\n"
            "3. 严禁提及'群内客服''内部群'，一律表述为'专人/工作人员为您跟进核实'。\n"
            f"4. {cat_hint}\n"
            f"{patch}"
            f"---【表格官方规章库】---\n{self.build_kb_context(chat_history)}\n"
        )

        raw = self._call_deepseek(system_prompt, f"工单记录：\n{chat_history}")
        if not raw:
            return default_cat, fallback_msg

        category, content = default_cat, ""
        m = re.search(r'\{[\s\S]*\}', raw)          # 宽松解析：容忍围栏与前后废话
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    if obj.get("category"):
                        category = str(obj["category"]).strip()
                    if obj.get("content"):
                        content = str(obj["content"]).strip()
            except Exception:
                pass
        if not content:
            content = re.sub(r'```[a-zA-Z]*|```', '', raw)
            content = re.sub(r'\{[\s\S]*\}', '', content).strip()
        if not content:
            content = fallback_msg

        content = self._normalize_hard_case(self.sanitize_reply(content))

        # 分类白名单校验：避免 AI 编出页面上点不到的分类
        if options and category not in options:
            matched = next((o for o in options if o in category or category in o), None)
            category = matched or default_cat
        return category, content

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