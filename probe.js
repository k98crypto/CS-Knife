// ==UserScript==
// @name         智能工单探针 (V7.4 免框选与动作回执版)
// @namespace    http://tampermonkey.net/
// @version      7.4
// @description  真实 DOM 靶点、防 Token 雪球、双音效引擎、WebSocket 指数退避重连（永不放弃）、页面内状态胶囊；V7.3 手动离线守护 + 强制状态复核；V7.4 提示音只认真新消息 + 挂起/恢复动作回执 + 分类不盲选
// @match        *://ticket.example.com/*
// @match        *://ticket.example.com/*
// @run-at       document-idle
// @noframes
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    const PROBE_VERSION = "7.4";
    console.log("🚀 [工单探针 V" + PROBE_VERSION + "] 真实靶点定位系统与防暴雷机制已就绪！");
    console.log("💡 调试入口：__probe.version() / __probe.status() / __probe.reconnect()");

    // ==================== 可调参数（也可在控制台改 __probe.config.xxx） ====================
    const PROBE_CONFIG = {
        keepManualOffline: true,    // 手动挂"离线"后，网页自己跳回在线 -> 自动改回离线（离线守护）
        manualGraceMs: 1500,        // 你自己点完状态后多久内不"抢"（留给你自己操作的时间）
        guardWindowMs: 10000,       // 两次离线守护之间的最小间隔（避免和网页互刷）
        guardMaxPerWindow: 3,       // 每次窗口内最多硬顶几次，超过就如实上报（不和网页无限对抗）
        forceStatusOnConnect: true  // 连上中继就立刻复核一次真实状态（服务端重启后不会残留旧状态）
    };

    let audioCtx = null;
    let sirenInterval = null;
    let isManualOffline = false;
    let lastIMStatus = null;          // 上一次上报过的 IM 状态，用于变化检测
    let lastUserStatusClickAt = 0;    // 用户最近一次自己点状态的时间（区分"人点的" vs "网页自己跳的"）
    let lastGuardAt = 0;              // 上次离线守护动作时间
    let guardHits = 0;                // 当前窗口内已硬顶次数
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
    // ★ V7.3 修复：只认"看得见"的节点，且优先用状态显示区（而不是下拉菜单里的选项）。
    //   旧版按 DOM 顺序取第一个文字匹配的节点，页面里只要存在一个隐藏的「IM在线」选项，
    //   就可能把"手动离线"读成"在线" —— 这就是手机端状态自己变成在线的原因之一。
    function isVisibleEl(el) {
        if (!el) return false;
        if (el.offsetParent) return true;                  // 常规可见（有定位祖先）
        if (el.offsetParent === null) {                    // 明确为 null：可能是 fixed 定位，也可能是真隐藏
            try {
                const r = el.getBoundingClientRect();
                if (r && r.width > 0 && r.height > 0) return true;
                return false;                              // 尺寸为 0 -> 确实是隐藏的
            } catch (e) { return true; }                   // 取不到尺寸就不武断判隐藏
        }
        return true;                                       // 既无 offsetParent 也无尺寸信息 -> 不拦
    }

    function statusTextOf(el) {
        if (!el) return null;
        const t = normText(el.innerText);
        return (t === 'IM离线' || t === 'IM在线' || t === 'IM忙碌') ? t : null;
    }

    function isMenuOption(el) {
        try {
            if (el.classList && el.classList.contains('el-dropdown-menu__item')) return true;
        } catch (e) {}
        return String(el.className || "").indexOf('el-dropdown-menu__item') !== -1;
    }

    function findIMStatusText() {
        const nodes = document.querySelectorAll('.el-dropdown-menu__item, span, div, button');
        let menuFallback = null;
        for (let i = 0; i < nodes.length; i++) {
            const t = statusTextOf(nodes[i]);
            if (!t) continue;
            if (!isVisibleEl(nodes[i])) continue;          // 隐藏的下拉选项绝不能当"当前状态"
            if (isMenuOption(nodes[i])) {                  // 菜单里的选项只作兜底
                if (!menuFallback) menuFallback = t;
                continue;
            }
            return t;                                      // 状态显示区优先
        }
        return menuFallback;
    }

    // 取级联选择器"最后一层面板"里的可见选项（Element UI 级联为逐层懒加载）
    function lastPaneNodes() {
        const panes = Array.from(document.querySelectorAll('.el-cascader-menu'));
        if (!panes.length) return [];
        const last = panes[panes.length - 1];
        return Array.from(last.querySelectorAll('.el-cascader-node')).filter(n => n.offsetParent !== null);
    }

    // ==================== 问题分类缓存（避免"下拉框自己跑下来"） ====================
    // 用户自己点开分类下拉时顺手把选项缓存下来；后端来问时优先给缓存，不再主动去点开。
    const CATEGORY_CACHE_MS = 30 * 60 * 1000;
    const categoryCache = { options: [], at: 0 };
    function cacheCategoryOptions(opts) {
        if (!opts || !opts.length) return;
        categoryCache.options = opts.slice(0, 200);
        categoryCache.at = Date.now();
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

    // ==================== 玩家身份解析（手机端卡片标题就靠它） ====================
    // 踩坑：右侧面板第一行往往是"玩家信息"这种**栏目名**，旧代码直接把它当玩家名，
    // 于是手机端 9 个会话全叫"玩家信息"，根本分不清谁是谁。
    const GENERIC_PANEL_WORDS = ['玩家信息', '玩家资料', '玩家详情', '客户信息', '用户信息',
                                 '基本信息', '会员信息', '信息', '玩家', '客户'];
    const FIELD_KEYS = '昵称|玩家昵称|玩家名|角色名|角色昵称|角色|游戏名|用户名|姓名|名字|昵称/账号';

    function cleanFieldValue(v) {
        let t = String(v || "").replace(/[|｜]/g, ' ').trim();
        t = t.replace(/[（(][^)）]*[)）]/g, ' ').trim();        // 去掉括号里的补充说明
        if (!t || t.length > 24) return "";
        if (t.indexOf('：') !== -1 || t.indexOf(':') !== -1) return "";   // "key: value" 不是名字
        if (/^\d+$/.test(t)) return "";
        if (GENERIC_PANEL_WORDS.indexOf(t) !== -1) return "";
        if (/^(UID|ID|账号|订单|工单|时间|渠道|区服|服务器|等级|VIP|状态|来源)/i.test(t)) return "";
        return t;
    }

    // 从右侧面板文本里解析 { name, uid }
    function parsePlayerIdentity(playerInfo) {
        const raw = String(playerInfo || "").replace(/\n+/g, ' | ');
        let name = "", uid = "";
        const mu = raw.match(/(?:UID|uid|账号|account)\s*[:：]\s*([A-Za-z0-9_-]{2,24})/);
        if (mu) uid = mu[1];

        // 1) 明确的昵称字段优先：昵称：张三
        try {
            const m1 = raw.match(new RegExp('(?:' + FIELD_KEYS + ')\\s*[:：]\\s*([^|]{1,30})'));
            if (m1) name = cleanFieldValue(m1[1]);
        } catch (e) {}

        // 2) 逐段挑"看起来像名字"的段落
        if (!name) {
            const parts = raw.split('|');
            for (let i = 0; i < parts.length; i++) {
                let seg = String(parts[i]).trim();
                try { seg = seg.replace(new RegExp('^(?:' + FIELD_KEYS + ')\\s*[:：]\\s*'), ''); } catch (e) {}
                const v = cleanFieldValue(seg);
                if (v) { name = v; break; }
            }
        }
        // 3) 实在没有昵称：用 UID 后 4 位兜底，至少能区分不同玩家
        if (!name && uid) name = '玩家' + String(uid).slice(-4);
        return { name: name, uid: uid };
    }

    // 返回 { gid, name }：gid 稳定且能区分玩家；name 用于手机端会话列表显示
    let lastIdentity = null;      // { gid, playerKey }
    function buildTicketIdentity(playerInfo, messages) {
        const ident = parsePlayerIdentity(playerInfo);
        const name = ident.name || "玩家";

        // ★ 玩家指纹只用"身份"字段，**绝不含聊天内容**：
        //   旧版把"第一条玩家消息"混进指纹，列表虚拟滚动/重新渲染就会换指纹，
        //   同一个工单被算成两个会话 —— 手机端于是出现重复卡片。
        const playerKey = (ident.uid || '') + "##" + (ident.name || '');
        let gid = pickTicketId();

        if (gid) {
            // 有工单号：若上次只有兜底 id 且还是同一个玩家，继续沿用兜底 id（避免分裂成两条）
            if (lastIdentity && lastIdentity.playerKey === playerKey
                && lastIdentity.gid && lastIdentity.gid.charAt(0) === 'P') {
                gid = lastIdentity.gid;
            }
        } else if (playerKey.trim() !== '##') {
            gid = "P" + simpleHash(playerKey);
        } else {
            // 连昵称/UID 都读不到：才退回"面板文本 + 首条玩家消息"（并尽量保持连续）
            let seedText = "";
            for (let i = 0; i < messages.length; i++) {
                if (messages[i].sender === 'player') { seedText = messages[i].text; break; }
            }
            gid = "P" + simpleHash((playerInfo || "") + "##" + seedText);
        }

        lastIdentity = { gid: gid, playerKey: playerKey };
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
    // 记录"人点击"的时间，用于把"客服自己选的"和"网页自己跳的"区分开：
    // 只有"网页自己跳"的状态才会被离线守护改回去。
    document.addEventListener('click', (e) => {
        initAudio();
        if (e.target && e.target.innerText) {
            const text = normText(e.target.innerText);
            if (text === 'IM离线') {
                isManualOffline = true;
                lastUserStatusClickAt = Date.now();
            } else if (text === 'IM在线' || text === 'IM忙碌') {
                isManualOffline = false;
                lastUserStatusClickAt = Date.now();
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

    // ==================== 动作结果回报（手机端不再"点完没反应"） ====================
    // 手机端点挂起/关单/发送后，探针把"到底点到没有、页面按钮叫什么"回传，
    // 由中继转成手机上的提示，避免"没反应"变成无从排查。
    function reportActionResult(command, ok, detail) {
        sendToBrain({ event: "ACTION_RESULT", data: { command: command, ok: !!ok, detail: detail || "" } });
        console.log((ok ? "✅ [探针] " : "❌ [探针] ") + command + " -> " + (detail || (ok ? "成功" : "失败")));
    }

    // ==================== DOM 操作器 ====================
    const Operator = {
        fillReplyBox: function(text) {
            const composer = document.querySelector('.editor-composer');
            if (!composer) return false;
            const inputBox = composer.querySelector('textarea')
                || composer.querySelector('input')
                || (composer.getAttribute('contenteditable') ? composer : null);
            if (!inputBox) return false;
            if (inputBox.tagName === 'TEXTAREA' || inputBox.tagName === 'INPUT') {
                inputBox.value = text;
                inputBox.dispatchEvent(new Event('input', { bubbles: true }));
            } else {
                inputBox.innerText = text;
                inputBox.dispatchEvent(new InputEvent('input', { bubbles: true }));
            }
            return true;
        },
        selectCategory: function(l1, l2, l3, fallbackKeyword) {
            Operator.selectCategoryByKeyword(l3 || "", [l1, l2], fallbackKeyword);
        },
        // 逐级下钻选择问题分类：关键字匹配 -> 路径提示 -> 兜底分类关键字。
        // ★ 全都匹配不上时**宁可不选**并如实回报候选列表（旧的"取第一项兜底"会把分类选错，
        //   客服还以为 AI 选对了）。叶节点"点击即选中并关闭"，全程只对末级点一次。
        selectCategoryByKeyword: function(keyword, pathHint, fallbackKeyword) {
            const kw = normText(keyword || "");
            const fb = normText(fallbackKeyword || "");
            const hints = (pathHint || []).map(normText);

            // 情况 A：扁平下拉（el-select），直接在下拉项里找关键字
            const flatTrigger = document.querySelector('.el-select input, input[placeholder*="分类"], input[placeholder*="类型"]');
            const isCascader = !!document.querySelector('.el-cascader input, input[placeholder="请选择问题分类"]');
            if (!isCascader && flatTrigger) {
                flatTrigger.click();
                setTimeout(() => {
                    const items = Array.from(document.querySelectorAll('.el-select-dropdown__item, .el-dropdown-menu__item, li'))
                        .filter(isVisibleEl)
                        .map(n => ({ el: n, text: normText(n.innerText) }))
                        .filter(o => o.text && o.text.length <= 20);
                    const hit = items.find(o => o.text === kw) || items.find(o => o.text.indexOf(kw) !== -1)
                        || (fb ? (items.find(o => o.text === fb) || items.find(o => o.text.indexOf(fb) !== -1)) : null);
                    if (hit) {
                        try { hit.el.click(); } catch (e) {}
                        reportActionResult("SELECT_CATEGORY", true, "已选择分类：" + hit.text);
                    } else {
                        try { document.body.click(); } catch (e) {}
                        reportActionResult("SELECT_CATEGORY", false,
                            "下拉里没有匹配「" + (keyword || "") + "」的分类；可选：" + items.map(o => o.text).join(" / "));
                    }
                }, 350);
                return;
            }

            const trigger = document.querySelector('.el-cascader input, input[placeholder="请选择问题分类"]');
            if (!trigger) {
                reportActionResult("SELECT_CATEGORY", false, "未找到问题分类选择器（既不是级联选择器也不是下拉框）");
                return;
            }
            trigger.click();

            let depth = 0;
            const step = () => {
                if (depth > 4) { try { document.body.click(); } catch (e) {} return; }
                const list = lastPaneNodes();
                if (!list.length) return;                 // 面板消失 => 已完成选择
                const texts = list.map(n => normText(n.innerText));
                let target = null;
                if (kw) target = list.find(n => normText(n.innerText) === kw) || list.find(n => normText(n.innerText).indexOf(kw) !== -1);
                if (!target && hints[depth]) target = list.find(n => normText(n.innerText).indexOf(hints[depth]) !== -1);
                if (!target && fb && depth > 0) target = list.find(n => normText(n.innerText).indexOf(fb) !== -1);
                if (!target) {
                    try { document.body.click(); } catch (e) {}
                    reportActionResult("SELECT_CATEGORY", false,
                        "分类候选里没有匹配「" + (keyword || "") + "」的项（第 " + (depth + 1) + " 层）；本层可选：" + texts.slice(0, 12).join(" / "));
                    return;
                }
                const picked = normText(target.innerText || "");
                const before = document.querySelectorAll(".el-cascader-menu").length;
                try { target.click(); } catch (e) {}
                depth++;
                setTimeout(() => {
                    const after = document.querySelectorAll(".el-cascader-menu").length;
                    if (after <= before) {                // 点的是叶子：已选中并关闭
                        reportActionResult("SELECT_CATEGORY", true, "已选择分类：" + picked);
                        return;
                    }
                    step();
                }, 230);
            };
            setTimeout(step, 300);
        },
        // 只读预览一级分类（不做任何选择，仅用于回报给后端/AI 参考）
        // ★ 客户反馈"电脑网页的问题分类下拉老是自己跑下来"：旧版每次探针连上都点开一次。
        //   现在：① 优先用缓存（用户自己点开时顺手采集，零打扰）；
        //        ② 缓存过期才点开一次，读完立刻再点一下收起。
        peekCategoryOptions: function() {
            return new Promise((resolve) => {
                if (categoryCache.options.length && (Date.now() - categoryCache.at) < CATEGORY_CACHE_MS) {
                    resolve(categoryCache.options.slice());
                    return;
                }
                const trigger = document.querySelector('.el-cascader input, input[placeholder="请选择问题分类"]');
                if (!trigger) { resolve([]); return; }
                const already = lastPaneNodes().map(n => normText(n.innerText)).filter(Boolean);
                if (already.length) {                    // 面板本来就开着：直接读，绝不再点
                    cacheCategoryOptions(already);
                    resolve(already.slice());
                    return;
                }
                trigger.click();
                setTimeout(() => {
                    const opts = lastPaneNodes().map(n => normText(n.innerText)).filter(Boolean).slice(0, 100);
                    cacheCategoryOptions(opts);
                    try { trigger.click(); } catch (e) {}    // 再点一下收起，不留下"自己跑下来"的下拉
                    resolve(opts);
                }, 350);
            });
        },
        // 列出当前页面上的操作按钮（排障用：手机点"挂起"没反应时，一眼看出页面按钮叫什么）
        listActionButtons: function() {
            return Array.from(document.querySelectorAll('.im-action-btn, button, .el-button, [role="button"]'))
                .filter(isVisibleEl)
                .map(el => normText(el.innerText || el.textContent || ""))
                .filter(t => t && t.length <= 8)
                .filter((t, i, arr) => arr.indexOf(t) === i)
                .slice(0, 30);
        },
        // 点击页面上的操作按钮：关键字匹配（完全相等优先、其次包含），
        // 优先点真正的按钮类元素，其次才退到 span/div；只点短标签，避免误点整块容器。
        // 返回 true/false —— 找不到就如实回报，绝不静默。
        safeClickActionBtn: function(keywords) {
            const kws = (Array.isArray(keywords) ? keywords : [keywords]).map(normText).filter(Boolean);
            if (!kws.length) return false;
            const collect = sel => Array.from(document.querySelectorAll(sel))
                .filter(isVisibleEl)
                .map(el => ({ el: el, text: normText(el.innerText || el.textContent || "") }))
                .filter(o => o.text && o.text.length <= 8);
            const groups = [
                collect('.im-action-btn, button, .el-button, [role="button"]'),
                collect('span, div, a')
            ];
            for (let g = 0; g < groups.length; g++) {
                const list = groups[g];
                if (!list.length) continue;
                for (let k = 0; k < kws.length; k++) {
                    for (let i = list.length - 1; i >= 0; i--) {      // 从后往前：更可能是叶子节点
                        if (list[i].text === kws[k]) { list[i].el.click(); return true; }
                    }
                    for (let i = list.length - 1; i >= 0; i--) {
                        if (list[i].text.indexOf(kws[k]) !== -1) { list[i].el.click(); return true; }
                    }
                }
            }
            return false;
        },
        switchIMStatus: function(targetStatus) {
            if (!targetStatus) return;
            // 触发下拉：只点"看得见"的状态显示区，避免点到隐藏节点
            const statusTrigger = Array.from(document.querySelectorAll('div, span, button')).find(el => {
                const t = statusTextOf(el);
                if (!t) return false;
                if (isMenuOption(el)) return false;
                return isVisibleEl(el);
            });
            if (statusTrigger) statusTrigger.click();

            setTimeout(() => {
                const items = Array.from(document.querySelectorAll('.el-dropdown-menu__item'))
                    .filter(item => isVisibleEl(item));      // 只点可见的菜单项
                const target = items.find(item => normText(item.innerText).indexOf(normText(targetStatus)) !== -1);
                if (target) target.click();
                else console.warn("⚠️ [探针] 未找到可见的 IM 状态选项：" + targetStatus);
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

    // 断线重连调度（指数退避 3s→30s 封顶）
    // ★ V7.3：不再"重试 10 次就彻底放弃"。旧版放弃后手机端会一直停在旧状态，
    //   看起来就像"我明明设了离线却显示在线"。现在即使中继没启动，也会每 30 秒继续试。
    function scheduleReconnect() {
        reconnectAttempts++;
        const delay = Math.min(3000 * reconnectAttempts, 30000);
        if (reconnectAttempts === maxReconnectAttempts) {
            console.warn("⚠️ [探针] 已连续重连 " + maxReconnectAttempts + " 次仍失败：请确认中继服务 (bridge_server.py) 是否已启动");
            setChip('dead', "中继未启动？点此重试");
        } else if (reconnectAttempts < maxReconnectAttempts) {
            setChip('waiting', (delay / 1000) + " 秒后第 " + reconnectAttempts + " 次重连");
        } else {
            setChip('waiting', "持续重连中（每 30 秒一次）");
        }
        clearTimeout(wsReconnectTimer);
        wsReconnectTimer = setTimeout(connectBrain, delay);
    }

    // 读一次页面上的真实状态并上报（forced=true 时即使没变化也上报，用于"复核"）
    function readAndReportIMStatus(forced) {
        const st = findIMStatusText();
        if (!st) return false;
        if (!forced && st === lastIMStatus) return false;
        lastIMStatus = st;
        sendToBrain({
            event: "IM_STATUS",
            data: { status: imStatusCode(st), manual: isManualOffline, forced: !!forced }
        });
        console.log("📡 [探针] 状态复核：" + st + (forced ? "（服务端请求）" : ""));
        return true;
    }

    // 离线守护：客服手动挂了"离线"，网页却自己跳回"在线/忙碌" -> 改回离线并如实上报
    function restoreManualOffline(flippedTo) {
        const now = Date.now();
        if (now - lastGuardAt > PROBE_CONFIG.guardWindowMs) {
            guardHits = 0;                                   // 距上次久远 -> 新窗口，重新计数
        } else if (now - lastGuardAt < 1500) {
            return true;                                     // 刚点过，等网页反应，不重复点
        }
        if (guardHits >= PROBE_CONFIG.guardMaxPerWindow) {
            // 网页就是不让改：不再硬顶，按网页真实状态上报，避免两边互相刷
            console.warn("⚠️ [探针] 网页反复把状态改回「" + flippedTo + "」，已停止强改，按网页真实状态上报");
            sendToBrain({ event: "IM_STATUS", data: { status: imStatusCode(flippedTo), manual: false, guarded: false, giveup: true } });
            return false;
        }
        lastGuardAt = now;
        guardHits++;
        console.warn("🛡️ [探针] 检测到网页把状态自动改成了「" + flippedTo + "」，按你的手动离线设置改回「IM离线」（第 " + guardHits + " 次）");
        Operator.switchIMStatus('IM离线');
        lastIMStatus = 'IM离线';                             // 先把手机端稳住，不再显示在线
        sendToBrain({ event: "IM_STATUS", data: { status: 3, manual: true, guarded: true } });
        return true;
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
            // ★ V7.3：一连上就复核一次真实状态。
            //   中继服务重启后内存里的状态是默认值，旧版只有"状态变化"才上报，
            //   于是手机端会一直停在"在线"（哪怕你早就手动挂了离线）。
            if (PROBE_CONFIG.forceStatusOnConnect) setTimeout(() => readAndReportIMStatus(true), 100);
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
                if (cmd.category) Operator.selectCategory("一级分类", "二级分类", cmd.category, cmd.defaultCategory);
            }
            else if (cmd.command === "ACTION_REPLY_CLOSE") {
                const filled = cmd.content ? Operator.fillReplyBox(cmd.content) : false;
                if (cmd.content && !filled) reportActionResult("ACTION_REPLY_CLOSE", false, "未找到回复输入框(.editor-composer)，已停止关单");
                if (!cmd.content) reportActionResult("ACTION_REPLY_CLOSE", true, "（无结束语，直接关单）");
                const path = cmd.categoryPath || ["一级分类", "二级分类"];
                if (cmd.category) {
                    Operator.selectCategory(path[0], path[1], cmd.category, cmd.defaultCategory);
                    // 留足级联下钻时间，避免分类还没选完就点了"回复并关单"
                    setTimeout(() => {
                        const ok = Operator.safeClickActionBtn(['回复并关单', '回复并关闭', '回复关闭', '关单']);
                        reportActionResult("ACTION_REPLY_CLOSE", ok,
                            ok ? "已点击「回复并关单」" : ("未找到「回复并关单」按钮，页面按钮：" + Operator.listActionButtons().join("/")));
                    }, 2200);
                } else {
                    setTimeout(() => {
                        const ok = Operator.safeClickActionBtn(['回复并关单', '回复并关闭', '回复关闭', '关单']);
                        reportActionResult("ACTION_REPLY_CLOSE", ok,
                            ok ? "已点击「回复并关单」" : ("未找到关单按钮，页面按钮：" + Operator.listActionButtons().join("/")));
                    }, 800);
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
                const ok = Operator.safeClickActionBtn(['挂起', '暂挂', '挂起工单', '暂停会话', '暂停']);
                reportActionResult("ACTION_HANGUP", ok,
                    ok ? "已点击「挂起」" : ("未找到「挂起」按钮，页面上的按钮：" + Operator.listActionButtons().join("/")));
            }
            else if (cmd.command === "ACTION_RESUME") {
                const ok = Operator.safeClickActionBtn(['恢复', '恢复会话', '继续', '重新接入', '接单', '接入']);
                reportActionResult("ACTION_RESUME", ok,
                    ok ? "已点击「恢复」" : ("未找到「恢复」按钮，页面上的按钮：" + Operator.listActionButtons().join("/")));
            }
            else if (cmd.command === "LIST_ACTIONS") {
                // 排障：把页面上的按钮清单回报给手机/中继
                const labels = Operator.listActionButtons();
                reportActionResult("LIST_ACTIONS", labels.length > 0,
                    labels.length ? ("页面按钮：" + labels.join(" / ")) : "没扫到任何操作按钮");
            }
            else if (cmd.command === "SEND_REPLY") {
                const filled = Operator.fillReplyBox(cmd.content);
                if (!filled) {
                    reportActionResult("SEND_REPLY", false, "未找到回复输入框(.editor-composer)，消息没发出去");
                } else {
                    setTimeout(() => {
                        const ok = Operator.safeClickActionBtn(['发送', '回复', '发送消息']);
                        reportActionResult("SEND_REPLY", ok,
                            ok ? "已填入并点击发送" : "已填入，但没找到「发送」按钮，请手动按回车");
                    }, 500);
                }
            }
            else if (cmd.command === "CHANGE_STATUS") {
                const map = { 1: 'IM在线', 2: 'IM忙碌', 3: 'IM离线' };
                // 这是"人在手机端点的"，属于用户意图：同步手动标记 + 记一次点击时间，
                // 免得离线守护把用户刚点的"在线"又改回去。
                isManualOffline = (cmd.status === 3);
                lastUserStatusClickAt = Date.now();
                lastIMStatus = null;                 // 下一次 tick 重新读 DOM 并如实上报
                console.log("📲 [探针] 收到手机端切换状态指令：" + (map[cmd.status] || cmd.status));
                Operator.switchIMStatus(map[cmd.status]);
            }
            else if (cmd.command === "REQUEST_IM_STATUS") {
                // 手机端一打开 / 回到前台就来要一次真实状态（"从电脑网页获取一下"）
                readAndReportIMStatus(true);
            }
            else if (cmd.command === "POLICY") {
                if (cmd.keepManualOffline !== undefined) PROBE_CONFIG.keepManualOffline = !!cmd.keepManualOffline;
                console.log("⚙️ [探针] 策略同步：手动离线守护 = " + (PROBE_CONFIG.keepManualOffline ? "开启" : "关闭"));
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
        if (!currentStatus) return;

        // ★ V7.3 离线守护：客服手动挂了"离线"，网页却自己跳回"在线/忙碌"（很多 IM 在窗口
        //   重新获得焦点时会自动上线）——在你离开页面/切窗口的那一刻就把它改回离线。
        //   只处理"网页自己跳的"：你自己点的状态有 manualGraceMs 的免打扰时间。
        if (currentStatus !== lastIMStatus && currentStatus !== 'IM离线'
            && isManualOffline && PROBE_CONFIG.keepManualOffline
            && (Date.now() - lastUserStatusClickAt) > PROBE_CONFIG.manualGraceMs) {
            if (restoreManualOffline(currentStatus)) return;   // 已改回离线并上报
        }

        if (currentStatus === lastIMStatus) return;   // 未变化不重复上报

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

        // 顺手采集问题分类：用户自己点开分类下拉时零打扰地缓存下来
        try {
            const vis = lastPaneNodes().map(n => normText(n.innerText)).filter(Boolean);
            if (vis.length) cacheCategoryOptions(vis);
        } catch (e) {}

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

        // ★ 只有「当前这个会话末尾真的多了玩家新消息」才响铃。
        //   旧逻辑只看"最后一条是不是玩家发的"，于是点开历史工单、切会话、恢复挂起工单
        //   都会响 —— 那些只是"翻了翻记录"，不是新消息。
        const prevState = window._lastChatState;
        const texts = messages.map(m => m.text);
        let isNewPlayerMsg = false;
        if (prevState && prevState.gid === ident.gid && prevState.texts.length <= texts.length) {
            let samePrefix = true;                       // 历史消息必须没变（纯追加才算新消息）
            for (let i = 0; i < prevState.texts.length; i++) {
                if (prevState.texts[i] !== texts[i]) { samePrefix = false; break; }
            }
            if (samePrefix && texts.length > prevState.texts.length) {
                isNewPlayerMsg = messages.slice(prevState.texts.length).some(m => m.sender === 'player');
            }
        }
        if (isNewPlayerMsg) playDingDong();

        window._lastChatState = { gid: ident.gid, texts: texts };
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
        config: PROBE_CONFIG,          // 可直接改：__probe.config.keepManualOffline = false
        status: function () {
            const st = { version: PROBE_VERSION, kind: chipKind, detail: chipDetail,
                         imStatus: lastIMStatus, manualOffline: isManualOffline,
                         keepManualOffline: PROBE_CONFIG.keepManualOffline,
                         guardHits: guardHits,
                         wsState: ws ? ws.readyState : -1, online: !!(ws && ws.readyState === WebSocket.OPEN) };
            console.table ? console.table(st) : console.log(st);
            return st;
        },
        // 立刻去读一次页面上的真实状态并上报（手机端"重新获取状态"走的就是这个）
        refreshStatus: function () { return readAndReportIMStatus(true) ? "已上报当前状态" : "未读到 IM 状态"; },
        reconnect: function () { reconnectAttempts = 0; connectBrain(); return "已触发重连"; }
    };

    // ==================== 启动 ====================
    setChip('idle', "");
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', connectBrain);
    else connectBrain();
})();