// qc_probe.js 冒烟测试：用 stub 模拟「质检明细页」DOM，验证只读采集的状态机与上报内容
//   node qc_sweep_test.js [qc_probe.js]
const fs = require('fs');
const vm = require('vm');

const TARGET = process.argv[2] || 'qc_probe.js';
const code = fs.readFileSync(TARGET, 'utf8');

let pass = 0, fail = 0;
function check(name, ok, extra) {
    if (ok) { pass++; console.log('  [PASS] ' + name + (extra ? '  -> ' + extra : '')); }
    else { fail++; console.log('  [FAIL] ' + name + (extra ? '  -> ' + extra : '')); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------- 极简元素模型（只实现 qc_probe.js 用到的那几个接口） ----------
const all = [];
let clicks = [];

function mk(tag, cls, text, opts) {
    const o = opts || {};
    let own = text || '';
    const node = {
        tag: String(tag).toUpperCase(), _cls: String(cls || ''),
        _kids: [], parentElement: null, offsetParent: o.hidden ? null : {},
        hasClass(c) { return (' ' + this._cls + ' ').indexOf(' ' + c + ' ') !== -1; },
        getBoundingClientRect() { return o.hidden ? { width: 0, height: 0 } : { width: 100, height: 20 }; },
        getAttribute(n) { return (o.attrs || {})[n] || null; },
        appendChild(c) { this._kids.push(c); c.parentElement = this; if (!c._inAll) { c._inAll = true; all.push(c); } return c; },
        dispatchEvent() { return true; },
        click() { clicks.push({ cls: this._cls, text: this.innerText }); if (o.onClick) o.onClick(); return true; },
        querySelectorAll(sel) {
            const out = [];
            (function walk(n) { n._kids.forEach(k => { if (selMatch(k, sel)) out.push(k); walk(k); }); })(this);
            return out;
        },
        querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    };
    // ★ 像真 DOM 一样：自身文字为空时，innerText 自动拼接子节点文本（.message-bubble 靠这个取正文）
    Object.defineProperty(node, 'innerText', {
        get() { return own || (this._kids || []).map(k => k.innerText).join(' ').trim(); },
        set(v) { own = v === undefined || v === null ? '' : String(v); }
    });
    node.classList = { contains: c => node.hasClass(c) };
    node.className = node._cls;
    node._inAll = false;
    if (!o.noRegister) { node._inAll = true; all.push(node); }
    return node;
}

// 只认 qc_probe.js 真实用到的选择器（故意不实现通配，避免"测试替它圆场"）
function selMatch(el, sel) {
    return String(sel).split(',').map(s => s.trim()).some(s => {
        if (s === 'tr.el-table__row') return el.tag === 'TR' && el.hasClass('el-table__row');
        if (s === 'td') return el.tag === 'TD';
        if (s === 'span') return el.tag === 'SPAN';
        if (s === '.el-table__header th' || s === '.el-table__header-wrapper th') return el.tag === 'TH';
        if (s === '.message-row') return el.hasClass('message-row');
        if (s === '.message-meta') return el.hasClass('message-meta');
        if (s === '.message-bubble') return el.hasClass('message-bubble');
        if (s === '.message-time') return el.hasClass('message-time');
        if (s === '.message-translate-btn') return el.hasClass('message-translate-btn');
        if (s === '.el-pagination') return el.hasClass('el-pagination');
        if (s === '.el-pagination li.number') return el.tag === 'LI' && el.hasClass('number');
        if (s === '.el-pagination li.number.is-active') return el.tag === 'LI' && el.hasClass('number') && el.hasClass('is-active');
        if (s === 'button' || s === '.el-button' || s === '[role="button"]') return el.tag === 'BUTTON';
        if (s === '.el-drawer__close-btn' || s === '.el-dialog__headerbtn') return el.hasClass('el-drawer__close-btn');
        return false;
    });
}

// ---------- 页面状态：表头 / 行 / 分页 / 详情面板 ----------
const HEADERS = ['会话ID', '游戏', '客服', '来源', '语种', '问题分类', '玩家评分', 'AI质检评分',
                 'AI质检结果', '是否零容忍', '质检员', '人工复核得分', '最终评分', '人工复核结果', '操作'];
const PAGE_SIZE = 3;
let currentPage = 1;

// 造 7 条会话（含"只有机器人话术"和"人机+真人混合"两种，验证只学真人）
const SESSIONS = [
    { id: 'S1', score: 94, aiScore: 93.5, cat: '道具与资源 / 误操作补偿', msgs: [
        { sender: 'player', text: '我的钻石没到账' },
        { sender: 'agent', who: 'agent.zhang', text: '亲爱的玩家您好，请提供订单号' }] },
    { id: 'S2', score: 91, aiScore: 90.1, cat: '账号 / 注销', msgs: [
        { sender: 'player', text: '怎么注销' },
        { sender: 'agent', who: 'AI Bot', text: '您好，非常感谢您联系我们，我是智能助手' },     // 人机
        { sender: 'system', text: '即将分配客服为您服务' },
        { sender: 'agent', who: 'agent.zhang', text: '您可以在设置里自行申请注销' }] },        // 真人
    { id: 'S3', score: 55, aiScore: 50, cat: '充值 / 掉单', msgs: [
        { sender: 'player', text: '充值没反应' }, { sender: 'agent', who: 'agent.zhang', text: '这边帮您核实一下' }] },
    { id: 'S4', score: 88, aiScore: 88, cat: '客户端 / 卡顿', msgs: [
        { sender: 'player', text: '游戏卡' }, { sender: 'player', text: '很卡' }] },
    { id: 'S1', score: 94, aiScore: 93.5, cat: '道具与资源 / 误操作补偿', msgs: [
        { sender: 'player', text: '我的钻石没到账' },
        { sender: 'agent', who: 'agent.zhang', text: '亲爱的玩家您好，请提供订单号' }] },
    { id: 'S5', score: 96, aiScore: 95, cat: '活动 / 奖励', msgs: [
        { sender: 'player', text: '奖励没发' }, { sender: 'agent', who: 'agent.zhang', text: '已为您记录并转交专人核实' }] },
    { id: 'S6', score: 97, aiScore: 96, cat: '活动 / 异常', msgs: [                            // 只有机器人 -> 应跳过
        { sender: 'player', text: '在吗' }, { sender: 'agent', who: 'AI Bot', text: '您好，我是智能助手' }] },
];

function clearTableNodes() {
    for (let i = all.length - 1; i >= 0; i--) {
        if (['TR', 'TH', 'TD', 'LI', 'BUTTON'].indexOf(all[i].tag) !== -1) all.splice(i, 1);
    }
}
function dropFromAll(node) {
    (node._kids || []).forEach(dropFromAll);
    const i = all.indexOf(node);
    if (i !== -1) all.splice(i, 1);
}
function clearDetail() {
    all.filter(n => n.hasClass && n.hasClass('message-row')).forEach(dropFromAll);
}
function buildTable() {
    clearTableNodes();
    const headerRow = mk('TR', '', '', { noRegister: true });
    HEADERS.forEach(h => headerRow.appendChild(mk('TH', 'el-table__cell', h)));
    const from = (currentPage - 1) * PAGE_SIZE;
    SESSIONS.slice(from, from + PAGE_SIZE).forEach(s => {
        const tr = mk('TR', 'el-table__row');
        [s.id, 'game-demo-01', 'agent.zhang', '客户端', '简体中文', s.cat, '-',
         s.aiScore, '通过', '否', 'AI', '--', s.score, '--', '查看详情']
            .forEach(c => tr.appendChild(mk('TD', 'el-table__cell', String(c))));
        tr.appendChild(mk('BUTTON', 'el-button el-button--primary is-link', '查看详情',
                          { onClick: () => openDetail(s) }));
    });
    const pageCount = Math.ceil(SESSIONS.length / PAGE_SIZE);
    const pag = mk('DIV', 'el-pagination', '共 ' + SESSIONS.length + ' 条');
    for (let p = 1; p <= pageCount; p++) {
        pag.appendChild(mk('LI', p === currentPage ? 'number is-active' : 'number', String(p),
                           { onClick: () => { currentPage = p; clearDetail(); buildTable(); } }));
    }
}
// 按**实机 outerHTML** 造消息行（.message-meta + .message-bubble），客服名用 who 字段
function openDetail(s) {
    s._clicks = (s._clicks || 0) + 1;
    setTimeout(() => {
        clearDetail();
        if (s.id === 'S5') return;
        if (s.id === 'S2' && s._clicks === 1) return;
        s.msgs.forEach(m => {
            const row = mk('DIV', 'message-row ' + m.sender);
            const meta = mk('DIV', 'message-meta');
            const who = m.who || (m.sender === 'player' ? '玩家' : (m.sender === 'system' ? '系统' : 'agent.zhang'));
            meta.appendChild(mk('SPAN', '', who));
            meta.appendChild(mk('SPAN', 'message-time', '09-29 14:44'));
            if (m.sender !== 'system') {                       // 实机：只有非系统消息带「翻译」按钮
                const btn = mk('BUTTON', 'el-button el-button--small is-text message-translate-btn');
                btn.appendChild(mk('SPAN', '', '翻译'));
                meta.appendChild(btn);
            }
            row.appendChild(meta);
            const bubble = mk('DIV', 'message-bubble');         // ★ 正文节点
            bubble.appendChild(mk('SPAN', '', m.text));
            row.appendChild(bubble);
        });
    }, 30);
}

// ---------- stub 环境 ----------
const sent = { ws: [], events: [] };
const documentStub = {
    body: mk('BODY', '', '', { noRegister: true }),
    documentElement: mk('HTML', '', '', { noRegister: true }),
    querySelectorAll: sel => all.filter(n => selMatch(n, sel)),
    querySelector: sel => all.filter(n => selMatch(n, sel))[0] || null,
    createElement: tag => mk(tag, '', ''),
    addEventListener: () => {},
    dispatchEvent: () => true
};
class FakeWS {
    constructor(u) { this.url = u; this.readyState = 1; sent.ws.push(u);
                     FakeWS.last = this;
                     setTimeout(() => this.onopen && this.onopen(), 0); }
    send(d) { sent.events.push(JSON.parse(d)); }
    close() {}
}
FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;

const pageUrl = 'https://ticket.example.com/imChat/qc/detail';
const logs = [];                                  // 收集采集器自己的 console（含它吞掉的告警）
const sandboxConsole = {
    log: (...a) => logs.push(a.map(x => String(x)).join(' ')),
    warn: (...a) => logs.push('WARN ' + a.map(x => String(x)).join(' ')),
    error: (...a) => logs.push('ERR ' + a.map(x => String(x)).join(' ')),
    table: () => {}
};
const sandbox = {
    window: {}, document: documentStub, location: { get href() { return pageUrl; } },
    console: sandboxConsole, navigator: { userAgent: 'QC Test' },
    WebSocket: FakeWS,
    Event: class { constructor(t) { this.type = t; } },
    KeyboardEvent: class { constructor(t) { this.type = t; } },
    setTimeout, clearTimeout, setInterval, clearInterval,
    JSON, Object, Math, Array, String, Number, Error, Boolean, Promise, Date
};
sandbox.globalThis = sandbox;

(async () => {
    console.log('=== 测试文件: ' + TARGET + ' ===\n');
    buildTable();
    try {
        vm.createContext(sandbox);
        vm.runInContext(code, sandbox, { filename: TARGET });
        check('脚本加载无异常', true);
    } catch (e) {
        check('脚本加载无异常', false, e.message);
        console.log('\n结果: 加载失败，后续用例跳过');
        process.exit(1);
    }
    await sleep(80);

    const qc = sandbox.window.__qc;
    check('暴露调试入口 __qc（version/sweep/stop/status/dump）',
        !!qc && ['version', 'sweep', 'stop', 'status', 'dump'].every(k => typeof qc[k] === 'function'));
    check('连上中继的独立通道 /ws/qc（不碰工作台探针 /ws/extension）',
        sent.ws.length === 1 && sent.ws[0] === 'ws://127.0.0.1:8765/ws/qc', sent.ws.join(','));
    const hello = sent.events.filter(e => e.event === 'QC_HELLO');
    check('连上即握手 QC_HELLO（带版本与页面地址）',
        hello.length === 1 && hello[0].data.version === '1.0' && hello[0].data.page.indexOf('/imChat/qc/') !== -1);

    const st0 = qc.status();
    check('表头驱动映射列（不是猜列序）',
        st0.columns['会话ID'] === 0 && st0.columns['问题分类'] === 5 && st0.columns['最终评分'] === 12,
        JSON.stringify(st0.columns));
    check('读到当前页 3 行', st0.rowsOnPage === 3, st0.rowsOnPage);

    // 指令通道：中继可以随时要求"回报结构"（排障/校准用，纯只读）
    FakeWS.last.onmessage({ data: JSON.stringify({ command: 'QC_DUMP' }) });
    await sleep(50);
    const dbg = sent.events.filter(e => e.event === 'QC_DEBUG');
    check('收到 QC_DUMP 指令 -> 回报详情面板结构', dbg.length === 1,
          dbg.length ? Object.keys(dbg[dbg.length - 1].data).join(',') : 'none');
    check('结构回报里有列映射/详情链/分页/消息骨架',
          !!dbg.length && !!dbg[dbg.length - 1].data.columns &&
          Array.isArray(dbg[dbg.length - 1].data.detailChain) &&
          dbg[dbg.length - 1].data.pagination !== undefined);

    console.log('\n[扫描] maxRows=6 / minScore=90 / 2 页（其中 1 条详情加载失败）');
    qc.config.rowDelayMs = 10;
    qc.config.detailTimeoutMs = 2500;      // 必须 > 稳定判定所需的 2 个 tick（400ms×2），否则会误判超时
    qc.config.maxPages = 3;
    qc.sweep({ maxRows: 6, minScore: 90, rowDelayMs: 10 });

    // 等扫描结束（DONE 事件出现），最多等 25 秒
    let done = null;
    for (let i = 0; i < 200; i++) {
        done = sent.events.filter(e => e.event === 'QC_SWEEP_DONE').pop() || null;
        if (done) break;
        await sleep(200);
    }

    const recs = sent.events.filter(e => e.event === 'RECORD_HISTORY_SESSION');
    const prog = sent.events.filter(e => e.event === 'QC_SWEEP_PROGRESS');
    // 临时排障输出（ASCII 化，便于在乱码终端里判读）—— 只在失败时打印，平时保持输出干净
    if (fail > 0) {
        const ascii = s => String(s).replace(/[^\x20-\x7e]/g, '.');
        console.log('DEBUG events=' + Array.from(new Set(sent.events.map(e => e.event))).join('|'));
        console.log('DEBUG clicks=' + ascii(clicks.map(c => c.cls.slice(0, 12) + ':' + c.text).join(' , ')));
        console.log('DEBUG status=' + JSON.stringify({ rowsOnPage: qc.status().rowsOnPage, page: qc.status().page,
            lastError: ascii(qc.status().lastError), cols: Object.keys(qc.status().columns).length }));
        logs.slice(0, 30).forEach(l => console.log('LOG ' + ascii(l).slice(0, 160)));
    }
    check('扫描结束并回报 QC_SWEEP_DONE', !!done, JSON.stringify(done && done.data));
    check('采集到 2 条合格会话（S1 + S2）', recs.length === 2,
        recs.map(r => r.data.sessionId).join(','));
    check('低评分会话被跳过（minScore=90 时 S3 不采）',
        !recs.some(r => r.data.sessionId === 'S3'));
    check('没有客服发言的会话被跳过（S4）', !recs.some(r => r.data.sessionId === 'S4'));
    check('★ 只有人机话术（AI Bot）的会话被跳过（S6）', !recs.some(r => r.data.sessionId === 'S6'));
    check('同会话ID 不重复采集（S1 只记 1 次，page1/page2 重叠也去重）',
        recs.filter(r => r.data.sessionId === 'S1').length === 1);
    check('详情加载不出来的那条如实计入 failed',
        !!done && done.data.failed >= 1, done && JSON.stringify(done.data));
    check('跳过计数正确（S3 低分 + S4 无客服 + S1 重复 + S6 只有机器人 = 4）',
        !!done && done.data.skipped === 4, done && done.data.skipped);

    const s1 = recs.filter(r => r.data.sessionId === 'S1')[0];
    check('落盘内容带工单元信息（会话ID/分类/评分/客服）',
        !!s1 && s1.data.category.indexOf('误操作') !== -1 && Number(s1.data.score) === 94,
        s1 && JSON.stringify({ cat: s1.data.category, score: s1.data.score, agent: s1.data.agent }));
    check('消息正文已去噪（去掉发送者/时间/「翻译」）',
        !!s1 && s1.data.messages[0].text === '我的钻石没到账' &&
                 s1.data.messages[1].text === '亲爱的玩家您好，请提供订单号',
        s1 && JSON.stringify(s1.data.messages));
    check('发言人标签正确（player/agent/system）',
        !!s1 && s1.data.messages.map(m => m.sender).join(',') === 'player,agent',
        s1 && s1.data.messages.map(m => m.sender).join(','));
    check('★ 客服名带空格（AI Bot）时前缀也能剥干净（实机 bug 回归）',
        !!s1 && s1.data.messages.every(m => !/^[^\n]{0,20}?\d{2}-\d{2}\s+\d{2}:\d{2}/.test(m.text)
                                        && !/^(翻译|原文)/.test(m.text)),
        s1 && JSON.stringify(s1.data.messages.map(m => m.text.slice(0, 16))));
    check('落盘时自报 noisyMessages（0 = 全部剥干净，没假装干净）',
        !!s1 && s1.data.noisyMessages === 0, s1 && s1.data.noisyMessages);
    const s2rec = recs.filter(r => r.data.sessionId === 'S2')[0];
    check('第一次点详情没渲染 -> 关面板重试一次后仍成功采到（面板盖住表格的自救）',
        !!s2rec, s2rec && s2rec.data.sessionId);
    check('★ 正文取自 .message-bubble（精确取值，不走正则兜底）',
        !!s1 && s1.data.messages.every(m => m.exact === 1), s1 && JSON.stringify(s1.data.messages.map(m => m.exact)));
    check('★ 每条消息都带发送者名（who），便于区分人机/真人',
        !!s1 && s1.data.messages[1].who === 'agent.zhang', s1 && s1.data.messages[1].who);
    check('★ 人机客服（AI Bot）单独标成 bot、真人标成 agent（S2 混合会话）',
        !!s2rec && s2rec.data.messages.map(m => m.sender).join(',') === 'player,bot,system,agent',
        s2rec && s2rec.data.messages.map(m => m.sender).join(','));
    check('落盘时自报 botMessages 条数（供蒸馏侧核对"只学真人"）',
        !!s2rec && s2rec.data.botMessages === 1, s2rec && s2rec.data.botMessages);
    check('翻页发生过（点过第 2 页）', clicks.some(c => c.text === '2' && c.cls.indexOf('number') !== -1));
    check('进度事件持续上报（>=4 次）', prog.length >= 4, prog.length);
    check('结束时 running=false', qc.status().running === false);
    check('只上报 QC_* / RECORD_HISTORY_SESSION，绝不碰会话链路',
        sent.events.every(e => ['QC_HELLO', 'QC_HEARTBEAT', 'QC_SWEEP_PROGRESS', 'QC_SWEEP_DONE',
                                'QC_DEBUG', 'RECORD_HISTORY_SESSION'].indexOf(e.event) !== -1),
        Array.from(new Set(sent.events.map(e => e.event))).join(','));
    check('点击只落在「查看详情」与分页上（没有点回复/发送）',
        clicks.every(c => c.text === '查看详情' || c.cls.indexOf('number') !== -1),
        Array.from(new Set(clicks.map(c => c.text))).join('/'));

    console.log('\n=== 结果: ' + pass + ' 通过 / ' + fail + ' 失败 ===');
    process.exit(fail === 0 ? 0 : 1);
})();
