// ==UserScript==
// @name         智能工单探针 (V8.4 静默嗅探人工回复 + 玩家名与会话列表同源)
// @namespace    http://tampermonkey.net/
// @version      8.4
// @description  真实 DOM 靶点、防 Token 雪球、双音效引擎、WebSocket 指数退避重连（永不放弃）、页面内状态胶囊；V7.3 手动离线守护 + 强制状态复核；V7.4 提示音只认真新消息 + 挂起/恢复动作回执 + 分类不盲选；V7.5 发送兜底（按钮/图标/回车 + 发后复验）、挂起恢复自动重试与「更多」菜单、图标按钮与 aria/title 匹配、动作回执带工单号；V7.7 实机校准（客服 Console dump）：回复框改用 Quill 的 .ql-editor（不再误写普通 input）、发送键认 .reply-btn、只读模式如实回报、IM 状态下拉懒渲染重试 + 人手点击/中继指令分开上报；V7.8 指令自检（PING/PONG 自报家门）+ onmessage 整段兜底（出错回报 PROBE_ERROR）；V7.9 状态胶囊实机修正（Element 下拉是 hover 触发 + 触发器由内到外逐个试 + 下拉面板结构回报）；V8.0 会话列表上报（手机端"全部会话"）+ 远程/自动切会话（OPEN_CONV，回复前先切对工单）；V8.1 草稿不再冲掉客服正在写的字、切会话必须确认页面真的切过去才回执；V8.2 重连后重新上报会话列表与聊天（中继重启不再显示空白）；V8.3 新增"人工回复静默嗅探"（setupManualReplySniffer：只读录制客服真人发出的回复 + 当时的工单上下文 -> RECORD_MANUAL_DEMO，供离线 distill_rules.py 蒸馏话术规则；绝不代发、绝不影响人工点击与输入）；V8.4 修「玩家名与会话列表不同源」（workstation 页右侧面板的页签名"服务记录"曾被当成玩家名 → 与网页会话列表里的真名对不上 → 手机端把所有会话都显示成"未打开"、点了进不去、通知也匹配不上）：面板解析支持"字段名: | 值"格式 + 排除页签名 + 玩家名以**会话列表高亮行**为准
// ⚠️ 下面 @match 里的域名是**占位符**：从本机中继 http://127.0.0.1:8765/probe.js 取脚本时，
//    中继会按 config.json 的 workbench_domains 自动替换成你自己的工单工作台域名（可填多个，会自动展开成多行）。
//    请务必从该地址复制脚本，不要直接从这个文件复制。
// @match        *://ticket.example.com/*
// @run-at       document-idle
// @noframes
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    const PROBE_VERSION = "8.4";
    console.log("🚀 [工单探针 V" + PROBE_VERSION + "] 真实靶点定位系统与防暴雷机制已就绪！");
    console.log("💡 调试入口：__probe.version() / __probe.status() / __probe.reconnect()");

    // ==================== 可调参数（也可在控制台改 __probe.config.xxx） ====================
    const PROBE_CONFIG = {
        keepManualOffline: true,    // 手动挂"离线"后，网页自己跳回在线 -> 自动改回离线（离线守护）
        manualGraceMs: 1500,        // 你自己点完状态后多久内不"抢"（留给你自己操作的时间）
        guardWindowMs: 10000,       // 两次离线守护之间的最小间隔（避免和网页互刷）
        guardMaxPerWindow: 3,       // 每次窗口内最多硬顶几次，超过就如实上报（不和网页无限对抗）
        forceStatusOnConnect: true, // 连上中继就立刻复核一次真实状态（服务端重启后不会残留旧状态）
        // ★ V8.3：人工回复"影子录制"（只读嗅探，回传 RECORD_MANUAL_DEMO 给中继落盘，供离线蒸馏规则）
        demoCapture: true,          // 总开关：false = 彻底不嗅探（连监听都不注册）
        demoMinLen: 2,              // 回复正文短于这个字数就不记（防误触/防刷屏）
        demoVerifyMs: 1200          // 发出后等多久回头复验"是不是真的发出去了"（没发出去就不记）
    };

    let audioCtx = null;
    let audioReady = false;          // ★ 只有"真的能出声"才为 true（浏览器未授权时是 false）
    let audioWarned = false;         // 是否已经提示过"需要点一下页面"
    let lastSoundWarnAt = 0;         // 上报"出不了声"的节流
    let sirenInterval = null;
    let isManualOffline = false;
    // ★ V7.5：手动"离开"状态（离线或忙碌）——客服铁律：离线/忙碌都不许被网页自动改成在线
    let isManualAway = false;
    let manualAwayStatus = null;      // 'IM离线' | 'IM忙碌' | null（手动设的目标状态）
    // ★ V7.7：把"人手在网页上点的"与"中继（手机/小窗）让探针切的"分成两个标志上报。
    //   旧版共用一个 manual 标志 —— 中继的"手动状态锁"被探针自己绕过，网页真实状态反而盖掉了客服的手动选择。
    let manualByUser = false;         // 人在网页上点过状态下拉（真实人工意图）
    let manualViaRelay = false;       // 最近一次状态变更来自中继指令（手机/小窗）
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

    // 状态文本归一化：兼容 "IM在线" 与 "在线" 两种写法（页面不同位置写法不一样）
    //   返回 'IM在线' / 'IM忙碌' / 'IM离线' / null
    function normStatus(raw) {
        const t = normText(raw);
        if (!t) return null;
        const bare = t.replace(/^IM/, '');
        if (t === 'IM离线' || bare === '离线') return 'IM离线';
        if (t === 'IM在线' || bare === '在线') return 'IM在线';
        if (t === 'IM忙碌' || bare === '忙碌') return 'IM忙碌';
        return null;
    }

    function statusTextOf(el) {
        if (!el) return null;
        const byText = normStatus(el.innerText);
        if (byText) return byText;
        // ★ 兜底：只看类名（本工作台状态胶囊是 .status-trigger.is-im-online / is-im-busy / is-im-offline）
        const cls = String(el.className || '');
        if (cls.indexOf('is-im-offline') !== -1) return 'IM离线';
        if (cls.indexOf('is-im-busy') !== -1) return 'IM忙碌';
        if (cls.indexOf('is-im-online') !== -1) return 'IM在线';
        return null;
    }

    // 状态选项的"同类"判定：菜单项可能写"在线"，目标可能是"IM在线"
    function sameStatus(a, b) {
        const x = normStatus(a), y = normStatus(b);
        return !!x && x === y;
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

    // ==================== ★ V8.8：问题分类「真实菜单」读取 / 搜索 / 按路径选择 ====================
    // 🛑 铁律：分类**只能来自网页上真实存在的菜单**，绝不写死/猜测/兜底取第一项。
    //   · categoryNodeInfo()    ：把可见的级联面板项读成 { text, hasChildren }
    //   · exploreCategory(path) ：按路径**下钻读下一层**（只点"有子级"的节点 -> 不会真的选中/提交任何值）
    //   · searchCategory(kw)    ：优先用**网页自己的级联搜索框**（Element filterable 自带）搜，返回完整路径
    //   · selectCategoryPath()  ：真正关单时用 —— 逐层点到位并**逐层校验**；任一层对不上就中止并回传该层真实选项
    function categoryTrigger() {
        return document.querySelector('.el-cascader input, .el-cascader__search-input, input[placeholder="请选择问题分类"]');
    }
    function categorySearchInput() {
        return document.querySelector('.el-cascader__search-input, .el-cascader input[placeholder*="搜索"], input[placeholder*="搜索"]');
    }
    function closeCategoryDropdown() {
        try { document.body.click(); } catch (e) {}
        try {
            const esc = new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27, which: 27, bubbles: true });
            const t = categoryTrigger();
            (t || document).dispatchEvent(esc);
        } catch (e) {}
    }
    function categoryNodeInfo(n) {
        let text = "", hasChildren = false;
        try { text = normText(n.innerText || ""); } catch (e) {}
        try {
            hasChildren = !!(n.querySelector && n.querySelector(
                '.el-cascader-node__postfix, .el-icon-arrow-right, i[class*="arrow-right"]'));
            if (!hasChildren) {
                const cls = String((n.className || "") + "");
                hasChildren = cls.indexOf("is-leaf") === -1 && !!n.querySelector("i");
            }
        } catch (e) {}
        return { text: text, hasChildren: !!hasChildren };
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
                                 '基本信息', '会员信息', '信息', '玩家', '客户',
                                 // ★ V8.5.1 实机：workstation 页右侧面板顶部有一排**页签**，
                                 //   面板全文里它们排在基本信息之前，会被当成"玩家名"混进来
                                 //   （实测抓到过 "服务记录"，导致与网页会话列表里的真名对不上）
                                 '服务记录', '数据查询Agent', 'AI 智能总结', '智能总结',
                                 '暂无总结数据', '暂无数据', '收起', '展开'];
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
        // ★ V8.5.1 实机修复：页面文本里"字段名"和"值"之间隔着竖线（换行被转成 " | "），
        //   形如 `UID: | 100000432614`、`角色名: | 羽婵`。旧正则只认 `字段名: 值`，
        //   于是 UID/角色名全都解析不出来 → 退化成"取第一个像名字的段" → 抓到页签名
        //   （"服务记录"）当玩家名 → 与网页会话列表里的真名对不上，
        //   手机端把所有会话都当成"未打开"（点了进不去、通知也匹配不上）。
        const mu = raw.match(/(?:UID|uid|账号|account)\s*[:：][\s|｜]*([A-Za-z0-9_-]{2,24})/);
        if (mu) uid = mu[1];

        // 1) 明确的昵称字段优先：昵称：张三 / 角色名: | 羽婵
        try {
            const m1 = raw.match(new RegExp('(?:' + FIELD_KEYS + ')\\s*[:：][\\s|｜]*([^|｜]{1,30})'));
            if (m1) name = cleanFieldValue(m1[1]);
        } catch (e) {}

        // 2) 逐段挑"看起来像名字"的段落
        if (!name) {
            const parts = raw.split('|');
            for (let i = 0; i < parts.length; i++) {
                let seg = String(parts[i]).trim();
                try { seg = seg.replace(new RegExp('^(?:' + FIELD_KEYS + ')\\s*[:：][\\s|｜]*'), ''); } catch (e) {}
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
        let name = ident.name || "玩家";

        // ★ V8.5.1 实机修复（关键）：手机端列表里显示的会话名、以及中继"切会话 / 匹配"用的名字，
        //   都来自**网页左侧会话列表**（CONV_LIST）。而聊天区 playerInfo 解析出的名字可能与之不同
        //   （本页实测：右侧面板的页签名"服务记录"曾被当成玩家名 → 两边对不上
        //    → 手机端把所有会话都显示成"未打开"，点了进不去、通知也匹配不上）。
        //   所以：会话列表里**当前高亮那一行**的名字才是权威（与手机端同源），优先采用它。
        try {
            const _act = scanConversationList().filter(function (r) { return r.active && r.name; })[0];
            if (_act && _act.name) name = _act.name;
        } catch (e) {}

        // ★ 玩家指纹只用"身份"字段，**绝不含聊天内容**：
        //   旧版把"第一条玩家消息"混进指纹，列表虚拟滚动/重新渲染就会换指纹，
        //   同一个工单被算成两个会话 —— 手机端于是出现重复卡片。
        const playerKey = (ident.uid || '') + "##" + (name || '');
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
    // ★ V8.6 血泪修复：**浏览器要求"用户先点过/按过页面"才允许出声**（WebAudio 的硬规则）。
    //   旧版 initAudio() 只是"尝试 resume"、从不检查结果，而且只有"点击页面"才会调用它 ——
    //   客服如果只打字 + 回车（不点页面），AudioContext 会一直是 suspended：
    //   **新消息叮咚、掉线警笛会一起哑掉**（刷新/重贴脚本后必然复发，客服投诉的"怎么又不提醒了"就是它）。
    //   现在：① 每次按键/聚焦/切换可见性都尝试解锁；② 真出不了声就**如实**告诉中继（由桌面悬浮窗兜底响铃）+ 胶囊提示。
    function initAudio() {
        try {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            if (audioCtx.state === 'suspended') { try { audioCtx.resume(); } catch (e) {} }
            audioReady = (audioCtx && audioCtx.state === 'running');
            if (!audioReady && !audioWarned) {
                audioWarned = true;
                console.warn("🔇 [探针] 浏览器还没允许本页出声：请在工作台页面上**点一下或按一下键**"
                             + "（在那之前：新消息叮咚 / 掉线警笛都不会响；中继会让桌面悬浮窗兜底提醒）。");
                setChip('waiting', "🔇 点一下工作台页面才能出声");
            } else if (audioReady && audioWarned) {
                audioWarned = false;
                console.log("🔊 [探针] 提示音已恢复");
                setChip('connected', "");
            }
        } catch (e) { audioReady = false; }
        return audioReady;
    }

    // 出不了声时如实上报（节流 10 秒，避免刷屏）——中继据此让**桌面悬浮窗**兜底响铃
    function reportSoundBlocked(what) {
        const now = Date.now();
        if (now - lastSoundWarnAt < 10000) return;
        lastSoundWarnAt = now;
        sendToBrain({ event: "SOUND_BLOCKED", data: { what: what, sound_ok: false,
                                                      hint: "浏览器需要一次点击/按键才允许出声" } });
    }

    function playDingDong() {
        if (!initAudio()) { reportSoundBlocked("ding"); return false; }
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
            return true;
        } catch (e) { return false; }
    }

    function playSiren() {
        if (!initAudio()) { reportSoundBlocked("siren"); return false; }
        if (sirenInterval) return true;
        sirenInterval = setInterval(() => {
            try {
                if (!audioReady) { initAudio(); return; }      // 还没解锁：等解锁了再响
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
        return true;
    }

    // 解锁提示音：按键 / 聚焦 / 切回本页 —— 任意一个都算"用户手势"，不再依赖"必须点一下"
    function setupAudioUnlock() {
        try {
            document.addEventListener('keydown', function () {
                try { initAudio(); } catch (e) {}
            }, true);
        } catch (e) {}
        try {
            window.addEventListener('focus', function () { try { initAudio(); } catch (e) {} });
            document.addEventListener('visibilitychange', function () {
                try { if (!document.hidden) initAudio(); } catch (e) {}
            });
            window.addEventListener('pageshow', function () { try { initAudio(); } catch (e) {} });
        } catch (e) {}
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
            const text = normStatus(e.target.innerText);
            if (text) { manualByUser = true; manualViaRelay = false; }   // ★ V7.7：这是"真人点的"
            if (text === 'IM离线') {
                isManualOffline = true;
                isManualAway = true;
                manualAwayStatus = 'IM离线';
                lastUserStatusClickAt = Date.now();
            } else if (text === 'IM忙碌') {
                isManualOffline = false;
                isManualAway = true;
                manualAwayStatus = 'IM忙碌';
                lastUserStatusClickAt = Date.now();
            } else if (text === 'IM在线') {
                isManualOffline = false;
                isManualAway = false;
                manualAwayStatus = null;
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
        sendToBrain({ event: "ACTION_RESULT", data: { command: command, ok: !!ok, detail: detail || "",
                                                     groupID: lastCmdGroupID || "" } });
        console.log((ok ? "✅ [探针] " : "❌ [探针] ") + command + " -> " + (detail || (ok ? "成功" : "失败")));
    }

    // ==================== DOM 操作器 ====================
    // ---- V7.5：按钮识别的公共小工具（图标按钮 / aria / title / class 都要认） ----
    let lastCmdGroupID = "";                 // 最近一次"带工单号"的下行指令，动作回执要带回去

    function elTextsOf(el) {                 // 元素的所有"可读标签"
        const out = [];
        const push = v => { const t = normText(v || ""); if (t) out.push(t); };
        try { push(el.innerText); push(el.textContent); } catch (e) {}
        ['aria-label', 'title', 'data-title', 'alt', 'placeholder', 'name'].forEach(a => {
            try { push(el.getAttribute && el.getAttribute(a)); } catch (e) {}
        });
        return out;
    }
    function elClassOf(el) {                 // 类名（图标按钮常常只有类名线索，如 el-icon-send）
        let s = "";
        try {
            s = (typeof el.className === 'string' ? el.className : "")
                || (el.getAttribute && el.getAttribute('class')) || "";
        } catch (e) {}
        return String(s).toLowerCase();
    }
    function fireClick(el) {                 // 只触发"一次"点击：部分框架不认 .click()，补一组鼠标事件
        ['pointerdown', 'mousedown', 'mouseup'].forEach(t => {
            try { if (typeof Event === 'function') el.dispatchEvent(new Event(t, { bubbles: true })); } catch (e) {}
        });
        try { if (typeof el.click === 'function') { el.click(); return true; } } catch (e) {}
        try { if (typeof Event === 'function') { el.dispatchEvent(new Event('click', { bubbles: true })); return true; } } catch (e) {}
        return false;
    }
    // ★ V7.9（线上实机数据修正）：Element 的 el-dropdown / el-tooltip 默认是 **hover 触发** ——
    //   客服工作台的状态胶囊实测为 `span.status-trigger.is-im-online.el-tooltip__trigger`，
    //   只派发 click 永远不会打开菜单（这正是"点了 3 次都找不到选项"的真因）。补一套 hover 事件。
    function fireHover(el) {
        ['pointerenter', 'pointerover', 'mouseenter', 'mouseover', 'mousemove'].forEach(t => {
            try { if (typeof Event === 'function') el.dispatchEvent(new Event(t, { bubbles: true })); } catch (e) {}
        });
    }


    const Operator = {
        // 回复输入框（★ V7.7 实机校准：本工作台的回复框是 Quill 富文本 div.ql-editor[contenteditable]）
        //   实机路径：.chat-input-area > .editor-composer > .im-editor-container.im-rich-editor
        //             > .im-quill-editor.ql-container > .ql-editor
        //   坑①：.editor-composer 里常有一个普通 input（"请选择问题分类"），旧版优先取 input ->
        //        草稿被我写进了分类框，真编辑区一直是空的（所以"回复"按钮一直是灰的）。
        //   坑②：Quill 自带一个隐藏的 .ql-clipboard（同样是 contenteditable），绝不能往里写。
        composerInput: function() {
            const composer = document.querySelector('.editor-composer') || document.querySelector('.chat-input-area');
            if (!composer) return null;
            const notClipboard = el => String((el && el.className) || '').indexOf('ql-clipboard') === -1;
            // ① Quill 正文优先
            try {
                const ql = composer.querySelector('.ql-editor');
                if (ql && notClipboard(ql)) return ql;
            } catch (e) {}
            // ② 其它 contenteditable（排除 Quill 的隐藏剪贴板；只认 DIV/SPAN/P 这类真正的编辑容器，
            //    避免把按钮等杂节点当成输入框 —— 测试桩里就踩到过）
            try {
                const ces = Array.from(composer.querySelectorAll('[contenteditable="true"]'))
                    .filter(notClipboard)
                    .filter(el => /^(DIV|SPAN|P|SECTION|ARTICLE)$/i.test(String(el.tagName || '')));
                if (ces.length) return ces[0];
            } catch (e) {}
            // ③ 普通输入框兜底（textarea 优先，其次 input；老工作台是 textarea）
            let el = null;
            try { el = composer.querySelector('textarea') || composer.querySelector('input'); } catch (e) {}
            if (!el && composer.getAttribute && composer.getAttribute('contenteditable')) el = composer;
            return el;
        },
        // 只读模式的原因（如实告诉客服，别报含糊的"未找到输入框"）
        editorFailureReason: function() {
            try {
                const ro = document.querySelector('.editor-readonly');
                if (ro && isVisibleEl(ro)) {
                    const t = normText(ro.innerText).slice(0, 40);
                    return "当前是只读模式（" + (t || '页面提示只读') + "）——请先在左侧会话列表接入/接手该工单";
                }
            } catch (e) {}
            return "未找到回复输入框（.editor-composer / .ql-editor 都没有，页面结构可能变了）";
        },
        // 往 contenteditable（Quill）里插入文本：优先 execCommand（能触发 Quill 的 text-change，
        //   页面上的"回复"按钮才会从灰色变可点），失败再直接写 DOM 并派发完整事件序列。
        insertIntoEditor: function(el, text) {
            try { el.focus(); } catch (e) {}
            try {
                const sel = window.getSelection ? window.getSelection() : null;
                const range = document.createRange();
                range.selectNodeContents(el);
                range.collapse(false);                       // 光标放到末尾
                if (sel) { sel.removeAllRanges(); sel.addRange(range); }
            } catch (e) {}
            let ok = false;
            try { if (document.execCommand) ok = document.execCommand('insertText', false, text); } catch (e) { ok = false; }
            let got = "";
            try { got = el.innerText || el.textContent || ""; } catch (e) {}
            if (!ok || normText(got) !== normText(text)) {      // 兜底：直接写 DOM
                try { el.innerText = text; } catch (e) { try { el.textContent = text; } catch (e2) {} }
            }
            try { el.classList && el.classList.remove('ql-blank'); } catch (e) {}
            ['beforeinput', 'input', 'keyup', 'change'].forEach(t => {
                try {
                    if (t === 'beforeinput' && typeof InputEvent === 'function') {
                        el.dispatchEvent(new InputEvent(t, { bubbles: true, cancelable: true, data: text }));
                    } else {
                        el.dispatchEvent(new Event(t, { bubbles: true }));
                    }
                } catch (e) {}
            });
            return true;
        },
        fillReplyBox: function(text, allowOverwrite) {
            const inputBox = Operator.composerInput();
            if (!inputBox) return false;
            // ★ V8.0.2：输入框里已经有内容（客服自己正在写的）时**绝不覆盖**。
            //   以前 AI 草稿/开场语会把客服编辑到一半的文字直接冲掉，客服以为是"被拦截了"
            //   （原话："好像还把我自己编辑的消息拦截了，我要再发一遍才能发出去"）。
            if (!allowOverwrite) {
                let cur = "";
                try { cur = (inputBox.value !== undefined && inputBox.value !== null)
                            ? inputBox.value : (inputBox.innerText || ""); } catch (e) {}
                if (normText(String(cur)) && normText(String(cur)) !== normText(String(text))) {
                    Operator._fillSkipped = "输入框里已有内容（未覆盖，避免冲掉你正在写的字）";
                    console.log("🛑 [探针] 不覆盖输入框已有内容：" + normText(String(cur)).slice(0, 30));
                    return false;
                }
            }
            Operator._fillSkipped = "";
            if (inputBox.tagName === 'TEXTAREA' || inputBox.tagName === 'INPUT') {
                inputBox.value = text;
                inputBox.dispatchEvent(new Event('input', { bubbles: true }));
                return true;
            }
            return Operator.insertIntoEditor(inputBox, text);      // Quill / contenteditable
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
        // ★ V8.8：**按真实路径**逐层选（比"关键字兜底"更准，且选不中就不关单）
        //   path = ["一级","二级","三级"]（来自网页真实菜单，由中继/手机选择）
        selectCategoryPath: function (path, cb) {
            const want = (path || []).map(normText).filter(Boolean);
            const finish = (res) => { try { cb && cb(res || {}); } catch (e) {} };
            const trig = categoryTrigger();
            if (!trig) {
                finish({ ok: false, level: 0, path: [], options: [],
                         error: "页面上没找到问题分类选择器（工单可能还没打开/还没渲染）" });
                return;
            }
            if (!want.length) { finish({ ok: false, error: "没有给出分类路径" }); return; }
            trig.click();
            let depth = 0, stalls = 0;
            const step = () => {
                const nodes = lastPaneNodes();
                if (!nodes.length) {
                    closeCategoryDropdown();
                    finish({ ok: false, level: depth, path: want.slice(0, depth), options: [],
                             error: "第 " + (depth + 1) + " 层的分类面板没出现" });
                    return;
                }
                const kw = want[depth];
                const hit = nodes.find(n => normText(n.innerText) === kw)
                            || nodes.find(n => normText(n.innerText).indexOf(kw) !== -1);
                if (!hit) {
                    const options = nodes.map(categoryNodeInfo);
                    closeCategoryDropdown();
                    finish({ ok: false, level: depth + 1, path: want.slice(0, depth), options: options,
                             error: "第 " + (depth + 1) + " 层没有「" + kw + "」，本层真实选项见 options" });
                    return;
                }
                const info = categoryNodeInfo(hit);
                const isLast = (depth === want.length - 1);
                if (!isLast && !info.hasChildren) {      // 还没走完路径就遇到叶子 -> 路径给错了，绝不硬点
                    const options = nodes.map(categoryNodeInfo);
                    closeCategoryDropdown();
                    finish({ ok: false, level: depth + 1, path: want.slice(0, depth), options: options,
                             error: "第 " + (depth + 1) + " 层「" + info.text + "」已是叶子分类但路径还有下一层，"
                                    + "路径不匹配，已中止（本层真实选项见 options）" });
                    return;
                }
                const before = document.querySelectorAll('.el-cascader-menu').length;
                try { hit.click(); } catch (e) {}
                depth++;
                setTimeout(() => {
                    const after = document.querySelectorAll('.el-cascader-menu').length;
                    if (isLast) {                        // 最后一层：点完应已选中并关闭
                        let shown = "";
                        try { shown = String((categoryTrigger() || {}).value || ""); } catch (e) {}
                        if (!after || shown.indexOf(want[want.length - 1]) !== -1) {
                            finish({ ok: true, level: depth, path: want, picked: want.join(" / "), options: [] });
                        } else {
                            finish({ ok: false, level: depth, path: want.slice(0, depth - 1),
                                     options: lastPaneNodes().map(categoryNodeInfo),
                                     error: "点了「" + want[want.length - 1] + "」但分类框里没显示出来（页面没接受）" });
                        }
                        return;
                    }
                    if (after <= before) {
                        stalls++;
                        if (stalls > 1) {
                            closeCategoryDropdown();
                            finish({ ok: false, level: depth, path: want.slice(0, depth),
                                     options: lastPaneNodes().map(categoryNodeInfo),
                                     error: "点「" + info.text + "」后下一层没有展开" });
                            return;
                        }
                    } else {
                        stalls = 0;
                    }
                    step();
                }, 260);
            };
            setTimeout(step, 320);
        },
        // ★ V8.8：只读"按路径下钻看下一层有什么"（只点有子级的节点，不会真选中/提交任何值）
        exploreCategory: function (path, cb) {
            const want = (path || []).map(normText).filter(Boolean);
            const finish = (res) => { try { cb && cb(res || {}); } catch (e) {} };
            const trig = categoryTrigger();
            if (!trig) {
                finish({ ok: false, level: 0, path: [], options: [], error: "页面上没找到问题分类选择器" });
                return;
            }
            trig.click();
            let depth = 0, stalls = 0;
            const step = () => {
                const nodes = lastPaneNodes();
                if (!nodes.length) {
                    closeCategoryDropdown();
                    finish({ ok: false, level: depth, path: want.slice(0, depth), options: [],
                             error: depth === 0 ? "分类下拉没打开（可能被别的弹层挡住）" : "下一层面板没出现" });
                    return;
                }
                if (depth >= want.length) {              // 到达目标层 -> 读出这一层（含"有没有下一层"）
                    const options = nodes.map(categoryNodeInfo);
                    closeCategoryDropdown();
                    finish({ ok: true, level: depth + 1, path: want.slice(), options: options });
                    return;
                }
                const kw = want[depth];
                const hit = nodes.find(n => normText(n.innerText) === kw)
                            || nodes.find(n => normText(n.innerText).indexOf(kw) !== -1);
                if (!hit) {
                    const options = nodes.map(categoryNodeInfo);
                    closeCategoryDropdown();
                    finish({ ok: false, level: depth + 1, path: want.slice(0, depth), options: options,
                             error: "第 " + (depth + 1) + " 层没有「" + kw + "」，本层真实选项见 options" });
                    return;
                }
                const info = categoryNodeInfo(hit);
                if (!info.hasChildren) {                 // 叶子：不再下钻（也**不点它**，避免真的选中）
                    const options = nodes.map(categoryNodeInfo);
                    closeCategoryDropdown();
                    finish({ ok: false, level: depth + 1, path: want.slice(0, depth), options: options,
                             error: "第 " + (depth + 1) + " 层「" + info.text + "」是叶子分类，没有下一层" });
                    return;
                }
                const before = document.querySelectorAll('.el-cascader-menu').length;
                try { hit.click(); } catch (e) {}
                depth++;
                setTimeout(() => {
                    if (document.querySelectorAll('.el-cascader-menu').length <= before) {
                        stalls++;
                        if (stalls > 1) {
                            closeCategoryDropdown();
                            finish({ ok: false, level: depth, path: want.slice(0, depth),
                                     options: lastPaneNodes().map(categoryNodeInfo),
                                     error: "点「" + info.text + "」后下一层没有展开" });
                            return;
                        }
                    } else {
                        stalls = 0;
                    }
                    step();
                }, 260);
            };
            setTimeout(step, 320);
        },
        // ★ V8.8：用**网页自己的搜索框**搜分类（Element 级联 filterable 自带），返回匹配的完整路径
        //   没有搜索框就如实回 searchable:false（绝不编造结果），由调用方决定是否退回本地过滤。
        searchCategory: function (kw, cb) {
            const finish = (res) => { try { cb && cb(res || {}); } catch (e) {} };
            const key = normText(kw || "");
            if (!key) { finish({ ok: false, searchable: false, options: [], error: "没有给出搜索词" }); return; }
            const trig = categoryTrigger();
            if (!trig) { finish({ ok: false, searchable: false, options: [], error: "没找到问题分类选择器" }); return; }
            trig.click();
            setTimeout(() => {
                const box = categorySearchInput();
                if (!box) {
                    const opts = lastPaneNodes().map(categoryNodeInfo)
                        .filter(o => o.text.indexOf(key) !== -1)
                        .map(o => ({ text: o.text, path: [o.text], hasChildren: o.hasChildren }));
                    closeCategoryDropdown();
                    finish({ ok: true, searchable: false, options: opts,
                             error: opts.length ? "" : "网页的分类框没有搜索功能，且当前这层没有匹配项" });
                    return;
                }
                try {
                    box.focus();
                    box.value = key;
                    box.dispatchEvent(new Event('input', { bubbles: true }));
                } catch (e) {}
                setTimeout(() => {
                    const items = Array.from(document.querySelectorAll(
                        '.el-cascader__suggestion-item, .el-cascader__suggestion-panel li, .el-cascader-menu .el-cascader-node'))
                        .filter(isVisibleEl);
                    const out = [];
                    items.slice(0, 40).forEach(n => {
                        let t = "";
                        try {
                            const lab = n.querySelector('.el-cascader__suggestion-item__label');
                            t = normText(((lab || n).innerText) || "");
                        } catch (e) {}
                        if (t) out.push({ text: t, path: t.split(/\s*[\/>»]\s*/).filter(Boolean) });
                    });
                    closeCategoryDropdown();
                    finish({ ok: true, searchable: true, options: out,
                             error: out.length ? "" : "网页的搜索框没有返回匹配项" });
                }, 450);
            }, 320);
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
            const out = [];
            Array.from(document.querySelectorAll('.im-action-btn, button, .el-button, [role="button"]'))
                .filter(isVisibleEl)
                .forEach(el => {
                    elTextsOf(el).forEach(t => { if (t.length <= 12 && out.indexOf(t) === -1) out.push(t); });
                });
            return out.slice(0, 30);
        },
        // 收集候选元素（按钮类优先，其次 span/div/a/i/svg）
        _candidates: function() {
            const collect = sel => Array.from(document.querySelectorAll(sel))
                .filter(isVisibleEl)
                .map(el => ({ el: el, texts: elTextsOf(el), cls: elClassOf(el) }));
            return [collect('.im-action-btn, button, .el-button, [role="button"]'),
                    collect('span, div, a, i, svg')];
        },
        // 点击页面上的操作按钮（V7.5 强化）：
        //   ① 文本完全相等 ② 文本包含（限短标签） ③ 图标类名命中（el-icon-send 之类）
        // 返回 true/false；找不到时调用方负责如实回报（绝不静默）。
        safeClickActionBtn: function(keywords, opts) {
            const kws = (Array.isArray(keywords) ? keywords : [keywords]).map(normText).filter(Boolean);
            if (!kws.length) return false;
            const iconHints = ((opts && opts.iconHints) || []).map(s => String(s).toLowerCase());
            const groups = Operator._candidates();
            for (let g = 0; g < groups.length; g++) {
                const list = groups[g];
                if (!list.length) continue;
                for (let k = 0; k < kws.length; k++) {
                    const kw = kws[k];
                    for (let i = list.length - 1; i >= 0; i--) {          // 从后往前：更可能是叶子节点
                        if (list[i].texts.some(t => t === kw)) { fireClick(list[i].el); return true; }
                    }
                    for (let i = list.length - 1; i >= 0; i--) {
                        if (list[i].texts.some(t => t.length <= 12 && t.indexOf(kw) !== -1)) {
                            fireClick(list[i].el); return true;
                        }
                    }
                }
                if (iconHints.length) {
                    for (let i = list.length - 1; i >= 0; i--) {
                        if (iconHints.some(h => list[i].cls.indexOf(h) !== -1)) { fireClick(list[i].el); return true; }
                    }
                }
            }
            return false;
        },
        // 点开「更多 / ⋯」菜单（有些工作台把挂起/恢复收在二级菜单里）
        openMoreMenu: function() {
            return Operator.safeClickActionBtn(['更多', '更多操作', '操作', '⋯', '...', '···'],
                { iconHints: ['more', 'ellipsis'] });
        },
        // 找"发送"按钮：先在回复框所在容器里按文字/aria/图标找，再退回整页的精确文字
        clickSendButton: function() {
            // ★ V7.7 实机校准：本工作台的"发送"就是 .im-action-btn.reply-btn（"回复"，primary 样式）；
            //   输入框为空/只读时它是 disabled（点不动）；"回复并关单"是 .close-btn，绝不能当发送键。
            try {
                const area = document.querySelector('.chat-input-area') || document;
                const reps = Array.from(area.querySelectorAll('button.im-action-btn.reply-btn, button.reply-btn'))
                    .filter(isVisibleEl);
                const rep = reps.filter(el => !el.disabled)[0];
                if (rep && fireClick(rep)) return true;
                Operator._sendDiag = reps.length ? "页面上的「回复」按钮是灰的（disabled，说明编辑区还是空的或当前只读）" : "";
            } catch (e) {}
            const strict = ['发送', '发送消息', 'send'];
            const loose = ['发送', '发送消息', '回复', '提交', 'send'];
            const iconHints = ['send', 'submit', 'send-btn', 'icon-send', 'sendbtn'];
            const scopes = [];
            try {
                const c = document.querySelector('.editor-composer') || document.querySelector('.chat-input-area');
                if (c) { scopes.push(c); if (c.parentElement) scopes.push(c.parentElement); }
            } catch (e) {}
            for (let s = 0; s < scopes.length; s++) {
                const list = Array.from(scopes[s].querySelectorAll('button, [role="button"], .el-button, a, i, svg, span, div'))
                    .filter(el => isVisibleEl(el) && !el.disabled)     // 灰按钮点了也没用，别谎报成功
                    .map(el => ({ el: el, texts: elTextsOf(el), cls: elClassOf(el) }));
                for (let k = 0; k < loose.length; k++) {
                    for (let i = list.length - 1; i >= 0; i--) {          // 从右往左：发送键通常在右侧
                        if (list[i].texts.some(t => t === loose[k] || (t.length <= 8 && t.indexOf(loose[k]) !== -1))) {
                            fireClick(list[i].el); return true;
                        }
                    }
                }
                for (let i = list.length - 1; i >= 0; i--) {
                    if (iconHints.some(h => list[i].cls.indexOf(h) !== -1)) { fireClick(list[i].el); return true; }
                }
            }
            // 整页兜底：只认"精确的发送字样"，避免误点"回复并关单"之类的操作按钮
            for (let k = 0; k < strict.length; k++) {
                for (let g = 0; g < 2; g++) {
                    const list = Operator._candidates()[g];
                    for (let i = list.length - 1; i >= 0; i--) {
                        if (list[i].el.disabled) continue;             // 灰按钮不点（避免"报成功其实没发出去"）
                        if (list[i].texts.some(t => t === strict[k])) { fireClick(list[i].el); return true; }
                    }
                }
            }
            return false;
        },
        // 在回复框里敲回车（很多 IM 就是"回车发送"）
        pressEnterInComposer: function() {
            const el = Operator.composerInput();
            if (!el) return false;
            const mk = type => {
                let ev = null;
                try {
                    if (typeof KeyboardEvent === 'function') {
                        ev = new KeyboardEvent(type, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                                                       bubbles: true, cancelable: true });
                    }
                } catch (e) {}
                if (!ev) { try { ev = new Event(type, { bubbles: true, cancelable: true }); } catch (e) {} }
                if (ev) { try { ev.key = 'Enter'; ev.keyCode = 13; ev.which = 13; } catch (e) {} }
                return ev;
            };
            let fired = false;
            ['keydown', 'keypress', 'keyup'].forEach(t => {
                const ev = mk(t);
                if (!ev) return;
                try { el.dispatchEvent(ev); fired = true; } catch (e) {}
            });
            return fired;
        },
        // 回复框是否已清空（发送成功的强信号）；找不到回复框返回 null（未知，不当成成功）
        isComposerEmpty: function() {
            const el = Operator.composerInput();
            if (!el) return null;
            try { if (el.classList && el.classList.contains('ql-blank')) return true; } catch (e) {}
            let v = "";
            try { v = (el.value !== undefined && el.value !== null) ? el.value : (el.innerText || ""); } catch (e) {}
            return normText(String(v)).length === 0;
        },
        // 填完内容后的"发送闭环"：点发送按钮 -> 没找到就回车 -> 再复验输入框是否清空
        sendFilledReply: function() {
            setTimeout(() => {
                const clicked = Operator.clickSendButton();
                if (!clicked) Operator.pressEnterInComposer();
                setTimeout(() => {
                    const empty = Operator.isComposerEmpty();
                    if (clicked || empty === true) {
                        reportActionResult("SEND_REPLY", true,
                            (clicked ? "已点击发送按钮" : "已用回车发送") + (empty === true ? "，输入框已清空" : ""));
                    } else {
                        reportActionResult("SEND_REPLY", false,
                            "内容已填入编辑区，但发送没成功：" +
                            (Operator._sendDiag || "没找到可点的「回复/发送」按钮，回车也没生效")
                            + " —— 请在电脑上手动点发送/按回车");
                    }
                }, 500);
            }, 500);
        },
        // 行内按钮点击 + 自动重试（按钮可能是懒渲染，或收在「更多」菜单里）
        clickActionWithRetry: function(keywords, command, okText, opts) {
            if (Operator.safeClickActionBtn(keywords, opts)) {
                reportActionResult(command, true, okText || "已点击");
                return true;
            }
            reportActionResult(command, false,
                "首次尝试未找到「" + keywords[0] + "」（可能还没渲染或在「更多」里），已自动重试… 当前按钮："
                + Operator.listActionButtons().join("/"));
            let left = 5;
            const tick = () => {
                if (Operator.safeClickActionBtn(keywords, opts)
                    || (Operator.openMoreMenu() && Operator.safeClickActionBtn(keywords, opts))) {
                    reportActionResult(command, true, (okText || "已点击") + "（第 " + (6 - left) + " 次尝试成功）");
                    return;
                }
                if (--left <= 0) {
                    reportActionResult(command, false,
                        "重试 5 次仍未找到「" + keywords[0] + "」；页面按钮：" + Operator.listActionButtons().join("/"));
                    return;
                }
                setTimeout(tick, 400);
            };
            setTimeout(tick, 400);
            return false;
        },
        // ★ V7.8：页面上所有"文本恰好是 在线/忙碌/离线"的可见元素
        //   用途：点开下拉前后做**差集** —— 新出现的那些就是菜单项。
        //   这样即使工作台换了类名也能点到正确选项（仍然只认"文本严格等于状态"的可见节点，
        //   不是猜类名，红线②依然守得住）。
        visibleStatusNodes: function() {
            const out = [];
            try {
                Array.from(document.querySelectorAll('div, span, button, li, a, p')).forEach(el => {
                    const t = statusTextOf(el) || normStatus(el.innerText);
                    if (!t || !isVisibleEl(el)) return;
                    const txt = normText(el.innerText);
                    if (!txt || txt.length > 12) return;              // 只认短标签（大容器排除）
                    out.push({ el: el, text: txt, cls: elClassOf(el), tag: el.tagName });
                });
            } catch (e) {}
            return out;
        },
        // ★ V7.9（实机数据修正）：收集"可能的状态触发器"，**越靠里的越优先**
        //   线上实测：`span.status-label-text`（叶子）→ `span.status-trigger.is-im-online.el-tooltip__trigger`
        //   → `div.el-dropdown.status-dropdown`（外层包装）。Element 的处理器挂在内层，
        //   点外层 div 是**点不开**的（这就是"点了 3 次都找不到选项"的真因）。
        //   点最里层靠事件冒泡一定能触达真正的触发器；不行再换下一个候选。
        statusTriggers: function() {
            const out = [];
            try {
                Array.from(document.querySelectorAll('div, span, button, a')).forEach(el => {
                    if (!statusTextOf(el)) return;
                    if (isMenuOption(el)) return;
                    if (!isVisibleEl(el)) return;
                    const t = normText(el.innerText);
                    if (!t || t.length > 12) return;                 // 只认短标签（大容器排除）
                    let depth = 0, n = el;
                    while (n && n.parentElement) { depth++; n = n.parentElement; }
                    out.push({ el: el, depth: depth, cls: elClassOf(el), tag: el.tagName,
                               text: t, isTriggerClass: /status-trigger|el-tooltip__trigger|el-dropdown/.test(elClassOf(el)) });
                });
            } catch (e) {}
            out.sort((a, b) => b.depth - a.depth);                    // 深（叶子）优先
            return out;
        },
        // 下拉面板有没有真的打开（Element 的下拉/popper 容器），用于如实回报"是没打开还是选项不匹配"
        panelAppeared: function() {
            const sel = '.el-dropdown-menu, .el-popper, .el-select-dropdown, .el-cascader__dropdown';
            let nodes = [];
            try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { return false; }
            return nodes.some(n => isVisibleEl(n));
        },
        // 收集"可见的状态菜单项"：① 已知实机靶点（Element 下拉/级联/原生 li）
        //   ② V7.8 兜底：点开下拉后"新出现"的状态文本节点（exclude = 点开前已有的元素）
        statusMenuItems: function(exclude) {
            const sel = '.el-dropdown-menu__item, .el-select-dropdown__item, [role="menuitem"], li';
            const raw = [];
            try { Array.from(document.querySelectorAll(sel)).forEach(el => raw.push(el)); } catch (e) {}
            const ex = exclude || [];
            try {
                Operator.visibleStatusNodes().forEach(o => {
                    if (ex.indexOf(o.el) === -1 && raw.indexOf(o.el) === -1) raw.push(o.el);
                });
            } catch (e) {}
            const out = [];
            for (let i = 0; i < raw.length; i++) {
                const el = raw[i];
                if (!isVisibleEl(el)) continue;                  // 隐藏项绝不算（V7.3 的教训）
                const t = normText(el.innerText);
                if (!t || t.length > 12) continue;               // 只认短标签，避免误点容器
                if (out.some(o => o.el === el)) continue;
                out.push({ el: el, text: t, cls: elClassOf(el), tag: el.tagName });
            }
            return out;
        },
        // 轮询等下拉渲染出来（★ V7.7：Element 下拉有过渡/懒渲染，旧版固定 200ms 经常空手）
        waitForMenu: function(done, deadlineMs, pollMs, exclude) {
            const t0 = Date.now();
            const step = () => {
                const items = Operator.statusMenuItems(exclude);
                if (items.length || (Date.now() - t0) >= deadlineMs) { done(items); return; }
                setTimeout(step, pollMs);
            };
            setTimeout(step, 0);
        },
        switchIMStatus: function(targetStatus) {
            if (!targetStatus) return;
            const wantTxt = normText(targetStatus);
            let attempt = 0;
            const doAttempt = () => {
                attempt++;
                // ★ V7.9（线上实机数据修正）：候选触发器按"由内到外"排，逐次尝试。
                //   实测：状态胶囊最里层是 span.status-label-text，其父是
                //   span.status-trigger.is-im-online.el-tooltip__trigger，最外层是 div.el-dropdown.status-dropdown。
                //   旧版取的是 DOM 顺序里第一个（= 最外层 div）→ 点不开 → 永远找不到选项。
                const cands = Operator.statusTriggers();
                const statusTrigger = cands.length ? cands[(attempt - 1) % cands.length].el : null;
                if (!statusTrigger) {
                    reportActionResult("CHANGE_STATUS", false,
                        "未找到「可见」的 IM 状态显示区（页面结构可能变了），状态没切");
                    return;
                }
                const beforeNodes = Operator.visibleStatusNodes().map(o => o.el);   // ★ V7.8：点开前已有的元素（差集用）
                fireHover(statusTrigger);        // ★ V7.9：Element 的下拉是 hover 触发，必须先派发 hover
                fireClick(statusTrigger);

                Operator.waitForMenu(function(items) {
                    // ★ 等价匹配：页面状态区写"IM在线"，菜单项可能只写"在线"（反之亦然）
                    const target = items.find(o => sameStatus(o.text, targetStatus))
                        || items.find(o => o.text.indexOf(wantTxt) !== -1);
                    if (!target) {
                        const cand = items.map(o => o.tag + ':' + o.text).filter(Boolean);
                        if (attempt < 3) { setTimeout(doAttempt, 400); return; }   // 菜单可能还没渲染完，再试
                        reportActionResult("CHANGE_STATUS", false,
                            "点了 " + attempt + " 次都没找到可见的状态选项「" + targetStatus + "」；下拉"
                            + (Operator.panelAppeared() ? "打开了但选项对不上" : "没打开")
                            + "；页面候选：" + (cand.join("/") || "（无）")
                            + "；完整清单见页面控制台 __probe.dumpStatus()");
                        Operator.dumpStatusMenu();      // ★ V7.7：失败自动回报"真实可见选项"，让中继能据此校准
                        return;
                    }
                    fireClick(target.el);
                    // 点完立刻回读真实状态并回报（切成功与否手机端马上能看到，不再"显示成功其实没变"）
                    setTimeout(() => {
                        const now = findIMStatusText();
                        const okNow = sameStatus(now, targetStatus);
                        if (okNow || attempt >= 3) {
                            reportActionResult("CHANGE_STATUS", okNow,
                                okNow ? ("已切换为 " + targetStatus + (attempt > 1 ? ("（第 " + attempt + " 次尝试）") : ""))
                                      : ("点了「" + target.text + "」但页面仍是 " + (now || "未知")
                                         + "（可能当前是只读/管理员视角）"));
                            readAndReportIMStatus(true);
                            return;
                        }
                        setTimeout(doAttempt, 400);              // 回读不一致 -> 换个时机再试
                    }, 400);
                }, 1800, 150, beforeNodes);
            };
            doAttempt();
        },
        // 排障/实机校准：把"状态显示区 + 下拉全部可见选项"原样回报（tag/class/文本），绝不靠猜标签
        dumpStatusMenu: function() {
            // ① 页面上"文字像状态"的全部可见节点（有它说明状态区在，且能看出真实写法与类名）
            const nodes = [];
            try {
                Array.from(document.querySelectorAll('div, span, button')).forEach(el => {
                    if (nodes.length >= 12) return;
                    const t = normStatus(el.innerText);
                    if (!t || !isVisibleEl(el)) return;
                    nodes.push({ tag: el.tagName, cls: elClassOf(el).slice(0, 60), text: t,
                                 isMenu: isMenuOption(el) });
                });
            } catch (e) {}
            const beforeNodes = Operator.visibleStatusNodes().map(o => o.el);   // ★ V7.8：点开前已有的（差集用）
            const beforeInfo = Operator.visibleStatusNodes().map(o => o.tag + ':' + o.cls);
            // ★ V7.9：用"由内到外"的候选触发器（实测最里层才挂 hover/click 处理器）
            const cands = Operator.statusTriggers();
            const trigger = cands.length ? cands[0].el : null;
            const triggerInfo = trigger
                ? { clicked: true, tag: trigger.tagName, cls: elClassOf(trigger).slice(0, 60),
                    text: normText(trigger.innerText), candidates: cands.length,
                    all: cands.map(o => o.tag + ':' + o.cls.slice(0, 50)) }
                : { clicked: false, candidates: 0 };
            if (trigger) { fireHover(trigger); fireClick(trigger); }        // ★ V7.9：hover + click 都派发
            Operator.waitForMenu(function(items) {
                const rows = items.map(o => ({ tag: o.tag, cls: o.cls, text: o.text }));
                // ★ V7.9：连"下拉面板本身"也回报（判断是没打开、还是选项结构不认识）
                const panel = (function () {
                    try {
                        const p = Array.from(document.querySelectorAll(
                            '.el-dropdown-menu, .el-popper, .el-select-dropdown, .el-cascader__dropdown')).filter(isVisibleEl)[0];
                        if (!p) return { appeared: false };
                        return { appeared: true, cls: elClassOf(p), tag: p.tagName,
                                 text: normText(p.innerText).slice(0, 200),
                                 html: String(p.innerHTML || '').slice(-800) };
                    } catch (e) { return { appeared: false, err: String((e && e.message) || e) }; }
                })();
                sendToBrain({ event: "STATUS_MENU_DUMP", data: { current: findIMStatusText(), items: rows,
                                                                 trigger: triggerInfo, visible_status_nodes: nodes,
                                                                 before_open_nodes: beforeInfo, panel: panel } });
                console.log("🧭 [探针] 状态显示区 / 下拉可见选项：",
                            { trigger: triggerInfo, beforeOpen: beforeInfo, visible: nodes, menuItems: rows, panel: panel });
                try { if (trigger) fireClick(trigger); } catch (e) {}    // 收起下拉
            }, 1500, 150, beforeNodes);
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
            data: { status: imStatusCode(st), manual: manualByUser, via_relay: manualViaRelay, forced: !!forced }
        });
        console.log("📡 [探针] 状态复核：" + st + (forced ? "（服务端请求）" : ""));
        return true;
    }

    // 手动状态守护：客服手动挂了"离线/忙碌"，网页却自己跳回"在线"（很多 IM 在窗口重新获得焦点时
    // 会自动上线）-> 改回你手动设的那个状态并如实上报。
    //   ★ 客服铁律：任何情况都不得把"离线/忙碌"自动改成"在线"，除非手动。
    function restoreManualOffline(flippedTo) {
        const want = manualAwayStatus || 'IM离线';
        const wantCode = imStatusCode(want);
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
        console.warn("🛡️ [探针] 检测到网页把状态自动改成了「" + flippedTo + "」，按你手动设置的「"
                     + want + "」改回去（第 " + guardHits + " 次）");
        Operator.switchIMStatus(want);
        lastIMStatus = want;                                 // 先把手机端稳住，不再显示在线
        sendToBrain({ event: "IM_STATUS", data: { status: wantCode, manual: true, guarded: true } });
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
            // ★ V8.2：重连后清掉"上次上报过的指纹" ——
            //   中继重启后它内存里的会话/聊天全没了，可探针还以为"内容没变、不用再报"，
            //   于是手机端一直空白，直到页面内容恰好变化（客服每次重启中继都会踩这个坑）。
            window._lastConvListHash = "";
            window._lastChatState = null;
            window._lastChatHash = "";
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
          try {                                    // ★ V7.7：整段兜底（红线：探针监听必须静默保护，
            let cmd;                               //   同时把错误回报给中继，别再"点了没反应还查不到原因"）
            try {
                cmd = JSON.parse(event.data);
            } catch (err) {
                return;
            }
            if (!cmd || !cmd.command) return;
            if (cmd.groupID) lastCmdGroupID = cmd.groupID;   // 动作回执要带工单号（中继据此定位会话）

            // 警报确认回执
            if (cmd.command === "ALARM_CONFIRMED") {
                console.log("✅ [探针] 掉线警报已被中继确认");
                return;
            }
            if (cmd.command === "RECOVERY_CONFIRMED") return;

            if (cmd.command === "FILL_DRAFT") {
                if (cmd.content) {
                    // ★ V8.0.2：默认**不覆盖**客服已经写在输入框里的内容（除中继显式 overwrite:true）
                    const filled = Operator.fillReplyBox(cmd.content, cmd.overwrite === true);
                    if (!filled) {
                        reportActionResult("FILL_DRAFT", false,
                            (Operator._fillSkipped || Operator.editorFailureReason() || "草稿未填入"));
                    }
                }
                if (cmd.category) Operator.selectCategory("一级分类", "二级分类", cmd.category, cmd.defaultCategory);
            }
            else if (cmd.command === "ACTION_REPLY_CLOSE") {
                const filled = cmd.content ? Operator.fillReplyBox(cmd.content, true) : false;
                if (cmd.content && !filled) reportActionResult("ACTION_REPLY_CLOSE", false, Operator.editorFailureReason() + "，已停止关单");
                if (!cmd.content) reportActionResult("ACTION_REPLY_CLOSE", true, "（无结束语，直接关单）");
                const doClickClose = (delay) => setTimeout(() => {
                    Operator.clickActionWithRetry(['回复并关单', '回复并关闭', '回复关闭', '回复并结束', '关单'],
                        "ACTION_REPLY_CLOSE", "已点击「回复并关单」",
                        { iconHints: ['close-ticket', 'reply-close'] });
                }, delay);
                const path = cmd.categoryPath || ["一级分类", "二级分类"];
                if (cmd.categoryPath && cmd.categoryPath.length) {
                    // ★ V8.8：**按网页真实路径逐层点选并逐层校验**；选不中就不关单
                    //   （旧的"关键字兜底"会把分类选错，客服还以为选对了 —— 关单是最不能出错的环节）
                    reportActionResult("SELECT_CATEGORY", true,
                        "开始按真实路径选择分类：" + cmd.categoryPath.join(" / "));
                    Operator.selectCategoryPath(cmd.categoryPath, (res) => {
                        if (!res || !res.ok) {
                            reportActionResult("SELECT_CATEGORY", false,
                                ((res && res.error) || "分类没选上") + "｜已中止关单，请手动选择分类后再关");
                            return;
                        }
                        reportActionResult("SELECT_CATEGORY", true, "已选择分类：" + (res.picked || ""));
                        doClickClose(1600);
                    });
                } else if (cmd.category) {
                    Operator.selectCategory(path[0], path[1], cmd.category, cmd.defaultCategory);
                    doClickClose(2200);
                } else {
                    doClickClose(800);
                }
            }
            else if (cmd.command === "CATEGORY_PROBE" || cmd.command === "CATEGORY_SEARCH") {
                // ★ V8.8：手机端"关单选分类"面板要用**网页真实菜单**：按路径下钻读下一层 / 用网页自带搜索框搜
                const reqId = String(cmd.reqId || "");
                const kind = (cmd.command === "CATEGORY_SEARCH") ? "search" : "probe";
                const done = (payload) => {
                    sendToBrain({ event: "CATEGORY_RESULT", data: Object.assign({
                        reqId: reqId, kind: kind, path: cmd.path || [], q: cmd.q || ""
                    }, payload || {}) });
                };
                if (kind === "search") Operator.searchCategory(String(cmd.q || ""), done);
                else Operator.exploreCategory(cmd.path || [], done);
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
                Operator.clickActionWithRetry(
                    ['挂起', '挂起工单', '暂挂', '暂停会话', '暂停服务', '暂停', '搁置', '休眠'],
                    "ACTION_HANGUP", "已点击「挂起」",
                    { iconHints: ['hangup', 'hang-up', 'suspend', 'pause'] });
            }
            else if (cmd.command === "ACTION_RESUME") {
                Operator.clickActionWithRetry(
                    ['恢复', '恢复会话', '重新接入', '重新接待', '继续会话', '继续', '接单', '接入', '激活'],
                    "ACTION_RESUME", "已点击「恢复」",
                    { iconHints: ['resume', 'reopen', 'restore', 'activate'] });
            }
            else if (cmd.command === "LIST_ACTIONS") {
                // 排障：把页面上的按钮清单回报给手机/中继
                const labels = Operator.listActionButtons();
                reportActionResult("LIST_ACTIONS", labels.length > 0,
                    labels.length ? ("页面按钮：" + labels.join(" / ")) : "没扫到任何操作按钮");
            }
            else if (cmd.command === "SEND_REPLY") {
                // 手机/中继明确要发这条内容 -> 允许覆盖（这段字是客服在手机上敲的）
                const filled = Operator.fillReplyBox(cmd.content, true);
                if (!filled) {
                    reportActionResult("SEND_REPLY", false, Operator.editorFailureReason() + "，消息没发出去");
                } else {
                    // V7.5：发送闭环（点发送按钮 -> 没找到就回车 -> 复验输入框是否清空）
                    Operator.sendFilledReply();
                }
            }
            else if (cmd.command === "CHANGE_STATUS") {
                const map = { 1: 'IM在线', 2: 'IM忙碌', 3: 'IM离线' };
                // 这是"人在手机端点的"，属于用户意图：同步手动标记 + 记一次点击时间，
                // 免得离线/忙碌守护把用户刚点的状态又改回去。
                // ★ V7.5 补充：忙碌也算"手动状态"（客服要求：离线/忙碌都不许被自动改成在线）
                isManualAway = (cmd.status === 2 || cmd.status === 3);
                isManualOffline = (cmd.status === 3);
                // ★ V7.7：这是"中继让我切的"，不是"人在网页上点的" —— 分开上报，
                //   否则中继的"手动状态锁"会被探针自己绕过（网页真实状态反过来盖掉客服的选择）。
                manualViaRelay = true;
                manualByUser = false;
                lastUserStatusClickAt = Date.now();
                lastIMStatus = null;                 // 下一次 tick 重新读 DOM 并如实上报
                console.log("📲 [探针] 收到手机端切换状态指令：" + (map[cmd.status] || cmd.status));
                Operator.switchIMStatus(map[cmd.status]);
            }
            else if (cmd.command === "OPEN_CONV") {
                // ★ V8.0：把电脑网页切到指定会话。
                //   为什么必须有它：探针的「回复/草稿/回复并关单」永远作用于**页面当前打开的工单**，
                //   手机端远程回复与 AI 自动回复都必须先把页面切到目标会话，才不会发错玩家。
                const wantName = normText(cmd.name || "");
                const wantLast = normText(cmd.lastText || "");
                const items = [];
                try {
                    Array.from(document.querySelectorAll('.session-item')).forEach(el => {
                        if (!isVisibleEl(el)) return;
                        const pick = sel => {
                            try { const n = el.querySelector(sel); return n ? normText(n.innerText) : ""; } catch (e) { return ""; }
                        };
                        items.push({ el: el, name: pick('.session-name'), last: pick('.session-last'),
                                     active: (function () { try { return el.classList.contains('active'); } catch (e) { return false; } })() });
                    });
                } catch (e) {}
                const hit = items.find(o => o.name === wantName && (!wantLast || o.last === wantLast))
                    || items.find(o => o.name === wantName)
                    || items.find(o => o.name && wantName && o.name.indexOf(wantName) !== -1);
                if (!hit) {
                    reportActionResult("OPEN_CONV", false,
                        "会话列表里没找到「" + (cmd.name || "") + "」；当前列表："
                        + items.map(o => o.name).join("/").slice(0, 140));
                    return;
                }
                if (hit.active) {
                    reportActionResult("OPEN_CONV", true, "「" + hit.name + "」已经是网页当前会话");
                    return;
                }
                fireClick(hit.el);
                console.log("🖱️ [探针] 已点击会话：" + hit.name);
                // ★ V8.0.2：这里**不再**立刻回一条 ok=true 的"已点击"回执 ——
                //   中继会据此以为"页面已经切过去了"从而马上发送，可能把消息发进上一个会话（发错玩家）。
                //   只有下面"确认页面真的切过去了"的回执才算数。
                (function confirmSwitch(tries) {
                    setTimeout(() => {
                        const nowActive = scanConversationList().filter(r => r.active)[0];
                        const okNow = !!(nowActive && nowActive.name === hit.name);
                        if (!okNow && tries > 0) { confirmSwitch(tries - 1); return; }
                        reportActionResult("OPEN_CONV", okNow,
                            okNow ? ("已切到「" + hit.name + "」")
                                  : ("已点击「" + hit.name + "」，但页面当前还是「"
                                     + ((nowActive && nowActive.name) || "未知") + "」"));
                    }, 900);
                })(1);
            }
            else if (cmd.command === "REQUEST_IM_STATUS") {
                // 手机端一打开 / 回到前台就来要一次真实状态（"从电脑网页获取一下"）
                readAndReportIMStatus(true);
            }
            else if (cmd.command === "DUMP_STATUS_MENU") {
                // 排障（★ V7.7）：把状态下拉的可见选项清单回报给中继/手机，用于实机校准匹配
                Operator.dumpStatusMenu();
            }
            else if (cmd.command === "PING") {
                // ★ V7.8 自报家门（只读诊断）：同时证明两件事 ——
                //   ① "中继发的指令我收到了"；② "浏览器里跑的到底是哪一版代码"（typeof 逐个查）。
                let composerInfo = "n/a";
                try {
                    const ce = Operator.composerInput();
                    composerInfo = ce ? (String(ce.tagName || "") + "." + String(ce.className || "").slice(0, 40))
                                      : "none";
                } catch (err) { composerInfo = "err:" + ((err && err.message) || err); }
                sendToBrain({ event: "PONG", data: {
                    version: PROBE_VERSION,
                    hasDump: typeof Operator.dumpStatusMenu === "function",
                    hasWaitForMenu: typeof Operator.waitForMenu === "function",
                    hasEditorFn: typeof Operator.editorFailureReason === "function",
                    hasReplyBtnTarget: String(Operator.clickSendButton).indexOf("reply-btn") !== -1,
                    // ★ V8.3：浏览器里跑的这版到底有没有"人工回复嗅探"（排查"改了没生效"）
                    hasDemoSniffer: typeof setupManualReplySniffer === "function",
                    composer: composerInfo,
                    imStatus: findIMStatusText(),
                    manualByUser: manualByUser,
                    manualViaRelay: manualViaRelay,
                    editorReadonly: (function () {
                        try { const ro = document.querySelector('.editor-readonly'); return !!(ro && isVisibleEl(ro)); }
                        catch (e) { return null; }
                    })(),
                    wsState: ws ? ws.readyState : -1,
                    nodes: (function () { try { return document.querySelectorAll('div, span, button').length; } catch (e) { return -1; } })(),
                    url: (function () { try { return location.href; } catch (e) { return ""; } })()
                } });
                console.log("🏓 [探针] PONG：已向中继自报家门");
            }
            else if (cmd.command === "POLICY") {
                if (cmd.keepManualOffline !== undefined) PROBE_CONFIG.keepManualOffline = !!cmd.keepManualOffline;
                console.log("⚙️ [探针] 策略同步：手动离线守护 = " + (PROBE_CONFIG.keepManualOffline ? "开启" : "关闭"));
            }
            else if (cmd.command === "SILENCE_ALARM") {
                stopSiren();
            }
          } catch (err) {
            try {
                sendToBrain({ event: "PROBE_ERROR", data: { where: "onmessage:" + ((cmd && cmd.command) || "?"),
                                                            error: String((err && err.message) || err) } });
            } catch (e2) {}
            console.warn("⚠️ [探针] 处理指令出错：", err);
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

        // ★ V7.5 手动状态守护：客服手动挂了"离线/忙碌"，网页却自己跳回"在线"（很多 IM 在窗口
        //   重新获得焦点时会自动上线）—— 只处理"网页自己跳的"（你刚点过的有 manualGraceMs 免打扰）。
        if (currentStatus !== lastIMStatus && currentStatus === 'IM在线'
            && isManualAway && manualAwayStatus && PROBE_CONFIG.keepManualOffline
            && (Date.now() - lastUserStatusClickAt) > PROBE_CONFIG.manualGraceMs) {
            if (restoreManualOffline(currentStatus)) return;   // 已改回你设的状态并上报
        }

        if (currentStatus === lastIMStatus) return;   // 未变化不重复上报

        const prev = lastIMStatus;
        lastIMStatus = currentStatus;

        // 1) 无论手动还是异常，都把真实状态同步给后端
        //    （否则手机端会一直显示旧状态，手动挂"离线"更是永远同步不过去）
        sendToBrain({
            event: "IM_STATUS",
            data: { status: imStatusCode(currentStatus), manual: manualByUser, via_relay: manualViaRelay,
                    sound_ok: audioReady }
        });

        // 2) 只有"异常掉线"才拉警报；手动离线不报警
        if (currentStatus === 'IM离线') {
            if (isManualOffline) {
                stopSiren();
            } else {
                playSiren();
                sendToBrain({ event: "ABNORMAL_OFFLINE",
                              data: { sound_ok: audioReady,
                                      hidden: (function () { try { return !!document.hidden; } catch (e) { return false; } })() } });
            }
        } else {
            stopSiren();
            if (prev === 'IM离线' && currentStatus === 'IM在线') {
                isManualOffline = false;
                isManualAway = false;          // 网页真的回到在线（守护没拦住/不需要拦）-> 手动离开状态结束
                manualAwayStatus = null;
                sendToBrain({ event: "ALARM_RECOVERED" });
            }
        }
    }, 2000);

    // ==================== 定时任务 2：工单消息抓取与上报 ====================
    // ★ V8.7：抽成具名函数 —— 除定时器之外，**DOM 变化事件（MutationObserver）也会调用它**。
    //   为什么：浏览器对**隐藏标签**的定时器会节流（隐藏 5 分钟后降到约 1 次/分钟，甚至被"内存节省"冻结），
    //   而 DOM 变化事件不受节流影响 —— 这样"不聚焦工作台页面"也能第一时间发现新消息并推给手机。
    function scanChatAndReport() {
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
            data: { groupID: ident.gid, name: ident.name, messages: messages, playerInfo: playerInfo,
                    sound_ok: audioReady,        // ★ 浏览器能不能出声（false 时中继让桌面悬浮窗兜底响铃）
                    hidden: (function () { try { return !!document.hidden; } catch (e) { return false; } })() }
                    // ★ V8.7 hidden：页面是否在后台（不聚焦）——后台标签的网页音频不可靠，
                    //   中继收到 hidden=true 会让**桌面悬浮窗**用系统声提醒（保证一定响、且不双响）。
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
        // ★ V8.7：页面在后台（不聚焦）时，网页音频在部分环境下会被浏览器压掉/静音，
        //   所以隐藏标签页**不在这里响**，改由中继通知"桌面悬浮窗"用系统声（一定响、且不双响）。
        if (isNewPlayerMsg && !document.hidden) playDingDong();

        window._lastChatState = { gid: ident.gid, texts: texts };
        window._lastChatHash = currentHash;
    }
    setInterval(scanChatAndReport, 2000);

    // ==================== 会话列表（★ V8.0 实机校准：客服 Console dump 的真实结构） ====================
    //   div.session-item[.active]
    //     ├ div.session-avatar > img[src]
    //     ├ div.session-info
    //     │   ├ div.session-top > span.session-name ＋ span/button.session-tag…
    //     │   └ div.session-last        ← 最后一条消息预览
    //     └ div.session-time
    // ★ 列表项里**没有**工单号（无 data-id），所以身份用"会话名（+头像地址/时间）"组合，
    //   手机端与中继都按**会话名**匹配（切会话/远程回复都靠它）。
    function scanConversationList() {
        const rows = [];
        try {
            Array.from(document.querySelectorAll('.session-item')).forEach(el => {
                if (!isVisibleEl(el)) return;
                const pick = sel => {
                    try { const n = el.querySelector(sel); return n ? normText(n.innerText) : ""; } catch (e) { return ""; }
                };
                const name = pick('.session-name');
                if (!name) return;
                let avatar = "";
                try {
                    const img = el.querySelector('.session-avatar img');
                    if (img) avatar = String(img.getAttribute('src') || "").slice(-60);
                } catch (e) {}
                const tags = [];
                try {
                    Array.from(el.querySelectorAll('.session-tag')).forEach(t => {
                        const x = normText(t.innerText);
                        if (x) tags.push(x.slice(0, 16));
                    });
                } catch (e) {}
                rows.push({
                    name: name.slice(0, 24),
                    last: pick('.session-last').slice(0, 60),
                    time: pick('.session-time').slice(0, 12),
                    active: (function () { try { return el.classList.contains('active'); } catch (e) { return false; } })(),
                    avatar: avatar,
                    tags: tags.slice(0, 3)
                });
            });
        } catch (e) {}
        return rows;
    }

    // ==================== ★ V8.3：人工回复"影子录制"（静默嗅探，供离线蒸馏规则） ====================
    // 目的：把客服**真人**在工作台上打出的回复，连同当时的工单上下文（玩家信息 + 完整对话）
    //   静默回传一条 RECORD_MANUAL_DEMO 给中继（落盘 demonstrations.jsonl），
    //   供离线工具 distill_rules.py 让大模型逆向蒸馏"常用固定句式 / 避坑策略 / 索要信息边界"。
    // 铁律（逐条对齐 .clinerules）：
    //   ① 纯只读嗅探：不 preventDefault / 不 stopPropagation / 不改输入框 / 不点按钮 / 不代为发送；
    //   ② 只认"真人操作"：AI 与手机代发走的是 fireClick() 的**合成事件**（isTrusted=false），
    //      这里一律跳过 —— 否则会把系统自己生成的话当成"人工标准答案"喂给蒸馏脚本（污染语料）；
    //   ③ 全程 try/catch，出错只 console.warn —— 绝不干扰客服界面的按钮点击与输入（红线：静默保护）；
    //   ④ 复用的 DOM 靶点与"定时任务 2"完全一致（.chat-bubble-row / .from-player / .from-agent /
    //      .ws-right-panel），不新增、不猜测任何类名（红线②：实机校准过的靶点不许乱动）；
    //   ⑤ 事后复验：只有输入框**真的被清空**（或对话里真的出现了这句话）才上报；
    //      点了却没发出去的误触不会污染语料。
    let _demoCaptured = 0;        // 命中"人工发送动作"的次数
    let _demoSent = 0;            // 复验通过、真的上报给中继的条数
    let _demoSkipped = 0;         // 复验未通过（没真发出去）而丢弃的条数
    let _lastDemoPreview = "";    // 最近一条已上报样本的回复预览（__probe.demos() 用）

    // 抓一份"当前工单上下文"：玩家信息 + 聊天气泡数组（与定时任务 2 同一套靶点，绝不另猜类名）
    function collectDemoSnapshot() {
        const snap = { playerInfo: "", messages: [] };
        try {
            const rightPanel = document.querySelector('.ws-right-panel');
            if (rightPanel) snap.playerInfo = rightPanel.innerText.replace(/\n+/g, ' | ').trim();
        } catch (e) {}
        try {
            document.querySelectorAll('.chat-bubble-row').forEach(row => {
                const isPlayer = row.classList.contains('from-player');
                const isAgent = row.classList.contains('from-agent');
                if (!isPlayer && !isAgent) return;
                const textNode = row.querySelector('.msg-rich-text') || row;
                const content = (textNode.innerText || "").trim();
                if (content) snap.messages.push({ sender: isPlayer ? 'player' : 'agent', text: content });
            });
        } catch (e) {}
        return snap;
    }

    // 读回复框里"客服此刻准备发出去"的原文（Quill / contenteditable / input 都兼容）
    function readComposerText() {
        try {
            const el = Operator.composerInput();
            if (!el) return "";
            const v = (el.value !== undefined && el.value !== null) ? el.value : (el.innerText || "");
            return String(v || "").trim();
        } catch (e) { return ""; }
    }

    // 被点的元素是不是"人工的回复/发送"？（只认实机校准过的靶点 + 精确文字，绝不猜类名）
    function isManualSendTrigger(target) {
        try {
            if (!target || typeof target.closest !== 'function') return false;
            // 实机靶点：button.im-action-btn.reply-btn（"回复"）；灰的（disabled）不算 —— 点了也不会发
            const rep = target.closest('button.im-action-btn.reply-btn, button.reply-btn');
            if (rep && !rep.disabled && isVisibleEl(rep)) return true;
            // 兜底：容器内文字恰为"发送 / 发送消息"的按钮（仍只认文字，不认猜测的类名）
            const btn = target.closest('button, .im-action-btn, [role="button"]');
            if (btn && !btn.disabled) {
                const texts = elTextsOf(btn);
                if (texts.some(t => t === '发送' || t === '发送消息')) return true;
            }
        } catch (e) {}
        return false;
    }

    // 嗅探核心：捕获阶段先把"客服打的原文 + 当前工单上下文"抓在手（页面还没清空输入框），
    //   再延迟复验"到底发出去没有"，通过了才回传中继。中继侧只落盘、零出站，不会代发。
    function captureManualDemo(trigger) {
        try {
            if (!PROBE_CONFIG.demoCapture) return;
            const humanReply = readComposerText();
            const minLen = Math.max(1, parseInt(PROBE_CONFIG.demoMinLen, 10) || 2);
            if (normText(humanReply).length < minLen) return;      // 空/太短：不记（防误触、防刷屏）
            _demoCaptured++;
            const snap = collectDemoSnapshot();
            const ident = buildTicketIdentity(snap.playerInfo, snap.messages);
            setTimeout(() => {
                try {
                    // 复验：① 回复框被清空  或  ② 对话里真的出现了这段话 —— 有一即算"真的发出去了"
                    const empty = Operator.isComposerEmpty();
                    const after = collectDemoSnapshot().messages.map(m => m.text);
                    const appeared = after.some(t => normText(t) === normText(humanReply));
                    if (!(empty === true || appeared)) {
                        _demoSkipped++;
                        console.warn("🛑 [探针] 人工回复嗅探：输入框没清空、对话里也没出现这段话，"
                                     + "判定为没真正发出，不记录（不影响你的操作）");
                        return;
                    }
                    sendToBrain({
                        event: "RECORD_MANUAL_DEMO",
                        data: {
                            timestamp: Date.now(),
                            trigger: trigger,                    // 'click' = 点了回复/发送按钮；'enter' = 键盘回车
                            groupID: ident.gid,
                            name: ident.name,
                            playerInfo: snap.playerInfo,
                            messages: snap.messages,
                            humanReply: humanReply,              // 客服真人准备发出的最终文本（原文，不清洗）
                            probeVersion: PROBE_VERSION
                        }
                    });
                    _demoSent++;
                    _lastDemoPreview = normText(humanReply).slice(0, 40);
                    console.log("🎙️ [探针] 已静默记录 1 条人工回复样本：" + _lastDemoPreview);
                } catch (e) { console.warn("⚠️ [探针] 人工回复嗅探复验异常：", e); }
            }, Math.max(200, parseInt(PROBE_CONFIG.demoVerifyMs, 10) || 1200));
        } catch (e) {
            console.warn("⚠️ [探针] 人工回复嗅探异常（不影响发送）：", e);
        }
    }

    // 注册嗅探监听（只注册一次，在启动处调用）。
    //   ★ 为什么用 mousedown（捕获）+ keydown（捕获）而不是 click：
    //     ① 捕获阶段**先于**页面自己的处理执行，能读到"客服刚打完、还没被清空"的原文；
    //     ② 不使用已有的 document 'click' 监听（那是"人手点状态"的守卫），两者互不干扰；
    //     ③ AI / 手机代发走的是 fireClick() 合成的 pointerdown/mousedown/mouseup/click 事件，
    //        这里靠 isTrusted === false 直接跳过 —— 系统自己发的话绝不会被当成人工样本。
    function setupManualReplySniffer() {
        if (!PROBE_CONFIG.demoCapture) return;
        try {
            document.addEventListener('mousedown', function (ev) {
                try {
                    if (!ev || ev.isTrusted === false) return;      // 合成事件（AI/手机代发）不算人工
                    if (!isManualSendTrigger(ev.target)) return;
                    captureManualDemo('click');
                } catch (e) { console.warn("⚠️ [探针] 人工回复嗅探(mousedown)异常：", e); }
            }, true);
        } catch (e) { console.warn("⚠️ [探针] 人工回复嗅探(mousedown)绑定失败：", e); }
        try {
            document.addEventListener('keydown', function (ev) {
                try {
                    if (!ev || ev.isTrusted === false) return;
                    // Shift+Enter 是换行、输入法组合中的回车都不是"发送"
                    if (ev.key !== 'Enter' || ev.shiftKey || ev.isComposing) return;
                    const box = Operator.composerInput();
                    if (!box) return;
                    const t = ev.target;
                    if (!(t === box || (box.contains && t && box.contains(t)))) return;  // 焦点不在回复框里的回车不算
                    captureManualDemo('enter');
                } catch (e) { console.warn("⚠️ [探针] 人工回复嗅探(keydown)异常：", e); }
            }, true);
        } catch (e) { console.warn("⚠️ [探针] 人工回复嗅探(keydown)绑定失败：", e); }
        console.log("🎙️ [探针] 人工回复嗅探已就绪（只读录制真人回复 -> RECORD_MANUAL_DEMO，不影响发送）");
    }

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

    // 定时任务 4：会话列表上报（手机端"全部会话"就靠它；没变化不上报，防刷屏/防 Token 雪球）
    // ⚠️ 放在最后注册：现有测试按 intervals[0/1/2] 索引取定时器（IM/聊天/心跳），别打乱顺序。
    function bumpConvListReport() {
        if (!pageLoadComplete) return;
        const rows = scanConversationList();
        if (!rows.length) return;
        const fp = rows.map(r => r.name + '|' + r.last + '|' + r.time + (r.active ? '|A' : '')).join('#');
        if (fp === window._lastConvListHash) return;
        if (!sendToBrain({ event: "CONV_LIST", data: { rows: rows } })) return;   // 未连接不更新 hash，重连后补发
        window._lastConvListHash = fp;
        console.log("📋 [探针] 会话列表已上报（" + rows.length + " 个）");
    }
    setInterval(bumpConvListReport, 3000);

    // ==================== ★ V8.7：不聚焦也能感知新消息（后台标签不再"失聪"） ====================
    // 背景：Chrome 对**隐藏标签**的定时器会节流（隐藏 5 分钟后约 1 次/分钟；开"内存节省"还会直接冻结 → JS 全停）。
    //   只靠 setInterval 的探针会漏掉新消息：电脑不响、手机也收不到推送。
    // 对策：MutationObserver 是 **DOM 变化事件驱动**，不受定时器节流影响 —— 聊天区/会话列表一变就立即扫描上报。
    //   原定时器全部保留（前台 2~3 秒一次，作为兜底），**不新增/不打乱任何 setInterval**。
    let lastKickAt = 0;
    function kickScan(why) {
        const now = Date.now();
        if (now - lastKickAt < 500) return;         // 防抖：同一批 DOM 变化只扫一次
        lastKickAt = now;
        try { scanChatAndReport(); } catch (e) {}
        try { bumpConvListReport(); } catch (e) {}
    }

    function setupFocusIndependentScan() {
        // ① DOM 变化即扫描（事件驱动，不受后台节流影响）
        try {
            if (typeof MutationObserver === "undefined") return;
            let target = null;
            try { target = document.querySelector('.ws-right-panel'); } catch (e) {}
            if (target && target.parentElement) target = target.parentElement;
            if (!target) target = document.body;
            if (!target) return;
            const mo = new MutationObserver(function () { kickScan("dom"); });
            mo.observe(target, { childList: true, subtree: true, characterData: true });
        } catch (e) {}
        // ② 切回前台 / 窗口重新聚焦：立刻补扫一次（把后台期间漏掉的补上）
        try {
            document.addEventListener('visibilitychange', function () {
                try { if (!document.hidden) kickScan("visible"); } catch (e) {}
            });
            window.addEventListener('focus', function () { kickScan("focus"); });
        } catch (e) {}
    }

    // ==================== 调试入口（客服/运维可在控制台直接调用） ====================
    window.__probe = {
        version: function () { console.log("探针版本 v" + PROBE_VERSION); return PROBE_VERSION; },
        config: PROBE_CONFIG,          // 可直接改：__probe.config.keepManualOffline = false
        status: function () {
            const st = { version: PROBE_VERSION, kind: chipKind, detail: chipDetail,
                         imStatus: lastIMStatus, manualOffline: isManualOffline,
                         manualAway: manualAwayStatus,
                         keepManualOffline: PROBE_CONFIG.keepManualOffline,
                         guardHits: guardHits,
                         wsState: ws ? ws.readyState : -1, online: !!(ws && ws.readyState === WebSocket.OPEN) };
            console.table ? console.table(st) : console.log(st);
            return st;
        },
        // 立刻去读一次页面上的真实状态并上报（手机端"重新获取状态"走的就是这个）
        refreshStatus: function () { return readAndReportIMStatus(true) ? "已上报当前状态" : "未读到 IM 状态"; },
        // ★ V8.0：看看网页左侧会话列表（手机端"全部会话"的数据源）
        convs: function () {
            const r = scanConversationList();
            console.table ? console.table(r) : console.log(r);
            return r;
        },
        // ★ V7.7：把"状态下拉的可见选项"原样打进控制台并回报中继（只读诊断，不改状态）
        dumpStatus: function () { Operator.dumpStatusMenu(); return "已把状态下拉选项回报给中继（看控制台/中继日志）"; },
        // ★ V7.7：回复框到底抓到哪个元素（排查"草稿写不进去 / 回复按钮是灰的"）
        editor: function () {
            const el = Operator.composerInput();
            const info = el
                ? { tag: el.tagName, cls: String(el.className || ""), empty: Operator.isComposerEmpty(), hint: "" }
                : { tag: "", cls: "", empty: null, hint: Operator.editorFailureReason() };
            console.table ? console.table(info) : console.log(info);
            return info;
        },
        // ★ V8.3：人工回复嗅探自检（客服/运维可直接看"到底记到没有、记了几条"）
        demos: function () {
            const st = { captureEnabled: !!PROBE_CONFIG.demoCapture, captured: _demoCaptured,
                         sent: _demoSent, skipped: _demoSkipped, lastReply: _lastDemoPreview };
            console.table ? console.table(st) : console.log(st);
            return st;
        },
        // ★ V8.7：立刻强制扫一次并上报（"不聚焦也不漏消息"的排障入口）
        forceScan: function () {
            lastKickAt = 0;
            kickScan("manual");
            return "已强制扫描一次（看中继日志 / 手机是否收到这条消息）";
        },
        // ★ V8.7：当前页面是否在后台（不聚焦）—— 后台时由桌面悬浮窗用系统声兜底提醒
        hidden: function () {
            const h = (function () { try { return !!document.hidden; } catch (e) { return false; } })();
            console.log(h ? "页面在后台（不聚焦）：提醒仍会推手机 + 桌面悬浮窗会响" : "页面在前台：网页自己会出声");
            return h;
        },
        reconnect: function () { reconnectAttempts = 0; connectBrain(); return "已触发重连"; }
    };

    // ==================== 启动 ====================
    setupAudioUnlock();                 // ★ V8.6：按键/聚焦即解锁提示音（不再依赖"必须点一下页面"）
    setupFocusIndependentScan();        // ★ V8.7：DOM 变化驱动感知（后台/不聚焦也不漏消息）
    setupManualReplySniffer();          // ★ V8.3：人工回复静默嗅探（只读录制，绝不影响客服操作）
    setChip('idle', "");
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', connectBrain);
    else connectBrain();
})();