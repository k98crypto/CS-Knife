// ==UserScript==
// @name         客服助手 - 智能工单探针 (V7 靶点精调与数组覆盖版)
// @namespace    http://tampermonkey.net/
// @version      7.1
// @description  真实 DOM 靶点、防 Token 雪球、双音效引擎、WebSocket 指数退避重连
// @match        *://ticket.example.com/*
// @grant        none
// ==/UserScript==

(function() {
    'use strict';
    console.log("🚀 [客服助手探针 V7.1] 真实靶点定位系统与防暴雷机制已就绪！");

    let audioCtx = null;
    let sirenInterval = null;
    let isManualOffline = false;
    window.__im_auth_headers = {};

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
            const catTrigger = document.querySelector('.el-cascader input, input[placeholder="请选择问题分类"]');
            if (catTrigger) catTrigger.click();
            setTimeout(() => {
                const clickOpt = (txt) => {
                    if (!txt) return;
                    const opts = Array.from(document.querySelectorAll('.el-cascader-node'));
                    const target = opts.find(opt => opt.innerText.includes(txt));
                    if (target) target.click();
                };
                clickOpt(l1);
                setTimeout(() => {
                    clickOpt(l2);
                    setTimeout(() => { clickOpt(l3); }, 250);
                }, 250);
            }, 300);
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
            return;
        }
        reconnectAttempts++;
        const delay = Math.min(3000 * reconnectAttempts, 30000);
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
            console.warn("⚠️ [探针] WebSocket 断开 (code=" + e.code + ")");
            scheduleReconnect();
        };

        ws.onerror = () => {
            console.error("❌ [探针] WebSocket 错误（中继服务可能未启动）");
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
                if (cmd.category) Operator.selectCategory("一级分类", "二级分类", cmd.category);
                setTimeout(() => Operator.safeClickActionBtn('回复并关单'), 1000);
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

    // ==================== 定时任务 1：VPN 掉线火警监控 ====================
    let lastIMStatus = null;
    setInterval(() => {
        if (!pageLoadComplete) return;

        const currentStatus = findIMStatusText();
        if (currentStatus === lastIMStatus) return;   // 状态未变化，不重复上报

        if (currentStatus === 'IM离线' && !isManualOffline) {
            playSiren();
            sendToBrain({ event: "ABNORMAL_OFFLINE" });
        } else if (currentStatus === 'IM在线') {
            isManualOffline = false;
            stopSiren();
            sendToBrain({ event: "ALARM_RECOVERED" });
        }
        lastIMStatus = currentStatus;
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

        const currentHash = messages.map(m => m.text).join('').replace(/\s+/g, '');
        if (window._lastChatHash === currentHash) return;   // 内容未变化，不上报

        const lastSender = messages[messages.length - 1].sender;
        if (lastSender === 'player' ||
            (window._lastChatHash && currentHash.length > window._lastChatHash.length)) {
            playDingDong();
        }
        window._lastChatHash = currentHash;

        sendToBrain({
            event: "PLAYER_MESSAGE",
            data: { groupID: "当前工单", messages: messages, playerInfo: playerInfo }
        });
    }, 2000);

    // ==================== 启动 ====================
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', connectBrain);
    else connectBrain();
})();