// ==UserScript==
// @name         质检页历史会话采集器 (QC Sweep 只读)
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  只读采集「质检明细」页的历史会话：逐行点「查看详情」→ 读对话 → 关掉 → 下一行 → 翻页，
//               每条回传 RECORD_HISTORY_SESSION 给本机中继落盘（demonstrations_history.jsonl），
//               供 distill_rules.py --mode history 逆向蒸馏话术规则。
//               ★ 绝不回复玩家、绝不修改页面数据；与工作台探针 probe.js 完全独立（各走一条 WS 通道）。
// @match        *://ticket.example.com/imChat/qc/*
// @run-at       document-idle
// @noframes
// @grant        none
// ==/UserScript==
//
// ⚠️ 这个脚本只跑在**质检页**（/imChat/qc/*），和客服工作台的 probe.js 互不影响：
//    · 它不会切会话、不会填草稿、不会发送任何消息；
//    · 它连的是中继的独立通道 /ws/qc，采集到的东西只落盘成语料，不进手机端会话列表、不推 Bark。
//    · 请从本机中继取脚本：http://127.0.0.1:8765/qc_probe.js（会自动注入你的真实域名）

(function () {
    'use strict';

    const QC_VERSION = "1.0";
    console.log("🔍 [QC 采集器 V" + QC_VERSION + "] 已加载（只读采集，不影响任何会话）");
    console.log("💡 调试入口：__qc.sweep({maxRows:60}) / __qc.status() / __qc.stop() / __qc.dump()");

    // ==================== 实机校准的选择器（来自 2026-09-30 客服 Console dump） ====================
    //  列表：table.el-table__body > tr.el-table__row > td（列序见表头：会话ID/游戏/客服/来源/语种/
    //        问题分类/玩家评分/AI质检评分/AI质检结果/是否零容忍/质检员/人工复核得分/最终评分/人工复核结果/操作）
    //  详情：点该行「查看详情」后，对话用 .message-row.player / .message-row.agent / .message-row.system
    //  ★ 这些类名都是实机 dump 出来的，不是猜的；还缺的两个（详情面板容器与关闭键）v1 用多候选兜底，
    //    并用 __qc.dump() 如实回报结构 —— 拿到实机结构后再精确化。
    const QC_SEL = {
        tableRow: 'tr.el-table__row',
        headerCell: '.el-table__header th, .el-table__header-wrapper th',
        detailBtn: 'button, .el-button',
        msgRow: '.message-row',
        msgMeta: '.message-meta',          // ★ 实机：发送者名 / 时间 / 「翻译」按钮
        msgWho: 'span',                    //   .message-meta 里的第一个非时间 span = 发送者名
        msgTime: '.message-time',
        translateBtn: '.message-translate-btn',
        msgBubble: '.message-bubble',      // ★ 实机：正文就在这里（精确取，不用正则去猜）
        pagination: '.el-pagination',
        pageItem: '.el-pagination li.number',
        pageActive: '.el-pagination li.number.is-active'
    };

    const QC_CONFIG = {
        maxRows: 60,            // 单次最多采集多少条会话
        maxPages: 10,           // 最多翻多少页（每页通常 20 条）
        minScore: 0,            // 只采集评分 >= 该值的会话（0 = 不过滤；建议 90，只喂"好样本"）
        rowDelayMs: 2200,       // 每行之间的间隔（点详情 → 读 → 关掉）
        detailTimeoutMs: 9000,  // 等对话渲染出来的上限
        closeTry: true,         // 读完是否尝试关掉详情面板（关不掉也不影响继续，见 closeDetail）
        // ★ 人机客服名单：这些"客服"其实是机器人（自动话术），采集时单独标成 bot，
        //   蒸馏时**只学真人客服的回复**。可用 __qc.config.botNames.push('智能助手') 追加。
        botNames: ['AI Bot', 'AI-Bot', 'AIBot', '机器人', '智能客服', '智能助手']
    };

    // ==================== 小工具 ====================
    function normText(s) { return String(s === undefined || s === null ? "" : s).replace(/\s+/g, " ").trim(); }
    function isVisibleEl(el) {
        if (!el) return false;
        try {
            if (el.offsetParent) return true;
            const r = el.getBoundingClientRect && el.getBoundingClientRect();
            return !!(r && r.width > 0 && r.height > 0);
        } catch (e) { return false; }
    }
    // 只触发一次点击（部分框架不认 .click()，补一组鼠标事件）——与 probe.js 同款做法
    function fireClick(el) {
        ['pointerdown', 'mousedown', 'mouseup'].forEach(t => {
            try { if (typeof Event === 'function') el.dispatchEvent(new Event(t, { bubbles: true })); } catch (e) {}
        });
        try { if (typeof el.click === 'function') { el.click(); return true; } } catch (e) {}
        try { if (typeof Event === 'function') { el.dispatchEvent(new Event('click', { bubbles: true })); return true; } } catch (e) {}
        return false;
    }

    // ==================== 页面上的进度胶囊（纯展示，不影响页面） ====================
    let chipEl = null, chipText = "QC 采集：待命";
    function setChip(text, kind) {
        chipText = text;
        try {
            if (!chipEl) {
                chipEl = document.createElement('div');
                chipEl.style.cssText = "position:fixed;left:10px;bottom:10px;z-index:2147483600;" +
                    "background:#111;color:#eee;font:12px/1.5 'Microsoft YaHei UI',sans-serif;" +
                    "padding:4px 10px;border-radius:12px;border:1px solid #444;opacity:.92;pointer-events:none";
                (document.body || document.documentElement).appendChild(chipEl);
            }
            chipEl.textContent = text;
            chipEl.style.borderColor = kind === 'err' ? '#F44747' : (kind === 'ok' ? '#4EC9B0' : '#444');
        } catch (e) {}
    }

    // ==================== WS：连中继的独立通道 /ws/qc（与工作台探针互不干扰） ====================
    let ws = null, reconnectAttempts = 0, wsTimer = null;
    function sendToRelay(payload) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify(payload)); return true; } catch (e) { return false; }
        }
        return false;
    }
    function scheduleReconnect() {
        reconnectAttempts++;
        const delay = Math.min(3000 * reconnectAttempts, 30000);
        clearTimeout(wsTimer);
        wsTimer = setTimeout(connectRelay, delay);
    }
    function connectRelay() {
        if (ws) {
            ws.onclose = ws.onerror = ws.onmessage = null;
            try { if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close(); } catch (e) {}
            ws = null;
        }
        try { ws = new WebSocket('ws://127.0.0.1:8765/ws/qc'); }
        catch (e) { console.error("❌ [QC] WebSocket 创建失败:", e); scheduleReconnect(); return; }
        ws.onopen = () => {
            reconnectAttempts = 0;
            console.log("✅ [QC] 已连上中继（/ws/qc）");
            sendToRelay({ event: "QC_HELLO", data: {
                version: QC_VERSION,
                page: (function () { try { return location.href; } catch (e) { return ""; } })(),
                ua: (typeof navigator !== "undefined" && navigator.userAgent) ? navigator.userAgent.slice(0, 120) : ""
            } });
            setChip("QC 采集：待命（已连接）");
        };
        ws.onclose = () => { console.warn("⚠️ [QC] 中继断开，准备重连"); scheduleReconnect(); };
        ws.onerror = () => {};
        ws.onmessage = (ev) => {
            try {
                let cmd = null;
                try { cmd = JSON.parse(ev.data); } catch (e) { return; }
                if (!cmd) return;
                // ★ 排障用：让采集器把"详情面板的真实结构"回报给中继（不点、不改页面）
                if (cmd.command === "QC_DUMP") { try { window.__qc.dump(); } catch (e) {} return; }
                if (cmd.command !== "QC_SWEEP") return;
                if (cmd.on === false) { stopSweep("中继要求停止"); return; }
                sweep({ maxRows: cmd.max_rows, maxPages: cmd.pages,
                        minScore: cmd.min_score, rowDelayMs: cmd.row_delay_ms });
            } catch (e) { console.warn("⚠️ [QC] 处理指令异常：", e); }
        };
    }

    // ==================== 列表解析（表头驱动映射列，绝不猜列序） ====================
    const FIELD = { id: "会话ID", game: "游戏", agent: "客服", source: "来源", lang: "语种",
                    category: "问题分类", score: "最终评分", aiScore: "AI质检评分",
                    aiResult: "AI质检结果", reviewer: "质检员" };

    function headerIndex() {
        const map = {};
        try {
            Array.from(document.querySelectorAll(QC_SEL.headerCell)).forEach((th, i) => {
                const t = normText(th.innerText);
                if (t && map[t] === undefined) map[t] = i;
            });
        } catch (e) {}
        return map;
    }
    function listRows() {
        const out = [];
        try {
            Array.from(document.querySelectorAll(QC_SEL.tableRow)).forEach(tr => {
                if (!isVisibleEl(tr)) return;
                const tds = Array.from(tr.querySelectorAll('td'));
                if (!tds.length) return;
                out.push({ el: tr, cells: tds.map(td => normText(td.innerText)) });
            });
        } catch (e) {}
        return out;
    }
    function cell(row, hidx, name) {
        const i = hidx[name];
        return (i === undefined) ? "" : normText(row.cells[i] || "");
    }
    function rowMeta(row, hidx) {
        const num = s => { const m = String(s || "").match(/-?\d+(\.\d+)?/); return m ? parseFloat(m[0]) : 0; };
        return {
            sessionId: cell(row, hidx, FIELD.id),
            game: cell(row, hidx, FIELD.game),
            agent: cell(row, hidx, FIELD.agent),
            source: cell(row, hidx, FIELD.source),
            language: cell(row, hidx, FIELD.lang),
            category: cell(row, hidx, FIELD.category),
            score: num(cell(row, hidx, FIELD.score)),
            aiScore: num(cell(row, hidx, FIELD.aiScore)),
            aiResult: cell(row, hidx, FIELD.aiResult),
            reviewer: cell(row, hidx, FIELD.reviewer)
        };
    }

    // ==================== 详情对话解析 ====================
    // ★ 实机（dump）：.message-row.player / .message-row.system / .message-row.agent
    //   整行 innerText 形如：`客服名 09-29 14:45 翻译 正文` / `玩家 09-29 14:44 翻译 正文` / `系统 09-29 14:44 正文`
    //   v1 用"去噪归一化"取正文（去掉发送者 + 时间 + 「翻译」标签）；
    //   等拿到详情面板内层结构（正文节点类名 / 关闭键）再改成精确取正文（更干净）。
    // 整行 innerText 形如：`客服名 09-29 14:45 翻译 正文` / `玩家 09-29 14:44 翻译 正文` / `系统 09-29 14:44 正文`
    //   ★ 实机踩坑（2026-09-30）：发送者名可能是**带空格的多个词**（例如 "AI Bot"），
    //     所以不能用 `^\S+\s+\d{2}-\d{2}`（它只吃到 "AI" 就匹配失败，前缀就留在正文里了）。
    //     改成"以**时间戳**为锚、且限制在前 48 个字符内"来剥离前缀 —— 正文里带日期的句子不受影响。
    //   v1 用去噪归一化取正文；等拿到详情面板内层结构（正文节点类名）再改成精确取正文（更干净）。
    // 整行结构（★ 实机 outerHTML，2026-09-30）：
    //   <div class="message-row agent">
    //     <div class="message-meta"><span>客服名</span><span class="message-time">09-29 14:45</span>
    //       <button class="... message-translate-btn"><span>翻译</span></button></div>
    //     <div class="message-bubble"><span>亲爱的联结者，您的问题我已经收到啦…</span></div>
    //   </div>
    //   → 正文**精确取 .message-bubble**（比正则去噪干净得多）；取不到才退回"去噪"兜底。
    //   → 发送者名：.message-meta 里第一个既非 .message-time、也非「翻译」按钮的 span。
    function msgKindOf(cls) {
        const c = String(cls || "");
        if (/(^|\s|-)player(\s|-|$)/.test(c)) return "player";
        if (/(^|\s|-)agent(\s|-|$)/.test(c)) return "agent";
        if (/(^|\s|-)system(\s|-|$)/.test(c)) return "system";
        return "unknown";
    }
    // 这一行的"客服"是不是机器人（人机）？——按配置名单 + 常见写法兜底判断
    function isBotName(name) {
        const n = normText(name).toLowerCase();
        if (!n) return false;
        const list = (QC_CONFIG.botNames || []).map(x => normText(x).toLowerCase()).filter(Boolean);
        if (list.some(b => n === b || n.indexOf(b) !== -1)) return true;
        return /ai[\s\-_]*bot|robot|chatbot|机器人|智能客服|智能助手/.test(n);
    }
    function senderNameOf(rowEl) {
        try {
            const meta = rowEl.querySelector(QC_SEL.msgMeta);
            if (!meta) return "";
            const spans = Array.from(meta.querySelectorAll('span'));
            for (let i = 0; i < spans.length && i < 4; i++) {
                const s = spans[i];
                if (s.classList && s.classList.contains('message-time')) continue;
                const t = normText(s.innerText);
                if (!t || t === '翻译' || t === '原文') continue;      // 「翻译」按钮里的 span 跳过
                return t.slice(0, 24);
            }
        } catch (e) {}
        return "";
    }
    // 兜底：整行文本里剥掉"发送者 + 时间 + 翻译"（仅在拿不到 .message-bubble 时用）
    function stripPrefix(raw) {
        let t = normText(raw);
        t = t.replace(/^[^\n]{0,48}?\d{2}-\d{2}\s+\d{2}:\d{2}\s*/, "")
             .replace(/^[^\n]{0,48}?\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*/, "");
        t = t.replace(/^(翻译|原文)\s*/, "").replace(/^(翻译|原文)\s*/, "");
        return t.trim();
    }
    // 归一化后如果正文**仍以时间戳/「翻译」开头**，说明没取干净（如实计数，不假装干净）
    function looksNoisy(text) {
        const t = String(text || "");
        return /^[^\n]{0,20}?\d{2}-\d{2}\s+\d{2}:\d{2}/.test(t) || /^(翻译|原文)\s/.test(t);
    }
    function readConversation() {
        const rows = Array.from(document.querySelectorAll(QC_SEL.msgRow));
        const msgs = [];
        rows.forEach(r => {
            const kind = msgKindOf(r.className);
            const who = senderNameOf(r);
            let text = "", exact = false;
            try {
                const bubble = r.querySelector(QC_SEL.msgBubble);
                if (bubble) { text = normText(bubble.innerText); exact = true; }
            } catch (e) {}
            if (!text) text = stripPrefix(r.innerText);                 // 兜底
            if (!text) return;
            let sender = kind;
            // ★ 人机客服（AI Bot 之类）单独标 bot：蒸馏时只学"真人客服"的回复
            if (kind === "agent" && (isBotName(who) || isBotName(text.slice(0, 12)))) sender = "bot";
            msgs.push({ sender: sender, text: text, who: who, exact: exact ? 1 : 0,
                        noisy: (exact ? 0 : (looksNoisy(text) ? 1 : 0)) });
        });
        return msgs;
    }
    function countNoisy(msgs) {
        return (msgs || []).filter(m => m.noisy).length;
    }
    function countBots(msgs) {
        return (msgs || []).filter(m => m.sender === "bot").length;
    }
    function convFingerprint(msgs) {
        const list = msgs || [];
        return list.length + "|" + (list.length ? list[0].text.slice(0, 30) : "") + "|" +
               (list.length ? list[list.length - 1].text.slice(0, 30) : "");
    }

    // 点这一行的「查看详情」（实机：td 里的 button.el-button--primary.is-link，文字恰好是「查看详情」）
    function clickDetailOf(rowEl) {
        try {
            const btns = Array.from(rowEl.querySelectorAll(QC_SEL.detailBtn)).filter(isVisibleEl);
            const hit = btns.filter(b => normText(b.innerText) === "查看详情")[0];
            if (!hit) return false;
            fireClick(hit);
            return true;
        } catch (e) { return false; }
    }

    // 等对话渲染出来：连续两次读到的指纹一致（且与上一条不同）才算稳定 —— 不猜任何类名，只认 .message-row
    //   ★ 必须"真的读到了消息"才算加载成功：面板空白（详情没加载出来/被清空）会一直等到超时 -> 如实计 failed，
    //     绝不把"空会话"当成一条样本（否则会把加载失败伪装成"没有客服发言"）。
    function waitConversationChange(prevFp, cb) {
        const t0 = Date.now();
        let lastFp = "", stable = 0;
        (function tick() {
            let fp = "", count = 0;
            try { const msgs = readConversation(); count = msgs.length; fp = convFingerprint(msgs); } catch (e) {}
            if (count > 0 && fp !== prevFp) {
                if (fp === lastFp) stable++; else stable = 0;
                lastFp = fp;
                if (stable >= 2) return cb(true, fp);
            } else { stable = 0; }
            if (Date.now() - t0 > QC_CONFIG.detailTimeoutMs) return cb(false, fp);
            setTimeout(tick, 400);
        })();
    }

    // 关掉详情面板：多候选 + ESC 兜底（★ 待实机校准，见 __qc.dump()）。
    //   ★ 关键：关不掉**不影响继续** —— 下一行点「查看详情」通常会把面板内容替换掉，
    //     我们靠"指纹必须变化"来判断真的读到了新会话；万一是同一个指纹，会按超时如实计入 failed。
    function closeDetail() {
        if (!QC_CONFIG.closeTry) return "skip";
        const cands = ['.el-drawer__close-btn', '.el-dialog__headerbtn', '.el-drawer__header .close',
                       'button[aria-label="Close"]', '.el-tag__close'];
        try {
            for (let i = 0; i < cands.length; i++) {
                const list = Array.from(document.querySelectorAll(cands[i])).filter(isVisibleEl);
                if (list.length) { fireClick(list[0]); return "clicked:" + cands[i]; }
            }
            // 兜底：按文字找「关闭 / ×」的按钮
            const btns = Array.from(document.querySelectorAll('button, .el-button, [role="button"]')).filter(isVisibleEl);
            const byText = btns.filter(b => ['关闭', '×', '✕', 'Close'].includes(normText(b.innerText)))[0];
            if (byText) { fireClick(byText); return "clicked:text"; }
            // 再兜底：ESC（交给页面自己的关闭逻辑）
            ['keydown', 'keyup'].forEach(type => {
                try {
                    document.dispatchEvent(new KeyboardEvent(type, { key: 'Escape', code: 'Escape',
                        keyCode: 27, which: 27, bubbles: true, cancelable: true }));
                } catch (e) {}
            });
            return "esc";
        } catch (e) { return "error"; }
    }

    // ==================== 翻页（实机：.el-pagination li.number，当前页带 .is-active） ====================
    function currentPage() {
        try {
            const a = document.querySelector(QC_SEL.pageActive);
            const n = a ? parseInt(normText(a.innerText), 10) : 1;
            return isNaN(n) ? 1 : n;
        } catch (e) { return 1; }
    }
    function goPage(n) {
        try {
            const items = Array.from(document.querySelectorAll(QC_SEL.pageItem)).filter(isVisibleEl);
            const hit = items.filter(li => parseInt(normText(li.innerText), 10) === n)[0];
            if (!hit) return false;
            fireClick(hit);
            return true;
        } catch (e) { return false; }
    }

    // ==================== 扫描主流程（纯 setTimeout 状态机：不阻塞页面、随时可停） ====================
    let SW = { running: false, stop: false, done: 0, skipped: 0, failed: 0, pages: 0,
               lastFp: "", startedAt: 0, seen: {}, lastError: "" };

    function reportProgress(extra) {
        sendToRelay({ event: "QC_SWEEP_PROGRESS", data: Object.assign({
            done: SW.done, skipped: SW.skipped, failed: SW.failed, pages: SW.pages,
            page: currentPage(), running: SW.running, elapsedMs: Date.now() - SW.startedAt,
            lastError: SW.lastError
        }, extra || {}) });
    }
    function finish(reason) {
        SW.running = false;
        const done = { done: SW.done, skipped: SW.skipped, failed: SW.failed, pages: SW.pages,
                       elapsedMs: Date.now() - SW.startedAt, reason: reason || "完成",
                       lastError: SW.lastError };
        sendToRelay({ event: "QC_SWEEP_DONE", data: done });
        setChip("QC 采集：" + (reason || "完成") + " · 已采 " + SW.done + " 条 / 跳过 " + SW.skipped, "ok");
        console.log("🔍 [QC] 扫描结束：", done);
    }
    function stopSweep(reason) {
        if (!SW.running) return "当前没有在扫描";
        SW.stop = true;
        SW.lastError = reason || "手动停止";
        return "已请求停止（当前这一条读完就停）";
    }

    function sweep(opts) {
        if (SW.running) return "已在扫描中（__qc.stop() 可停）";
        const o = opts || {};
        ["maxRows", "maxPages", "minScore", "rowDelayMs"].forEach(k => {
            if (o[k] !== undefined && o[k] !== null && !isNaN(parseFloat(o[k]))) QC_CONFIG[k] = parseFloat(o[k]);
        });
        SW = { running: true, stop: false, done: 0, skipped: 0, failed: 0, pages: 0,
               lastFp: "", startedAt: Date.now(), seen: {}, lastError: "" };
        console.log("🔍 [QC] 开始扫描：", QC_CONFIG);
        setChip("QC 采集：开始…");
        reportProgress();
        setTimeout(() => stepRow(currentPage(), 0, ""), 500);
        return "已开始（进度看页面左下角胶囊 / 中继日志 / /api/qc_status）";
    }

    function stepRow(pageNo, idx, prevFp) {
        if (!SW.running || SW.stop) return finish(SW.stop ? "已停止" : "结束");
        if (SW.done >= QC_CONFIG.maxRows) return finish("到条数上限（" + QC_CONFIG.maxRows + "）");
        const hidx = headerIndex();
        const rows = listRows();
        if (!rows.length) {
            SW.failed++;
            SW.lastError = "这一页没读到会话行（表格还没渲染好？）";
            return finish("读不到列表行");
        }
        if (idx >= rows.length) {                      // 本页读完 -> 翻下一页
            const next = pageNo + 1;
            if (next > QC_CONFIG.maxPages) return finish("到页数上限");
            if (!goPage(next)) return finish("没有第 " + next + " 页（共 " + pageNo + " 页）");
            SW.pages = pageNo;
            setChip("QC 采集：翻到第 " + next + " 页…");
            reportProgress({ page: next });
            return setTimeout(() => stepRow(next, 0, prevFp), 1600);   // 等表格刷新
        }
        const meta = rowMeta(rows[idx], hidx);
        if (!clickDetailOf(rows[idx].el)) {
            SW.failed++;
            SW.lastError = "第 " + (idx + 1) + " 行没找到「查看详情」按钮";
            reportProgress({ row: idx + 1, sessionId: meta.sessionId });
            return setTimeout(() => stepRow(pageNo, idx + 1, prevFp), 600);
        }
        setChip("QC 采集：第 " + pageNo + " 页 · 第 " + (idx + 1) + "/" + rows.length + " 条");
        const handle = (ok, fp, retried) => {
            if (!ok) {
                // ★ 实机踩坑（2026-09-30，failed:2 的真因）：详情面板没关掉时会**盖住表格**，
                //   后续点击落不到行上 -> 一直读不到新对话。所以第一次超时先"关面板 + 重新点一次"，
                //   仍不行才如实计 failed（只重试一次，不无限点）。
                if (!retried) {
                    closeDetail();
                    reportProgress({ row: idx + 1, sessionId: meta.sessionId, retry: 1 });
                    return setTimeout(() => {
                        if (!clickDetailOf(rows[idx].el)) return handle(false, fp, true);
                        waitConversationChange(prevFp, (ok2, fp2) => handle(ok2, fp2, true));
                    }, 600);
                }
                SW.failed++;
                SW.lastError = "等不到对话渲染（" + Math.round(QC_CONFIG.detailTimeoutMs / 1000)
                             + " 秒超时，已重试 1 次）";
                reportProgress({ row: idx + 1, sessionId: meta.sessionId, error: SW.lastError });
                closeDetail();
                return setTimeout(() => stepRow(pageNo, idx + 1, prevFp), 800);
            }
            const msgs = readConversation();
            const noisy = countNoisy(msgs);
            const bots = countBots(msgs);
            const hasAgent = msgs.some(m => m.sender === "agent" && m.text.length >= 2);   // ★ 只认"真人客服"
            const score = meta.score || meta.aiScore || 0;
            const key = meta.sessionId || fp;
            const skipReason = (QC_CONFIG.minScore > 0 && score && score < QC_CONFIG.minScore) ? "评分低于阈值"
                             : (!hasAgent ? "只有机器人话术/没有真人客服发言" : (msgs.length < 2 ? "消息太少" : ""));
            if (skipReason || SW.seen[key]) {
                SW.skipped++;
            } else {
                SW.seen[key] = 1;
                sendToRelay({ event: "RECORD_HISTORY_SESSION", data: {
                    sessionId: meta.sessionId, game: meta.game, agent: meta.agent,
                    source: meta.source, language: meta.language, category: meta.category,
                    score: score, aiScore: meta.aiScore, aiResult: meta.aiResult, reviewer: meta.reviewer,
                    messages: msgs, noisyMessages: noisy, botMessages: bots,
                    page: pageNo, row: idx + 1, qcVersion: QC_VERSION, scannedAt: Date.now()
                } });
                SW.done++;
            }
            const closed = closeDetail();
            reportProgress({ row: idx + 1, sessionId: meta.sessionId, category: meta.category,
                             score: score, msgs: msgs.length, noisy: noisy, bots: bots,
                             skipReason: skipReason, closed: closed });
            setTimeout(() => stepRow(pageNo, idx + 1, fp), Math.max(300, QC_CONFIG.rowDelayMs));
        };
        waitConversationChange(prevFp, (ok, fp) => handle(ok, fp, false));
    }

    // ==================== 调试入口（Console，只读） ====================
    window.__qc = {
        version: function () { console.log("QC 采集器 v" + QC_VERSION); return QC_VERSION; },
        config: QC_CONFIG,
        sweep: function (opts) { return sweep(opts); },
        stop: function () { return stopSweep("控制台手动停止"); },
        status: function () {
            const st = { version: QC_VERSION, running: SW.running, done: SW.done, skipped: SW.skipped,
                         failed: SW.failed, pages: SW.pages, page: currentPage(),
                         ws: ws ? ws.readyState : -1, chip: chipText,
                         rowsOnPage: listRows().length, columns: headerIndex(), lastError: SW.lastError };
            console.table ? console.table(st) : console.log(st);
            return st;
        },
        // ★ 把"列表 + 详情面板"的真实结构回报给中继/控制台 —— 用来精确校准
        //   「正文节点」与「关闭键」（v1 的盲区）。只读，不改页面。
        dump: function () {
            const skel = (el, depth) => {
                if (!el || depth > 3) return null;
                try {
                    return {
                        tag: el.tagName, cls: String(el.className || "").slice(0, 80),
                        text: normText(el.innerText).slice(0, 80),
                        kids: Array.from(el.children || []).slice(0, 6).map(c => skel(c, depth + 1))
                    };
                } catch (e) { return null; }
            };
            const rows = Array.from(document.querySelectorAll(QC_SEL.msgRow));
            const first = rows[0] || null;
            const chain = [];
            let n = first;
            while (n && chain.length < 6) {
                chain.push({ tag: n.tagName, cls: String(n.className || "").slice(0, 80) });
                n = n.parentElement;
            }
            const out = {
                url: (function () { try { return location.href; } catch (e) { return ""; } })(),
                rows: rows.length,
                rowKinds: rows.slice(0, 8).map(r => String(r.className)),
                firstRowSkeleton: first ? skel(first, 0) : null,
                detailChain: chain,
                visibleButtons: Array.from(document.querySelectorAll(
                        'button, [role="button"], .el-drawer__close-btn, .el-dialog__headerbtn'))
                    .filter(isVisibleEl)
                    .map(b => ({ cls: String(b.className || "").slice(0, 70), text: normText(b.innerText).slice(0, 20),
                                 aria: normText(b.getAttribute && b.getAttribute("aria-label")).slice(0, 20) }))
                    .slice(0, 25),
                pagination: (function () {
                    const p = document.querySelector(QC_SEL.pagination);
                    return p ? { text: normText(p.innerText).slice(0, 60), current: currentPage() } : null;
                })(),
                columns: headerIndex(),
                sweep: { running: SW.running, done: SW.done, skipped: SW.skipped, failed: SW.failed }
            };
            console.log(JSON.stringify(out));
            sendToRelay({ event: "QC_DEBUG", data: out });
            return out;
        }
    };

    // ==================== 启动 ====================
    setChip("QC 采集：待命");
    connectRelay();
    setInterval(() => {                       // 心跳：让中继知道采集器还活着（30 秒一次，轻量）
        sendToRelay({ event: "QC_HEARTBEAT", data: { version: QC_VERSION, running: SW.running,
                                                     done: SW.done, page: currentPage() } });
    }, 30000);
})();
