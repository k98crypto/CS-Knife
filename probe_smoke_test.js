// probe.js 冒烟测试：用 stub 模拟浏览器环境，验证脚本可加载且逻辑正确
const fs = require('fs');
const vm = require('vm');

const TARGET = process.argv[2] || 'probe_new.js';
const code = fs.readFileSync(TARGET, 'utf8');

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
function FakeAudioContext() {
    this.state = 'running';
    this.currentTime = 0;
    this.destination = {};
    this.resume = () => {};
    this.createOscillator = () => ({
        type: '', frequency: { setValueAtTime() {}, linearRampToValueAtTime() {} },
        connect() {}, start() {}, stop() {}
    });
    this.createGain = () => ({
        gain: { setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} },
        connect() {}
    });
}

// ---------- Fake DOM ----------
function makeEl(text, classes) {
    return {
        innerText: text,
        classList: { contains: c => (classes || []).includes(c) },
        querySelector: () => null,
        matches: () => false,
        getAttribute: () => null,
        click() {}
    };
}

let statusText = 'IM在线';
let bubbles = [];
let playerInfoText = '玩家A\nUID:12345';
let ticketIdAttr = '';                       // 模拟页面上真实工单号（data-ticket-id）
let pageUrl = 'https://ticket.example.com/workbench';

const docListeners = {};
const documentStub = {
    readyState: 'complete',
    body: { innerText: 'IM工作台' },
    addEventListener: (ev, fn) => { docListeners[ev] = fn; },
    createElement: () => ({}),
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

    // 3) 定时器已注册（状态监控 + 消息抓取）
    check('注册 2 个 setInterval', intervals.length === 2, '实际=' + intervals.length);

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
    wsLast.close();
    const rt = timeouts.filter(t => t.ms > 0).pop();
    if (rt) rt.fn();
    await new Promise(r => setTimeout(r, 30));
    sent.length = 0;
    intervals[0]();                      // 状态没变化，但 onopen 重置了记忆 -> 应重新上报
    check('重连后会重新同步当前 IM 状态',
        sent.some(s => s.event === 'IM_STATUS'), JSON.stringify(sent));

    console.log('\n=== 结果: ' + pass + ' 通过 / ' + fail + ' 失败 ===');
    process.exit(fail === 0 ? 0 : 1);
})();