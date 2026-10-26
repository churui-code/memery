import json
import secrets
import httpx
import pytest
from dataclasses import replace
from cryptography.exceptions import InvalidTag
from app.config import Settings
from app.errors import MemoryError
from app.model import MiniMax
from app.backup import encrypt,decrypt
from app.text import chunks


@pytest.mark.parametrize('status,body,expected',[
    (429,{},'model_rate_limited'),
    (401,{'secret':'never expose this'},'model_rejected'),
    (200,{'stop_reason':'max_tokens','content':[{'type':'text','text':'partial'}]},'model_invalid_response'),
    (200,{'stop_reason':'end_turn','content':[{'type':'thinking','thinking':'private reasoning'}]},'model_invalid_response')])
def test_provider_errors_are_sanitized(monkeypatch,status,body,expected):
    original=httpx.Client
    transport=httpx.MockTransport(lambda request:httpx.Response(status,json=body))
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=transport,**kwargs))
    model=MiniMax(replace(Settings(),model_key='dummy'))
    with pytest.raises(MemoryError) as e:model.chat('system',[{'role':'user','content':'hello'}])
    assert e.value.code==expected
    assert 'never expose' not in e.value.message


def test_only_final_text_is_returned(monkeypatch):
    original=httpx.Client
    seen=[]
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'stop_reason':'end_turn','content':[{'type':'thinking','thinking':'secret'},{'type':'text','text':'最终回答'}],'usage':{'output_tokens':5}})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    result=MiniMax(replace(Settings(),model_key='dummy')).chat('system',[{'role':'user','content':'hello'}])
    assert result['text']=='最终回答' and 'secret' not in str(result)
    assert seen[0]['thinking']['type']=='disabled'


@pytest.mark.parametrize('stop,name,proposals,accepted',[
    ('tool_use','propose_memories',[{'type':'tool_use','name':'propose_memories','input':{'actions':[]}}],True),
    ('max_tokens','propose_memories',[{'type':'tool_use','name':'propose_memories','input':{'actions':[]}}],False),
    ('tool_use','other',[{'type':'tool_use','name':'other','input':{'actions':[]}}],False),
    ('end_turn','propose_memories',[{'type':'text','text':'记住了'}],False),
])
def test_structured_decisions_require_one_complete_expected_tool(monkeypatch,stop,name,proposals,accepted):
    original=httpx.Client
    seen=[]
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'stop_reason':stop,'content':proposals})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    model=MiniMax(replace(Settings(),model_key='dummy'))
    if accepted:assert model.decide_memories('system','test')['decision']=={'actions':[]}
    else:
        with pytest.raises(MemoryError):model.decide_memories('system','test')
    assert seen[0]['tool_choice']=={'type':'tool','name':'propose_memories'}


def test_encrypted_backup_authentication_and_journal():
    key=secrets.token_bytes(32)
    data=b'PGDMPsynthetic local memory'
    records=[{'id':'delete-1'}]
    envelope=encrypt(data,key,records)
    assert data not in envelope
    assert decrypt(envelope,key,records)==data
    with pytest.raises(InvalidTag):decrypt(envelope,secrets.token_bytes(32),records)
    with pytest.raises(InvalidTag):decrypt(envelope[:-1]+bytes([envelope[-1]^1]),key,records)
    with pytest.raises(ValueError):decrypt(envelope,key,[])


def test_chunking_preserves_all_text():
    text='记忆内容'*1000
    pieces=chunks(text)
    assert max(map(len,pieces))<=300
    rebuilt=pieces[0]+''.join(piece[50:] for piece in pieces[1:])
    assert rebuilt==text
