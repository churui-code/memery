"""Independent synthetic HTTP chat check. Never touches the frozen reflection budget.

Serve inside an isolated container with /qa mounted; run the client on the host.
Server ledger persists before every provider request, including failures/retries.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4


def atomic_json(path, value):
    import os
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2,default=str)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class RequestLedger:
    def __init__(self,path,limit=30):
        self.path,self.limit,self.lock=Path(path),limit,Lock()
        if not self.path.exists(): atomic_json(self.path,{'limit':limit,'calls':[]})

    def reserve(self):
        with self.lock:
            state=json.loads(self.path.read_text())
            if state['limit']!=self.limit or len(state['calls'])>=self.limit:
                raise RuntimeError('chat_repair_request_limit')
            identifier=str(uuid4())
            state['calls'].append({'id':identifier,'status':'reserved','at':datetime.now(timezone.utc).isoformat()})
            atomic_json(self.path,state)
            return identifier

    def finish(self,identifier,status,usage=None,error=None):
        with self.lock:
            state=json.loads(self.path.read_text())
            row=next(row for row in state['calls'] if row['id']==identifier)
            row.update(status=status,usage=usage or {},error=error)
            atomic_json(self.path,state)


def serve():
    from dataclasses import replace
    import uvicorn
    import app.lab as lab
    from app.main import create_app
    from app.config import Settings
    from app.errors import MemoryError
    directory=Path('/qa');directory.mkdir(exist_ok=True)
    ledger=RequestLedger(directory/'requests.json')
    original=lab.MeteredMiniMax
    class AuditedMiniMax(original):
        def _request(self,*args,**kwargs):
            # Per-chat cap is still enforced by the production metered adapter.
            if self.cloud_calls>=self.call_limit:
                raise MemoryError('lab_call_budget','本轮调用额度已用完。',429)
            try: identifier=ledger.reserve()
            except RuntimeError as exc: raise MemoryError('qa_call_budget','本次复验调用额度已用完。',429) from exc
            try: result=super()._request(*args,**kwargs)
            except Exception as exc:
                ledger.finish(identifier,'failed',getattr(exc,'usage',None),getattr(exc,'code',type(exc).__name__))
                raise
            trace=directory/'provider';trace.mkdir(exist_ok=True)
            atomic_json(trace/(identifier+'.json'), {'system':args[0],'messages':args[1],'result':result})
            ledger.finish(identifier,'completed',result.get('usage'))
            return result
    lab.MeteredMiniMax=AuditedMiniMax
    settings=replace(Settings(),db_schema='qa_chat_repairs_service',lab_enabled=True,
        lab_data_dir='/qa/runtime',deletion_journal='/qa/service-journal.jsonl')
    Path(settings.deletion_journal).touch(exist_ok=True)
    uvicorn.run(create_app(settings),host='0.0.0.0',port=8000,access_log=False)


STORY = [
    (0,'10月19日我和朋友周岚去了河湾街的陶艺店。我拿回自己做的蓝色小碗，烧制费付了52元。周岚给自己买了一个18元的木勺。出门时她把装碗的纸袋递给我，说：“慢慢来，歪一点也有自己的样子。”我最近画画总嫌自己画不好，听了这句话轻松了不少。'),
    (0,'换个话题，帮我给这次涂鸦练习起三个轻松一点的名字，不要太文艺。'),
    (1,'上次我和周岚去河湾街陶艺店，我付了多少钱、拿回什么？她买了什么、花了多少？递纸袋时那句话原话是什么？'),
    (1,'10月26日我又和周岚去河湾街，这次去了花店。她花76元买了一盆薄荷，我花14元买了一包种子。她说：“这次看谁先把芽等出来。”没有去陶艺店。'),
    (2,'我刚核对了10月19日的收据，之前陶艺店的烧制费用说错了，实际是25元，不是52元。周岚木勺的18元没错，花店那次也没变。'),
    (3,'我和周岚去河湾街陶艺店那次，最后核对的烧制费用是多少？她买木勺花了多少？她递纸袋时安慰我的原话还记得吗？'),
    (3,'陶艺店的店主叫什么名字？我们那天是坐公交还是走路去的？'),
    (3,'对了，我平时给练习作品起名字都喜欢轻松直白的，以后也按这个风格给我建议。'),
    (4,'河湾街陶艺店和花店两次分别是哪天？我和周岚各自花了什么钱？我平时给练习作品起名喜欢什么风格？'),
]


def client(base,directory,token_file):
    import urllib.request
    import urllib.error
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'chat.json'
    if path.exists(): raise RuntimeError('refuse_automatic_rerun')
    state={'synthetic':True,'max_turns':10,'max_requests':30,'turns':[],
           'conversations':[str(uuid4()) for _ in range(5)]}
    def request(route,body=None):
        req=urllib.request.Request(base+route,data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+token_file.read_text().strip(),'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=300) as response:return {'status':response.status,'body':json.load(response)}
        except urllib.error.HTTPError as exc:return {'status':exc.code,'body':json.load(exc)}
    state['experiment']=request('/v1/lab/experiments',{'name':'修复后自然聊天-陶艺与花店','scenario_id':'empty',
        'model_mode':'live','embedding_mode':'local','reflection_profile':'off','synthetic':True,
        'start_time':'2026-10-27T14:00:00+08:00'})
    atomic_json(path,state)
    if state['experiment']['status']!=201:raise RuntimeError('experiment_creation_failed')
    route='/v1/lab/experiments/'+state['experiment']['body']['id']
    state['before']=request(route);atomic_json(path,state)
    for conversation,text in STORY:
        assert len(state['turns'])<10
        turn={'number':len(state['turns'])+1,'conversation_id':state['conversations'][conversation],'user':text}
        state['turns'].append(turn);atomic_json(path,state)
        turn['response']=request(route+'/chat',{'text':text,'conversation_id':turn['conversation_id'],'synthetic':True})
        atomic_json(path,state)
        body=turn['response']['body']
        print(json.dumps({'turn':turn['number'],'status':turn['response']['status'],'answer':body.get('answer'),
                          'error':body.get('error'),'calls':body.get('cloud_calls'),'memory_status':body.get('memory_status')},ensure_ascii=False),flush=True)
    state['after']=request(route);atomic_json(path,state)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--base',default='http://127.0.0.1:8767')
    parser.add_argument('--directory',type=Path,default=Path('data/qa/chat-repairs-20261027'))
    parser.add_argument('--token-file',type=Path,default=Path('data/lab/secrets/api_token'))
    args=parser.parse_args()
    if args.serve:serve()
    else:client(args.base,args.directory,args.token_file)
