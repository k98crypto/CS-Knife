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
        style: {},                              // ★ V8.3：视图切换会写行内 display，这里必须有
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
        insertAdjacentHTML(pos, v) { if (pos === 'beforeend') this._html += String(v); },
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
                this._cards = out;      // 缓存，便于测试触发点击
            } else if (sel.indexOf('[data-openname]') === 0) {
                // ★ V8.0.1："电脑网页上的会话"里"中继还不认识"的行（data-openname / data-openlast）
                const re = /data-openname="([^"]*)"\s+data-openlast="([^"]*)"/g;
                let mm;
                while ((mm = re.exec(this._html)) !== null) {
                    const row = makeEl('page-conv-row');
                    row.dataset.openname = mm[1];
                    row.dataset.openlast = mm[2];
                    out.push(row);
                }
                this._openRows = out;
            } else if (sel.indexOf('[data-toggle-other]') === 0) {
                // ★ V8.2："其它会话（不在网页列表里）"的展开/收起摘要行
                if (this._html.indexOf('data-toggle-other="1"') !== -1) {
                    const tg = makeEl('other-toggle');
                    tg.dataset.toggleOther = '1';
                    out.push(tg);
                }
                this._otherToggles = out;
            }
            return out;
        }
    };
}

// 顶部两个下拉现在是"自绘按钮 + 底部面板"，假 DOM 里只需要普通元素
const els = {};
const IDS = ['im-pill', 'im-label', 'im-dot', 'afk-pill', 'afk-label', 'afk-dot',
             'sheet', 'sheet-title', 'sheet-body', 'sheet-backdrop',
             'link-chips', 'brand-sub', 'player-card', 'toast',
             'alarm-overlay', 'chat-input', 'conv-container',
             'chat-stream', 'chat-player-name', 'chat-ticket-id', 'chat-view', 'list-view'];
IDS.forEach(i => { els[i] = makeEl(i); });

const documentStub = {
    getElementById: id => els[id] || (els[id] = makeEl(id)),
    querySelectorAll: () => [],
    addEventListener: () => {}
};

const alerts = [];
const timeouts = [];
let oscCount = 0;                             // 统计振荡器数量 = 响了几声（叮咚一次建 2 个）
const FakeWebSocket = class {
    constructor(url) {
        this.url = url;
        this.readyState = 0;                  // CONNECTING
        this.onopen = this.onclose = this.onerror = this.onmessage = null;
        FakeWebSocket.instances.push(this);
    }
    send(data) { FakeWebSocket.sent.push(JSON.parse(data)); }   // 记录下行动作，供断言
    close() { this.readyState = 3; }
};
FakeWebSocket.sent = [];
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
        this.createOscillator = () => {
            oscCount++;
            return { frequency: { setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {}, start() {}, stop() {} };
        };
        this.createGain = () => ({ gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {} });
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

console.log('\n[7.5] V8.0 电脑网页上的会话（唯一一份列表）+ 点一下就能进会话');
onmsg({
    type: 'FULL_SYNC',
    data: {
        afk_mode: false, alarm_status: false,
        conv_list: [
            { name: '玩家甲', last: '甲的问题', time: '5分钟前', active: true },
            { name: '玩家丁' + EVIL_IMG, last: '丁的问题', time: '刚刚', active: false },
            { name: '玩家戊', last: '戊的问题', time: '1小时前', active: false, fresh: true }
        ],
        companies: { main: { conversations: {
            'T-1': { name: '玩家甲', updatedAt: nowMs - 300000, msgs: [{ sender: 'player', text: '甲的问题', ts: nowMs - 300000 }] },
            'T-2': { name: '玩家乙', updatedAt: nowMs - 10000,  msgs: [{ sender: 'agent',  text: '乙的回复', ts: nowMs - 10000 }] }
        } } }
    }
});
const lh = els['conv-container'].innerHTML;
check('主页有一份"电脑网页上的会话"清单（含没聊过的，标「未打开」）',
    lh.indexOf('电脑网页上的会话（3）') !== -1 && lh.indexOf('玩家戊') !== -1 && lh.indexOf('未打开') !== -1);
check('中继认识的会话直接渲染成完整卡片（不再两份列表互相重复）',
    lh.indexOf('data-gid="T-1"') !== -1 && lh.indexOf('甲的问题') !== -1);
check('不在网页列表里的会话默认收起成一行摘要（不再堆一大坨"鸡肋"列表）',
    lh.indexOf('其它会话（不在网页列表里）') !== -1 && lh.indexOf('data-gid="T-2"') === -1);
const otherToggles = els['conv-container']._otherToggles || [];
check('那一行摘要可以点开', otherToggles.length === 1, '数量=' + otherToggles.length);
if (otherToggles.length) {
    otherToggles[0]._click();
    const lh2 = els['conv-container'].innerHTML;
    check('点开后能看到那些旧会话（玩家乙）',
        lh2.indexOf('data-gid="T-2"') !== -1 && lh2.indexOf('玩家乙') !== -1);
    otherToggles[0]._click();                  // 再点一下收起，避免影响后续用例
}
check('当前网页打开的那个会话有「当前」标记', lh.indexOf('🖥 当前') !== -1 && lh.indexOf('page-active') !== -1);
check('网页列表里的新内容有未读点（fresh）', lh.indexOf('unread-dot') !== -1);
check('顶栏计数跟着网页列表走', els['list-count'].innerText.indexOf('电脑网页') !== -1,
    els['list-count'].innerText);
check('名字已转义（未打开的会话行也用 data-* 传值）',
    lh.indexOf('data-openname=') !== -1 && lh.indexOf('<img') === -1);
const openRows = els['conv-container']._openRows || [];
check('只有"中继还不认识"的会话才需要点一下去打开', openRows.length === 2, '数量=' + openRows.length);
wsInst.readyState = 1;                     // 恢复"已连接"，否则点击只会提示断线
FakeWebSocket.sent.length = 0;
const rowXin = openRows.filter(r => r.dataset.openname.indexOf('玩家戊') !== -1)[0];
rowXin && rowXin._click();
const openSent = FakeWebSocket.sent.filter(m => m.action === 'OPEN_CONV');
check('点「未打开」的会话 -> 请电脑切过去（OPEN_CONV）',
    openSent.length === 1 && openSent[0].name.indexOf('玩家戊') !== -1, JSON.stringify(openSent));
// ★ 关键修复：切过去之后，探针上报里就有这条会话了 —— 手机必须**自动把聊天页打开**（客服原话："点不进卡片"）
check('点击时不会假装已打开（如实提示"正在让电脑打开…"）',
    !els['chat-view'].classList.contains('active')
    && String(els['toast'].innerText).indexOf('正在让电脑打开') !== -1,
    String(els['toast'].innerText));
onmsg({
    type: 'FULL_SYNC',
    data: {
        afk_mode: false, alarm_status: false,
        conv_list: [
            { name: '玩家甲', last: '甲的问题', time: '5分钟前', active: false },
            { name: '玩家戊', last: '戊的问题', time: '刚刚', active: true }
        ],
        companies: { main: { conversations: {
            'T-1': { name: '玩家甲', updatedAt: nowMs - 300000, msgs: [{ sender: 'player', text: '甲的问题', ts: nowMs - 300000 }] },
            'P-9': { name: '玩家戊', updatedAt: nowMs, msgs: [{ sender: 'player', text: '戊的问题', ts: nowMs }] }
        } } }
    }
});
check('电脑切过去并上报后 -> 手机自动进聊天页（不用再点第二次）',
    els['chat-view'].classList.contains('active')
    && els['chat-player-name'].innerText === '玩家戊',
    els['chat-player-name'].innerText + ' / active=' + els['chat-view'].classList.contains('active'));
check('打开后返回列表', (function () { sandbox.popChat(); return els['list-view'].classList.contains('active'); })());

// ★ V8.2：点卡片"进不去会话"的加固（客服反馈了两次）
const pushBody = h5code.slice(h5code.indexOf('function pushChat'), h5code.indexOf('function popChat'));
check('点卡片先切视图再渲染（渲染里出错也不会"点了没反应"）',
    pushBody.indexOf('showChat();') !== -1
    && pushBody.indexOf('showChat();') < pushBody.indexOf('renderPlayerCard(conv);'));
check('渲染步骤各自 try/catch（单条数据异常不连累整个页面）',
    pushBody.indexOf('try { renderChatStream(conv); } catch') !== -1
    || pushBody.indexOf('renderChatStream(conv); } catch') !== -1);
check('列表点击有兜底委托（某次渲染没绑上也能进会话，且不会重复处理）',
    h5code.indexOf('bindListFallback') !== -1 && h5code.indexOf('_h5Handled') !== -1);
check('兜底委托同时挂在容器与 document 上（容错到底）',
    h5code.indexOf("document.addEventListener('click', onTap)") !== -1);
check('视图切换 class + 行内 display 双保险（CSS 出问题也切得过去）',
    h5code.indexOf("cv.style.display = 'flex'") !== -1
    && h5code.indexOf("lv.style.display = 'none'") !== -1);
check('每次点击的提示都带页面版本（方便确认"手机到底刷没刷上"）',
    h5code.indexOf("（页面 v' + H5_VER + '）") !== -1);
let threwPush = null;
try { sandbox.pushChat('不存在的会话'); } catch (e) { threwPush = e.message; }
check('点一条"数据还没到"的会话不抛异常，并给出提示',
    threwPush === null && String(els['toast'].innerText).indexOf('数据还没到') !== -1,
    threwPush || String(els['toast'].innerText));

console.log('\n[8] 布局与滚动（静态检查）');
check('已移除 transform 滑动（iOS 文字发虚的元凶）', py.indexOf('transform: translateX') === -1);
check('页面用 fixed 锁定视口（内容不再滑到浏览器工具栏下方）', /#app\s*\{[^}]*position:\s*fixed/.test(py));
check('会话列表是独立滚动容器', /\.conv-list\s*\{[^}]*overflow-y:\s*auto/.test(py));
check('聊天记录是独立滚动容器', /\.chat-stream\s*\{[^}]*overflow-y:\s*auto/.test(py));
check('页面切换改用 display 而非 transform', py.indexOf('.view.active { display: flex; }') !== -1);
check('弹性高度用 min-height:0 收敛（保证内部能滚动）', (py.match(/min-height:\s*0/g) || []).length >= 3);

console.log('\n[9] 顶部两个下拉（V7.4：自绘按钮 + 底部面板，不再用原生 select）');
check('顶栏是自绘胶囊按钮 id="im-pill"', py.indexOf('id="im-pill"') !== -1);
check('顶栏是自绘胶囊按钮 id="afk-pill"', py.indexOf('id="afk-pill"') !== -1);
check('点击打开底部选择面板（openSheet）',
    py.indexOf("openSheet('im')") !== -1 && py.indexOf("openSheet('mode')") !== -1);
check('没有原生 <select>（避免"框框"与卡顿）', py.indexOf('<select') === -1);
check('旧的"IM 状态按钮 + 弹出菜单"已移除',
    py.indexOf('id="status-menu"') === -1 && py.indexOf('toggleStatusMenu') === -1);
check('品牌名过长会省略号截断（左上角不再越界）',
    /\.brand-name\{[^}]*overflow:hidden/.test(py) && /\.brand-name\{[^}]*text-overflow:ellipsis/.test(py));

console.log('\n[10] 手机端启动不编造状态（V7.4：先从电脑网页取）');
check('状态未核实前显示「正在获取…」', h5code.indexOf('正在获取…') !== -1);
check('电脑端未连接时显示「电脑未连接」', h5code.indexOf('电脑未连接') !== -1);
check('依赖服务端 im_status_known 标志（未核实不亮绿灯）', h5code.indexOf('im_status_known') !== -1);
check('会主动向电脑端索要真实状态（REQUEST_IM_STATUS）',
    h5code.indexOf("action: 'REQUEST_IM_STATUS'") !== -1);

// 行为：先来一份"未核实"的快照 -> 胶囊必须是"正在获取"，不能显示在线
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true,
    im_status: 1, im_status_known: false,
    companies: { main: { conversations: {} } }
} });
check('im_status_known=false -> 胶囊显示「正在获取…」而不是在线',
    els['im-label'].innerText === '正在获取…', els['im-label'].innerText);

// 行为：探针核实后（离线）-> 胶囊显示离线，且不会再被顶成在线
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true,
    im_status: 3, im_status_known: true, im_status_manual: true,
    companies: { main: { conversations: {} } }
} });
check('核实为离线 -> 胶囊显示「IM 离线」', els['im-label'].innerText === 'IM 离线', els['im-label'].innerText);

// 行为：点胶囊打开面板 -> 三个状态 + 当前项打勾
sandbox.openSheet('im');
check('IM 面板列出在线/忙碌/离线', ['IM 在线', 'IM 忙碌', 'IM 离线'].every(s => els['sheet-body'].innerHTML.indexOf(s) !== -1),
    els['sheet-body'].innerHTML.slice(0, 80));
check('当前状态被打勾（离线）',
    /class="sheet-item cur"[^>]*data-set="im:3"/.test(els['sheet-body'].innerHTML), els['sheet-body'].innerHTML.slice(0, 120));
check('面板已显示', els['sheet'].classList.contains('show'));
sandbox.closeSheet();
check('取消后关闭面板', !els['sheet'].classList.contains('show'));

console.log('\n[10.2] V7.7 状态不一致时如实显示 + 逃生舱按钮');
check('依赖服务端的「网页实际状态」字段（不做假的"已同步"）',
    h5code.indexOf('im_status_page') !== -1);
check('不一致时胶囊标出「网页仍 X」', h5code.indexOf('（网页仍') !== -1);
check('新增 retryIMStatus / resetIMStatus 两个函数',
    h5code.indexOf('function retryIMStatus') !== -1 && h5code.indexOf('function resetIMStatus') !== -1);
check('「以网页为准」走 RESET_IM_STATE 动作', py.indexOf("action: 'RESET_IM_STATE'") !== -1);
check('排障入口：请电脑端读一下状态选项（DUMP_STATUS）', py.indexOf("action: 'DUMP_STATUS'") !== -1);

// 行为：记录=忙碌 但网页实际=在线 -> 胶囊如实标出，不假装一致
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true,
    im_status: 2, im_status_known: true, im_status_manual: true, im_status_page: 1,
    companies: { main: { conversations: {} } }
} });
check('记录=忙碌、网页=在线 -> 胶囊显示「IM 忙碌（网页仍在线）」',
    els['im-label'].innerText.indexOf('网页仍在线') !== -1, els['im-label'].innerText);
sandbox.openSheet('im');
check('冲突时面板出现「重试同步」与「以网页为准」',
    els['sheet-body'].innerHTML.indexOf('im:retry') !== -1
    && els['sheet-body'].innerHTML.indexOf('im:page') !== -1,
    els['sheet-body'].innerHTML.slice(0, 160));
check('面板里常驻「读一下网页的状态选项」（排障）',
    els['sheet-body'].innerHTML.indexOf('im:dump') !== -1);
sandbox.closeSheet();

// 行为：探针掉线 -> 胶囊显示"电脑未连接"，不亮绿灯
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: false,
    im_status: 3, im_status_known: false,
    companies: { main: { conversations: {} } }
} });
check('电脑端掉线 -> 胶囊显示「电脑未连接」', els['im-label'].innerText === '电脑未连接', els['im-label'].innerText);

console.log('\n[10.5] 回复模式三档（V7.4：AI 自动起草可以关掉了）');
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, auto_draft: true, reply_mode: 'semi', alarm_status: false, extension_online: true,
    im_status: 1, im_status_known: true,
    companies: { main: { conversations: {} } }
} });
check('默认显示「半自动」', els['afk-label'].innerText === '半自动', els['afk-label'].innerText);
sandbox.openSheet('mode');
check('模式面板列出 手动/半自动/AFK 三档',
    ['手动', '半自动', 'AFK 全自动'].every(s => els['sheet-body'].innerHTML.indexOf(s) !== -1),
    els['sheet-body'].innerHTML.slice(0, 100));
check('半自动是当前项并打勾', /data-set="mode:semi"[^>]*|\s*<span class="tick">/.test(els['sheet-body'].innerHTML)
    && els['sheet-body'].innerHTML.indexOf('mode:semi') !== -1, els['sheet-body'].innerHTML.slice(0, 140));
check('手动档明确写着"AI 不自动起草"（能关掉）',
    els['sheet-body'].innerHTML.indexOf('AI 不自动起草') !== -1, els['sheet-body'].innerHTML.slice(0, 160));
wsInst.readyState = 1;
FakeWebSocket.sent.length = 0;
sandbox.setReplyMode('manual');
let modeMsgs = FakeWebSocket.sent.filter(m => m.action === 'SET_MODE');
check('选「手动」-> 下发 SET_MODE mode=manual',
    modeMsgs.length === 1 && modeMsgs[0].mode === 'manual', JSON.stringify(modeMsgs));
check('选完后胶囊立刻变「手动」（关掉自动起草）', els['afk-label'].innerText === '手动', els['afk-label'].innerText);
FakeWebSocket.sent.length = 0;
sandbox.setReplyMode('afk');
check('选「AFK 全自动」-> 下发 SET_MODE mode=afk',
    FakeWebSocket.sent.some(m => m.action === 'SET_MODE' && m.mode === 'afk'), JSON.stringify(FakeWebSocket.sent));
check('胶囊变「AFK 全自动」', els['afk-label'].innerText === 'AFK 全自动', els['afk-label'].innerText);

console.log('\n[11] 顶栏连接状态条（中继 / 探针 / 会话数）');
check('渲染连接状态条', (els['link-chips'].innerHTML || '').indexOf('中继') !== -1, els['link-chips'].innerHTML.slice(0, 60));
check('状态条里带探针在线状态', (els['link-chips'].innerHTML || '').indexOf('电脑探针') !== -1);

console.log('\n[13] 卡片信息更全（V7.4：玩家名不再是"玩家信息"）');
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true, im_status: 1, im_status_known: true,
    companies: { main: { conversations: {
        'T-INFO': { name: '张三', updatedAt: nowMs, playerInfo: '玩家信息 | 昵称：张三 | UID:10001 | 区服:S12',
                    msgs: [{ sender: 'player', text: '抽卡没到账', ts: nowMs }] }
    } } }
} });
const infoHtml = els['conv-container'].innerHTML;
check('卡片显示玩家名（不是"玩家信息"）', infoHtml.indexOf('张三') !== -1, infoHtml.slice(0, 80));
check('卡片带 UID 副信息，便于分清是谁',
    infoHtml.indexOf('UID 10001') !== -1, infoHtml.slice(0, 200));
const infoCards = els['conv-container']._cards || [];
if (infoCards.length) infoCards[0]._click();
check('聊天页顶部有玩家信息卡', (els['player-card'].innerText || '').indexOf('👤') !== -1, els['player-card'].innerText);
check('长信息默认折叠（露 2 行 + "展开全部"提示）',
    (els['player-card'].innerText || '').indexOf('展开全部') !== -1
    && (els['player-card'].innerText || '').indexOf('UID:10001') === -1, els['player-card'].innerText);
sandbox.togglePlayerCard();
check('点一下展开全部（能看到 UID/区服）',
    (els['player-card'].innerText || '').indexOf('UID:10001') !== -1
    && (els['player-card'].innerText || '').indexOf('区服') !== -1, els['player-card'].innerText);

console.log('\n[12] 手机端挂起 / 恢复（V7.4：补上"恢复" + 结果提示）');
check('操作栏同时有「挂起」与「恢复」按钮',
    py.indexOf("execCommand('HANGUP')") !== -1 && py.indexOf("execCommand('RESUME')") !== -1);
fullSync({ 'T-R1': { name: '玩家庚', updatedAt: nowMs, msgs: [{ sender: 'player', text: '帮我看看', ts: nowMs }] } });
const cardsR = els['conv-container']._cards || [];
check('准备好一个可操作的会话', cardsR.length === 1, '数量=' + cardsR.length);
if (cardsR.length) cardsR[0]._click();
wsInst.readyState = 1;                     // OPEN（前面的断线用例把它设成了 CLOSED）
FakeWebSocket.sent.length = 0;
sandbox.execCommand('HANGUP');
let acts = FakeWebSocket.sent.filter(m => m.action === 'EXT_COMMAND');
check('点「挂起」-> 下发 ACTION_HANGUP 给探针',
    acts.length === 1 && acts[0].command === 'ACTION_HANGUP', JSON.stringify(acts));
check('挂起后立刻提示"已请求…"（不再静默）',
    (els['toast'].innerText || '').indexOf('挂起') !== -1, els['toast'].innerText);
FakeWebSocket.sent.length = 0;
sandbox.execCommand('RESUME');
acts = FakeWebSocket.sent.filter(m => m.action === 'EXT_COMMAND');
check('点「恢复」-> 下发 ACTION_RESUME 给探针',
    acts.length === 1 && acts[0].command === 'ACTION_RESUME', JSON.stringify(acts));
check('恢复后立刻提示"已请求…"',
    (els['toast'].innerText || '').indexOf('恢复') !== -1, els['toast'].innerText);
onmsg({ type: 'AI_STATUS', status: 'error',
        message: '挂起失败 · 未找到「挂起」按钮，页面上的按钮：转交他人/结束会话' });
check('探针回报失败原因 -> 手机端弹出具体原因（含页面真实按钮名）',
    (els['toast'].innerText || '').indexOf('未找到') !== -1, els['toast'].innerText);
check('失败原因还常驻显示（toast 一闪而过看不清/复制不了）',
    els['err-bar'] && els['err-bar'].classList.contains('show')
    && (els['err-text'].innerText || '').indexOf('未找到') !== -1,
    (els['err-text'] || {}).innerText);
onmsg({ type: 'AI_STATUS', status: 'ok', message: '挂起成功 · 已点击「挂起」' });
check('成功后常驻条自动收起', !els['err-bar'].classList.contains('show'));

console.log('\n[12.1] V7.5 手机输入框：自动长高 + 有上限 + 超出滚动');
check('输入框是多行 textarea（长草稿能换行看全）',
    py.indexOf('<textarea class="chat-text-input" id="chat-input"') !== -1);
check('样式里有高度上限与滚动',
    py.indexOf('max-height:38vh') !== -1 && py.indexOf('overflow-y:hidden') !== -1
    && py.indexOf('.err-bar{position:fixed') !== -1);
check('有自动长高实现（按内容 + 上限 + 超出可滚）',
    h5code.indexOf('function autoGrowInput') !== -1 && h5code.indexOf('inputMaxHeight') !== -1
    && h5code.indexOf("el.style.overflowY = (full > max) ? 'auto' : 'hidden'") !== -1);
check('收到草稿（FILL_DRAFT）时会自动长高',
    h5code.indexOf('payload.type === \'FILL_DRAFT\'') !== -1
    && h5code.indexOf('autoGrowInput();                       // 草稿可能很长') !== -1);
check('输入/聚焦时绑定自动长高', h5code.indexOf('bindInputGrow') !== -1);

console.log('\n[12.2] V7.5 关单中标记（确认前不移除会话）');
check('会话卡片有关单中标记与文案',
    h5code.indexOf('closingTag') !== -1 && h5code.indexOf('关单中…（等待页面确认）') !== -1);
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true, im_status: 1, im_status_known: true,
    human_alerts: [],
    companies: { main: { conversations: {
        'T-CLOSING': { name: '关单中玩家', updatedAt: nowMs, closing: true,
                       msgs: [{ sender: 'player', text: '帮我关单', ts: nowMs }] }
    } } }
} });
const closeHtml = els['conv-container'].innerHTML;
check('关单中的会话在列表里显示 ⏳ 与"关单中…"',
    closeHtml.indexOf('⏳') !== -1 && closeHtml.indexOf('关单中…') !== -1, closeHtml.slice(0, 140));

console.log('\n[14] 需要人工介入（V7.4：表格没答案 -> 横幅 + 专属提示音 + 置顶）');
check('有"需要人工"横幅与"知道了"按钮',
    py.indexOf('id="human-banner"') !== -1 && py.indexOf('ackHumanAlert()') !== -1);
check('专属提示音与"新消息叮咚""掉线警报"分开实现（playHumanAlert）',
    h5code.indexOf('function playHumanAlert') !== -1 && h5code.indexOf('function playDingDong') === -1
    && h5code.indexOf('playMobileSiren') !== -1);
check('收到 HUMAN_ALERT 事件会立刻提示', h5code.indexOf("payload.type === 'HUMAN_ALERT'") !== -1);
check('置顶会话排在列表最前', h5code.indexOf('convs[a].pinned') !== -1 || h5code.indexOf('&& convs[a].pinned') !== -1);
check('确认告警走 ACK_ALERT', h5code.indexOf("action: 'ACK_ALERT'") !== -1);

// 行为：FULL_SYNC 带告警 -> 横幅出现，内容含问题总结
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true, im_status: 1, im_status_known: true,
    human_alerts: [{ id: 'A-1', ts: nowMs, groupID: 'T-AL', name: '玩家丁', summary: '充值未到账，要求补发',
                     reason: '表格里没有对应答案，已发送安抚话术' }],
    companies: { main: { conversations: {
        'T-PIN': { name: '玩家戊', updatedAt: nowMs - 999999, msgs: [{ sender: 'player', text: '排序测试', ts: nowMs - 999999 }] },
        'T-AL': { name: '玩家丁', updatedAt: nowMs, pinned: true, alert: true,
                  msgs: [{ sender: 'player', text: '充值没到账', ts: nowMs }] }
    } } }
} });
check('告警横幅已显示', els['human-banner'].classList.contains('show'));
check('横幅标题带玩家名', (els['hb-title'].innerText || '').indexOf('玩家丁') !== -1, els['hb-title'].innerText);
check('横幅带问题总结', (els['hb-text'].innerText || '').indexOf('充值未到账') !== -1, els['hb-text'].innerText);
const pinHtml = els['conv-container'].innerHTML;
check('置顶会话排在最前（尽管时间更早）',
    pinHtml.indexOf('T-AL') < pinHtml.indexOf('T-PIN'), pinHtml.slice(0, 120));
check('置顶/需人工会话有 📌🙋 标记',
    pinHtml.indexOf('pin-badge') !== -1 && pinHtml.indexOf('📌') !== -1 && pinHtml.indexOf('🙋') !== -1);
wsInst.readyState = 1;
FakeWebSocket.sent.length = 0;
sandbox.ackHumanAlert();
check('点"知道了" -> 下发 ACK_ALERT 并收起横幅',
    FakeWebSocket.sent.some(m => m.action === 'ACK_ALERT') && !els['human-banner'].classList.contains('show'),
    JSON.stringify(FakeWebSocket.sent));

console.log('\n[15] V7.5 新消息即时通知（手机：叮咚 + 提示 + 震动）');
check('H5 有新消息"叮咚"音效（两个正弦音）',
    h5code.indexOf('function playNewMsgSound') !== -1 && h5code.indexOf("osc.type = 'sine'") !== -1);
check('三种声音分开实现（新消息/掉线警笛/需要人工）',
    h5code.indexOf('function playNewMsgSound') !== -1
    && h5code.indexOf('function playMobileSiren') !== -1
    && h5code.indexOf('function playHumanAlert') !== -1);
check('处理 NEW_MESSAGE 事件（含同一条只响一次的判重）',
    h5code.indexOf("payload.type === 'NEW_MESSAGE'") !== -1
    && h5code.indexOf('notifiedMsgTs') !== -1);
check('失败原因常驻条与"已连接"提示不冲突（err-bar 仍在）', py.indexOf('id="err-bar"') !== -1);

let oscBefore = oscCount;
onmsg({ type: 'NEW_MESSAGE', groupID: 'T-M1', name: '玩家辛', preview: '在吗？充值没到账', mode: 'semi', ts: 111 });
check('收到新消息 -> 响一次「叮咚」（两个音）', oscCount - oscBefore === 2, '振荡器 +' + (oscCount - oscBefore));
check('收到新消息 -> 顶部提示带玩家名与预览',
    (els['toast'].innerText || '').indexOf('玩家辛') !== -1
    && (els['toast'].innerText || '').indexOf('充值没到账') !== -1, els['toast'].innerText);

oscBefore = oscCount;
onmsg({ type: 'NEW_MESSAGE', groupID: 'T-M1', name: '玩家辛', preview: '在吗？充值没到账', mode: 'semi', ts: 111 });
check('同一条消息重复推送 -> 不再重复响', oscCount === oscBefore, '振荡器 +' + (oscCount - oscBefore));

oscBefore = oscCount;
onmsg({ type: 'NEW_MESSAGE', groupID: 'T-M1', name: '玩家辛', preview: '还没人理我', mode: 'manual', ts: 222 });
check('又一条新消息 -> 再响一次', oscCount - oscBefore === 2, '振荡器 +' + (oscCount - oscBefore));
check('手动模式下提示"AI 未自动起草"',
    (els['toast'].innerText || '').indexOf('未自动起草') !== -1, els['toast'].innerText);

console.log('\n[16] 统一提示（不区分是否正在看该会话）');
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true, im_status: 1, im_status_known: true,
    human_alerts: [],
    companies: { main: { conversations: {
        'T-VIEW': { name: '正在看的人', updatedAt: nowMs, msgs: [{ sender: 'player', text: '我在看这个会话', ts: nowMs }] },
        'T-OTHER': { name: '别的玩家', updatedAt: nowMs - 5000, msgs: [{ sender: 'player', text: '别的会话消息', ts: nowMs - 5000 }] }
    } } }
} });
const cards16 = (els['conv-container']._cards || []);
const cardView = cards16.filter(c => c.dataset.gid === 'T-VIEW')[0];
check('会话卡片可点击进入聊天页（事件委托绑定）', !!(cardView && cardView._click));
if (cardView && cardView._click) cardView._click();          // -> activeGroupId = T-VIEW
let osc16 = oscCount;
onmsg({ type: 'NEW_MESSAGE', groupID: 'T-VIEW', name: '正在看的人', preview: '又发了一条', mode: 'semi', ts: 3333 });
check('正在看的会话来新消息 -> 照常响铃（客服要求：统一都要提示）',
    oscCount - osc16 === 2, '振荡器 +' + (oscCount - osc16));
check('正在看的会话也弹提示（玩家名+预览）',
    (els['toast'].innerText || '').indexOf('又发了一条') !== -1, els['toast'].innerText);

osc16 = oscCount;
onmsg({ type: 'NEW_MESSAGE', groupID: 'T-OTHER', name: '别的玩家', preview: '别的会话新消息', mode: 'semi', ts: 4444 });
check('别的会话来新消息 -> 也响铃', oscCount - osc16 === 2, '振荡器 +' + (oscCount - osc16));

console.log('\n[17] 三个 AI 动作的独立开关（手机侧）');
check('H5 读取中继下发的 features（ai_close / auto_draft / f10_polish）',
    h5code.indexOf('function featureOn') !== -1 && h5code.indexOf('globalState.features') !== -1);
check('有"已关闭"提示条（feature-hint）', py.indexOf('id="feature-hint"') !== -1
    && h5code.indexOf('function renderFeatureButtons') !== -1);
check('「AI 回复并关单」按钮被关闭时点不动（走 AI_CLOSE 前先检查开关）',
    h5code.indexOf("if (!featureOn('ai_close'))") !== -1);
onmsg({ type: 'FULL_SYNC', data: {
    afk_mode: false, alarm_status: false, extension_online: true, im_status: 1, im_status_known: true,
    human_alerts: [], features: { ai_close: false, auto_draft: false, f10_polish: true },
    companies: { main: { conversations: {
        'T-M2': { name: '开关测试', updatedAt: nowMs, msgs: [{ sender: 'player', text: '你好', ts: nowMs }] }
    } } }
} });
check('已关闭的功能在手机端标出来（⛔ 已关闭：…）',
    (els['feature-hint'].innerText || '').indexOf('已关闭') !== -1
    && (els['feature-hint'].innerText || '').indexOf('AI 回复并关单') !== -1,
    els['feature-hint'].innerText);
// 进入该会话（否则 activeGroupId 为空，execCommand 直接返回，测不到开关拦截）
const cards17 = (els['conv-container']._cards || []);
const card17 = cards17.filter(c => c.dataset.gid === 'T-M2')[0];
if (card17 && card17._click) card17._click();
wsInst.readyState = 1;
FakeWebSocket.sent.length = 0;
els['err-text'].innerText = '';
sandbox.execCommand('AI_CLOSE');
const sentHasAiClose = FakeWebSocket.sent.some(m => m.action === 'AI_CLOSE');
const errHasClosed = (els['err-text'].innerText || '').indexOf('关闭') !== -1;
check('关闭后点「AI 回复并关单」不会下发 AI_CLOSE（本地就拦住并提示）',
    !sentHasAiClose && errHasClosed,
    'sentHasAiClose=' + sentHasAiClose + ' errHasClosed=' + errHasClosed
    + ' | sent=' + JSON.stringify(FakeWebSocket.sent)
    + ' | err=' + (els['err-text'].innerText || ''));

console.log('\n=== 结果: ' + pass + ' 通过 / ' + fail + ' 失败 ===');
process.exit(fail === 0 ? 0 : 1);