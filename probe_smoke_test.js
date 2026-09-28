// probe.js 冒烟测试：用 stub 模拟浏览器环境，验证脚本可加载且逻辑正确
const fs = require('fs');
const vm = require('vm');

const TARGET = process.argv[2] || 'probe.js';
const code = fs.readFileSync(TARGET, 'utf8');
const EXPECT_VER = '7.4';        // 与实际 @version 对齐（升级脚本时同步改这里）

const sent = [];
const intervals = [];
const timeouts = [];

// ---------- FakeWebSocket ----------
class FakeWebSocket {
    constructor(url) {
        this.url = url;
        this.readyState = FakeWebSocket.CONNECTING;
        this.onopen = this.onclose = this.onerror = this.onmessage = null;
        FakeWebSocket.instances.push(this);
        setTimeout(() => {
            this.readyState = FakeWebSocket.OPEN;
            if (this.onopen) this.onopen();
        }, 0);
    }
    send(data) { sent.push(JSON.parse(data)); }
    close() {
        this.readyState = FakeWebSocket.CLOSED;
        if (this.onclose) this.onclose({ code: 1000 });
    }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];

// ---------- Fake Audio ----------
let oscCount = 0;                            // 统计振荡器数量 = 响铃次数（叮咚一次建 2 个）
function FakeAudioContext() {
    this.state = 'running';
    this.currentTime = 0;
    this.destination = {};
    this.resume = () => {};
    this.createOscillator = () => {
        oscCount++;
        return {
            type: '', frequency: { setValueAtTime() {}, linearRampToValueAtTime() {} },
            connect() {}, start() {}, stop() {}
        };
    };
    this.createGain = () => ({
        gain: { setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} },
        connect() {}
    });
}

// ---------- Fake DOM ----------
let actionButtons = [];                      // 模拟页面上的操作按钮（挂起/恢复/关单）
let clickedLabels = [];
function makeEl(text, classes) {
    return {
        innerText: text,
        classList: { contains: c => (classes || []).includes(c) },
        querySelector: () => null,
        matches: () => false,
        getAttribute: () => null,
        click() { clickedLabels.push(text); }
    };
}

let statusText = 'IM在线';
let bubbles = [];
let playerInfoText = '玩家A\nUID:12345';
let ticketIdAttr = '';                       // 模拟页面上真实工单号（data-ticket-id）
let pageUrl = 'https://ticket.example.com/workbench';

const docListeners = {};
const chipNodes = [];
function makeNode(tag) {
    return {
        tagName: String(tag).toUpperCase(), style: {}, textContent: '', _children: [], _listeners: {},
        appendChild(c) { this._children.push(c); chipNodes.push(c); return c; },
        addEventListener(ev, fn) { this._listeners[ev] = fn; },
        querySelector: () => null,
        querySelectorAll: () => []
    };
}
const documentStub = {
    readyState: 'complete',
    body: {
        innerText: 'IM工作台',
        _children: [],
        appendChild(c) { this._children.push(c); return c; }
    },
    addEventListener: (ev, fn) => { docListeners[ev] = fn; },
    createElement: tag => makeNode(tag),
    querySelector: sel => {
        if (sel === '.ws-right-panel') return { innerText: playerInfoText };
        if (sel === '[data-ticket-id]') {
            return ticketIdAttr
                ? { getAttribute: () => ticketIdAttr }
                : null;
        }
        return null;
    },
    querySelectorAll: sel => {
        if (sel.indexOf('.el-dropdown-menu__item') === 0) return [makeEl(statusText)];
        if (sel.indexOf('.im-action-btn') === 0) return actionButtons;
        if (sel === '.chat-bubble-row') return bubbles;
        return [];
    }
};

const windowStub = {
    AudioContext: FakeAudioContext,
    fetch: () => Promise.resolve(),
    document: documentStub
};

const locationStub = { get href() { return pageUrl; } };

const sandbox = {
    window: windowStub,
    document: documentStub,
    location: locationStub,
    console,
    WebSocket: FakeWebSocket,
    navigator: { userAgent: 'Mozilla/5.0 (Test) ProbeSmoke' },
    Headers: class {},
    InputEvent: class {},
    Event: class {},
    setInterval: (fn, ms) => { intervals.push(fn); return intervals.length; },
    clearInterval: () => {},
    setTimeout: (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; },
    clearTimeout: () => {},
    JSON, Object, Math, Array, String, Number, Error, Boolean, Promise, Date
};
sandbox.globalThis = sandbox;

let pass = 0, fail = 0;
function check(name, ok, extra) {
    if (ok) { pass++; console.log('  [PASS] ' + name + (extra ? '  -> ' + extra : '')); }
    else { fail++; console.log('  [FAIL] ' + name + (extra ? '  -> ' + extra : '')); }
}

(async () => {
    console.log('=== 测试文件: ' + TARGET + ' ===\n');

    // 1) 加载不抛异常
    try {
        vm.createContext(sandbox);
        vm.runInContext(code, sandbox, { filename: TARGET });
        check('脚本加载无异常', true);
    } catch (e) {
        check('脚本加载无异常', false, e.message);
        console.log('\n结果: 加载失败，后续测试跳过');
        process.exit(1);
    }

    // 2) WebSocket 已创建
    await new Promise(r => setTimeout(r, 20));
    check('创建 WebSocket 连接', FakeWebSocket.instances.length === 1,
        FakeWebSocket.instances[0] ? FakeWebSocket.instances[0].url : 'none');

    // 3) 定时器已注册（IM 状态监控 + 消息抓取 + 心跳/胶囊刷新）
    check('注册 3 个 setInterval（状态/抓取/心跳）', intervals.length >= 3, '实际=' + intervals.length);

    // 3.5) 手动触发脚本注册的 3000ms "页面加载完成" 定时器
    //      （沙箱里的 setTimeout 只做收集，不会自动执行）
    timeouts.filter(t => t.ms === 3000).forEach(t => t.fn());
    check('页面加载完成标记已触发', timeouts.some(t => t.ms === 3000));

    // 4) 掉线警报：在线 -> 离线
    statusText = 'IM离线';
    intervals[0]();
    const offline = sent.filter(s => s.event === 'ABNORMAL_OFFLINE');
    check('检测到 IM离线 并上报 ABNORMAL_OFFLINE', offline.length === 1, '次数=' + offline.length);

    // 5) 状态未变化时不重复上报（BUG-002 修复验证）
    const before = sent.length;
    intervals[0]();
    intervals[0]();
    check('相同状态不重复上报（防雪球）', sent.length === before, '新增=' + (sent.length - before));

    // 6) 恢复：离线 -> 在线
    statusText = 'IM在线';
    intervals[0]();
    const recovered = sent.filter(s => s.event === 'ALARM_RECOVERED');
    check('恢复后上报 ALARM_RECOVERED', recovered.length === 1, '次数=' + recovered.length);

    // 7) 消息抓取与上报
    bubbles = [makeEl('我的号登不上去了', ['from-player']), makeEl('亲爱的玩家您好', ['from-agent'])];
    intervals[1]();
    const msg = sent.filter(s => s.event === 'PLAYER_MESSAGE');
    check('抓取并上报 PLAYER_MESSAGE', msg.length === 1);
    if (msg.length) {
        const d = msg[0].data;
        check('  messages 数组结构正确',
            Array.isArray(d.messages) && d.messages.length === 2 &&
            d.messages[0].sender === 'player' && d.messages[1].sender === 'agent',
            JSON.stringify(d.messages.map(m => m.sender)));
        check('  playerInfo 已提取', d.playerInfo.indexOf('玩家A') !== -1, JSON.stringify(d.playerInfo));
        check('  groupID 已不再硬编码为"当前工单"', d.groupID !== '当前工单', d.groupID);
        check('  groupID 非空', typeof d.groupID === 'string' && d.groupID.length > 0);
        check('  name 字段已上报', !!d.name, JSON.stringify(d.name));
    }

    // 8) 相同内容不重复上报
    const before2 = sent.length;
    intervals[1]();
    check('相同聊天内容不重复上报', sent.length === before2, '新增=' + (sent.length - before2));

    // 9) 内容变化（新消息）才上报
    bubbles.push(makeEl('我充值也没到账', ['from-player']));
    intervals[1]();
    check('内容变化后重新上报', sent.length === before2 + 1, '新增=' + (sent.length - before2));

    // 9.5) 工单身份识别：不同玩家必须得到不同标识，否则手机端永远只有一个会话
    console.log('\n[7] 工单身份识别（多玩家区分）');
    function resetAndReport() {
        sandbox.window._lastChatHash = undefined;
        sent.length = 0;
        intervals[1]();
        const hits = sent.filter(s => s.event === 'PLAYER_MESSAGE');
        return hits.length ? hits[0].data : null;
    }

    // 7a. URL 带工单号 -> 直接采用
    pageUrl = 'https://ticket.example.com/workbench?ticketId=TKT-URL-001';
    ticketIdAttr = '';
    playerInfoText = '玩家甲\nUID:1001';
    bubbles = [makeEl('我卡在登录界面了', ['from-player'])];
    let r = resetAndReport();
    check('URL 中的工单号被采用', !!r && r.groupID === 'TKT-URL-001', r && r.groupID);

    // 7b. DOM 的 data-ticket-id -> 采用
    pageUrl = 'https://ticket.example.com/workbench';
    ticketIdAttr = 'TKT-DOM-777';
    r = resetAndReport();
    check('DOM 的 data-ticket-id 被采用', !!r && r.groupID === 'TKT-DOM-777', r && r.groupID);

    // 7c. 都没有 -> 用玩家信息生成兜底标识
    ticketIdAttr = '';
    playerInfoText = '玩家甲\nUID:1001';
    bubbles = [makeEl('我卡在登录界面了', ['from-player'])];
    r = resetAndReport();
    const gidA = r && r.groupID;
    check('无工单号时生成兜底标识', !!gidA && gidA.charAt(0) === 'P', gidA);

    // 7d. 同一玩家追加消息 -> 标识保持稳定
    bubbles = [makeEl('我卡在登录界面了', ['from-player']), makeEl('还是不行', ['from-player'])];
    r = resetAndReport();
    check('同一玩家标识保持稳定', !!r && r.groupID === gidA, r && r.groupID);

    // 7e. 换玩家 -> 标识必须不同（这是"不再覆盖旧会话"的关键）
    playerInfoText = '玩家乙\nUID:2002';
    bubbles = [makeEl('我的钻石没到账', ['from-player'])];
    r = resetAndReport();
    const gidB = r && r.groupID;
    check('不同玩家得到不同标识', !!gidB && gidB !== gidA, gidA + '  vs  ' + gidB);

    // 7f. 玩家名随消息上报，供手机端当会话名
    check('玩家名已随消息上报', !!r && r.name === '玩家乙', r && JSON.stringify(r.name));

    // 恢复默认环境，避免影响后续用例
    pageUrl = 'https://ticket.example.com/workbench';
    playerInfoText = '玩家A\nUID:12345';

    // 10) 非工作台页面不抓取
    documentStub.body.innerText = '其他页面';
    const before3 = sent.length;
    intervals[1]();
    check('非 IM工作台 页面不抓取', sent.length === before3);

    // 11) 指令处理不抛异常（含非法 JSON）
    const wsInst = FakeWebSocket.instances[0];
    try {
        wsInst.onmessage({ data: JSON.stringify({ command: 'SEND_REPLY', content: '测试回复' }) });
        wsInst.onmessage({ data: JSON.stringify({ command: 'ALARM_CONFIRMED' }) });
        wsInst.onmessage({ data: 'not-json' });
        check('处理下行指令不抛异常', true);
    } catch (e) {
        check('处理下行指令不抛异常', false, e.message);
    }

    // 12) 断线后调度重连定时器
    const toBefore = timeouts.length;
    wsInst.close();
    const reconnectTimers = timeouts.slice(toBefore).filter(t => t.ms > 0);
    check('断线后调度重连定时器', reconnectTimers.length === 1,
        reconnectTimers.length ? (reconnectTimers[0].ms + 'ms 后重连') : 'none');

    // 12.5) 触发重连定时器，验证重建连接
    const instBefore = FakeWebSocket.instances.length;
    reconnectTimers.forEach(t => t.fn());
    check('重连后重建 WebSocket 连接', FakeWebSocket.instances.length === instBefore + 1,
        '实例数 ' + instBefore + ' -> ' + FakeWebSocket.instances.length);

    // 13) HEADERS_SYNC 在 onopen 后延迟发送（Token 已捕获场景）
    sandbox.window.__im_auth_headers = { 'x-cs-token': 'abc123' };
    const t500 = timeouts.filter(t => t.ms === 500);
    check('onopen 注册 500ms 延迟的 HEADERS_SYNC', t500.length >= 1, '数量=' + t500.length);

    console.log('\n[8] IM 状态同步（修复"手动离线同步不过去"）');

    // 前置：确保 WS 处于 OPEN（前面的断线重连用例可能还没走完 onopen）
    const curWs = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    if (curWs.readyState !== FakeWebSocket.OPEN) {
        curWs.readyState = FakeWebSocket.OPEN;
        if (curWs.onopen) curWs.onopen();
    }
    check('前置：WebSocket 已就绪', curWs.readyState === FakeWebSocket.OPEN);

    // 8a. 手动点工作台的「IM离线」-> 必须上报状态，但绝对不能误报警
    if (docListeners.click) docListeners.click({ target: { innerText: 'IM离线' } });
    statusText = 'IM离线';
    sent.length = 0;
    intervals[0]();
    const imsA = sent.filter(s => s.event === 'IM_STATUS');
    check('手动离线会上报 IM_STATUS(status=3)',
        imsA.length === 1 && imsA[0].data.status === 3, JSON.stringify(imsA));
    check('手动离线带 manual=true 标记',
        imsA.length === 1 && imsA[0].data.manual === true);
    check('手动离线不触发 ABNORMAL_OFFLINE（不误报警）',
        sent.filter(s => s.event === 'ABNORMAL_OFFLINE').length === 0);

    // 8b. 切回在线 -> 上报状态 + 恢复事件
    statusText = 'IM在线';
    sent.length = 0;
    intervals[0]();
    check('切回在线会上报 IM_STATUS(status=1)',
        sent.some(s => s.event === 'IM_STATUS' && s.data.status === 1), JSON.stringify(sent));
    check('从离线恢复会发 ALARM_RECOVERED', sent.some(s => s.event === 'ALARM_RECOVERED'));

    // 8c. 忙碌状态也要上报（旧版完全不报）
    statusText = 'IM忙碌';
    sent.length = 0;
    intervals[0]();
    check('忙碌会上报 IM_STATUS(status=2)',
        sent.some(s => s.event === 'IM_STATUS' && s.data.status === 2), JSON.stringify(sent));
    check('忙碌不会误触发警报事件',
        !sent.some(s => s.event === 'ABNORMAL_OFFLINE' || s.event === 'ALARM_RECOVERED'));

    // 8d. WS 重连后必须重新同步一次当前状态（否则服务端会一直用旧值）
    const wsLast = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    sent.length = 0;
    wsLast.close();
    const rt = timeouts.filter(t => t.ms > 0).pop();
    if (rt) rt.fn();
    await new Promise(r => setTimeout(r, 30));
    intervals[0]();                      // 兜底：即使 onopen 没发，这里也必须补发
    check('重连后会重新同步当前 IM 状态',
        sent.some(s => s.event === 'IM_STATUS'), JSON.stringify(sent));

    // 8e. 服务端/手机端主动来要状态 -> 必须立刻复核并上报（哪怕状态没变化）
    console.log('\n[8.5] 强制复核（V7.3：手机端一打开就从电脑网页取真实状态）');
    const wsNow = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    if (wsNow.readyState !== FakeWebSocket.OPEN) wsNow.readyState = FakeWebSocket.OPEN;
    statusText = 'IM离线';
    sent.length = 0;
    wsNow.onmessage({ data: JSON.stringify({ command: 'REQUEST_IM_STATUS' }) });
    const forced = sent.filter(s => s.event === 'IM_STATUS' && s.data.forced === true);
    check('收到 REQUEST_IM_STATUS 立刻复核并上报', forced.length === 1, JSON.stringify(sent));
    check('复核上报的是页面真实状态（离线=3）',
        forced.length === 1 && forced[0].data.status === 3, JSON.stringify(forced));

    // 8f. 连上中继就自动复核一次（服务端重启后不会残留旧状态）
    const t100 = timeouts.filter(t => t.ms === 100);
    check('onopen 注册 100ms 即时状态复核', t100.length >= 1, '数量=' + t100.length);
    sent.length = 0;
    t100.forEach(t => t.fn());
    check('即时复核会上报 IM_STATUS(forced)',
        sent.some(s => s.event === 'IM_STATUS' && s.data.forced === true), JSON.stringify(sent));

    // 8g. 手动离线守护：客服手动挂"离线"后，网页自己跳回"在线"必须被改回离线
    console.log('\n[8.6] 手动离线守护（V7.3：手动离线不允许被网页改回在线）');
    sandbox.window.__probe.config.manualGraceMs = 0;      // 测试里不留免打扰时间
    statusText = 'IM在线';
    intervals[0]();                                        // 先让探针记住"在线"
    if (docListeners.click) docListeners.click({ target: { innerText: 'IM离线' } });
    statusText = 'IM离线';                                 // 客服手动点离线
    sent.length = 0;
    intervals[0]();
    const st8g = sent.filter(s => s.event === 'IM_STATUS');
    check('手动离线先如实上报 status=3',
        st8g.some(s => s.data.status === 3 && s.data.manual === true), JSON.stringify(sent));

    await new Promise(r => setTimeout(r, 5));              // 越过免打扰窗口
    statusText = 'IM在线';                                 // 网页自己跳回在线（非人工点击）
    sent.length = 0;
    intervals[0]();
    const guarded = sent.filter(s => s.event === 'IM_STATUS' && s.data.guarded === true);
    check('网页把状态跳回在线 -> 探针按手动离线改回', guarded.length === 1, JSON.stringify(sent));
    check('守护后会向服务端回报离线（status=3）',
        guarded.length === 1 && guarded[0].data.status === 3, JSON.stringify(guarded));
    check('守护时不误触发 ALARM_RECOVERED（不报警）',
        sent.filter(s => s.event === 'ALARM_RECOVERED').length === 0, JSON.stringify(sent));

    // 8h. 关闭守护后必须"如实上报"（不再硬顶）
    sandbox.window.__probe.config.keepManualOffline = false;
    statusText = 'IM离线';
    sent.length = 0;
    intervals[0]();                                        // 如实上报离线
    statusText = 'IM在线';
    sent.length = 0;
    intervals[0]();                                        // 守护已关 -> 如实上报在线
    check('关闭离线守护后如实上报在线',
        sent.some(s => s.event === 'IM_STATUS' && s.data.status === 1), JSON.stringify(sent));
    sandbox.window.__probe.config.keepManualOffline = true;

    // 8i. 断线不再"重连 10 次就放弃"（放弃后手机端会一直停在旧状态）
    console.log('\n[8.7] 重连永不放弃（V7.3）');
    const rtCount = timeouts.length;
    for (let i = 0; i < 12; i++) {
        const w = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
        if (w.readyState !== FakeWebSocket.OPEN) {
            w.readyState = FakeWebSocket.OPEN;
            if (w.onopen) w.onopen();                      // 模拟连上再断，才走重连分支
        }
        w.close();
        const t = timeouts.filter(t => t.ms > 0).pop();
        if (t) t.fn();                                     // 触发重连（会新建连接）
    }
    check('连续 12 次断线后仍在调度重连', timeouts.length - rtCount >= 12,
        '新增定时器 ' + (timeouts.length - rtCount) + ' 个');
    // 收尾：把最后一次重连真正连上，恢复「已连接」胶囊（[9] 用例依赖）
    const tLast = timeouts.filter(t => t.ms > 0).pop();
    if (tLast) tLast.fn();
    await new Promise(r => setTimeout(r, 20));

    console.log('\n[8.8] 新消息提示音只在"真有新玩家消息"时响（V7.3）');
    documentStub.body.innerText = 'IM工作台';        // 前面的用例把它改成了"其他页面"，这里恢复
    // 场景1：首次打开会话（历史最后一条是玩家发的）-> 不应该响
    pageUrl = 'https://ticket.example.com/workbench';
    ticketIdAttr = '';
    playerInfoText = '玩家甲\nUID:1001';
    sandbox.window._lastChatHash = undefined;
    sandbox.window._lastChatState = undefined;
    bubbles = [makeEl('我的号登不上去了', ['from-player'])];
    oscCount = 0;
    intervals[1]();
    check('首次打开旧会话不响铃（旧版在这里就"叮咚"了）', oscCount === 0, '振荡器=' + oscCount);

    // 场景2：切到另一个会话（最后一条也是玩家发的）-> 不应该响
    playerInfoText = '玩家乙\nUID:2002';
    bubbles = [makeEl('钻石没到账', ['from-player']), makeEl('麻烦尽快处理', ['from-player'])];
    oscCount = 0;
    intervals[1]();
    check('切换到别的会话不响铃', oscCount === 0, '振荡器=' + oscCount);

    // 场景3：当前会话里玩家真的追加了一条消息 -> 必须响
    bubbles.push(makeEl('怎么还没人理我', ['from-player']));
    oscCount = 0;
    intervals[1]();
    check('当前会话玩家真发新消息 -> 响铃', oscCount >= 2, '振荡器=' + oscCount);

    // 场景4：自己（客服）回了一条 -> 不应该响
    bubbles.push(makeEl('亲爱的玩家您好', ['from-agent']));
    oscCount = 0;
    intervals[1]();
    check('客服自己回复不响铃', oscCount === 0, '振荡器=' + oscCount);

    // 场景5：恢复/重新打开同一会话（内容整体重渲染，前缀对不上）-> 不应该响
    bubbles = [makeEl('钻石没到账', ['from-player']), makeEl('麻烦尽快处理', ['from-player']),
               makeEl('怎么还没人理我', ['from-player']), makeEl('【系统】会话已恢复', ['from-agent'])];
    oscCount = 0;
    intervals[1]();
    check('恢复挂起会话（历史被重排）不响铃', oscCount === 0, '振荡器=' + oscCount);

    console.log('\n[8.9] 手机端操作结果回报（V7.3：不再"点了没反应"）');
    const wsAct = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    if (wsAct.readyState !== FakeWebSocket.OPEN) wsAct.readyState = FakeWebSocket.OPEN;

    // 页面上没有按钮时：必须回报"失败 + 页面按钮清单"，而不是静默
    actionButtons = [];
    clickedLabels = [];
    sent.length = 0;
    wsAct.onmessage({ data: JSON.stringify({ command: 'ACTION_HANGUP' }) });
    let actRes = sent.filter(s => s.event === 'ACTION_RESULT');
    check('挂起失败会回报 ACTION_RESULT(ok=false)', actRes.length === 1 && actRes[0].data.ok === false,
        JSON.stringify(actRes));
    check('失败回报里带"未找到按钮"说明',
        actRes.length === 1 && String(actRes[0].data.detail).indexOf('未找到') !== -1,
        actRes.length ? actRes[0].data.detail : 'none');

    // 页面上有「挂起」按钮时：必须点到它并回报成功
    actionButtons = [makeEl('转交他人', ['im-action-btn']), makeEl('挂起', ['im-action-btn']),
                     makeEl('回复并关单', ['im-action-btn'])];
    clickedLabels = [];
    sent.length = 0;
    wsAct.onmessage({ data: JSON.stringify({ command: 'ACTION_HANGUP' }) });
    actRes = sent.filter(s => s.event === 'ACTION_RESULT');
    check('挂起成功：真的点了「挂起」按钮', clickedLabels.indexOf('挂起') !== -1, JSON.stringify(clickedLabels));
    check('挂起成功会回报 ACTION_RESULT(ok=true)', actRes.length === 1 && actRes[0].data.ok === true,
        JSON.stringify(actRes));

    // 恢复：页面上是「恢复」按钮
    actionButtons = [makeEl('挂起', ['im-action-btn']), makeEl('恢复', ['im-action-btn'])];
    clickedLabels = [];
    sent.length = 0;
    wsAct.onmessage({ data: JSON.stringify({ command: 'ACTION_RESUME' }) });
    actRes = sent.filter(s => s.event === 'ACTION_RESULT');
    check('恢复：点到「恢复」按钮并回报成功',
        clickedLabels.indexOf('恢复') !== -1 && actRes.length === 1 && actRes[0].data.ok === true,
        JSON.stringify(clickedLabels) + ' ' + JSON.stringify(actRes));

    // 关单：优先点「回复并关单」
    actionButtons = [makeEl('挂起', ['im-action-btn']), makeEl('回复并关单', ['im-action-btn'])];
    clickedLabels = [];
    sent.length = 0;
    wsAct.onmessage({ data: JSON.stringify({ command: 'ACTION_REPLY_CLOSE', category: '' }) });
    await new Promise(r => setTimeout(r, 5));
    timeouts.filter(t => t.ms === 800).forEach(t => t.fn());
    check('关单：点到「回复并关单」并回报成功',
        clickedLabels.indexOf('回复并关单') !== -1 && sent.some(s => s.event === 'ACTION_RESULT' && s.data.ok === true),
        JSON.stringify(clickedLabels));

    // 分类不盲选：源码里不允许再有"取第一项兜底"
    check('分类选不中时不再盲选第一项（改回报候选）',
        code.indexOf('兜底：取第一项') === -1 && code.indexOf('宁可不选') !== -1);

    console.log('\n[8.10] 玩家名与工单标识（V7.4：不再全叫"玩家信息"、不再重复卡片）');
    documentStub.body.innerText = 'IM工作台';
    pageUrl = 'https://ticket.example.com/workbench';
    ticketIdAttr = '';
    // 面板第一行是栏目名"玩家信息"，真正的昵称在第 2 行
    playerInfoText = '玩家信息\n昵称：张三\nUID:10001\n区服：S12';
    bubbles = [makeEl('抽卡没到账', ['from-player'])];
    let r1 = resetAndReport();
    check('玩家名取的是昵称，不是栏目名「玩家信息」', !!r1 && r1.name === '张三', r1 && r1.name);
    const gidName = r1 && r1.groupID;
    check('工单标识仍以 P 开头（无工单号时）', !!gidName && gidName.charAt(0) === 'P', gidName);

    // 追加消息 -> 标识必须不变（旧版把"首条玩家消息"混进指纹，会分裂成两个会话=重复卡片）
    bubbles = [makeEl('抽卡没到账', ['from-player']), makeEl('还没处理好吗', ['from-player'])];
    let r2 = resetAndReport();
    check('同一玩家追加消息 -> 工单标识不变（不再产生重复卡片）',
        !!r2 && r2.groupID === gidName, (r2 && r2.groupID) + ' vs ' + gidName);

    // 模拟"列表虚拟滚动/重渲染"导致首条消息变了 —— 标识依然不能变
    bubbles = [makeEl('还没处理好吗', ['from-player']), makeEl('真的很急', ['from-player'])];
    let r3 = resetAndReport();
    check('首条消息变化（滚动/重渲染）也不换标识',
        !!r3 && r3.groupID === gidName, (r3 && r3.groupID) + ' vs ' + gidName);

    // 没有昵称时用 UID 后 4 位兜底，至少能区分不同玩家
    playerInfoText = '玩家信息\nUID:98765';
    bubbles = [makeEl('帮我看看充值', ['from-player'])];
    let r4 = resetAndReport();
    check('没有昵称时用 UID 兜底命名', !!r4 && r4.name === '玩家8765', r4 && r4.name);
    check('不同玩家 -> 不同标识', !!r4 && r4.groupID !== gidName, (r4 && r4.groupID) + ' vs ' + gidName);

    // 恢复默认环境
    playerInfoText = '玩家A\nUID:12345';

    console.log('\n[8.11] 问题分类下拉不再"自己跑下来"（V7.4）');
    check('探针内置分类缓存（用户自己点开时顺手采集）',
        code.indexOf('CATEGORY_CACHE_MS') !== -1 && code.indexOf('cacheCategoryOptions') !== -1);
    check('缓存命中时不再点开网页上的分类下拉',
        code.indexOf('categoryCache.options.length') !== -1 && code.indexOf('面板本来就开着') !== -1);
    check('读完会把下拉收起，不留一个自己弹开的菜单', code.indexOf('再点一下收起') !== -1);
    // 中继侧：只有"还没拿到过分类"时才请求（避免每次连接都去点开）
    let serverSrc = '';
    try { serverSrc = fs.readFileSync('bridge_server.py', 'utf8'); } catch (e) {}
    check('中继只在没拿到分类时才请求（避免反复点开）',
        serverSrc.indexOf('if not state.get("category_options")') !== -1,
        serverSrc ? '' : 'bridge_server.py 不可读');

    console.log('\n[9] 自检可见性（V7.2 新增：页面胶囊 + 握手 + 心跳）');
    const chip = (documentStub.body._children || []).find(n => n.tagName === 'DIV');
    const chipTextOf = n => (n && n._text && n._text.textContent) || '';
    check('已挂载页面内状态胶囊（不再只能靠控制台判断）', !!chip);
    check('胶囊文字带版本号 v' + EXPECT_VER, chipTextOf(chip).indexOf('v' + EXPECT_VER) !== -1, chipTextOf(chip));
    check('连接正常时胶囊显示「已连接」', chipTextOf(chip).indexOf('已连接') !== -1, chipTextOf(chip));

    const curWs2 = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    curWs2.close();
    check('断线后胶囊提示「重连中」', chipTextOf(chip).indexOf('重连中') !== -1, chipTextOf(chip));

    sent.length = 0;
    const instBefore2 = FakeWebSocket.instances.length;
    if (chip && chip._listeners && chip._listeners.click) chip._listeners.click({ target: null });
    check('点击胶囊可立即重连', FakeWebSocket.instances.length === instBefore2 + 1,
        '实例数 ' + instBefore2 + ' -> ' + FakeWebSocket.instances.length);
    await new Promise(r => setTimeout(r, 20));
    check('重连成功后胶囊恢复「已连接」', chipTextOf(chip).indexOf('已连接') !== -1, chipTextOf(chip));

    const hello = sent.filter(s => s.event === 'PROBE_HELLO');
    check('连上即发 PROBE_HELLO 握手（后端 /api/diag 可查版本）',
        hello.length >= 1 && hello[0].data.version === EXPECT_VER,
        JSON.stringify(hello[0] ? hello[0].data : null));
    check('PROBE_HELLO 携带页面地址', !!hello.length && String(hello[0].data.page).indexOf('ticket-web') !== -1,
        hello.length ? hello[0].data.page : 'none');

    sent.length = 0;
    intervals[2]();
    const hb = sent.filter(s => s.event === 'PROBE_HEARTBEAT');
    check('心跳定时器上报 PROBE_HEARTBEAT',
        hb.length === 1 && hb[0].data.version === EXPECT_VER, JSON.stringify(hb[0] ? hb[0].data : null));

    const probeApi = sandbox.window.__probe || {};
    check('__probe 调试入口可用（version/status/reconnect）',
        typeof probeApi.version === 'function' && typeof probeApi.status === 'function' &&
        typeof probeApi.reconnect === 'function');
    check('__probe.config 可调（离线守护等参数）',
        probeApi.config && typeof probeApi.config.keepManualOffline === 'boolean' &&
        typeof probeApi.config.manualGraceMs === 'number',
        JSON.stringify(probeApi.config));
    check('__probe.refreshStatus 可手动复核状态',
        typeof probeApi.refreshStatus === 'function');

    console.log('\n=== 结果: ' + pass + ' 通过 / ' + fail + ' 失败 ===');
    process.exit(fail === 0 ? 0 : 1);
})();