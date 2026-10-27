#!/usr/bin/env python3
"""Frozen synthetic comparisons with a persistent all-history request ceiling.

The fixed synthetic v0.5 scope already has 142 confirmed historical attempts
(68 + 74), exceeding the original total of 96. Host startup backfills preserved
reports and refuses live work if this history is missing or the total is spent.
Retries, failed requests and interrupted precharges never receive refunds.
--cumulative-call-limit is an explicitly approved new ALL-HISTORY total, not
another allowance per run; omitting it retains the persisted limit. Provider
usage is recorded separately and reservations do not assert actual billing.
Runtime counters: data/qa/reflection-v0.5-budget/ledger.json. No personal data.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from reflection_v5_fixtures import CASES, FIXTURE_VERSION, FROZEN_SHA256
from verify_memory_v4 import error_record


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


DEFAULT_CUMULATIVE_LIMIT = 96
MINIMUM_HISTORICAL_CALLS = 142  # Confirmed v1/v1.1 acceptance runs: 68 + 74.
LEDGER_NAME = 'ledger.json'
LEDGER_DIRECTORY = Path('data/qa/reflection-v0.5-budget')
LEDGER_SCOPE = 'reflection-v0.5-frozen-synthetic-acceptance'
LEDGER_VERSION = 'reflection-qa-budget-v1'
CONTAINER_LEDGER = Path('/verification-budget') / LEDGER_NAME


class BudgetLimitError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.status = code, 429


def _usage(value):
    return {key:value[key] for key in ('input_tokens','output_tokens')
            if isinstance(value,dict) and type(value.get(key)) is int and value[key]>=0}


def _report_usage(report):
    calls=[call for case in report.get('cases',[]) for arm in case.get('arms',{}).values()
           for call in arm.get('calls',[])]
    known=[_usage(call) for call in calls]
    return {'known_input_tokens':sum(row.get('input_tokens',0) for row in known),
            'known_output_tokens':sum(row.get('output_tokens',0) for row in known),
            'complete':len(calls)==report['cloud_calls'] and all(len(row)==2 for row in known)}


class PersistentCallLedger:
    """Synthetic QA counters only. A reservation is durable before any I/O to M3."""
    def __init__(self, path, *, create=False):
        self.path=Path(path)
        if create:self.path.parent.mkdir(parents=True,exist_ok=True)
        if not self.path.parent.is_dir() or (not create and not self.path.is_file()):
            raise RuntimeError('verification_budget_ledger_required')
        with self._locked(create=create) as state:
            if not self.path.exists():self._write(state)

    @contextmanager
    def _locked(self,create=False):
        # Lock a stable companion inode: atomically replacing the JSON must not
        # permit another process to acquire a lock on a different data inode.
        with self.path.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    state=json.loads(self.path.read_text(encoding='utf-8'))
                elif create:
                    state={'version':LEDGER_VERSION,'scope':LEDGER_SCOPE,'fixture_sha256':FROZEN_SHA256,
                           'limit':DEFAULT_CUMULATIVE_LIMIT,
                           'imports':{},'reservations':[],'limit_changes':[]}
                else:raise RuntimeError('verification_budget_ledger_missing')
                self._validate(state)
                yield state
            finally:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    @staticmethod
    def _validate(state):
        if (not isinstance(state,dict) or state.get('version')!=LEDGER_VERSION
            or state.get('scope')!=LEDGER_SCOPE or state.get('fixture_sha256')!=FROZEN_SHA256
            or type(state.get('limit')) is not int or state['limit']<1
            or not isinstance(state.get('imports'),dict) or not isinstance(state.get('reservations'),list)
            or not isinstance(state.get('limit_changes'),list)):
            raise RuntimeError('verification_budget_ledger_invalid')
        ids,ordinals=set(),set()
        for run,row in state['imports'].items():
            if not isinstance(run,str) or not isinstance(row,dict) or type(row.get('calls')) is not int or row['calls']<0:
                raise RuntimeError('verification_budget_import_invalid')
        for row in state['reservations']:
            if (not isinstance(row,dict) or not isinstance(row.get('id'),str) or row['id'] in ids
                or not isinstance(row.get('run_id'),str)
                or type(row.get('run_call')) is not int or row['run_call']<1
                or (row['run_id'],row['run_call']) in ordinals or row.get('status') not in ('reserved','completed','failed')):
                raise RuntimeError('verification_budget_reservation_invalid')
            ids.add(row['id']);ordinals.add((row['run_id'],row['run_call']))

    def _write(self,state):
        temporary=self.path.with_name(self.path.name+'.'+uuid4().hex+'.tmp')
        try:
            with temporary.open('x',encoding='utf-8') as handle:
                os.chmod(temporary,0o600)
                json.dump(state,handle,ensure_ascii=False,indent=2,allow_nan=False)
                handle.write('\n');handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,self.path)
            directory=os.open(str(self.path.parent),os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if temporary.exists():temporary.unlink()

    @staticmethod
    def _counts(state):
        counts=Counter()
        for row in state['reservations']:counts[row['run_id']]=max(counts[row['run_id']],row['run_call'])
        for run,item in state['imports'].items():counts[run]=max(counts[run],item['calls'])
        return counts

    @classmethod
    def _snapshot(cls,state):
        counts=cls._counts(state)
        usage={'known_input_tokens':0,'known_output_tokens':0,'complete':True}
        for run,count in counts.items():
            rows=[row for row in state['reservations'] if row['run_id']==run]
            imported=state['imports'].get(run,{'calls':0,'provider_usage':{
                'known_input_tokens':0,'known_output_tokens':0,'complete':True}})
            if {row['run_call'] for row in rows}==set(range(1,count+1)):
                selected={'known_input_tokens':0,'known_output_tokens':0,'complete':True}
            else:
                selected=dict(imported.get('provider_usage',{'complete':False}))
                rows=[row for row in rows if row['run_call']>imported['calls']]
            for row in rows:
                actual=_usage(row.get('usage'))
                selected['known_input_tokens']=selected.get('known_input_tokens',0)+actual.get('input_tokens',0)
                selected['known_output_tokens']=selected.get('known_output_tokens',0)+actual.get('output_tokens',0)
                selected['complete']=bool(selected.get('complete')) and len(actual)==2
            for key in ('known_input_tokens','known_output_tokens'):usage[key]+=selected.get(key,0)
            usage['complete']=usage['complete'] and bool(selected.get('complete'))
        charged=sum(counts.values())
        return {'version':LEDGER_VERSION,'scope':LEDGER_SCOPE,'fixture_sha256':FROZEN_SHA256,'cumulative_limit':state['limit'],'charged_calls':charged,
            'remaining_calls':max(0,state['limit']-charged),'over_limit':charged>state['limit'],
            'reserved_pending_calls':sum(row['status']=='reserved' for row in state['reservations']),
            'provider_usage':usage,'historical_runs':len(state['imports']),
            'backfill_runs':[{'run_id':run,'calls':item['calls'],'reports':item['reports']} for run,item in sorted(state['imports'].items())],
            'counting':'durable precharge; failures and interrupted reservations remain charged; not a billing claim'}

    def snapshot(self):
        with self._locked() as state:return self._snapshot(state)

    def set_explicit_limit(self,limit):
        if type(limit) is not int or limit<1:raise ValueError('invalid_cumulative_limit')
        with self._locked() as state:
            if limit!=state['limit']:
                state['limit_changes'].append({'at':datetime.now(timezone.utc).isoformat(),
                    'previous':state['limit'],'limit':limit,'reason':'explicit_cumulative_call_limit_argument'})
                state['limit']=limit
                self._write(state)
            return self._snapshot(state)

    def backfill(self,paths):
        candidates=[]
        for path in paths:
            path=Path(path)
            if not path.is_file():continue
            # A malformed historical file is an accounting failure, not zero.
            report=json.loads(path.read_text(encoding='utf-8'))
            if report.get('version')!='0.5' or report.get('real_model_requested') is not True:continue
            if report.get('fixture_sha256')!=FROZEN_SHA256:raise RuntimeError('verification_historical_fixture_mismatch')
            count=report.get('cloud_calls')
            if type(count) is not int or count<0:raise RuntimeError('verification_historical_count_missing')
            if not report.get('started_at'):raise RuntimeError('verification_historical_run_identity_missing')
            run=report.get('verification_run_id') or 'legacy-'+digest({
                'version':report['version'],'fixture':report.get('fixture_sha256'),'started_at':report['started_at']})
            candidates.append((run,count,path.name,digest(report),_report_usage(report)))
        with self._locked() as state:
            for run,count,name,checksum,usage in candidates:
                previous=state['imports'].get(run,{'calls':0,'reports':[]})
                source={'name':name,'sha256':checksum}
                if source not in previous['reports']:previous['reports'].append(source)
                if count>=previous['calls']:
                    previous.update(calls=count,provider_usage=usage)
                state['imports'][run]=previous
            self._write(state)
            return self._snapshot(state)

    def require_known_history(self):
        if self.snapshot()['charged_calls']<MINIMUM_HISTORICAL_CALLS:
            raise RuntimeError('verification_historical_budget_incomplete')

    def assert_available(self):
        state=self.snapshot()
        if not state['remaining_calls']:
            raise BudgetLimitError('verification_cumulative_call_budget',
                f"累计已记录/预扣调用 {state['charged_calls']} 次，总上限 {state['cumulative_limit']}；禁止新增云请求。")
        return state

    def reserve(self,run_id,run_limit):
        with self._locked() as state:
            counts=self._counts(state)
            if counts[run_id]>=run_limit:
                raise BudgetLimitError('verification_run_call_budget','本轮已达到请求上限。')
            if sum(counts.values())>=state['limit']:
                raise BudgetLimitError('verification_cumulative_call_budget','全历史累计请求预算已耗尽。')
            identifier=str(uuid4())
            state['reservations'].append({'id':identifier,'run_id':run_id,'run_call':counts[run_id]+1,'status':'reserved',
                'created_at':datetime.now(timezone.utc).isoformat()})
            self._write(state)
            return identifier

    def finish(self,identifier,status,usage=None,error_code=None):
        if status not in ('completed','failed'):raise ValueError('invalid_budget_completion')
        with self._locked() as state:
            row=next(row for row in state['reservations'] if row['id']==identifier)
            if row['status']!='reserved':raise RuntimeError('verification_reservation_already_finished')
            row.update(status=status,usage=_usage(usage),finished_at=datetime.now(timezone.utc).isoformat())
            if error_code:row['error_code']=str(error_code)
            self._write(state)

    def run_calls(self,run_id):
        with self._locked() as state:return self._counts(state)[run_id]


class PersistentCallBudget:
    def __init__(self,ledger,run_id,limit):
        ledger.require_known_history()
        self.ledger,self.run_id,self.limit=ledger,run_id,limit
    @property
    def calls(self):return self.ledger.run_calls(self.run_id)
    def consume(self):return self.ledger.reserve(self.run_id,self.limit)
    def finish(self,*args,**kwargs):return self.ledger.finish(*args,**kwargs)


def historical_reports(root):
    return [root/'docs'/'verification-v0.5.json',
            *sorted((root/'docs'/'verification-history').glob('verification-v0.5-*.json'))]


def preserve_report(destination):
    if not destination.is_file():return
    history=destination.parent/'verification-history'
    history.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    shutil.copy2(destination,history/f'{destination.stem}-{stamp}-{uuid4().hex}.json')


def inside(args):
    sys.path.insert(0,str(Path.cwd()))
    from psycopg import sql
    from app.clock import MutableClock
    from app.config import Settings
    from app.context import ESTIMATOR_VERSION
    from app.embeddings import LocalEmbeddings
    from app.episodes import EpisodeProposal
    from app.errors import MemoryError
    from app.lab import LabCallBudget, LabEmbeddings, LabManager, LabModel, MeteredMiniMax
    from app.life import ExperienceDescription, SelfUpdate
    from app.model import MiniMax
    from app.reflection import ReflectionService
    from app.repository import Repository
    from app.service import MemoryService
    from app.worker import process_one

    selected=[case for case in CASES if not args.cases or case['id'] in args.cases.split(',')]
    request_limit=8*len(selected)
    report={'version':'0.5','fixture_version':FIXTURE_VERSION,'fixture_sha256':digest(CASES),
        'started_at':datetime.now(timezone.utc).isoformat(),'real_model_requested':args.real_model,
        'verification_run_id':args.verification_run_id,
        'real_vectors_requested':args.local_vectors,'request_limit':request_limit,'selected_cases':[case['id'] for case in selected],
        'full_suite':len(selected)==12,'cases':[],'failures':[],
        'isolation':'random per-case base/off/on schemas; independent temporary journals; synthetic sources only',
        'estimator_version':ESTIMATOR_VERSION,'passed':False}
    if report['fixture_sha256']!=FROZEN_SHA256:
        report['failures'].append({'stage':'fixture','reason':'frozen_fixture_hash_changed'})
    ledger=PersistentCallLedger(CONTAINER_LEDGER) if args.real_model else None
    budget=PersistentCallBudget(ledger,args.verification_run_id,request_limit) if ledger else LabCallBudget(request_limit)
    if ledger:
        report['cumulative_budget_before']=ledger.snapshot()
        try:ledger.assert_available()
        except BudgetLimitError as exc:
            report.update(cloud_calls=0,cumulative_budget=ledger.snapshot(),temporary_schemas_removed=True,
                finished_at=datetime.now(timezone.utc).isoformat())
            report['failures'].append({'stage':'cumulative_budget',**error_record(exc)})
            print(json.dumps(report,ensure_ascii=False));return 1

    class BudgetedMiniMax(MeteredMiniMax):
        def _request(self,*values,**kwargs):
            if self.cloud_calls>=self.call_limit:
                raise MemoryError('lab_call_budget','当前阶段已达到云调用上限。',429)
            try:reservation=self.budget.consume()
            except BudgetLimitError as exc:raise MemoryError(exc.code,str(exc),429) from exc
            self.cloud_calls+=1
            try:
                result=MiniMax._request(self,*values,**kwargs)
            except BaseException as exc:
                self.budget.finish(reservation,'failed',getattr(exc,'usage',None),getattr(exc,'code',type(exc).__name__))
                raise
            self.budget.finish(reservation,'completed',result.get('usage'))
            return result

    class AuditedReflectionModel(BudgetedMiniMax):
        """Only this synthetic QA process retains proposal values for replay."""
        def __init__(self,*values,**kwargs):
            super().__init__(*values,**kwargs)
            self.decisions=[]
            self.protocol=[]

        def _request(self,*values,**kwargs):
            result=super()._request(*values,**kwargs)
            self.protocol.append({'stop_reason':result.get('stop_reason'),
                'tool_uses':[{'name':row.get('name'),'input':row.get('input')} for row in result.get('content',[])
                             if row.get('type')=='tool_use']})
            return result

        def _structured(self,system,prompt,tool):
            result=super()._structured(system,prompt,tool)
            record={'tool':tool['name'],'decision':result['decision']}
            self.decisions.append(record)
            if tool['name']=='propose_reflection':
                from app.reflection_types import validate_reflection_proposal
                try:validate_reflection_proposal(result['decision'],json.loads(prompt))
                except MemoryError as exc:record['validation']=error_record(exc)
            return result

    def protected(repo):
        value={}
        with repo.pool.connection() as c:
            for table in ('experiences','episodes','memories','episode_rehearsals','messages'):
                rows=c.execute(sql.SQL('SELECT row_to_json(t) AS row FROM {} t').format(sql.Identifier(table))).fetchall()
                value[table]=sorted(rows,key=lambda row:json.dumps(row,sort_keys=True,ensure_ascii=False,default=str))
        return digest(value)

    def seed(repo,case):
        for index,event in enumerate(case['events']):
            text=event['text']
            states=[]
            if index==0 and case.get('prior'):
                domain,key,statement=case['prior']
                states=[SelfUpdate(domain=domain,key=key,text=statement,quote=text,confidence=.5,
                    reason='固定合成基线中的暂定解释，等待后续材料修订。',
                    **({'intensity':.7,'half_life_hours':24} if domain=='emotion' else {}))]
            repo.save_answer(uuid4(),text,'合成夹具已导入。',[],repo.epoch(),
                experience_description=ExperienceDescription(title=event['title'],summary=event['title'],context_type=event['context_type']),
                episode_proposals=[EpisodeProposal(title=event['title'],gist=event['title'],quote=text,
                    context_type=event['context_type'],importance=event['importance'],importance_reason='固定合成验收材料')],
                self_updates=states)
            repo.clock.advance(days=1)

    with tempfile.TemporaryDirectory(prefix='reflection-v5-') as directory:
        for case in selected:
            entry={'id':case['id'],'category':case['category'],'query':case['query'],'arms':{},'checks':{},'passed':False}
            repositories=[]
            try:
                def new_repo(suffix):
                    schema='lab_reflection_v5_'+uuid4().hex
                    journal=Path(directory)/(schema+'.jsonl')
                    journal.touch()
                    settings=replace(Settings(),db_schema=schema,deletion_journal=str(journal),lab_enabled=True)
                    repo=Repository(settings,clock=MutableClock(datetime(2025,1,1,tzinfo=timezone.utc)))
                    repositories.append(repo)
                    repo.migrate()
                    return repo
                base=new_repo('base')
                seed(base,case)
                embeddings=LocalEmbeddings(base.settings) if args.local_vectors else LabEmbeddings()
                while process_one(base,embeddings):
                    pass
                snapshot=protected(base)
                snapshots=[]
                for arm in ('off','on'):
                    repo=new_repo(arm)
                    LabManager._copy_schema(repo,base.settings.db_schema,repo.settings.db_schema)
                    repo.clock.set(base.clock.now())
                    repo.reset_reflections_after_restore(reason='cloned')
                    snapshots.append(protected(repo))
                    prior=repo.self_state(per_domain=100)
                    arm_data={'before':prior}
                    entry['arms'][arm]=arm_data
                    if arm=='on':
                        model=AuditedReflectionModel(repo.settings,call_limit=2,budget=budget) if args.real_model else LabModel()
                        service=ReflectionService(repo,embeddings,model)
                        initial=protected(repo)
                        preview=service.preview()
                        arm_data['preview']=preview
                        queued=service.enqueue()
                        result=service.execute(queued['id']) if queued.get('id') else queued
                        arm_data['reflection']=result
                        arm_data['synthetic_provider_decisions']=getattr(model,'decisions',[])
                        arm_data['synthetic_provider_protocol']=getattr(model,'protocol',[])
                        arm_data['after']=repo.self_state(per_domain=100)
                        arm_data['cloud_calls']=model.cloud_calls
                        entry['checks']['reflection_preserves_originals_facts_activity']=initial==protected(repo)
                        safe_rejection=(result.get('status')=='failed'
                            and result.get('error_code')=='reflection_review_rejected'
                            and digest(prior)==digest(arm_data['after']))
                        arm_data['reflection_outcome']=('completed' if result.get('status')=='completed'
                            else 'safe_no_update' if safe_rejection else 'failed')
                        entry['checks']['reflection_completed_or_safely_rejected']=(
                            result.get('status')=='completed' or safe_rejection)
                        new=[row for row in arm_data['after'] if row.get('generation_type')=='reflection']
                        arm_data['reflective_states']=new
                        entry['checks']['reflection_has_bound_sources']=all(row.get('evidence_manifest') and row.get('reflection_run_id') for row in new)
                        if case.get('repeat') and result.get('status')=='completed':
                            before_repeat=digest(repo.self_state(per_domain=100))
                            with repo.pool.connection() as c:
                                calls=c.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n']
                            for _ in range(case['repeat']):
                                service.preview()
                                try:
                                    repeated=service.enqueue()
                                    if repeated.get('id'):
                                        service.execute(repeated['id'])
                                except MemoryError as exc:
                                    # No new evidence is an expected skip, not a new round.
                                    if exc.status not in (409,429):raise
                            with repo.pool.connection() as c:
                                after_calls=c.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n']
                            entry['checks']['repeat_100_no_state_or_call_change']=before_repeat==digest(repo.self_state(per_domain=100)) and calls==after_calls and initial==protected(repo)
                    else:
                        arm_data['after']=prior
                    model=BudgetedMiniMax(repo.settings,call_limit=3,budget=budget) if args.real_model else LabModel()
                    query=case['query']+' 本轮仅做合成验收，不保存本轮新记录。'
                    answer=MemoryService(repo,embeddings,model).chat(query)
                    arm_data['answer']=answer['answer']
                    arm_data['actual_context']=answer.get('actual_context')
                    arm_data['chat_calls']=answer.get('calls',[])
                    arm_data['chat_cloud_calls']=model.cloud_calls
                    context=answer.get('actual_context',{})
                    entry['checks'][arm+'_actual_context']=bool(context)
                    entry['checks'][arm+'_bounded_chat_calls']=len(answer.get('calls',[]))<=3
                    if args.real_model:
                        entry['checks'][arm+'_required_answer']=all(word in answer['answer'] for word in case.get('required',[]))
                    evidence=json.dumps(context.get('experience_evidence',[]),ensure_ascii=False,default=str)
                    entry['checks'][arm+'_required_source']=all(word in evidence for word in case.get('source_required',[]))
                    if arm=='on':
                        observed={str(row['id']) for row in context.get('self_state',[])}
                        updated={str(row['id']) for row in arm_data.get('reflective_states',[])}
                        entry['checks']['new_state_in_actual_context']=not updated or bool(observed.intersection(updated))
                    with repo.pool.connection() as c:
                        arm_data['calls']=c.execute('SELECT phase,status,input_tokens,output_tokens,reflection_run_id FROM model_calls ORDER BY created_at,id').fetchall()
                entry['checks']['same_snapshot']=snapshots==[snapshot,snapshot]
                changed=bool(entry['arms']['on'].get('reflective_states'))
                entry['comparison']={'state_change':'changed' if changed else 'unchanged',
                    'quality':'requires_review' if args.real_model else 'mock_not_quality_evidence',
                    'note':'形成更多状态并不自动表示回答改善；回答与来源供逐例复核。'}
                entry['passed']=all(entry['checks'].values())
                if not entry['passed']:
                    report['failures'].append({'case':case['id'],'failed_checks':[key for key,value in entry['checks'].items() if not value]})
            except Exception as exc:
                entry['error']=error_record(exc)
                report['failures'].append({'case':case['id'],**entry['error']})
            finally:
                for repo in reversed(repositories):
                    try:
                        LabManager._drop(repo,repo.settings.db_schema)
                    except Exception as exc:
                        report['failures'].append({'case':case['id'],'stage':'cleanup',**error_record(exc)})
                    finally:
                        repo.close()
                report['cases'].append(entry)
                print(f"v0.5: {case['id']} {'passed' if entry['passed'] else 'failed'} ({budget.calls}/{request_limit} live requests)",file=sys.stderr,flush=True)
    report.update(cloud_calls=budget.calls,finished_at=datetime.now(timezone.utc).isoformat(),
        passed=not report['failures'],temporary_schemas_removed=not any(item.get('stage')=='cleanup' for item in report['failures']))
    if ledger:report['cumulative_budget']=ledger.snapshot()
    print(json.dumps(report,ensure_ascii=False,default=str))
    return 0 if report['passed'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside',action='store_true')
    parser.add_argument('--real-model',action='store_true')
    parser.add_argument('--local-vectors',action='store_true')
    parser.add_argument('--cases',help='Optional comma-separated frozen case IDs for diagnostic reruns')
    parser.add_argument('--cumulative-call-limit',type=int,help=(
        'Explicitly approved ALL-HISTORY total ceiling, persisted across runs. '
        'Omit to retain the existing ceiling (initially 96); never a per-run allowance.'))
    parser.add_argument('--verification-run-id',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.cases and (not set(args.cases.split(','))<=set(case['id'] for case in CASES)):
        parser.error('--cases contains an unknown frozen case ID')
    if args.cumulative_call_limit is not None and (not args.real_model or args.cumulative_call_limit<1 or args.inside):
        parser.error('--cumulative-call-limit requires a positive explicit host --real-model total')
    if args.inside:
        if args.real_model and not args.verification_run_id:parser.error('real inside mode requires the host run identity and mounted ledger')
        args.verification_run_id=args.verification_run_id or str(uuid4())
        return inside(args)
    root=Path(__file__).resolve().parent.parent
    args.verification_run_id=str(uuid4())
    ledger=None
    if args.real_model:
        ledger=PersistentCallLedger(root/LEDGER_DIRECTORY/LEDGER_NAME,create=True)
        ledger.backfill(historical_reports(root))
        if args.cumulative_call_limit is not None:ledger.set_explicit_limit(args.cumulative_call_limit)
        ledger.require_known_history()
        summary=ledger.snapshot()
        (root/'docs'/'verification-v0.5-budget.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        try:ledger.assert_available()
        except BudgetLimitError as exc:
            # Preserve the latest real verification verbatim. A blocked launch
            # gets its own report instead of replacing model-quality evidence.
            report={'version':'0.5','verification_run_id':args.verification_run_id,
                'started_at':datetime.now(timezone.utc).isoformat(),'real_model_requested':True,
                'fixture_sha256':FROZEN_SHA256,'passed':False,'cloud_calls':0,'cumulative_budget':summary,
                'failures':[{'stage':'cumulative_budget',**error_record(exc)}]}
            destination=root/'docs'/'verification-history'/f'verification-v0.5-budget-blocked-{args.verification_run_id}.json'
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'passed':False,'cloud_calls':0,'cumulative_budget':summary,'report':str(destination)},ensure_ascii=False))
            return 1
    command=['docker','compose','run','--rm','-T','--no-deps','-v',str(root/'scripts')+':/verification:ro']
    if ledger:command.extend(['-v',str(root/LEDGER_DIRECTORY)+':/verification-budget'])
    command.extend(['api','python','/verification/verify_reflection_v5.py','--inside',
                    '--verification-run-id',args.verification_run_id])
    if args.real_model:command.append('--real-model')
    if args.local_vectors:command.append('--local-vectors')
    if args.cases:command.extend(['--cases',args.cases])
    result=subprocess.run(command,cwd=root,stdout=subprocess.PIPE,text=True,check=False)
    try:report=json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError,IndexError):
        report={'version':'0.5','verification_run_id':args.verification_run_id,
            'started_at':datetime.now(timezone.utc).isoformat(),'real_model_requested':args.real_model,
            'fixture_sha256':FROZEN_SHA256,'passed':False,'cloud_calls':ledger.run_calls(args.verification_run_id) if ledger else 0,
            'failures':[{'stage':'container','exit_code':result.returncode}]}
    if ledger:
        report['cumulative_budget']=ledger.snapshot()
        report['cloud_calls']=ledger.run_calls(args.verification_run_id)
        (root/'docs'/'verification-v0.5-budget.json').write_text(json.dumps(report['cumulative_budget'],ensure_ascii=False,indent=2)+'\n')
    report['process_exit_code']=result.returncode
    destination=root/'docs'/('verification-v0.5.json' if args.real_model else 'verification-v0.5-mock.json')
    preserve_report(destination)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report.get('passed',False),'failures':len(report.get('failures',[])),
        'cloud_calls':report.get('cloud_calls',0),'cumulative_budget':report.get('cumulative_budget'),
        'report':str(destination)},ensure_ascii=False))
    return 0 if result.returncode==0 and report.get('passed') else 1


if __name__=='__main__':sys.exit(main())
