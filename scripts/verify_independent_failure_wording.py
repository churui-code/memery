"""One pre-registered fault injection; only the answer calls the real provider."""
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, '/app/tests')
from conftest import FakeEmbeddings, FakeModel
from app.config import Settings
from app.repository import Repository
from app.service import MemoryService
from app.model import MiniMax
from scripts.verify_independent_acceptance import Ledger
from scripts.verify_chat_repairs import atomic_json


def main():
    directory=Path('/qa')
    output=directory/'final-failure-injection.json'
    if output.exists(): raise RuntimeError('refuse_repeat')
    settings=replace(Settings(),db_schema='qa_failure_wording_'+uuid4().hex,
        deletion_journal='/qa/failure-injection-journal.jsonl',model_name='MiniMax-M3',thinking='disabled',
        model_timeout=90,chat_deadline_seconds=240,context_budget_tokens=24000)
    Path(settings.deletion_journal).touch(exist_ok=True)
    repo=Repository(settings); repo.migrate()
    fake=FakeModel()
    seed='10月12日我和顾岚去了松柏街自行车店。我花91元修车篮，顾岚花22元买了反光贴。她递回车篮时说：“慢慢骑，前面的路还长。”'
    fake.decision={'episodes':[{'title':'修车篮','gist':seed,'quote':seed,'details':[{'kind':'amount','quote':'我花91元修车篮，'},{'kind':'amount','quote':'顾岚花22元买了反光贴。'},{'kind':'speech','quote':'她递回车篮时说：“慢慢骑，前面的路还长。”'}]}]}
    first=MemoryService(repo,FakeEmbeddings(),fake).chat(seed)
    event=repo.episode(first['episode_ids'][0]); detail=next(d for d in event['details'] if '91' in d['quote'])
    text='我核对了松柏街自行车店收据，修车篮之前说的91元不对，实际是77元。请更新，以后按这个金额记住。'
    decision={'actions':[], 'self_updates':[]}
    ledger=Ledger(directory)
    state={'synthetic':True,'fault_injection':True,'schema':settings.db_schema,'input':text,'scripted_intake':decision,'injected_reassessment':{'unexpected_invalid_field':True},'request_start':len(json.loads(ledger.path.read_text())['calls'])}
    atomic_json(output,state)
    class Audited(MiniMax):
        def decide_memories(self,system,prompt): return {'decision':decision,'usage':{}}
        def reassess_memories(self,system,prompt): return {'decision':state['injected_reassessment'],'usage':{}}
        def _request(self,system,messages,**kwargs):
            identifier=ledger.reserve('answer'); state['request_id']=identifier
            trace={'fault_injection':True,'system':system,'messages':messages,'phase':'answer','model':settings.model_name}
            path=directory/'provider'/(identifier+'.json'); atomic_json(path,trace)
            try: result=super()._request(system,messages,**kwargs)
            except Exception as exc:
                ledger.finish(identifier,status='failed',error=getattr(exc,'code',type(exc).__name__)); raise
            trace['result']=result; atomic_json(path,trace)
            ledger.finish(identifier,status='completed',usage=result.get('usage',{})); return result
    class Forced(MemoryService):
        def _signature(self,context): return str(uuid4())
    def snapshot():
        with repo.pool.connection() as c:
            tables=[r['table_name'] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'").fetchall()]
            from psycopg import sql
            return {t:hashlib.sha256(json.dumps(sorted([json.dumps(r,default=str,sort_keys=True) for r in c.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(t))).fetchall()]),sort_keys=True).encode()).hexdigest() for t in tables if t not in ('model_calls','chat_activity','reflection_settings')}
    state['before']=snapshot(); atomic_json(output,state)
    try:
        state['response']=Forced(repo,FakeEmbeddings(),Audited(settings)).chat(text)
    except Exception as exc:
        state['error']={'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)}
    state['after']=snapshot(); state['changed_tables']=[t for t in state['before'] if state['before'][t]!=state['after'][t]]
    atomic_json(output,state); repo.close()
    print(json.dumps({k:state.get(k) for k in ('error','response','changed_tables','request_id')},ensure_ascii=False,default=str))

if __name__=='__main__': main()
