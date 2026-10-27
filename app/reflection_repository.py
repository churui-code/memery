"""Bounded background interpretations with source fencing and real-time quotas."""
from datetime import datetime, timedelta
import json
import re
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from psycopg.types.json import Jsonb
from app.context import content_hash
from app.errors import MemoryError
from app.life import SelfUpdate, observation_view
from app.reflection_types import ReflectionUpdate, validate_reflection_proposal, validate_reflection_review


_DOMAINS={'emotion','relationship','identity','belief','goal'}
_HASH=re.compile(r'^[0-9a-f]{64}$')


def _json(value):
    return json.loads(json.dumps(value,default=str))


def _identifier(value):
    return str(UUID(str(value)))


def _audit(value):
    return {key:count for key,count in (value or {}).items()
            if key in ('proposed','accepted','rejected','calls') and type(count) is int and 0<=count<=1000}


def _manifest(value):
    """Keep only source identifiers, versions, hashes and verified coordinates."""
    if not isinstance(value,list) or len(value)>1000:
        raise ValueError('invalid_reflection_manifest')
    result=[]
    for original in value:
        if original.get('source_type') not in ('episode','experience','self'):
            raise ValueError('invalid_reflection_source_type')
        item={'source_type':original['source_type'],'id':_identifier(original['id'])}
        revision=original.get('revision')
        if type(revision) is not int or revision<1:
            raise ValueError('invalid_reflection_source_revision')
        item['revision']=revision
        for key in ('hash','source_hash'):
            if key in original:
                if not isinstance(original[key],str) or not _HASH.fullmatch(original[key]):
                    raise ValueError('invalid_reflection_source_hash')
                item[key]=original[key]
        if 'as_of' in original:
            at=datetime.fromisoformat(str(original['as_of']))
            if at.tzinfo is None:raise ValueError('invalid_reflection_as_of')
            item['as_of']=at.isoformat()
        if 'pending' in original:
            item['pending']=bool(original['pending'])
        if 'level' in original:
            if original['level'] not in ('cue','gist','fragment','full'):
                raise ValueError('invalid_reflection_level')
            item['level']=original['level']
        if 'evidence_ids' in original:
            item['evidence_ids']=list(dict.fromkeys(_identifier(eid) for eid in original['evidence_ids']))
        if 'spans' in original:item['spans']=[]
        for raw in original.get('spans',[]):
            span={}
            for key in ('id','evidence_id','experience_id'):
                if key in raw:span[key]=_identifier(raw[key])
            for key in ('start','end','source_revision'):
                if key in raw:
                    if type(raw[key]) is not int or raw[key]<0:
                        raise ValueError('invalid_reflection_span')
                    span[key]=raw[key]
            if 'role' in raw:
                if raw['role'] not in ('user','assistant'):raise ValueError('invalid_reflection_role')
                span['role']=raw['role']
            for key in ('hash','source_hash'):
                if key in raw:
                    if not isinstance(raw[key],str) or not _HASH.fullmatch(raw[key]):
                        raise ValueError('invalid_reflection_span_hash')
                    span[key]=raw[key]
            item['spans'].append(span)
        result.append(item)
    return result


class ReflectionRepository:
    @staticmethod
    def _reflection_wall_time(c):
        return c.execute('SELECT clock_timestamp() AS at').fetchone()['at']

    @staticmethod
    def _reflection_day(at):
        return at.astimezone(ZoneInfo('Asia/Shanghai')).date()

    def record_chat_activity(self):
        with self.pool.connection() as c:
            c.execute('''UPDATE reflection_settings SET last_chat_activity_at=clock_timestamp(),
                last_chat_business_at=%s,updated_at=clock_timestamp() WHERE id=1''',(self.clock.now(),))

    def _reflection_pending(self,c,limit=None):
        query='''SELECT e.id,e.revision,e.source_hash,e.importance,e.reflection_correction FROM episodes e
            LEFT JOIN reflection_processed_episodes p ON p.episode_id=e.id
            WHERE e.deleted_at IS NULL AND NOT e.context_stale AND e.allow_cloud
            AND (p.episode_id IS NULL OR p.revision<>e.revision OR p.source_hash<>e.source_hash)
            AND NOT EXISTS(SELECT 1 FROM episode_evidence v JOIN experiences x ON x.id=v.experience_id
                WHERE v.episode_id=e.id AND (x.deleted_at IS NOT NULL OR x.context_stale OR NOT x.allow_cloud))
            ORDER BY e.reflection_correction DESC,(e.importance>=.8) DESC,e.recorded_at,e.id'''
        return c.execute(query+(' LIMIT %s' if limit is not None else ''),
                         (limit,) if limit is not None else ()).fetchall()

    def reflection_candidates(self,limit=12):
        with self.pool.connection() as c:
            return [self._episode_view(c,row['id']) for row in self._reflection_pending(c,min(max(limit,1),24))]

    def reflection_states(self,ids=None):
        with self.pool.connection() as c:
            if ids is None:
                rows=c.execute('''SELECT * FROM (SELECT DISTINCT ON(domain,state_key) * FROM self_observations
                    ORDER BY domain,state_key,revision DESC) s
                    WHERE deleted_at IS NULL AND invalidated_at IS NULL ORDER BY domain,state_key''').fetchall()
            else:
                rows=c.execute('''SELECT * FROM self_observations WHERE id=ANY(%s::uuid[])
                    AND deleted_at IS NULL AND invalidated_at IS NULL ORDER BY domain,state_key''',(list(ids),)).fetchall()
            return [observation_view(row,at=self.clock.now()) for row in rows]

    def reflection_targets(self):
        """Invalidated interpretations remain revision targets, never evidence."""
        with self.pool.connection() as c:
            rows=c.execute('''SELECT id,domain,state_key AS key,revision FROM (
                SELECT DISTINCT ON(domain,state_key) * FROM self_observations
                ORDER BY domain,state_key,revision DESC) s
                WHERE deleted_at IS NULL ORDER BY domain,state_key''').fetchall()
            blocked=self._private_reflection_targets(c,[row['id'] for row in rows])
            return [row for row in rows if row['id'] not in blocked]

    @staticmethod
    def _private_reflection_targets(c,ids):
        if not ids:return set()
        # A key is exposed metadata too. Invalidated text may remain a revision
        # target after correction, but private or erased ancestry cannot cross
        # the cloud boundary. UNION also terminates cyclic dependency graphs.
        rows=c.execute('''WITH RECURSIVE ancestors(origin,kind,id) AS (
            SELECT id,'self'::text,id FROM unnest(%s::uuid[]) AS roots(id)
            UNION
            SELECT a.origin,l.source_type,l.source_id FROM ancestors a
            JOIN context_links l ON l.target_type=a.kind AND l.target_id=a.id
        ) SELECT DISTINCT a.origin FROM ancestors a
        LEFT JOIN memories m ON a.kind='memory' AND m.id=a.id
        LEFT JOIN experiences e ON a.kind='experience' AND e.id=a.id
        LEFT JOIN episodes p ON a.kind='episode' AND p.id=a.id
        LEFT JOIN self_observations s ON a.kind='self' AND s.id=a.id
        WHERE (a.kind='memory' AND (m.id IS NULL OR m.deleted_at IS NOT NULL OR NOT m.allow_cloud))
            OR (a.kind='experience' AND (e.id IS NULL OR e.deleted_at IS NOT NULL OR NOT e.allow_cloud))
            OR (a.kind='episode' AND (p.id IS NULL OR p.deleted_at IS NOT NULL OR NOT p.allow_cloud))
            OR (a.kind='self' AND (s.id IS NULL OR s.deleted_at IS NOT NULL))''',(ids,)).fetchall()
        return {row['origin'] for row in rows}

    def _validate_reflection_targets(self,c,targets):
        seen=set()
        for target in targets:
            key=(target['domain'],target['key'])
            if key in seen:raise ValueError('duplicate_reflection_target')
            seen.add(key)
            row=c.execute('''SELECT id,revision,deleted_at FROM self_observations
                WHERE domain=%s AND state_key=%s ORDER BY revision DESC LIMIT 1''',key).fetchone()
            if row and row['deleted_at']:
                raise MemoryError('reflection_key_deleted','已删除的内部认识不能由后台整理重新建立。',409)
            if ((row and (str(row['id'])!=target['id'] or row['revision']!=target['revision']))
                or (not row and (target['id'] is not None or target['revision']!=0))):
                raise MemoryError('reflection_target_changed','内部认识目标版本已变化。',409)
        if self._private_reflection_targets(c,[target['id'] for target in targets if target['id']]):
            raise MemoryError('reflection_source_changed','内部认识目标的来源不允许云端处理。',409)

    def _reflection_settings(self,c,automatic=False):
        at=self._reflection_wall_time(c)
        day=self._reflection_day(at)
        business_at=self.clock.now()
        business_day=self._reflection_day(business_at)
        settings=c.execute('SELECT * FROM reflection_settings WHERE id=1').fetchone()
        usage=c.execute('SELECT * FROM reflection_daily_usage WHERE day=%s',(day,)).fetchone() or {'rounds':0,'calls':0}
        pending=self._reflection_pending(c)
        active=c.execute("SELECT id FROM reflection_runs WHERE status IN ('pending','running') AND (status='pending' OR lease_until>%s)",(at,)).fetchone()
        common=[]
        if not pending:common.append('no_pending_evidence')
        if usage['rounds']>=2:common.append('daily_round_limit')
        if usage['calls']>2:common.append('daily_call_limit')
        if active:common.append('reflection_busy')
        auto=list(common)
        if not settings['enabled']:auto.append('disabled')
        if settings['last_chat_business_at'] and business_at-settings['last_chat_business_at']<timedelta(minutes=15):
            auto.append('chat_not_idle')
        if settings['last_started_business_at'] and business_at-settings['last_started_business_at']<timedelta(hours=6):
            auto.append('minimum_spacing')
        daily=business_at.astimezone(ZoneInfo('Asia/Shanghai')).hour>=3 and settings['last_daily_business_date']!=business_day
        important=any(row['importance']>=.8 or row['reflection_correction'] for row in pending)
        if not daily and not important:auto.append('not_due')
        trigger_reasons=[]
        if any(row['importance']>=.8 for row in pending):trigger_reasons.append('important_event')
        if any(row['reflection_correction'] for row in pending):trigger_reasons.append('verified_correction')
        if daily and pending:trigger_reasons.append('daily_schedule')
        local=business_at.astimezone(ZoneInfo('Asia/Shanghai'))
        next_due=business_at
        if not important and not daily:
            next_due=local.replace(hour=3,minute=0,second=0,microsecond=0)
            if next_due<=business_at:next_due+=timedelta(days=1)
        for previous,delay in ((settings['last_chat_business_at'],timedelta(minutes=15)),
                               (settings['last_started_business_at'],timedelta(hours=6))):
            if previous:next_due=max(next_due,previous+delay)
        quota_reset=(at.astimezone(ZoneInfo('Asia/Shanghai'))+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        if (usage['rounds']>=2 or usage['calls']>2) and not self.settings.db_schema.startswith('lab_'):
            next_due=max(next_due,quota_reset)
        reasons=auto if automatic else common
        return {**settings,'eligible':not reasons,'reasons':reasons,
                'manual_eligible':not common,'manual_reasons':common,
                'automatic_eligible':not auto,'automatic_reasons':auto,
                'pending_count':len(pending),'quota':{'day':day,'rounds_used':usage['rounds'],
                    'calls_used':usage['calls'],'round_limit':2,'call_limit':4,'resets_at':quota_reset},
                'active_run_id':active['id'] if active else None,
                'next_due_at':next_due if pending and settings['enabled'] else None,'trigger_reasons':trigger_reasons}

    def reflection_settings(self,automatic=True):
        with self.pool.connection() as c:
            return self._reflection_settings(c,automatic)

    def set_reflection_enabled(self,enabled):
        if type(enabled) is not bool:raise ValueError('invalid_reflection_enabled')
        with self.pool.connection() as c:
            self._state_lock(c)
            c.execute('UPDATE reflection_settings SET enabled=%s,updated_at=clock_timestamp() WHERE id=1',(enabled,))
            if not enabled:
                c.execute("""UPDATE reflection_runs SET status='cancelled',error_code='disabled',
                    lease_token=NULL,lease_until=NULL,finished_at=clock_timestamp()
                    WHERE automatic AND status IN ('pending','running')""")
                self._interrupt_reflection_calls(c)
        return self.reflection_settings()

    def reflection_run(self,run_id):
        with self.pool.connection() as c:
            row=c.execute('SELECT * FROM reflection_runs WHERE id=%s',(run_id,)).fetchone()
            if not row:raise MemoryError('not_found','整理任务不存在。',404)
            return self._reflection_run_view(c,row)

    @staticmethod
    def _reflection_run_view(c,row):
        calls=c.execute('''SELECT id,phase,status,input_tokens,output_tokens,estimated_input_tokens,
            policy_version,estimator_version,created_at FROM model_calls WHERE reflection_run_id=%s
            ORDER BY CASE WHEN phase='reflection_proposal' THEN 0 ELSE 1 END,id''',(row['id'],)).fetchall()
        usage={key:sum(call[key] for call in calls) if calls and all(call[key] is not None for call in calls) else None
               for key in ('input_tokens','output_tokens')}
        return {**row,'calls':calls,'cloud_calls':len(calls),'usage':usage}

    def reflection_runs(self,limit=50,offset=0):
        with self.pool.connection() as c:
            rows=c.execute('SELECT * FROM reflection_runs ORDER BY created_at DESC,id LIMIT %s OFFSET %s',
                           (min(max(limit,1),200),max(offset,0))).fetchall()
            return [self._reflection_run_view(c,row) for row in rows]

    def _validate_reflection_sources(self,c,manifest):
        by_kind={kind:{row['id'] for row in manifest if row['source_type']==kind}
                 for kind in ('episode','experience','self')}
        for ref in manifest:
            kind=ref['source_type']
            table={'episode':'episodes','experience':'experiences','self':'self_observations'}[kind]
            row=c.execute(f'SELECT * FROM {table} WHERE id=%s',(ref['id'],)).fetchone()
            if not row or row['deleted_at'] or row['revision']!=ref['revision']:
                raise MemoryError('reflection_source_changed','整理依据已变更，请重新选择。',409)
            if kind=='self':
                if row['invalidated_at']:
                    raise MemoryError('reflection_source_changed','内部认识依据已失效。',409)
                continue
            if row['context_stale'] or not row['allow_cloud'] or ref.get('source_hash')!=row['source_hash']:
                raise MemoryError('reflection_source_changed','整理依据已变更或不允许云端处理。',409)
            if kind=='episode':
                evidence=c.execute('SELECT * FROM episode_evidence WHERE episode_id=%s',(row['id'],)).fetchall()
                if not {str(e['experience_id']) for e in evidence}.issubset(by_kind['experience']):
                    raise MemoryError('reflection_source_missing','整理缺少原始来源。',409)
                if not set(ref.get('evidence_ids',[])).issubset({str(e['id']) for e in evidence}):
                    raise MemoryError('reflection_source_changed','整理引文已变更。',409)
            else:
                for span in ref.get('spans',[]):
                    if not {'role','start','end'}.issubset(span):continue
                    text=row[span['role']+'_text']
                    if not 0<=span['start']<span['end']<=len(text):
                        raise MemoryError('reflection_source_changed','整理引文位置已变更。',409)
                    if (span.get('source_revision',row['revision'])!=row['revision']
                        or span.get('source_hash',row['source_hash'])!=row['source_hash']
                        or (span.get('hash') and span['hash']!=content_hash(text[span['start']:span['end']]))):
                        raise MemoryError('reflection_source_changed','整理引文已变更。',409)

    def _expire_reflections(self,c,at):
        c.execute("""UPDATE reflection_runs SET status='failed',error_code='lease_expired',finished_at=%s,
            lease_token=NULL,lease_until=NULL WHERE status='running' AND lease_until<=%s""",(at,at))
        self._interrupt_reflection_calls(c)

    @staticmethod
    def _interrupt_reflection_calls(c):
        c.execute('''UPDATE model_calls m SET status='interrupted' FROM reflection_runs r
            WHERE m.reflection_run_id=r.id AND m.status='running'
            AND r.status IN ('failed','cancelled')''')

    def enqueue_reflection(self,manifest,expected_epoch,target_versions,input_signature,automatic=False,
                           pending_ids=None,policy_version='reflection-v1',model_mode='real'):
        if model_mode not in ('real','mock') or (model_mode=='mock' and not
                (self.settings.lab_enabled and self.settings.db_schema.startswith('lab_'))):
            raise ValueError('invalid_reflection_model_mode')
        manifest=_manifest(manifest)
        if not isinstance(input_signature,str) or not _HASH.fullmatch(input_signature):
            raise ValueError('invalid_reflection_signature')
        if not isinstance(policy_version,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',policy_version):
            raise ValueError('invalid_reflection_policy')
        targets=[]
        for target in target_versions:
            key=target.get('key',target.get('state_key'))
            if target['domain'] not in _DOMAINS or not isinstance(key,str) or not key.strip() or len(key)>100:
                raise ValueError('invalid_reflection_target')
            if type(target['revision']) is not int or target['revision']<0:
                raise ValueError('invalid_reflection_target_revision')
            targets.append({'domain':target['domain'],'key':key,'revision':target['revision'],
                            'id':_identifier(target['id']) if target.get('id') else None})
        pending_ids=list(dict.fromkeys(_identifier(eid) for eid in (pending_ids or [])))
        with self.pool.connection() as c:
            epoch=self._state_lock(c)
            at=self._reflection_wall_time(c)
            self._expire_reflections(c,at)
            previous=c.execute("SELECT * FROM reflection_runs WHERE input_signature=%s AND policy_version=%s AND status='completed' AND invalidated_at IS NULL",(input_signature,policy_version)).fetchone()
            if previous:return previous
            active=c.execute("SELECT * FROM reflection_runs WHERE status IN ('pending','running')").fetchone()
            if active and active['input_signature']==input_signature and active['policy_version']==policy_version:return active
            if epoch!=expected_epoch:
                raise MemoryError('context_changed','整理前记忆已变更。',409)
            state=self._reflection_settings(c,automatic)
            if model_mode=='mock':
                state['reasons']=[reason for reason in state['reasons'] if reason not in ('daily_round_limit','daily_call_limit')]
                state['eligible']=not state['reasons']
            if not state['eligible']:
                raise MemoryError('reflection_not_eligible','当前不符合整理条件：'+','.join(state['reasons']),409)
            self._validate_reflection_sources(c,manifest)
            self._validate_reflection_targets(c,targets)
            available={str(row['id']) for row in self._reflection_pending(c)}
            represented={row['id'] for row in manifest if row['source_type']=='episode'}
            if not pending_ids or not set(pending_ids).issubset(available&represented):
                raise MemoryError('reflection_source_changed','整理必须包含尚未处理的事件。',409)
            run_id=uuid4()
            triggers=list(state['trigger_reasons'])+([] if automatic else ['manual'])
            return c.execute('''INSERT INTO reflection_runs(id,status,automatic,input_signature,policy_version,
                expected_epoch,source_manifest,target_versions,pending_ids,business_at,created_at,model_mode,trigger_reasons)
                VALUES(%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (run_id,automatic,input_signature,policy_version,expected_epoch,Jsonb(manifest),Jsonb(targets),
                 Jsonb(pending_ids),self.clock.now(),at,model_mode,Jsonb(triggers))).fetchone()

    def claim_reflection(self,run_id=None):
        with self.pool.connection() as c:
            self._state_lock(c)
            at=self._reflection_wall_time(c)
            self._expire_reflections(c,at)
            row=c.execute("""SELECT * FROM reflection_runs WHERE status='pending'
                AND (%s::uuid IS NULL OR id=%s) ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (run_id,run_id)).fetchone()
            if not row:return None
            if row['automatic'] and not c.execute('SELECT enabled FROM reflection_settings WHERE id=1').fetchone()['enabled']:
                c.execute("UPDATE reflection_runs SET status='cancelled',error_code='disabled',finished_at=%s WHERE id=%s",(at,row['id']))
                return None
            try:
                if self._state_lock(c)!=row['expected_epoch']:
                    raise MemoryError('context_changed','整理任务依据已过期。',409)
                self._validate_reflection_sources(c,row['source_manifest'])
                self._validate_reflection_targets(c,row['target_versions'])
            except MemoryError as exc:
                c.execute("UPDATE reflection_runs SET status='cancelled',error_code=%s,finished_at=%s WHERE id=%s",(exc.code,at,row['id']))
                return None
            if row['automatic']:
                state=self._reflection_settings(c,True)
                reasons=[reason for reason in state['reasons'] if reason not in ('reflection_busy','daily_round_limit','daily_call_limit')]
                if reasons:
                    c.execute("UPDATE reflection_runs SET status='cancelled',error_code='automatic_not_due',finished_at=%s WHERE id=%s",(at,row['id']))
                    return None
            day=self._reflection_day(at)
            c.execute('INSERT INTO reflection_daily_usage(day) VALUES(%s) ON CONFLICT DO NOTHING',(day,))
            quota=c.execute('SELECT * FROM reflection_daily_usage WHERE day=%s FOR UPDATE',(day,)).fetchone()
            if row['model_mode']=='real' and (quota['rounds']>=2 or quota['calls']>2):
                raise MemoryError('reflection_daily_limit','今日整理请求已达上限。',429)
            if row['model_mode']=='real':
                c.execute('UPDATE reflection_daily_usage SET rounds=rounds+1 WHERE day=%s',(day,))
            c.execute('UPDATE reflection_settings SET last_started_at=%s,last_started_business_at=%s WHERE id=1',(at,self.clock.now()))
            return c.execute("""UPDATE reflection_runs SET status='running',started_at=%s,quota_day=%s,
                lease_token=%s,lease_until=%s WHERE id=%s RETURNING *""",
                (at,day,uuid4(),at+timedelta(seconds=300),row['id'])).fetchone()

    def _reflection_lease(self,c,job):
        row=c.execute('SELECT * FROM reflection_runs WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
        at=self._reflection_wall_time(c)
        if not row or row['status']!='running' or row['lease_token']!=job.get('lease_token') or row['lease_until']<=at:
            raise MemoryError('reflection_lease_lost','整理任务租约已失效。',409)
        if self._state_lock(c)!=row['expected_epoch']:
            raise MemoryError('context_changed','整理期间记忆已改变。',409)
        self._validate_reflection_sources(c,row['source_manifest'])
        self._validate_reflection_targets(c,row['target_versions'])
        return row,at

    def start_reflection_call(self,job,phase,estimated_input_tokens=None,estimator_version=None):
        if not isinstance(phase,str) or not re.fullmatch(r'[a-z_]{1,64}',phase):
            raise ValueError('invalid_reflection_phase')
        with self.pool.connection() as c:
            self._state_lock(c)
            row,at=self._reflection_lease(c,job)
            if row['model_mode']!='real':raise ValueError('mock_has_no_cloud_call')
            if c.execute('SELECT count(*) AS n FROM model_calls WHERE reflection_run_id=%s',(row['id'],)).fetchone()['n']>=2:
                raise MemoryError('reflection_call_limit','本次整理已达到两次请求上限。',429)
            day=self._reflection_day(at)
            c.execute('INSERT INTO reflection_daily_usage(day) VALUES(%s) ON CONFLICT DO NOTHING',(day,))
            quota=c.execute('SELECT * FROM reflection_daily_usage WHERE day=%s FOR UPDATE',(day,)).fetchone()
            if quota['calls']>=4:raise MemoryError('reflection_daily_limit','今日整理请求已达上限。',429)
            c.execute('UPDATE reflection_daily_usage SET calls=calls+1 WHERE day=%s',(day,))
            call_id=uuid4()
            source_refs=row['source_manifest']+[
                {'source_type':'self','id':target['id'],'revision':target['revision'],
                 'metadata_only':True,'hash':content_hash(target)}
                for target in row['target_versions'] if target['id']]
            c.execute('''INSERT INTO model_calls(id,conversation_id,source_ids,model,status,phase,source_refs,
                estimated_input_tokens,policy_version,created_at,estimator_version,reflection_run_id)
                VALUES(%s,NULL,'{}',%s,'running',%s,%s,%s,%s,%s,%s,%s)''',
                (call_id,self.settings.model_name,phase,Jsonb(source_refs),estimated_input_tokens,
                 row['policy_version'],self.clock.now(),estimator_version,row['id']))
            return call_id

    def finish_reflection_step(self,job,phase):
        if phase not in ('reflection_proposal','reflection_review'):
            raise ValueError('invalid_reflection_phase')
        with self.pool.connection() as c:
            self._state_lock(c)
            row,_=self._reflection_lease(c,job)
            if row['model_mode']!='mock':raise ValueError('real_step_requires_call_audit')
            steps=row['completed_steps']
            if phase in steps:return
            if phase=='reflection_review' and steps!=['reflection_proposal']:
                raise ValueError('reflection_review_before_proposal')
            c.execute('UPDATE reflection_runs SET completed_steps=%s WHERE id=%s',(Jsonb(steps+[phase]),row['id']))

    def fail_reflection(self,job,code,status='failed'):
        if status not in ('failed','cancelled'):raise ValueError('invalid_reflection_status')
        code=code if isinstance(code,str) and re.fullmatch(r'[a-z0-9_]{1,100}',code) else 'reflection_failed'
        with self.pool.connection() as c:
            self._state_lock(c)
            c.execute('''UPDATE reflection_runs SET status=%s,error_code=%s,audit=%s,finished_at=clock_timestamp(),
                lease_token=NULL,lease_until=NULL WHERE id=%s AND status IN ('pending','running')
                AND (lease_token IS NULL OR lease_token=%s)''',
                (status,code,Jsonb(_audit(job.get('audit'))),job['id'],job.get('lease_token')))
            # Source deletion or restore may already have cancelled this run.
            # Its late worker may add counters, but cannot alter the terminal state.
            c.execute("""UPDATE reflection_runs SET audit=%s WHERE id=%s
                AND status IN ('failed','cancelled') AND audit='{}'::jsonb""",
                (Jsonb(_audit(job.get('audit'))),job['id']))
            self._interrupt_reflection_calls(c)
        return self.reflection_run(job['id'])

    @staticmethod
    def _validate_reflection_submission(c,run,updates,review,audit,phases):
        # Reconstruct only the validation inputs from frozen persisted metadata.
        # Neither the original proposal nor the review notes are stored here.
        payload={'episodes':[], 'self_state':[], 'target_versions':run['target_versions']}
        for ref in run['source_manifest']:
            if ref['source_type']=='episode':
                payload['episodes'].append({'pending':ref['id'] in run['pending_ids'],
                    'evidence':[{'id':eid} for eid in ref.get('evidence_ids',[])]})
            elif ref['source_type']=='self' and ref.get('as_of'):
                row=c.execute('SELECT * FROM self_observations WHERE id=%s',(ref['id'],)).fetchone()
                payload['self_state'].append(observation_view(row,at=datetime.fromisoformat(ref['as_of'])))
        original=[raw.model_dump(mode='python') if hasattr(raw,'model_dump') else dict(raw) for raw in updates]
        proposal=validate_reflection_proposal({'updates':[
            {key:data[key] for key in ReflectionUpdate.model_fields if key in data} for data in original]},payload)
        if proposal.updates:
            checked=validate_reflection_review(review,proposal)
            if any(not verdict.accept for verdict in checked.reviews):
                raise MemoryError('reflection_review_rejected','复核未全部通过，本轮整理不提交。',409)
        elif review is not None:
            raise MemoryError('reflection_review_invalid','空提案不能附带其他提案的复核。',502)
        if any(audit.get(key)!=value for key,value in {'proposed':len(proposal.updates),
                'accepted':len(proposal.updates),'rejected':0,'calls':len(phases)}.items()):
            raise MemoryError('reflection_audit_mismatch','整理计数与提交及复核不一致。',409)

    def save_reflection(self,job,updates,review=None):
        with self.pool.connection() as c:
            self._state_lock(c)
            existing=c.execute('SELECT * FROM reflection_runs WHERE id=%s',(job['id'],)).fetchone()
            if existing and existing['status']=='completed':return existing
            run,at=self._reflection_lease(c,job)
            audit=_audit(job.get('audit'))
            if audit.get('rejected',0)>0:
                raise MemoryError('reflection_review_rejected','复核未全部通过，本轮整理不提交。',409)
            calls=c.execute("""SELECT phase,status FROM model_calls WHERE reflection_run_id=%s
                ORDER BY CASE WHEN phase='reflection_proposal' THEN 0 ELSE 1 END,id""",(run['id'],)).fetchall()
            phases=[row['phase'] for row in calls] if run['model_mode']=='real' else run['completed_steps']
            expected=['reflection_proposal'] if audit.get('proposed')==0 and not updates else ['reflection_proposal','reflection_review']
            if phases!=expected or (run['model_mode']=='real' and any(row['status']=='running' or not (row['status']=='completed' or row['status'].endswith('_completed')) for row in calls)):
                raise MemoryError('reflection_review_missing','整理必须完成提案与复核后才能提交。',409)
            allowed_evidence={eid for ref in run['source_manifest'] if ref['source_type']=='episode' for eid in ref.get('evidence_ids',[])}
            pending_evidence={eid for ref in run['source_manifest'] if ref['source_type']=='episode' and ref['id'] in run['pending_ids'] for eid in ref.get('evidence_ids',[])}
            target_versions={(t['domain'],t['key']):t for t in run['target_versions']}
            roots=[(ref['source_type'],UUID(ref['id'])) for ref in run['source_manifest']]
            roots.extend(('self',UUID(target['id'])) for target in run['target_versions'] if target['id'])
            changes=[]
            seen=set()
            for raw in updates:
                data=raw.model_dump(mode='python') if hasattr(raw,'model_dump') else dict(raw)
                key=(data['domain'],data['key'])
                if key in seen:raise ValueError('duplicate_reflection_key')
                seen.add(key)
                previous=c.execute('''SELECT * FROM self_observations WHERE domain=%s AND state_key=%s
                    ORDER BY revision DESC LIMIT 1''',key).fetchone()
                if previous and previous['deleted_at']:
                    raise MemoryError('reflection_key_deleted','已删除的内部认识不能由后台整理重新建立。',409)
                expected=data.get('expected_revision',0)
                frozen=target_versions.get(key)
                if type(expected) is not int or expected!=(previous['revision'] if previous else 0):
                    raise MemoryError('reflection_target_changed','内部认识版本已变化。',409)
                if previous and (not frozen or frozen['revision']!=expected or frozen['id']!=str(previous['id']) or str(data.get('target_id'))!=str(previous['id'])):
                    raise MemoryError('reflection_target_changed','内部认识目标不在冻结依据中。',409)
                if not previous and (data.get('target_id') or expected):
                    raise MemoryError('reflection_target_changed','新认识不能引用不存在的目标。',409)
                evidence_ids=list(dict.fromkeys(_identifier(eid) for eid in data.get('evidence_ids',[])))
                if not evidence_ids or not set(evidence_ids).issubset(allowed_evidence) or not set(evidence_ids).intersection(pending_evidence):
                    raise MemoryError('reflection_evidence_invalid','每条认识必须引用本次新增的有效原文证据。',502)
                evidence=c.execute('SELECT * FROM episode_evidence WHERE id=ANY(%s::uuid[]) ORDER BY id',(evidence_ids,)).fetchall()
                if len(evidence)!=len(evidence_ids):raise MemoryError('reflection_source_changed','整理引文已失效。',409)
                quote='\n'.join(row['quote'] for row in evidence)
                proposal=SelfUpdate.model_validate({name:data[name] for name in
                    ('domain','key','text','reason','confidence','intensity','half_life_hours') if name in data}|{'quote':'已逐条验证的原始证据'})
                oid=uuid4()
                proof=[{'id':str(row['id']),'experience_id':str(row['experience_id']),'episode_id':str(row['episode_id']),
                        'role':row['role'],'start':row['start_offset'],'end':row['end_offset'],
                        'source_revision':row['source_revision'],'source_hash':row['source_hash']} for row in evidence]
                c.execute('''INSERT INTO self_observations(id,domain,state_key,revision,text,reason,evidence_quote,
                    confidence,intensity,half_life_hours,created_at,generation_type,reflection_run_id,evidence_manifest)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reflection',%s,%s)''',
                    (oid,proposal.domain,proposal.key,expected+1,proposal.text,proposal.reason,quote,
                     proposal.confidence,proposal.intensity,proposal.half_life_hours,self.clock.now(),run['id'],Jsonb(proof)))
                self._link(c,roots+([('self',previous['id'])] if previous and not previous['invalidated_at'] else []),'self',oid)
                changes.append(oid)
            self._validate_reflection_submission(c,run,updates,review,audit,phases)
            for eid in run['pending_ids']:
                episode=c.execute('SELECT revision,source_hash FROM episodes WHERE id=%s',(eid,)).fetchone()
                c.execute('''INSERT INTO reflection_processed_episodes(episode_id,revision,source_hash,run_id,processed_at)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(episode_id) DO UPDATE SET revision=EXCLUDED.revision,
                    source_hash=EXCLUDED.source_hash,run_id=EXCLUDED.run_id,processed_at=EXCLUDED.processed_at''',
                    (eid,episode['revision'],episode['source_hash'],run['id'],self.clock.now()))
            if changes:c.execute('UPDATE system_state SET epoch=epoch+1 WHERE id=1')
            c.execute('''UPDATE reflection_settings SET last_completed_at=%s,
                last_daily_date=CASE WHEN %s THEN %s ELSE last_daily_date END,
                last_daily_business_date=CASE WHEN %s THEN %s ELSE last_daily_business_date END WHERE id=1''',
                (at,run['automatic'],self._reflection_day(at),run['automatic'],self._reflection_day(self.clock.now())))
            completed=c.execute("""UPDATE reflection_runs SET status='completed',finished_at=%s,result_ids=%s,audit=%s,
                lease_token=NULL,lease_until=NULL WHERE id=%s RETURNING *""",(at,Jsonb([str(oid) for oid in changes]),Jsonb(audit),run['id'])).fetchone()
            return self._reflection_run_view(c,completed)

    def _invalidate_reflections(self,c,affected,erase=False):
        if not affected:return
        kinds=[row['kind'] for row in affected]
        ids=[str(row['id']) for row in affected]
        c.execute('''UPDATE reflection_runs r SET invalidated_at=COALESCE(invalidated_at,clock_timestamp()),
            status=CASE WHEN status IN ('pending','running') THEN 'cancelled' ELSE status END,
            lease_token=NULL,lease_until=NULL,error_code='source_invalidated',
            target_versions=CASE WHEN %s THEN '[]'::jsonb ELSE target_versions END
            WHERE EXISTS(SELECT 1 FROM jsonb_array_elements(r.source_manifest) ref
                JOIN unnest(%s::text[],%s::text[]) a(kind,id)
                ON ref->>'source_type'=a.kind AND ref->>'id'=a.id)
            OR EXISTS(SELECT 1 FROM jsonb_array_elements(r.target_versions) target
                JOIN unnest(%s::text[],%s::text[]) a(kind,id)
                ON a.kind='self' AND target->>'id'=a.id)''',(erase,kinds,ids,kinds,ids))
        self._interrupt_reflection_calls(c)

    def reset_reflections_after_restore(self,reason='restored'):
        reason=reason if reason in ('restored','cloned') else 'restored'
        with self.pool.connection() as c:
            self._state_lock(c)
            c.execute('UPDATE reflection_settings SET enabled=false,updated_at=clock_timestamp() WHERE id=1')
            changed=c.execute("""UPDATE reflection_runs SET status='cancelled',error_code=%s,
                lease_token=NULL,lease_until=NULL,finished_at=clock_timestamp()
                WHERE status IN ('pending','running')""",(reason,)).rowcount
            self._interrupt_reflection_calls(c)
            return changed
