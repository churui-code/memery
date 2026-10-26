import httpx
from uuid import uuid4
from dataclasses import replace
import pytest
from app.config import Settings
from app.errors import MemoryError
from app.model import MiniMax
from app.service import MemoryService
from conftest import FakeEmbeddings


def test_provider_timeout(monkeypatch):
    original=httpx.Client
    def timeout(request):raise httpx.ReadTimeout('synthetic timeout',request=request)
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(timeout),**kwargs))
    with pytest.raises(MemoryError) as e:
        MiniMax(replace(Settings(),model_key='dummy')).chat('system',[{'role':'user','content':'test'}])
    assert e.value.code=='model_timeout'


@pytest.mark.parametrize('path,payload',[('/v1/search',{'query':'   '}),('/v1/chat',{'text':'   '})])
def test_blank_queries_do_not_call_model(client,model,path,payload):
    assert client.post(path,json=payload).status_code==422
    assert model.requests==[]


def test_change_between_save_and_return_is_rejected(repo,model,monkeypatch):
    item=repo.remember({'text':'卧室空调26度'})[0]
    original=repo.finish_call
    def change(call_id,status,usage=None):
        original(call_id,status,usage)
        if status=='completed':repo.forget(item['id'])
    monkeypatch.setattr(repo,'finish_call',change)
    with pytest.raises(MemoryError) as e:
        MemoryService(repo,FakeEmbeddings(),model).chat('空调几度',uuid4())
    assert e.value.code=='context_changed'


def test_slow_model_cannot_exhaust_storage_pool(repo,model):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    release=Event()
    model.callback=lambda:release.wait(5)
    service=MemoryService(repo,FakeEmbeddings(),model)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures=[executor.submit(service.chat,'普通合成问题') for _ in range(3)]
        try:
            for _ in range(100):
                if len(model.requests)==3:break
                time.sleep(.01)
            assert len(model.requests)==3
            with pytest.raises(MemoryError) as e:service.chat('第四个问题')
            assert e.value.status==429
            assert repo.remember({'text':'模型很慢时仍能保存'})[0]['storage_status']=='durable'
        finally:release.set()
        assert all(f.result()['answer'] for f in futures)
