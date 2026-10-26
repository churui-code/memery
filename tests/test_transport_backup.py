import json
import secrets
import io
import httpx
import pytest
from dataclasses import replace
from cryptography.exceptions import InvalidTag
from app.config import Settings
from app.errors import MemoryError
from app.model import MiniMax
from app.backup import encrypt,decrypt,encrypt_stream,decrypt_stream,CHUNK_SIZE
from app.text import chunks


@pytest.mark.parametrize('status,body,expected',[
    (429,{},'model_rate_limited'),
    (401,{'secret':'never expose this'},'model_rejected'),
    (200,{'stop_reason':'max_tokens','content':[{'type':'text','text':'partial'}]},'model_invalid_response'),
    (200,{'stop_reason':'end_turn','content':[{'type':'thinking','thinking':'private reasoning'}]},'model_invalid_response')])
def test_provider_errors_are_sanitized(monkeypatch,status,body,expected):
    original=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(status,json=body))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=transport,**kwargs))
    model=MiniMax(replace(Settings(),model_key='dummy'))
    with pytest.raises(MemoryError) as e:model.chat('system',[{'role':'user','content':'hello'}])
    assert e.value.code==expected
    assert 'never expose' not in e.value.message


def test_only_final_text_is_returned(monkeypatch):
    original=httpx.AsyncClient
    seen=[]
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'stop_reason':'end_turn','content':[{'type':'thinking','thinking':'secret'},{'type':'text','text':'最终回答'}],'usage':{'output_tokens':5}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    result=MiniMax(replace(Settings(),model_key='dummy')).chat('system',[{'role':'user','content':'hello'}])
    assert result['text']=='最终回答' and 'secret' not in str(result)
    assert seen[0]['thinking']['type']=='disabled'


@pytest.mark.parametrize('structured',[False,True])
@pytest.mark.parametrize('usage,expected',[
    (None,{}), ({},{}),
    ({'input_tokens':101,'output_tokens':0,'thinking':'synthetic private text'},
     {'input_tokens':101,'output_tokens':0}),
    ({'input_tokens':True,'output_tokens':-1},{}),
    ({'input_tokens':'12','output_tokens':2147483648},{}),
])
def test_protocol_errors_only_carry_safe_usage_counters(monkeypatch,structured,usage,expected):
    model=MiniMax(replace(Settings(),model_key='dummy'))
    body={'stop_reason':'max_tokens','content':[{'type':'thinking','thinking':'synthetic private text'}],
          'usage':usage}
    monkeypatch.setattr(model,'_request',lambda *args,**kwargs:body)
    with pytest.raises(MemoryError) as exc:
        if structured:
            model.decide_memories('system','test')
        else:
            model.chat('system',[{'role':'user','content':'test'}])
    assert exc.value.usage==expected
    assert 'synthetic private text' not in str(vars(exc.value))


@pytest.mark.parametrize('stop,name,proposals,accepted',[
    ('tool_use','propose_memories',[{'type':'tool_use','name':'propose_memories','input':{'actions':[]}}],True),
    ('max_tokens','propose_memories',[{'type':'tool_use','name':'propose_memories','input':{'actions':[]}}],False),
    ('tool_use','other',[{'type':'tool_use','name':'other','input':{'actions':[]}}],False),
    ('end_turn','propose_memories',[{'type':'text','text':'记住了'}],False),
])
def test_structured_decisions_require_one_complete_expected_tool(monkeypatch,stop,name,proposals,accepted):
    original=httpx.AsyncClient
    seen=[]
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'stop_reason':stop,'content':proposals})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
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


def test_streaming_backup_roundtrip_and_legacy_restore():
    key=secrets.token_bytes(32)
    dump=b'PGDMP'+secrets.token_bytes(CHUNK_SIZE*2+31)
    records=[{'id':'synthetic-deletion'}]
    encrypted=io.BytesIO()
    encrypt_stream(io.BytesIO(dump),encrypted,key,records)
    assert dump[:100] not in encrypted.getvalue()
    restored=io.BytesIO()
    decrypt_stream(io.BytesIO(encrypted.getvalue()),restored,key,records)
    assert restored.getvalue()==dump
    legacy=encrypt(b'PGDMPlegacy',key,records)
    output=io.BytesIO()
    decrypt_stream(io.BytesIO(legacy),output,key,records)
    assert output.getvalue()==b'PGDMPlegacy'


@pytest.mark.parametrize('change',['tamper','truncate','trailing','journal','wrong_key'])
def test_streaming_backup_rejects_incomplete_or_unauthenticated_data(change):
    key=secrets.token_bytes(32)
    records=[{'id':'synthetic-deletion'}]
    encrypted=io.BytesIO()
    encrypt_stream(io.BytesIO(b'PGDMPsynthetic'),encrypted,key,records)
    body=encrypted.getvalue()
    if change=='tamper':body=body[:-1]+bytes([body[-1]^1])
    elif change=='truncate':body=body[:-20]
    elif change=='trailing':body+=b'x'
    elif change=='journal':records=[]
    else:key=secrets.token_bytes(32)
    with pytest.raises((ValueError,InvalidTag)):
        decrypt_stream(io.BytesIO(body),io.BytesIO(),key,records)
