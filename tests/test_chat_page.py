from dataclasses import replace
from fastapi.testclient import TestClient
from app.main import create_app
from tests.conftest import FakeEmbeddings


def test_chat_page_and_authenticated_roundtrip(client):
    page = client.get('/chat')
    assert page.status_code == 200
    assert '从这里，继续聊。' in page.text
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert client.get('/', follow_redirects=False).headers['location'] == '/chat'
    assert client.get('/chat-assets/chat.js').status_code == 200
    assert client.get('/chat-assets/chat.css').status_code == 200
    assert client.get('/chat-assets/../../config.py').status_code == 404
    result = client.post('/v1/chat', json={'text': '合成测试：今天在公园散步。', 'auto_memory': True})
    assert result.status_code == 200
    data = result.json()
    assert all(key in data for key in ('answer', 'memory_changes', 'experience_sources', 'recall_trace'))
    history = client.get('/v1/conversations/' + data['conversation_id']).json()
    assert [row['role'] for row in history['messages']] == ['user', 'assistant']
    assert history['messages'][-1]['content'] == data['answer']
    client.headers.pop('authorization')
    assert client.get('/chat').status_code == 200
    assert client.get('/v1/conversations/' + data['conversation_id']).status_code == 401
    assert client.post('/v1/chat', json={'text': '测试'}).status_code == 401


def test_lab_does_not_serve_production_chat(repo, model):
    settings = replace(repo.settings, lab_enabled=True)
    app = create_app(settings, repo=repo, embeddings=FakeEmbeddings(), model=model)
    with TestClient(app) as client:
        assert client.get('/chat').status_code == 404
        assert client.get('/chat-assets/chat.js').status_code == 404
        assert client.get('/', follow_redirects=False).headers['location'] == '/lab'
