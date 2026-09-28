// H5 安全回归测试：从 bridge_server.py 提取内嵌手机端 JS，在 Node vm 中用假 DOM 运行，
// 验证 1) 存储型 XSS 已修复  2) 断线时按钮不再抛异常  3) 重连为指数退避
const fs = require('fs');
const vm = require('vm');

const py = fs.readFileSync('bridge_server.py', 'utf8');
const m = py.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log('[FAIL] 未能在 bridge_server.py 中找到 <script> 块'); process.exit(1); }
const h5code = m[1];

let pass = 0, fail = 0;
function check(name, ok, extra) {
    if (ok) { pass++; console.log('  [PASS] ' + name + (extra ? '  -> ' + extra : '')); }
    else { fail++; console.log('  [FAIL] ' + name + (extra ? '  -> ' + extra : '')); }
}

// ---------------- 假 DOM ----------------
function makeEl(id) {
    return {
        id: id, _html: '', className: '', innerText: '', value: '',
        scrollTop: 0, scrollHeight: 100, clientHeight: 50,
        dataset: {}, _click: null, _listeners: {},
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); },
            remove(c) { this._s.delete(c); },
            contains(c) { return this._s.has(c); }
        },
        set innerHTML(v) { this._html = String(v); },
        get innerHTML() { return this._html; },
        addEventListener(ev, fn) { this._listeners[ev] = fn; if (ev === 'click') this._click = fn; },
        querySelectorAll(sel) {
            const out = [];
            if (sel === '.conv-card') {
                const re = /data-gid="([^"]*)"/g;
                let mm;
                while ((mm = re.exec(this._html)) !== null) {
                    const card = makeEl('conv-card');
                    card.dataset.gid = mm[1];
                    out.push(card);
                }
                this._cards = out;      // 缓存，便于测试触发点击
            }
            return out;
        }
    };
}

const els = {};
const IDS = ['afk-btn', 'alarm-overlay', 'chat-input', 'conv-container',
             'chat-stream', 'chat-player-name', 'chat-view', 'list-view'];
IDS.forEach(i => { els[i] = makeEl(i); });

const documentStub = {
    getElementById: id => els[id] || (els[id] = makeEl(id)),
    querySelectorAll: () => [],
    addEventListener: () => {}
};

const alerts = [];
const timeouts = [];
const FakeWebSocket = class {
    constructor(url) {
        this.url = url;
        this.readyState = 0;                  // CONNECTING
        this.onopen = this.onclose = this.onerror = this.onmessage = null;
        FakeWebSocket.instances.push(this);
    }
    send() {}
    close() { this.readyState = 3; }
};
FakeWebSocket.CONNECTING = 0; FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2; FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];

const windowStub = {
    location: { protocol: 'http:', host: '127.0.0.1:8765' },
    addEventListener: () => {},
    removeEventListener: () => {},
    AudioContext: function () {
        this.state = 'running'; this.currentTime = 0; this.destination = {};
        this.resume = () => {};
        this.createOscillator = () => ({ frequency: { setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {}, start() {}, stop() {} });
        this.createGain = () => ({ gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} });
    }
};

const sandbox = {
    window: windowStub, document: documentStub, console,
    WebSocket: FakeWebSocket,
    alert: msg => alerts.push(msg),
    confirm: () => true,
    setInterval: () => 0, clearInterval: () => {},
    setTimeout: (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; },
    clearTimeout: () => {},
    JSON, Math, Array, String, Object, Number, Error, Boolean, Promise, Date
};
sandbox.globalThis = sandbox;

// ---------------- 执行 H5 脚本 ----------------
console.log('=== H5 安全回归测试 ===\n');
try {
    vm.createContext(sandbox);
    vm.runInContext(h5code, sandbox, { filename: 'h5_embedded.js' });
    check('H5 脚本加载无异常', true);
} catch (e) {
    check('H5 脚本加载无异常', false, e.message);
    console.log('\n结果: 加载失败，后续跳过');
    process.exit(1);
}

const wsInst = FakeWebSocket.instances[0];
check('initWS 已建立 WebSocket', FakeWebSocket.instances.length === 1,
    wsInst ? wsInst.url : 'none');

// ---------------- 注入恶意数据 ----------------
const EVIL_IMG = '<img src=x onerror=alert(1)>';
const EVIL_NAME = '"><img src=x onerror=alert(2)>';
const EVIL_SENDER = 'player"; onmouseover="alert(3)';
const EVIL_GID = '当前工单';

function onmsg(obj) { wsInst.onmessage({ data: JSON.stringify(obj) }); }

onmsg({
    type: 'FULL_SYNC',
    data: {
        afk_mode: false, alarm_status: false,
        companies: { main: { conversations: {
            [EVIL_GID]: { name: EVIL_NAME, msgs: [{ sender: EVIL_SENDER, text: EVIL_IMG }] }
        } } }
    }
});

console.log('\n[1] 会话列表渲染（renderAll）');
const convHtml = els['conv-container'].innerHTML;
check('玩家昵称中的 <img> 已被转义', convHtml.indexOf('<img') === -1);
check('昵称中的属性逃逸引号已被转义', convHtml.indexOf('onerror=alert(2)>') === -1);
check('会话卡片改用 data-gid（不再内联 onclick 拼数据）',
    convHtml.indexOf('data-gid=') !== -1 && convHtml.indexOf("onclick=\"pushChat(") === -1);

console.log('\n[2] 点击会话卡片（事件委托）');
const cards = els['conv-container']._cards || [];
check('成功解析出会话卡片', cards.length === 1, '数量=' + cards.length);
if (cards.length) {
    cards[0]._click && cards[0]._click();
    check('点击后 chat-view 被激活', els['chat-view'].classList.contains('active'));
    check('点击后玩家名以 innerText 安全写入', els['chat-player-name'].innerText.indexOf('<img') !== -1);
}

console.log('\n[3] 聊天流渲染（renderChatStream）');
const streamHtml = els['chat-stream'].innerHTML;
check('消息中的 <img> 已被转义为 &lt;img', streamHtml.indexOf('<img') === -1 && streamHtml.indexOf('&lt;img') !== -1);
check('恶意 sender 被限制为 player/agent（无 class 注入）',
    streamHtml.indexOf('onmouseover') === -1 && /class="msg-row (player|agent)"/.test(streamHtml),
    streamHtml.slice(0, 90));

console.log('\n[4] 断线时按钮不再"失灵"');
wsInst.readyState = 3;                 // CLOSED
let threw = null;
try { sandbox.execCommand('SEND'); } catch (e) { threw = e.message; }
check('execCommand 在断线时不抛异常', threw === null, threw || '');
check('execCommand 断线时给出提示', alerts.length > 0, alerts[alerts.length - 1] || '');

let threw2 = null;
try { sandbox.toggleAFK(); } catch (e) { threw2 = e.message; }
check('toggleAFK 在断线时不抛异常', threw2 === null, threw2 || '');

console.log('\n[5] 重连改为指数退避');
timeouts.length = 0;
wsInst.onclose && wsInst.onclose();
check('首次重连延迟 2000ms（原为固定 2500ms）',
    timeouts.length === 1 && timeouts[0].ms === 2000,
    timeouts.length ? (timeouts[0].ms + 'ms') : 'none');

console.log('\n[6] 脏包容错');
let threw3 = null;
try { wsInst.onmessage({ data: 'not-a-json' }); } catch (e) { threw3 = e.message; }
check('非法 JSON 下行消息不抛异常', threw3 === null, threw3 || '');

console.log('\n[7] 多会话（微信式好友列表）');
function fullSync(convs) {
    onmsg({ type: 'FULL_SYNC', data: { afk_mode: false, alarm_status: false,
        companies: { main: { conversations: convs } } } });
}
const nowMs = Date.now();
fullSync({
    'T-1': { name: '玩家甲', updatedAt: nowMs - 300000, msgs: [{ sender: 'player', text: '甲的问题', ts: nowMs - 300000 }] },
    'T-2': { name: '玩家乙', updatedAt: nowMs - 10000,  msgs: [{ sender: 'agent',  text: '乙的回复', ts: nowMs - 10000 }] },
    'T-3': { name: '玩家丙', updatedAt: nowMs - 60000,  msgs: [{ sender: 'player', text: '丙的问题', ts: nowMs - 60000 }] }
});
const listHtml = els['conv-container'].innerHTML;
const cardCount = (listHtml.match(/data-gid=/g) || []).length;
check('主页同时显示全部 3 个会话（不再只剩一个）', cardCount === 3, '卡片数=' + cardCount);
check('三个玩家名都出现在列表中',
    listHtml.indexOf('玩家甲') !== -1 && listHtml.indexOf('玩家乙') !== -1 && listHtml.indexOf('玩家丙') !== -1);
const pT2 = listHtml.indexOf('玩家乙'), pT3 = listHtml.indexOf('玩家丙'), pT1 = listHtml.indexOf('玩家甲');
check('按最近活动倒序排列（乙 → 丙 → 甲）', pT2 < pT3 && pT3 < pT1, `乙@${pT2} 丙@${pT3} 甲@${pT1}`);
check('玩家发来且未打开的会话显示未读点', (listHtml.match(/unread-dot/g) || []).length >= 1);
check('旧会话的消息没有被新会话覆盖',
    listHtml.indexOf('甲的问题') !== -1 && listHtml.indexOf('丙的问题') !== -1);

// 打开其中一个，其余必须保留
const cards3 = els['conv-container']._cards || [];
check('解析出 3 张会话卡片', cards3.length === 3, '数量=' + cards3.length);
if (cards3.length === 3) {
    cards3[2]._click();                    // 倒序后第 3 张 = 玩家甲
    check('打开某个会话后，其余会话仍在列表里',
        ((els['conv-container'].innerHTML.match(/data-gid=/g) || []).length) === 3);
    check('打开后切到聊天页', els['chat-view'].classList.contains('active'));
    check('打开后列表页隐藏', !els['list-view'].classList.contains('active'));
    check('聊天页标题为对应玩家', els['chat-player-name'].innerText === '玩家甲', els['chat-player-name'].innerText);
    const stream2 = els['chat-stream'].innerHTML;
    check('加载了该玩家的聊天记录', stream2.indexOf('甲的问题') !== -1);
    check('聊天记录显示时间', stream2.indexOf('msg-time') !== -1);
    check('打开后返回列表仍保留全部会话', (function () {
        sandbox.popChat();
        return ((els['conv-container'].innerHTML.match(/data-gid=/g) || []).length) === 3
            && els['list-view'].classList.contains('active');
    })());
}

console.log('\n[8] 布局与滚动（静态检查）');
check('已移除 transform 滑动（iOS 文字发虚的元凶）', py.indexOf('transform: translateX') === -1);
check('页面用 fixed 锁定视口（内容不再滑到浏览器工具栏下方）', /#app\s*\{[^}]*position:\s*fixed/.test(py));
check('会话列表是独立滚动容器', /\.conv-list\s*\{[^}]*overflow-y:\s*auto/.test(py));
check('聊天记录是独立滚动容器', /\.chat-stream\s*\{[^}]*overflow-y:\s*auto/.test(py));
check('页面切换改用 display 而非 transform', py.indexOf('.view.active { display: flex; }') !== -1);
check('弹性高度用 min-height:0 收敛（保证内部能滚动）', (py.match(/min-height:\s*0/g) || []).length >= 3);

console.log('\n=== 结果: ' + pass + ' 通过 / ' + fail + ' 失败 ===');
process.exit(fail === 0 ? 0 : 1);