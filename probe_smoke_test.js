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

const documentStub = {
    readyState: 'complete',
    body: { innerText: 'IM工作台' },
    addEventListener: () => {},
    createElement: () => ({}),
    querySelector: sel => {
        if (sel === '.ws-right-panel') return { innerText: '玩家A\nUID:12345' };
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

const sandbox = {
    window: windowStub,
    document: documentStub,
    console,
    WebSocket: FakeWebSocket,
    Headers: class {},
    InputEvent: class {},
    Event: class {},
    setInterval: (fn, ms) => { intervals.push(fn); return intervals.length; },
    clearInterval: () => {},
    setTimeout: (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; },
    clearTimeout: () => {},
    JSON, Object, Math, Array, String, Number, Error, Boolean
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
        check('  groupID 字段存在', d.groupID === '当前工单', d.groupID);
    }

    // 8) 相同内容不重复上报
    const before2 = sent.length;
    intervals[1]();
    check('相同聊天内容不重复上报', sent.length === before2, '新增=' + (sent.length - before2));

    // 9) 内容变化（新消息）才上报
    bubbles.push(makeEl('我充值也没到账', ['from-player']));
    intervals[1]();
    check('内容变化后重新上报', sent.length === before2 + 1, '新增=' + (sent.length - before2));

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

    console.log('\n=== 结果: ' + pass + ' 通过 / ' + fail + ' 失败 ===');
    process.exit(fail === 0 ? 0 : 1);
})();