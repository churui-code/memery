'use strict';
const $ = id => document.getElementById(id);
let token = '', busy = false;
const tokenKey = 'memory-api-token';
try { token = localStorage.getItem(tokenKey) || ''; } catch (_) {}
let conversationId;
try { conversationId = sessionStorage.getItem('memory-conversation'); } catch (_) {}
if (!/^[0-9a-f-]{36}$/i.test(conversationId || '')) conversationId = crypto.randomUUID();
function rememberSession() {
    try { sessionStorage.setItem('memory-conversation', conversationId); } catch (_) {}
    $('session-label').textContent = `会话 ${conversationId.slice(0, 8)}`;
}
function setBusy(value) {
    busy = value;
    for (const id of ['send', 'new-chat', 'disconnect', 'recall-mode']) $(id).disabled = value;
    $('message').disabled = value;
    $('send').textContent = value ? '正在回想…' : '发送 ↑';
}
async function api(path, body) {
    const response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('服务返回异常，请检查本地服务后重新连接。'); }
    if (!response.ok) {
        if (response.status === 401) {
            try { localStorage.removeItem(tokenKey); } catch (_) {}
            token = '';
        }
        throw new Error(result.message || (response.status === 401 ? '访问令牌无效，请重新连接。' : `请求失败（${response.status}）`));
    }
    return result;
}
function addMessage(role, content, result) {
    const article = document.createElement('article');
    article.className = `message ${role === 'user' ? 'user' : 'assistant'}`;
    const label = document.createElement('div');
    label.className = 'message-label'; label.textContent = role === 'user' ? '你' : 'MEMORY';
    const text = document.createElement('div');
    text.className = 'message-content'; text.textContent = content;
    article.append(label, text);
    if (result) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = `记忆详情 · ${result.memory_changes.length} 项事实变更 · ${result.experience_sources.length} 条经历引用`;
        const pre = document.createElement('pre');
        pre.textContent = JSON.stringify({事实变更: result.memory_changes, 引用事实: result.sources,
            引用经历: result.experience_sources, 本轮经历: result.experience_id,
            对话已保存: result.history_saved, 召回过程: result.recall_trace}, null, 2);
        details.append(summary, pre); article.append(details);
    }
    $('messages').append(article); $('welcome').hidden = true;
}
async function connect() {
    if (busy) return;
    setBusy(true); $('connect').disabled = true;
    $('status').textContent = '正在连接…';
    try {
        const history = await api(`/v1/conversations/${conversationId}?limit=200`);
        let remembered = true;
        try { localStorage.setItem(tokenKey, token); } catch (_) { remembered = false; }
        $('messages').replaceChildren(); $('welcome').hidden = history.messages.length > 0;
        for (const message of history.messages) addMessage(message.role, message.content);
        $('token').value = ''; $('login').hidden = true; $('chat').hidden = false;
        $('disconnect').hidden = false; $('connection-state').textContent = '● 已连接';
        rememberSession();
        $('status').textContent = remembered
            ? (history.messages.length === 200 ? '已载入最近 200 条消息。' : '')
            : '已连接，但浏览器不允许保存令牌，下次打开需重新输入。';
    } catch (error) {
        $('login').hidden = false;
        $('status').textContent = error.message;
    } finally {
        setBusy(false); $('connect').disabled = false;
        $('new-chat').disabled = !$('login').hidden;
        $('retry-connect').hidden = $('login').hidden || !token;
        if ($('login').hidden) $('message').focus();
    }
}
$('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    token = $('token').value.trim();
    await connect();
});
$('retry-connect').addEventListener('click', connect);
$('new-chat').addEventListener('click', () => {
    if (busy) return;
    conversationId = crypto.randomUUID(); rememberSession();
    $('messages').replaceChildren(); $('welcome').hidden = false; $('message').value = '';
    $('status').textContent = '已开始新对话，长期记忆仍然保留。'; $('message').focus();
});
$('disconnect').addEventListener('click', () => {
    if (busy) return;
    try { localStorage.removeItem(tokenKey); } catch (_) {}
    token = ''; $('retry-connect').hidden = true; $('messages').replaceChildren(); $('message').value = '';
    $('chat').hidden = true; $('login').hidden = false; $('disconnect').hidden = true;
    $('new-chat').disabled = true; $('connection-state').textContent = '○ 尚未连接';
    $('status').textContent = ''; $('token').focus();
});
$('composer').addEventListener('submit', async event => {
    event.preventDefault();
    const text = $('message').value;
    if (busy || !text.trim()) return;
    setBusy(true); $('status').textContent = '正在整理记忆并回答，可能需要几分钟，请保持页面打开。';
    try {
        const result = await api('/v1/chat', {text, conversation_id: conversationId, auto_memory: true, recall_mode: $('recall-mode').value});
        conversationId = result.conversation_id; rememberSession();
        addMessage('user', text); addMessage('assistant', result.answer, result);
        $('message').value = '';
        $('status').textContent = result.memory_status === 'failed' ? '本轮记忆整理失败，回答未保存。' : result.history_saved ? '本轮对话已保存。' : '本轮回答未保存到对话历史。';
        $('messages').lastElementChild.scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (error) {
        $('status').textContent = `${error.message}\n输入已保留。若连接中断，发送结果可能尚未确认；请先重新连接查看历史，避免重复提交。`;
    } finally { setBusy(false); $('message').focus({preventScroll: true}); }
});
$('message').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !busy) {
        event.preventDefault(); $('composer').requestSubmit();
    }
});
for (const button of document.querySelectorAll('[data-prompt]')) button.addEventListener('click', () => {
    if (busy) return;
    $('message').value = button.dataset.prompt; $('message').focus();
});

if (token) connect();
