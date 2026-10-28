// Run with: node --test tests/test_chat_ui.cjs (no dependencies).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {randomUUID} = require('node:crypto');

class Element {
    constructor() { this.value = ''; this.children = []; this.events = {}; }
    addEventListener(name, handler) { this.events[name] = handler; }
    append(...items) { this.children.push(...items); }
    replaceChildren() { this.children = []; }
    get lastElementChild() { return this.children.at(-1); }
    focus() {}
    scrollIntoView() {}
}
function setup({savedToken, status = 200, unavailable = false, storageBlocked = false} = {}) {
    const elements = new Map(), stored = new Map(), calls = [];
    const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
    const persistent = new Map(savedToken ? [['memory-api-token', savedToken]] : []);
    let response = unavailable ? new Error('暂时断网') : {messages: []};
    const context = {
        document: {getElementById: el, createElement: () => new Element(), querySelectorAll: () => []},
        sessionStorage: {getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value)},
        localStorage: {getItem: key => persistent.get(key),
            setItem: (key, value) => { if (storageBlocked) throw new Error('blocked'); persistent.set(key, value); },
            removeItem: key => persistent.delete(key)},
        crypto: {randomUUID},
        fetch: async (path, options) => {
            calls.push({path, options});
            if (response instanceof Error) throw response;
            return {ok: status === 200, status, json: async () => response};
        },
    };
    runInNewContext(readFileSync('app/static/chat/chat.js', 'utf8'), context);
    const event = () => ({preventDefault() {}, submitter: new Element()});
    return {el, stored, persistent, calls, respond: value => response = value,
        fire: (id, type = 'click') => el(id).events[type](event())};
}
test('connect, send, render text safely, start a separate conversation and disconnect', async () => {
    const ui = setup();
    ui.el('token').value = 'synthetic-token';
    await ui.fire('login-form', 'submit');
    assert.equal(ui.el('chat').hidden, false);
    assert.equal(ui.persistent.get('memory-api-token'), 'synthetic-token');
    const id = ui.stored.get('memory-conversation');
    ui.respond({conversation_id: id, answer: '<script>unsafe()</script>', memory_changes: [],
        experience_sources: [], sources: [], recall_trace: {}, history_saved: true});
    ui.el('message').value = '合成输入';
    const sending = ui.fire('composer', 'submit');
    await ui.fire('composer', 'submit');
    await sending;
    assert.equal(ui.calls.length, 2); // One history load, one send despite double submit.
    assert.equal(JSON.parse(ui.calls[1].options.body).conversation_id, id);
    assert.equal(ui.el('messages').children.length, 2);
    assert.equal(ui.el('messages').lastElementChild.children[1].textContent, '<script>unsafe()</script>');
    assert.equal(ui.el('message').value, '');
    await ui.fire('new-chat');
    assert.notEqual(ui.stored.get('memory-conversation'), id);
    assert.equal(ui.el('messages').children.length, 0);
    assert.deepEqual([...ui.stored.keys()], ['memory-conversation']);
    await ui.fire('disconnect');
    assert.equal(ui.el('chat').hidden, true);
    assert.equal(ui.persistent.has('memory-api-token'), false);
});
test('failed sends preserve the draft and unlock controls without adding a false saved turn', async () => {
    const ui = setup();
    ui.el('token').value = 'synthetic-token';
    await ui.fire('login-form', 'submit');
    ui.respond(new Error('断网'));
    ui.el('message').value = '不能丢失的输入';
    await ui.fire('composer', 'submit');
    assert.equal(ui.el('message').value, '不能丢失的输入');
    assert.equal(ui.el('messages').children.length, 0);
    assert.equal(ui.el('send').disabled, false);
    assert.match(ui.el('status').textContent, /重新连接查看历史/);
});

const settled = () => new Promise(resolve => setImmediate(resolve));
test('saved token reconnects automatically without storing chat content', async () => {
    const ui = setup({savedToken: 'saved-token'});
    await settled();
    assert.equal(ui.el('chat').hidden, false);
    assert.equal(ui.calls[0].options.headers.Authorization, 'Bearer saved-token');
    assert.deepEqual([...ui.persistent.keys()], ['memory-api-token']);
});
test('invalid saved token is removed and login remains available', async () => {
    const ui = setup({savedToken: 'expired-token', status: 401});
    await settled();
    assert.equal(ui.persistent.has('memory-api-token'), false);
    assert.equal(ui.el('login').hidden, false);
    assert.equal(ui.el('new-chat').disabled, true);
    assert.equal(ui.el('retry-connect').hidden, true);
});
test('temporary outage keeps saved token and allows reconnect without retyping', async () => {
    const ui = setup({savedToken: 'saved-token', unavailable: true});
    await settled();
    assert.equal(ui.persistent.get('memory-api-token'), 'saved-token');
    assert.equal(ui.el('retry-connect').hidden, false);
    ui.respond({messages: []});
    await ui.fire('retry-connect');
    assert.equal(ui.el('chat').hidden, false);
});
test('blocked browser storage still allows login with a clear persistence notice', async () => {
    const ui = setup({storageBlocked: true});
    ui.el('token').value = 'synthetic-token';
    await ui.fire('login-form', 'submit');
    assert.equal(ui.el('chat').hidden, false);
    assert.match(ui.el('status').textContent, /浏览器不允许保存令牌/);
});

test('degraded answer shows unsaved status while rendering the actual response', async () => {
    const ui = setup();
    ui.el('token').value = 'synthetic-token';
    await ui.fire('login-form', 'submit');
    ui.respond({conversation_id: ui.stored.get('memory-conversation'), answer: '这是回答。',
        memory_status: 'failed', history_saved: false, memory_changes: [], sources: [], experience_sources: [], recall_trace: {},
        warnings: [{code: 'memory_reassessment_failed', message: '本轮记忆整理失败，回答未保存。'}]});
    ui.el('message').value = '测试问题';
    await ui.fire('composer', 'submit');
    assert.equal(ui.el('status').textContent, '本轮记忆整理失败，回答未保存。');
    assert.equal(ui.el('messages').lastElementChild.children[1].textContent, '这是回答。');
});
