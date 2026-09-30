// H5 操作链路回归测试（V8.9）：复现"点会话 -> 输入 -> 点发送/各按钮"，
// 用假 DOM + 假 WebSocket 抓帧，验证每一次点击都**真的发出指令**或有**明确原因**。
// 背景：客服反馈"输入了内容也点不了发送；点上方所有按钮都没反应" —— 这里就是那条链路的体检。
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

// ---------------- 假 DOM（比安全测试更完整：支持 createElement/body/querySelector） ----------------
function makeEl(id) {
    const el = {
        id: id, _html: '', className: '', innerText: '', value: '', textContent: '',
        style: {}, scrollTop: 0, scrollHeight: 100, clientHeight: 50,
        dataset: {}, _click: null, _listeners: {}, _q: {},
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
            contains(c) { return this._s.has(c); }
        },
        set innerHTML(v) { this._html = String(v); },
        get innerHTML() { return this._html; },
        addEventListener(ev, fn) { this._listeners[ev] = fn; if (ev === 'click') this._click = fn; },
        removeEventListener() {},
        appendChild(c) { return c; },
        setAttribute() {}, getAttribute() { return ''; },
        focus() {}, blur() {},
        closest(sel) {
            if (sel === '.conv-card[data-gid]' && this.dataset && this.dataset.gid) return this;
            if (sel === '[data-openname]' && this.dataset && this.dataset.openname) return this;
            return null;
        },
        insertAdjacentHTML(pos, v) { if (pos === 'beforeend') this._html += String(v); },
        querySelector(sel) { return this._q[sel] || (this._q[sel] = makeEl(sel)); },
        querySelectorAll(sel) {
            const out = [];
            if (sel.indexOf('.conv-card') === 0) {
                const re = /data-gid="([^"]*)"/g;
                let mm;
                while ((mm = re.exec(this._html)) !== null) {
                    const card = makeEl('conv-card');
                    card.dataset.gid = mm[1];
                    out.push(card);
                }
                this._cards = out;
            } else if (sel.indexOf('[data-openname]') === 0) {
                const re = /data-openname="([^"]*)"\s+data-openlast="([^"]*)"/g;
                let mm;
                while ((mm = re.exec(this._html)) !== null) {
                    const row = makeEl('page-conv-row');
                    row.dataset.openname = mm[1];
                    row.dataset.openlast = mm[2];
                    out.push(row);
                }
            }
            return out;
        }
    };
    return el;
}

const els = {};
['im-pill', 'im-label', 'im-dot', 'afk-pill', 'afk-label', 'afk-dot',
 'sheet', 'sheet-title', 'sheet-body', 'sheet-backdrop', 'link-chips', 'brand-sub',
 'player-card', 'toast', 'alarm-overlay', 'chat-input', 'conv-container', 'chat-stream',
 'chat-player-name', 'chat-ticket-id', 'chat-view', 'list-view', 'btn-send',
 'action-bar', 'feature-hint', 'human-banner', 'err-bar', 'err-text'].forEach(i => { els[i] = makeEl(i); });

const documentStub = {
    getElementById: id => els[id] || (els[id] = makeEl(id)),
    querySelector: sel => els[sel] || (els[sel] = makeEl(sel)),
    querySelectorAll: () => [],
    createElement: tag => makeEl('el-' + tag),
    addEventListener: () => {},
    body: makeEl('body'),
    documentElement: makeEl('html')
};

const timeouts = [];
const FakeWebSocket = class {
    constructor(url) {
        this.url = url; this.readyState = 0;
        this.onopen = this.onclose = this.onerror = this.onmessage = null;
        FakeWebSocket.instances.push(this);
    }
    send(data) { FakeWebSocket.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; }
};
FakeWebSocket.sent = [];
FakeWebSocket.instances = [];
FakeWebSocket.CONNECTING = 0; FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2; FakeWebSocket.CLOSED = 3;

const windowStub = {
    location: { protocol: 'http:', host: '127.0.0.1:8765' },
    addEventListener: () => {}, removeEventListener: () => {},
    AudioContext: function () {
        this.state = 'running'; this.currentTime = 0; this.destination = {};
        this.resume = () => {};
        this.createOscillator = () => ({ frequency: { setValueAtTime() {}, linearRampToValueAtTime() {} },
                                         connect() {}, start() {}, stop() {} });
        this.createGain = () => ({ gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {},
                                           linearRampToValueAtTime() {} }, connect() {} });
    }
};

function makeSandbox() {
    const sb = {
        window: Object.assign({}, windowStub), document: documentStub, console,
        WebSocket: FakeWebSocket,
        alert: () => {}, confirm: () => true,
        setInterval: () => 0, clearInterval: () => {},
        setTimeout: (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; },
        clearTimeout: () => {},
        JSON, Math, Array, String, Object, Number, Error, Boolean, Promise, Date
    };
    sb.globalThis = sb;
    vm.createContext(sb);
    vm.runInContext(h5code, sb, { filename: 'h5_embedded.js' });
    return sb;
}


console.log('=== H5 操作链路回归测试（V8.9） ===\n');

let sb;
try {
    sb = makeSandbox();
    check('H5 脚本加载无异常', true);
} catch (e) {
    check('H5 脚本加载无异常', false, e.message);
    process.exit(1);
}

const wsInst = FakeWebSocket.instances[0];
wsInst.readyState = 1;                 // OPEN：模拟"手机已连上中继"
if (typeof wsInst.onopen === 'function') wsInst.onopen();
function onmsg(obj) { wsInst.onmessage({ data: JSON.stringify(obj) }); }
const h5 = () => sb.window.__h5();

check('页面版本已升到 8.7（能一眼确认手机上跑的是新版）', h5().ver === '8.7', h5().ver);
check('有 __h5() 排障入口（版本/连接/会话/上次提示/上次异常）',
      typeof sb.window.__h5 === 'function');

// 造一条会话并同步（模拟中继 FULL_SYNC）
const CONV = { name: '测试玩家', updatedAt: Date.now(),
               msgs: [{ sender: 'player', text: '你好', ts: Date.now() }] };
function fullSync(convs, online) {
    onmsg({ type: 'FULL_SYNC', data: { afk_mode: false, alarm_status: false,
        extension_online: online !== false, im_status: 1, im_status_known: true, human_alerts: [],
        companies: { main: { conversations: convs || {} } } } });
}
fullSync({ 'T-1': CONV });
check('FULL_SYNC 后手机认识 1 条会话', h5().convs === 1, String(h5().convs));

// ① 点会话卡片 -> 必须真的进入会话（activeGroupId 有值）
//    ★ 这就是"点上方所有按钮都没反应"的根因位：activeGroupId 为空时所有按钮都不干活
const cards = els['conv-container']._cards || [];
check('列表渲染出了会话卡片', cards.length >= 1, String(cards.length));
if (cards.length) {
    if (typeof cards[0]._click === 'function') { cards[0]._click({}); }
    else if (els['conv-container']._listeners && els['conv-container']._listeners.click) {
        els['conv-container']._listeners.click({ target: cards[0], _h5Handled: false });
    }
}
check('★ 点卡片后 activeGroupId 有值（否则所有按钮都会"没反应"）',
      !!h5().activeGroupId, String(h5().activeGroupId));

// ② 输入内容后点发送 -> 必须真的发出 SEND_REPLY
FakeWebSocket.sent.length = 0;
els['chat-input'].value = '好的，帮您记录一下';
sb.execCommand('SEND');
const sent = FakeWebSocket.sent.filter(x => x.action === 'SEND_REPLY');
check('★ 输入内容后点发送 -> 发出 SEND_REPLY（带内容与 clientId）',
      sent.length === 1 && sent[0].content === '好的，帮您记录一下' && !!sent[0].clientId,
      JSON.stringify(FakeWebSocket.sent));
check('发送后进入"发送中"状态（等回执）', h5().pendingSend === true);

// ③ 中继回执成功 -> 清空输入框 + 复位
if (sent.length) {
    onmsg({ type: 'AI_STATUS', status: 'ok', echo: sent[0].clientId, groupID: 'T-1',
            message: '已发送给玩家' });
    check('★ 收到 ok 回执 -> 清空输入框并复位发送状态',
          els['chat-input'].value === '' && h5().pendingSend === false,
          'input=' + JSON.stringify(els['chat-input'].value));
}

// ④ 动作按钮逐个点，必须各自发出指令
[['HANGUP', 'ACTION_HANGUP'], ['RESUME', 'ACTION_RESUME'],
 ['F9', 'TRIGGER_F9'], ['AI_CLOSE', 'AI_CLOSE']].forEach(function (c) {
    FakeWebSocket.sent.length = 0;
    sb.execCommand(c[0]);
    const hit = FakeWebSocket.sent.filter(x => x.action === c[0] || x.action === c[1]
        || (x.action === 'EXT_COMMAND' && x.command === c[1]));
    check('按钮「' + c[0] + '」-> 发出指令', hit.length >= 1, JSON.stringify(FakeWebSocket.sent));
});

// ⑤ 「关单」-> 打开分类面板（不抛异常）+ 面板会去要"网页真实分类"
FakeWebSocket.sent.length = 0;
els['chat-input'].value = '感谢理解，先帮您关单';
sb.execCommand('CLOSE');
check('按钮「关单」-> 打开问题分类面板（不再写死分类）', h5().csOpen === true, JSON.stringify(h5()));
check('打开关单面板不抛异常（异常会被红条显示出来）', !h5().last_error, h5().last_error);
FakeWebSocket.sent.length = 0;
sb.csReq('CATEGORY_PROBE', [], '');
check('关单面板 -> 向中继请求"网页真实分类第一层"',
      FakeWebSocket.sent.filter(x => x.action === 'CATEGORY_PROBE').length === 1,
      JSON.stringify(FakeWebSocket.sent));

// ⑥ 没有打开会话时点按钮 -> 必须给出明确原因（不再静默）
const sb2 = makeSandbox();
FakeWebSocket.sent.length = 0;
sb2.execCommand('SEND');
check('★ 没有打开会话时点发送 -> 明确提示原因（不再静默无反应）',
      String(sb2.window.__h5().last_toast || '').indexOf('没有打开的会话') !== -1,
      sb2.window.__h5().last_toast);

// ⑦ 会话"还没同步到"时不许把人踢回列表（旧版会：一进会话就被弹回 -> 之后按钮全没反应）
const sb3 = makeSandbox();
const ws3 = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
const onmsg3 = o => ws3.onmessage({ data: JSON.stringify(o) });
onmsg3({ type: 'FULL_SYNC', data: { extension_online: true,
        companies: { main: { conversations: { 'T-9': CONV } } } } });
const cards3 = els['conv-container']._cards || [];
if (cards3.length) {
    if (typeof cards3[0]._click === 'function') { cards3[0]._click({}); }
    else if (els['conv-container']._listeners && els['conv-container']._listeners.click) {
        els['conv-container']._listeners.click({ target: cards3[0], _h5Handled: false });
    }
}
const gidBefore = sb3.window.__h5().activeGroupId;
onmsg3({ type: 'FULL_SYNC', data: { extension_online: true,
         companies: { main: { conversations: {} } } } });
check('★ 会话"中继还没同步到"时不会把人踢出会话（保留 activeGroupId）',
      !!gidBefore && sb3.window.__h5().activeGroupId === gidBefore,
      gidBefore + ' -> ' + sb3.window.__h5().activeGroupId);

// ⑧ 只有"关单成功"才返回列表（避免被误弹回后按钮全没反应）
onmsg3({ type: 'AI_STATUS', status: 'ok', groupID: 'T-9',
         message: '已回复并关单 · 已点击「回复并关单」' });
check('★ 收到"关单成功"回执 -> 返回列表（唯一会主动返回列表的时机）',
      sb3.window.__h5().activeGroupId === null,
      'activeGroupId=' + String(sb3.window.__h5().activeGroupId));

// ⑨ 点击遥测：不管后面成功与否，"点了什么"必须先送到中继（"点了没反应"时靠这条定位）
FakeWebSocket.sent.length = 0;
sb.execCommand('SEND');
check('★ 每次点击都会先把"点击遥测"发给中继（H5_TAP）',
      FakeWebSocket.sent.some(x => x.action === 'H5_TAP' && x.cmd === 'SEND'),
      JSON.stringify(FakeWebSocket.sent));

// ⑩ 常驻诊断条 + 诊断按钮（不用控制台也能看到/复制状态）
check('顶栏有常驻诊断条（连接/中继/探针/上次点击）', els['h5-diag'].textContent.indexOf('v8.7') !== -1,
      String(els['h5-diag'].textContent));
els['err-text'].innerText = '';
sb.showDiag();
check('「🧪 诊断」把 __h5() 全量状态显示到红条（可截图/复制）',
      String(els['err-text'].innerText).indexOf('"ver"') !== -1,
      String(els['err-text'].innerText).slice(0, 70));

console.log('\n=== 结果: ' + pass + ' 通过 / ' + fail + ' 失败 ===');
process.exit(fail === 0 ? 0 : 1);
