// ==UserScript==
// @name         客服助手 - 智能工单探针 (V7.2 自检可见版)
// @namespace    http://tampermonkey.net/
// @version      7.2
// @description  真实 DOM 靶点、防 Token 雪球、双音效引擎、WebSocket 指数退避重连、页面内状态胶囊（一眼确认脚本是否生效）
// @match        *://ticket.example.com/*
// @match        *://ticket.example.com/*
// @run-at       document-idle
// @noframes
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    const PROBE_VERSION = "7.2";
    console.log("🚀 [客服助手探针 V" + PROBE_VERSION + "] 真实靶点定位系统与防暴雷机制已就绪！");
    console.log("💡 调试入口：__probe.version() / __probe.status() / __probe.reconnect()");

    let audioCtx = null;
    let sirenInterval = null;
    let isManualOffline = false;
    let lastIMStatus = null;          // 上一次上报过的 IM 状态，用于变化检测
    window.__im_auth_headers = {};

    // ==================== 自检状态（页面内胶囊 + __probe 调试入口） ====================
    let chipEl = null;                // 页面内状态胶囊 DOM
    let chipKind = 'idle';            // idle | connected | waiting | dead
    let chipDetail = "";              // 附加说明（如"3 秒后重连"）
    let chipHidden = false;           // 用户手动关掉胶囊后不再自动弹出
    let lastSendAt = 0;               // 最近一次成功发出的时间戳

    // ==================== 工具函数 ====================
    // 归一化文本：去掉所有空白字符，兼容 "IM离线" 与 "IM 离线" 两种写法
    function normText(s) {
        return (s || "").replace(/\s+/g, "");
    }

    // 查找当前 IM 状态文本，返回 'IM离线' / 'IM在线' / 'IM忙碌'，找不到返回 null
    function findIMStatusText() {
        const nodes = document.querySelectorAll('.el-dropdown-menu__item, span, div, button');
        for (let i = 0; i < nodes.length; i++) {
            const t = normText(nodes[i].innerText);
            if (t === 'IM离线' || t === 'IM在线' || t === 'IM忙碌') return t;
        }
        return null;
    }

    // 取级联选择器"最后一层面板"里的可见选项（Element UI 级联为逐层懒加载）
    function lastPaneNodes() {
        const panes = Array.from(document.querySelectorAll('.el-cascader-menu'));
        if (!panes.length) return [];
        const last = panes[panes.length - 1];
        return Array.from(last.querySelectorAll('.el-cascader-node')).filter(n => n.offsetParent !== null);
    }

    // ==================== 页面内状态胶囊（一眼确认"油猴脚本到底生效了没"） ====================
    // 旧版全靠控制台日志判断，客服看不到日志就以为"脚本没了"。
    // 这里在页面左下角常驻一个小胶囊：绿=已连上中继，黄=正在重连，红=没连上。
    // 单击胶囊 = 立刻重连；点右侧 ✕ = 收起（刷新页面后重新出现）。
    const CHIP_STYLE = {
        connected: { bg: "#1B4B36", fg: "#7CE3B0", dot: "🟢", label: "已连接" },
        waiting:   { bg: "#4A3C10", fg: "#F0C674", dot: "🟡", label: "重连中" },
        dead:      { bg: "#4A1D1A", fg: "#F28B82", dot: "🔴", label: "未连接" },
        idle:      { bg: "#2A2F3A", fg: "#AAB2BF", dot: "⚪", label: "启动中" }
    };

    function ensureChip() {
        if (chipEl) return chipEl;
        try {
            if (!document || !document.body || typeof document.body.appendChild !== "function") return null;
            if (typeof document.createElement !== "function") return null;
            const box = document.createElement("div");
            if (!box || !box.style) return null;
            box.style.cssText = [
                "position:fixed", "left:12px", "bottom:12px", "z-index:2147483000",
                "display:flex", "align-items:center", "gap:6px",
                "padding:5px 10px", "border-radius:14px",
                "font:12px/1.4 'Microsoft YaHei UI',system-ui,sans-serif",
                "box-shadow:0 2px 10px rgba(0,0,0,.35)", "cursor:pointer",
                "user-select:none", "opacity:.93", "pointer-events:auto"
            ].join(";");

            const txt = document.createElement("span");
            box.appendChild(txt);

            const closeBtn = document.createElement("span");
            closeBtn.textContent = "✕";
            closeBtn.style.cssText = "opacity:.6;padding:0 2px;font-size:11px";
            box.appendChild(closeBtn);

            box.addEventListener("click", function (ev) {
                if (ev && ev.target === closeBtn) {
                    chipHidden = true;
                    box.style.display = "none";
                    return;
                }
                console.log("🔄 [探针] 手动触发重连…");
                reconnectAttempts = 0;
                connectBrain();
            });

            document.body.appendChild(box);
            chipEl = box;
            chipEl._text = txt;
            return chipEl;
        } catch (e) {
            return null;
        }
    }

    function setChip(kind, detail) {
        chipKind = kind || 'idle';
        chipDetail = detail || "";
        renderChip();
    }

    function renderChip() {
        if (chipHidden) return;
        reassertChipText();
    }

    function reassertChipText() {
        const el = ensureChip();
        if (!el || !el._text || !el.style) return;
        const cfg = CHIP_STYLE[chipKind] || CHIP_STYLE.idle;
        const extra = chipDetail ? "（" + chipDetail + "）" : "";
        try {
            el._text.textContent = cfg.dot + " 探针 v" + PROBE_VERSION + " · " + cfg.label + extra;
            el.style.background = cfg.bg;
            el.style.color = cfg.fg;
        } catch (e) {}
    }

    // ==================== 工单身份识别（区分不同玩家，手机端才能分别显示） ====================
    // 注意：旧版本把所有工单都硬编码成 "当前工单"，导致手机端永远只有一个会话、
    //       新玩家上来就把旧玩家的记录覆盖掉。这里改为生成稳定且互不相同的工单标识。
    function simpleHash(str) {
        let h = 5381;
        const s = String(str || "");
        for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
        return (h >>> 0).toString(36);
    }

    function pickTicketId() {
        // 1) URL 查询串 / hash 中的工单号
        try {
            const m = location.href.match(/(?:ticketId|ticket_id|sessionId|session_id|chatId|chat_id|orderId|order_id|id)=([A-Za-z0-9_-]{3,64})/i);
            if (m) return m[1];
        } catch (e) {}

        // 2) DOM 上常见的 data-* 工单属性
        const attrs = ['data-ticket-id', 'data-session-id', 'data-chat-id', 'data-conversation-id'];
        for (let i = 0; i < attrs.length; i++) {
            const el = document.querySelector('[' + attrs[i] + ']');
            if (el) {
                const v = el.getAttribute(attrs[i]);
                if (v) return String(v).slice(0, 64);
            }
        }

        // 3) 左侧会话列表中"当前选中"的那一项
        const actives = ['.is-active[data-id]', '.active[data-id]', '.conv-item.active',
                         '.session-item.active', '.chat-item.active', '.conversation-item.active'];
        for (let i = 0; i < actives.length; i++) {
            const el = document.querySelector(actives[i]);
            if (el) {
                const v = el.getAttribute('data-id') || el.getAttribute('data-key') || el.id;
                if (v) return String(v).slice(0, 64);
            }
        }
        return "";
    }

    // 返回 { gid, name }：gid 稳定且能区分玩家；name 用于手机端会话列表显示
    function buildTicketIdentity(playerInfo, messages) {
        const firstLine = String(playerInfo || "").split('|')[0].trim().slice(0, 20);
        const name = firstLine || "玩家";
        let gid = pickTicketId();
        if (!gid) {
            // 兜底：用"玩家信息 + 该工单最早一条玩家消息"生成稳定摘要
            let seedText = "";
            for (let i = 0; i < messages.length; i++) {
                if (messages[i].sender === 'player') { seedText = messages[i].text; break; }
            }
            gid = "P" + simpleHash((playerInfo || "") + "##" + seedText);
        }
        return { gid: gid, name: name };
    }

    // ==================== 音效引擎 ====================
    function initAudio() {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (audioCtx.state === 'suspended') audioCtx.resume();
    }

    function playDingDong() {
        initAudio();
        try {
            const now = audioCtx.currentTime;
            const osc1 = audioCtx.createOscillator(), gain1 = audioCtx.createGain();
            osc1.type = 'sine'; osc1.frequency.setValueAtTime(1046.5, now);
            gain1.gain.setValueAtTime(0, now);
            gain1.gain.linearRampToValueAtTime(0.8, now + 0.02);
            gain1.gain.exponentialRampToValueAtTime(0.01, now + 0.4);
            osc1.connect(gain1); gain1.connect(audioCtx.destination);
            osc1.start(now); osc1.stop(now + 0.5);

            const osc2 = audioCtx.createOscillator(), gain2 = audioCtx.createGain();
            osc2.type = 'sine'; osc2.frequency.setValueAtTime(1318.5, now + 0.1);
            gain2.gain.setValueAtTime(0, now + 0.1);
            gain2.gain.linearRampToValueAtTime(0.6, now + 0.12);
            gain2.gain.exponentialRampToValueAtTime(0.01, now + 0.6);
            osc2.connect(gain2); gain2.connect(audioCtx.destination);
            osc2.start(now + 0.1); osc2.stop(now + 0.7);
        } catch (e) {}
    }

    function playSiren() {
        initAudio();
        if (sirenInterval) return;
        sirenInterval = setInterval(() => {
            try {
                const now = audioCtx.currentTime;
                const osc = audioCtx.createOscillator(), gain = audioCtx.createGain();
                osc.type = 'sawtooth';
                osc.frequency.setValueAtTime(600, now);
                osc.frequency.linearRampToValueAtTime(800, now + 0.4);
                gain.gain.setValueAtTime(0.3, now);
                gain.gain.exponentialRampToValueAtTime(0.01, now + 0.8);
                osc.connect(gain); gain.connect(audioCtx.destination);
                osc.start(now); osc.stop(now + 0.8);
            } catch (e) {}
        }, 1000);
    }

    function stopSiren() {
        if (sirenInterval) { clearInterval(sirenInterval); sirenInterval = null; }
    }

    // ==================== 手动状态切换监听 ====================
    document.addEventListener('click', (e) => {
        initAudio();
        if (e.target && e.target.innerText) {
            const text = normText(e.target.innerText);
            if (text === 'IM离线') {
                isManualOffline = true;
            } else if (text === 'IM在线' || text === 'IM忙碌') {
                isManualOffline = false;
                stopSiren();
            }
        }
    }, true);

    // ==================== Token 拦截器 ====================
    const originalFetch = window.fetch;
    window.fetch = async function(...args) {
        try {
            if (args[1] && args[1].headers) {
                const h = args[1].headers instanceof Headers
                    ? Object.fromEntries(args[1].headers.entries())
                    : args[1].headers;
                for (let k in h) {
                    if (k.toLowerCase().includes('token')) window.__im_auth_headers[k] = h[k];
                }
            }
        } catch (e) {}
        return originalFetch.apply(this, args);
    };

    // ==================== DOM 操作器 ====================
    const Operator = {
        fillReplyBox: function(text) {
            const composer = document.querySelector('.editor-composer');
            if (!composer) return;
            const inputBox = composer.querySelector('textarea')
                || composer.querySelector('input')
                || (composer.getAttribute('contenteditable') ? composer : null);
            if (!inputBox) return;
            if (inputBox.tagName === 'TEXTAREA' || inputBox.tagName === 'INPUT') {
                inputBox.value = text;
                inputBox.dispatchEvent(new Event('input', { bubbles: true }));
            } else {
                inputBox.innerText = text;
                inputBox.dispatchEvent(new InputEvent('input', { bubbles: true }));
            }
        },
        selectCategory: function(l1, l2, l3) {
            Operator.selectCategoryByKeyword(l3 || "", [l1, l2]);
        },
        // 逐级下钻选择问题分类：优先按关键字匹配，其次按提示路径，最后落回第一项。
        // 叶子节点"点击即选中并关闭"，因此全程只对末级做一次选择点击，不会误改分类。
        selectCategoryByKeyword: function(keyword, pathHint) {
            const trigger = document.querySelector('.el-cascader input, input[placeholder="请选择问题分类"]');
            if (!trigger) { console.warn("⚠️ [探针] 未找到问题分类选择器，跳过分类选择"); return; }
            const kw = normText(keyword || "");
            const hints = (pathHint || []).map(normText);
            trigger.click();

            let depth = 0;
            const step = () => {
                if (depth > 4) { try { document.body.click(); } catch (e) {} return; }
                const list = lastPaneNodes();
                if (!list.length) return;                 // 面板消失 => 已完成选择
                let target = null;
                if (kw) target = list.find(n => normText(n.innerText).includes(kw));
                if (!target && hints[depth]) target = list.find(n => normText(n.innerText).includes(hints[depth]));
                if (!target) target = list[0];            // 兜底：取第一项，保证关单不卡住
                const before = document.querySelectorAll(".el-cascader-menu").length;
                try { target.click(); } catch (e) {}
                depth++;
                setTimeout(() => {
                    const after = document.querySelectorAll(".el-cascader-menu").length;
                    if (after <= before) {                // 点的是叶子：已选中并关闭
                        console.log("✅ [探针] 已选择问题分类：", normText(target.innerText || ""));
                        return;
                    }
                    step();
                }, 230);
            };
            setTimeout(step, 300);
        },
        // 只读预览一级分类（不做任何选择，仅用于回报给后端/AI 参考）
        peekCategoryOptions: function() {
            return new Promise((resolve) => {
                const trigger = document.querySelector('.el-cascader input, input[placeholder="请选择问题分类"]');
                if (!trigger) { resolve([]); return; }
                trigger.click();
                setTimeout(() => {
                    const opts = lastPaneNodes().map(n => normText(n.innerText)).filter(Boolean).slice(0, 100);
                    try { document.body.click(); } catch (e) {}   // 点空白处关闭，不触发选择
                    resolve(opts);
                }, 350);
            });
        },
        safeClickActionBtn: function(btnName) {
            const btns = Array.from(document.querySelectorAll('.im-action-btn'));
            const targetBtn = btns.find(b => b.innerText && b.innerText.trim() === btnName);
            if (targetBtn) targetBtn.click();
        },
        switchIMStatus: function(targetStatus) {
            if (!targetStatus) return;
            const statusTrigger = Array.from(document.querySelectorAll('div, span, button')).find(el => {
                const t = normText(el.innerText);
                return t.includes('IM在线') || t.includes('IM离线') || t.includes('IM忙碌');
            });
            if (statusTrigger) statusTrigger.click();

            setTimeout(() => {
                const items = Array.from(document.querySelectorAll('.el-dropdown-menu__item'));
                const target = items.find(item => normText(item.innerText).includes(targetStatus));
                if (target) target.click();
            }, 200);
        }
    };

    // ==================== WebSocket 中继连接 ====================
    let ws = null;
    let reconnectAttempts = 0;
    const maxReconnectAttempts = 10;
    let wsReconnectTimer = null;

    // 安全发送：仅在连接可用时发送，避免抛异常
    function sendToBrain(payload) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            try {
                ws.send(JSON.stringify(payload));
                lastSendAt = Date.now();
                return true;
            } catch (e) {
                return false;
            }
        }
        return false;
    }

    // 断线重连调度（指数退避，最多 maxReconnectAttempts 次）
    function scheduleReconnect() {
        if (reconnectAttempts >= maxReconnectAttempts) {
            console.error("❌ [探针] 重连已达上限，请确认中继服务 (bridge_server.py) 是否已启动");
            setChip('dead', "重连已放弃，点此重试");
            return;
        }
        reconnectAttempts++;
        const delay = Math.min(3000 * reconnectAttempts, 30000);
        setChip('waiting', (delay / 1000) + " 秒后第 " + reconnectAttempts + " 次重连");
        clearTimeout(wsReconnectTimer);
        wsReconnectTimer = setTimeout(connectBrain, delay);
        console.warn("⚠️ [探针] " + (delay / 1000) + " 秒后进行第 " + reconnectAttempts + " 次重连...");
    }

    function connectBrain() {
        // 清理旧连接，防止内存泄漏
        if (ws) {
            ws.onclose = null;
            ws.onerror = null;
            ws.onmessage = null;
            try {
                if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close();
            } catch (e) {}
            ws = null;
        }

        try {
            ws = new WebSocket('ws://127.0.0.1:8765/ws/extension');
        } catch (err) {
            console.error("❌ [探针] WebSocket 创建失败:", err);
            scheduleReconnect();
            return;
        }

        ws.onopen = () => {
            console.log("✅ [探针] WebSocket 连接成功");
            reconnectAttempts = 0;   // 连接成功后才重置重连计数
            lastIMStatus = null;     // 重连后强制把当前 IM 状态重新同步一次
            setChip('connected', "");
            // 握手：把"油猴脚本到底跑了没、跑的哪一版"变成后端可查的事实（GET /api/diag）
            const pageUrl = (function () { try { return location.href; } catch (e) { return ""; } })();
            const ua = (typeof navigator !== "undefined" && navigator.userAgent) ? navigator.userAgent.slice(0, 120) : "";
            sendToBrain({ event: "PROBE_HELLO", data: { version: PROBE_VERSION, page: pageUrl, ua: ua } });
            // 延迟发送 HEADERS_SYNC，确保 fetch 拦截器已捕获 Token
            setTimeout(() => {
                if (window.__im_auth_headers && Object.keys(window.__im_auth_headers).length > 0) {
                    sendToBrain({ event: "HEADERS_SYNC", data: window.__im_auth_headers });
                } else {
                    console.warn("⚠️ [探针] 尚未捕获到 Token，等待下一次重连时重试");
                }
            }, 500);
        };

        ws.onclose = (e) => {
            console.warn("⚠️ [探针] WebSocket 断开 (code=" + e.code + ")，准备重连");
            scheduleReconnect();
        };

        ws.onerror = () => {
            console.error("❌ [探针] WebSocket 错误（中继服务 bridge_server.py 可能未启动）");
            setChip('dead', "请确认中继服务已启动");
        };

        ws.onmessage = (event) => {
            let cmd;
            try {
                cmd = JSON.parse(event.data);
            } catch (err) {
                return;
            }
            if (!cmd || !cmd.command) return;

            // 警报确认回执
            if (cmd.command === "ALARM_CONFIRMED") {
                console.log("✅ [探针] 掉线警报已被中继确认");
                return;
            }
            if (cmd.command === "RECOVERY_CONFIRMED") return;

            if (cmd.command === "FILL_DRAFT") {
                if (cmd.content) Operator.fillReplyBox(cmd.content);
                if (cmd.category) Operator.selectCategory("一级分类", "二级分类", cmd.category);
            }
            else if (cmd.command === "ACTION_REPLY_CLOSE") {
                if (cmd.content) Operator.fillReplyBox(cmd.content);
                const path = cmd.categoryPath || ["一级分类", "二级分类"];
                if (cmd.category) {
                    Operator.selectCategory(path[0], path[1], cmd.category);
                    // 留足级联下钻时间，避免分类还没选完就点了"回复并关单"
                    setTimeout(() => Operator.safeClickActionBtn('回复并关单'), 2200);
                } else {
                    setTimeout(() => Operator.safeClickActionBtn('回复并关单'), 800);
                }
            }
            else if (cmd.command === "REQUEST_CATEGORIES") {
                Operator.peekCategoryOptions().then(opts => {
                    if (opts && opts.length) {
                        sendToBrain({ event: "CATEGORY_OPTIONS", data: { options: opts } });
                        console.log("📋 [探针] 已回报问题分类：", opts);
                    } else {
                        console.warn("⚠️ [探针] 未读到问题分类（选择器可能未渲染）");
                    }
                });
            }
            else if (cmd.command === "ACTION_HANGUP") {
                Operator.safeClickActionBtn('挂起');
            }
            else if (cmd.command === "SEND_REPLY") {
                Operator.fillReplyBox(cmd.content);
                setTimeout(() => Operator.safeClickActionBtn('回复'), 500);
            }
            else if (cmd.command === "CHANGE_STATUS") {
                const map = { 1: 'IM在线', 2: 'IM忙碌', 3: 'IM离线' };
                Operator.switchIMStatus(map[cmd.status]);
            }
            else if (cmd.command === "SILENCE_ALARM") {
                stopSiren();
            }
        };
    }

    // ==================== 页面加载完成标记（防止误判掉线） ====================
    let pageLoadComplete = false;
    setTimeout(() => { pageLoadComplete = true; }, 3000);

    // ==================== 定时任务 1：IM 状态同步 + 掉线火警监控 ====================
    function imStatusCode(text) {
        return text === 'IM在线' ? 1 : (text === 'IM忙碌' ? 2 : 3);
    }
    setInterval(() => {
        if (!pageLoadComplete) return;

        const currentStatus = findIMStatusText();     // 'IM在线' | 'IM忙碌' | 'IM离线' | null
        if (!currentStatus || currentStatus === lastIMStatus) return;   // 未变化不重复上报

        const prev = lastIMStatus;
        lastIMStatus = currentStatus;

        // 1) 无论手动还是异常，都把真实状态同步给后端
        //    （否则手机端会一直显示旧状态，手动挂"离线"更是永远同步不过去）
        sendToBrain({
            event: "IM_STATUS",
            data: { status: imStatusCode(currentStatus), manual: isManualOffline }
        });

        // 2) 只有"异常掉线"才拉警报；手动离线不报警
        if (currentStatus === 'IM离线') {
            if (isManualOffline) {
                stopSiren();
            } else {
                playSiren();
                sendToBrain({ event: "ABNORMAL_OFFLINE" });
            }
        } else {
            stopSiren();
            if (prev === 'IM离线' && currentStatus === 'IM在线') {
                isManualOffline = false;
                sendToBrain({ event: "ALARM_RECOVERED" });
            }
        }
    }, 2000);

    // ==================== 定时任务 2：工单消息抓取与上报 ====================
    setInterval(() => {
        // 仅在 IM 工作台页面抓取
        if (!document.body || !document.body.innerText.includes('IM工作台')) return;

        // 提取右侧面板玩家信息
        let playerInfo = "";
        const rightPanel = document.querySelector('.ws-right-panel');
        if (rightPanel) playerInfo = rightPanel.innerText.replace(/\n+/g, ' | ').trim();

        // 提取中间真实聊天气泡，构造数组彻底隔绝雪球
        const messages = [];
        document.querySelectorAll('.chat-bubble-row').forEach(row => {
            const isPlayer = row.classList.contains('from-player');
            const isAgent = row.classList.contains('from-agent');
            if (!isPlayer && !isAgent) return;

            const textNode = row.querySelector('.msg-rich-text') || row;
            const content = (textNode.innerText || "").trim();
            if (content) messages.push({ sender: isPlayer ? 'player' : 'agent', text: content });
        });

        if (messages.length === 0) return;

        // 识别当前工单：不同玩家/工单必须得到不同的 groupID，手机端才会分开显示
        const ident = buildTicketIdentity(playerInfo, messages);

        const currentHash = ident.gid + "||" + messages.map(m => m.text).join('').replace(/\s+/g, '');
        if (window._lastChatHash === currentHash) return;   // 内容未变化，不上报

        const payload = {
            event: "PLAYER_MESSAGE",
            data: { groupID: ident.gid, name: ident.name, messages: messages, playerInfo: playerInfo }
        };
        // 未连接时不更新 hash，等重连后自动补发
        if (!sendToBrain(payload)) return;

        const lastSender = messages[messages.length - 1].sender;
        if (lastSender === 'player' ||
            (window._lastChatHash && currentHash.length > window._lastChatHash.length)) {
            playDingDong();
        }
        window._lastChatHash = currentHash;
    }, 2000);

    // ==================== 定时任务 3：心跳 + 胶囊状态刷新 ====================
    // 心跳让后端 /api/diag 能显示"探针活着、跑的是哪一版"，胶囊让客服肉眼可见。
    setInterval(() => {
        if (!sendToBrain({
            event: "PROBE_HEARTBEAT",
            data: {
                version: PROBE_VERSION,
                page: (function () { try { return location.href; } catch (e) { return ""; } })(),
                lastSendAgoMs: lastSendAt ? (Date.now() - lastSendAt) : -1
            }
        })) {
            return;                       // 未连接时不刷新胶囊文字，避免盖掉重连倒计时
        }
        if (chipKind !== 'connected') setChip('connected', "");
    }, 30000);

    // ==================== 调试入口（客服/运维可在控制台直接调用） ====================
    window.__probe = {
        version: function () { console.log("探针版本 v" + PROBE_VERSION); return PROBE_VERSION; },
        status: function () {
            const st = { version: PROBE_VERSION, kind: chipKind, detail: chipDetail,
                         wsState: ws ? ws.readyState : -1, online: !!(ws && ws.readyState === WebSocket.OPEN) };
            console.table ? console.table(st) : console.log(st);
            return st;
        },
        reconnect: function () { reconnectAttempts = 0; connectBrain(); return "已触发重连"; }
    };

    // ==================== 启动 ====================
    setChip('idle', "");
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', connectBrain);
    else connectBrain();
})();