// ==UserScript==
// @name         客服助手 - 智能工单探针 (V7 靶点精调与数组覆盖版)
// @namespace    http://tampermonkey.net/
// @version      7.0
// @description  真实 DOM 靶点、防 Token 雪球、双音效引擎
// @match        *://ticket.example.com/*
// @grant        none
// ==/UserScript==

(function() {
    'use strict';
    console.log("🚀 [客服助手探针 V7] 真实靶点定位系统与防暴雷机制已就绪！");

    let audioCtx = null;
    let sirenInterval = null;
    let isManualOffline = false; 
    window.__im_auth_headers = {};

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
            gain1.gain.setValueAtTime(0, now); gain1.gain.linearRampToValueAtTime(0.8, now + 0.02); gain1.gain.exponentialRampToValueAtTime(0.01, now + 0.4);
            osc1.connect(gain1); gain1.connect(audioCtx.destination);
            osc1.start(now); osc1.stop(now + 0.5);

            const osc2 = audioCtx.createOscillator(), gain2 = audioCtx.createGain();
            osc2.type = 'sine'; osc2.frequency.setValueAtTime(1318.5, now + 0.1); 
            gain2.gain.setValueAtTime(0, now + 0.1); gain2.gain.linearRampToValueAtTime(0.6, now + 0.12); gain2.gain.exponentialRampToValueAtTime(0.01, now + 0.6);
            osc2.connect(gain2); gain2.connect(audioCtx.destination);
            osc2.start(now + 0.1); osc2.stop(now + 0.7);
        } catch(e) {}
    }

    function playSiren() {
        initAudio();
        if (sirenInterval) return; 
        sirenInterval = setInterval(() => {
            try {
                const now = audioCtx.currentTime;
                const osc = audioCtx.createOscillator(), gain = audioCtx.createGain();
                osc.type = 'sawtooth'; osc.frequency.setValueAtTime(600, now); osc.frequency.linearRampToValueAtTime(800, now + 0.4); 
                gain.gain.setValueAtTime(0.3, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.8);
                osc.connect(gain); gain.connect(audioCtx.destination);
                osc.start(now); osc.stop(now + 0.8);
            } catch(e) {}
        }, 1000);
    }

    function stopSiren() {
        if (sirenInterval) { clearInterval(sirenInterval); sirenInterval = null; }
    }

    document.addEventListener('click', (e) => {
        initAudio(); 
        if (e.target && e.target.innerText) {
            const text = e.target.innerText.trim();
            if (text === 'IM离线') isManualOffline = true;
            else if (text === 'IM在线' || text === 'IM忙碌') { isManualOffline = false; stopSiren(); }
        }
    }, true);

    const originalFetch = window.fetch;
    window.fetch = async function(...args) {
        if (args[1] && args[1].headers) {
            const h = args[1].headers instanceof Headers ? Object.fromEntries(args[1].headers.entries()) : args[1].headers;
            for (let k in h) if (k.toLowerCase().includes('token')) window.__im_auth_headers[k] = h[k];
        }
        return originalFetch.apply(this, args);
    };

    const Operator = {
        fillReplyBox: function(text) {
            const composer = document.querySelector('.editor-composer');
            if (!composer) return;
            const inputBox = composer.querySelector('textarea') || composer.querySelector('input') || (composer.getAttribute('contenteditable') ? composer : null);
            if (inputBox) {
                if (inputBox.tagName === 'TEXTAREA' || inputBox.tagName === 'INPUT') {
                    inputBox.value = text;
                    inputBox.dispatchEvent(new Event('input', { bubbles: true }));
                } else {
                    inputBox.innerText = text;
                    inputBox.dispatchEvent(new InputEvent('input', { bubbles: true }));
                }
            }
        },
        selectCategory: function(l1, l2, l3) {
            const catTrigger = document.querySelector('.el-cascader input, input[placeholder="请选择问题分类"]');
            if (catTrigger) catTrigger.click();
            setTimeout(() => {
                const clickOpt = (txt) => {
                    const opts = Array.from(document.querySelectorAll('.el-cascader-node'));
                    const target = opts.find(opt => opt.innerText.includes(txt));
                    if (target) target.click();
                };
                clickOpt(l1);
                setTimeout(() => { clickOpt(l2); setTimeout(() => { clickOpt(l3); }, 250); }, 250);
            }, 300);
        },
        safeClickActionBtn: function(btnName) {
            const btns = Array.from(document.querySelectorAll('.im-action-btn'));
            const targetBtn = btns.find(b => b.innerText && b.innerText.trim() === btnName);
            if (targetBtn) targetBtn.click();
        },
        switchIMStatus: function(targetStatus) {
            const statusTrigger = Array.from(document.querySelectorAll('div, span, button')).find(el => 
                el.innerText && (el.innerText.includes('IM在线') || el.innerText.includes('IM离线') || el.innerText.includes('IM忙碌'))
            );
            if (statusTrigger) statusTrigger.click();

            setTimeout(() => {
                const items = Array.from(document.querySelectorAll('.el-dropdown-menu__item'));
                const target = items.find(item => item.innerText.trim().includes(targetStatus));
                if (target) target.click();
            }, 200);
        }
    };
    let ws = null;
    let reconnectAttempts = 0;
    let maxReconnectAttempts = 10;
    let wsReconnectTimer = null;
    
    function connectBrain() {
        // 清理旧连接（修复 BUG-004：内存泄漏）
        if (ws) {
            ws.onclose = null; // 阻止触发重连
            if (ws.readyState === WebSocket.OPEN) ws.close();
            ws = null;
        }
        
        try {
            ws = new WebSocket('ws://127.0.0.1:8765/ws/extension');
            reconnectAttempts = 0;
            
            ws.onopen = () => {
                console.log("✅ [探针] WebSocket 连接成功");
                // 延迟发送 HEADERS_SYNC，确保 fetch 拦截器已捕获 Token
                setTimeout(() => {
                    if (Object.keys(window.__im_auth_headers).length > 0) {
                        ws.send(JSON.stringify({ event: "HEADERS_SYNC", data: window.__im_auth_headers }));
                    } else {
                        console.warn("⚠️ [探针] 尚未捕获到 Token，等待下一次重连时重试");
                    }
                }, 500);
            };
            
            ws.onclose = (e) => {
                console.warn(`⚠️ [探针] WebSocket 断开 (code=${e.code}), ${reconnectAttempts < maxReconnectAttempts ? '尝试重连...' : '已达最大重连次数'}`);
                if (reconnectAttempts < maxReconnectAttempts) {
                    reconnectAttempts++;
                    const delay = Math.min(3000 * reconnectAttempts, 30000); // 指数退避，最大 30 秒
                    wsReconnectTimer = setTimeout(connectBrain, delay);
                } else {
                    console.error("❌ [探针] WebSocket 重连失败，已达最大尝试次数");
                }
            };
            
            ws.onerror = (e) => {
                console.error("❌ [探针] WebSocket 错误:", e);
            };

            ws.onmessage = (event) => {
            const cmd = JSON.parse(event.data);
            // 新增：处理警报确认回执（修复 BUG-002）
            if (cmd.command === "ALARM_CONFIRMED") {
                console.log("✅ [探针] 掉线警报已确认，停止重复上报");
                return;
            }
            if (cmd.command === "RECOVERY_CONFIRMED") {
                console.log("✅ [探针] 恢复确认已收到");
                return;
            }
            // 原有命令处理
            if (cmd.command === "FILL_DRAFT") {
                if (cmd.content) Operator.fillReplyBox(cmd.content);
                if (cmd.category) Operator.selectCategory("一级分类", "二级分类", cmd.category);
            } 
            else if (cmd.command === "ACTION_REPLY_CLOSE") {
                if (cmd.content) Operator.fillReplyBox(cmd.content);
                if (cmd.category) Operator.selectCategory("一级分类", "二级分类", cmd.category);
                setTimeout(() => Operator.safeClickActionBtn('回复并关单'), 1000); 
            } 
            else if (cmd.command === "ACTION_HANGUP") { Operator.safeClickActionBtn('挂起'); }
            else if (cmd.command === "SEND_REPLY") {
                Operator.fillReplyBox(cmd.content); 
                setTimeout(() => Operator.safeClickActionBtn('回复'), 500); 
            }
            else if (cmd.command === "CHANGE_STATUS") {
                const map = {1: 'IM在线', 2: 'IM忙碌', 3: 'IM离线'};
                Operator.switchIMStatus(map[cmd.status]);
            }
            else if (cmd.command === "SILENCE_ALARM") { stopSiren(); }
        };
    }
    // 修复 BUG-010：页面加载缓慢时误判掉线
    let pageLoadComplete = false;
    setTimeout(() => { pageLoadComplete = true; }, 3000); // 3 秒后认为页面加载完成
    
    setInterval(() => {
        // 监控 VPN 掉线火警
        const statusEl = Array.from(document.querySelectorAll('.el-dropdown-menu__item, span, div')).find(el => 
            el.innerText === 'IM 离线' || el.innerText === 'IM 在线' || el.innerText === 'IM 忙碌'
        );
        if (statusEl && pageLoadComplete) {  // 仅当页面加载完成后才检测
            const currentStatus = statusEl.innerText.trim();
            if (currentStatus === 'IM 离线' && !isManualOffline) {
                playSiren();
                if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ event: "ABNORMAL_OFFLINE" }));
            } else if (currentStatus === 'IM 在线') {
                isManualOffline = false; stopSiren();
                if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ event: "ALARM_RECOVERED" }));
            }
        }

    setInterval(() => {
        // 监控 VPN 掉线火警
        const statusEl = Array.from(document.querySelectorAll('.el-dropdown-menu__item, span, div')).find(el => 
            el.innerText === 'IM离线' || el.innerText === 'IM在线' || el.innerText === 'IM忙碌'
        );
        if (statusEl) {
            const currentStatus = statusEl.innerText.trim();
            if (currentStatus === 'IM离线' && !isManualOffline) {
                playSiren();
                if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ event: "ABNORMAL_OFFLINE" }));
            } else if (currentStatus === 'IM在线') {
                isManualOffline = false; stopSiren();
                if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ event: "ALARM_RECOVERED" }));
            }
        }

        const isIMWorkspace = document.body.innerText.includes('IM工作台');
        if (!isIMWorkspace) return;

        // 提取右侧面板玩家信息
        let playerInfo = "";
        const rightPanel = document.querySelector('.ws-right-panel');
        if (rightPanel) playerInfo = rightPanel.innerText.replace(/\n+/g, ' | ').trim();

        // 提取中间真实聊天气泡，不再拼接成长字符串，而是构造数组彻底隔绝雪球
        let messages = [];
        const bubbles = document.querySelectorAll('.chat-bubble-row');
        bubbles.forEach(row => {
            const isPlayer = row.matches('.from-player') || row.classList.contains('from-player');
            const isAgent = row.matches('.from-agent') || row.classList.contains('from-agent');
            if (!isPlayer && !isAgent) return; 

            const textNode = row.querySelector('.msg-rich-text') || row;
            const content = textNode ? textNode.innerText.trim() : "";
            if (content) messages.push({ sender: isPlayer ? 'player' : 'agent', text: content });
        });

        const currentHash = messages.map(m => m.text).join('').replace(/\s+/g, '');
        if (messages.length > 0 && ws && ws.readyState === WebSocket.OPEN) {
            if (window._lastChatHash !== currentHash) {
                if (messages[messages.length - 1].sender === 'player' || (window._lastChatHash && currentHash.length > window._lastChatHash.length)) { 
                    playDingDong(); 
                }
                window._lastChatHash = currentHash;
                ws.send(JSON.stringify({
                    event: "PLAYER_MESSAGE",
                    data: { groupID: "当前工单", messages: messages, playerInfo: playerInfo }
                }));
            }
        }
    }, 2000);

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', connectBrain);
    else connectBrain();
})();