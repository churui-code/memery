from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from uuid import uuid4
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from app.errors import MemoryError
from app.journal import DeletionJournal
from app.text import terms, ts_query, tokens
from app.life_repository import LifeRepository
from app.episode_repository import EpisodeRepository
from app.clock import SystemClock
from psycopg.types.json import Jsonb


def now():
    return datetime.now(timezone.utc)


def vector_literal(vector):
    if len(vector) != 512 or any(not math.isfinite(v) for v in vector):
        raise ValueError('invalid_embedding')
    return '['+','.join(str(float(v)) for v in vector)+']'


class Repository(LifeRepository, EpisodeRepository):
    def __init__(self, settings, clock=None):
        self.settings = settings
        self.clock = clock or SystemClock()
        if not re.fullmatch(r'[a-z][a-z0-9_]*', settings.db_schema):
            raise ValueError('invalid_schema')
        self.journal = DeletionJournal(settings.deletion_journal)
        self.pool = ConnectionPool(kwargs={
            'host': settings.db_host, 'dbname': settings.db_name, 'user': settings.db_user,
            'password': settings.db_password, 'options': f'-c search_path={settings.db_schema},public',
            'row_factory': dict_row, 'connect_timeout': 8}, min_size=1, max_size=8, timeout=10, open=True)
        self.pool.wait(timeout=15)

    def close(self):
        self.pool.close()

    def migrate(self):
        with self.pool.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(76915542)')
            c.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.settings.db_schema)))
            if not c.execute("SELECT to_regclass('schema_migrations') AS name").fetchone()['name']:
                c.execute(Path(__file__).with_name('schema.sql').read_text(), prepare=False)
            version = c.execute('SELECT max(version) AS v FROM schema_migrations').fetchone()['v']
            if version not in (1, 2, 3, 4, 5):
                raise MemoryError('schema_version', '数据库版本不受当前应用支持。', 503)
            for target in range(version+1,6):
                c.execute(Path(__file__).with_name(f'migration_{target:03}.sql').read_text(), prepare=False)
                if target==3:
                    from app.text import chunks
                    rows=c.execute('SELECT id,user_text,assistant_text FROM experiences WHERE legacy_message_id IS NOT NULL').fetchall()
                    for row in rows:
                        index_terms=' '.join(terms(part) for part in chunks(row['user_text']+'\n'+row['assistant_text']))
                        c.execute("UPDATE experiences SET terms=to_tsvector('simple',%s) WHERE id=%s",(index_terms,row['id']))

    @staticmethod
    def _state_lock(c):
        return c.execute('SELECT epoch FROM system_state WHERE id=1 FOR UPDATE').fetchone()['epoch']

    @staticmethod
    def _enqueue(c, memory_id, revision):
        c.execute('INSERT INTO outbox(memory_id,revision) VALUES(%s,%s) ON CONFLICT DO NOTHING', (memory_id, revision))

    def _view(self, c, memory_id, history=False):
        row = c.execute('SELECT * FROM memories WHERE id=%s', (memory_id,)).fetchone()
        if not row or row['deleted_at']:
            raise MemoryError('not_found', '记忆不存在或已删除。', 404)
        for key in ('terms','request_hash','idempotency_key'):
            row.pop(key, None)
        job = c.execute('SELECT status,last_error FROM outbox WHERE memory_id=%s AND revision=%s', (memory_id,row['revision'])).fetchone()
        row.update(storage_status='durable', index_status=job['status'] if job else 'missing', index_error=job['last_error'] if job else None)
        row['source'] = {'memory_id':row['id'], 'revision':row['revision'], 'speaker_id':row['speaker_id']}
        row['source']['evidence'] = c.execute('SELECT revision,conversation_id,quote,created_at FROM memory_evidence WHERE memory_id=%s AND revision=%s', (memory_id,row['revision'])).fetchone()
        if history:
            row['history'] = c.execute('SELECT revision,operation,content,created_at FROM events WHERE memory_id=%s ORDER BY revision', (memory_id,)).fetchall()
            row['evidence_history'] = c.execute('SELECT revision,conversation_id,quote,created_at FROM memory_evidence WHERE memory_id=%s ORDER BY revision', (memory_id,)).fetchall()
        return row

    def remember(self, data, idempotency_key=None):
        digest = sha256(json.dumps(data,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()
        at = self.clock.now()
        memory_id, start = uuid4(), data.get('valid_from') or at
        until = data.get('valid_until')
        if until and until <= start:
            raise MemoryError('invalid_validity','结束时间必须晚于生效时间。',422)
        with self.pool.connection() as c:
            self._state_lock(c)
            if idempotency_key:
                old = c.execute('SELECT * FROM memories WHERE idempotency_key=%s',(idempotency_key,)).fetchone()
                if old:
                    if old['deleted_at'] or old['request_hash'] != digest:
                        raise MemoryError('idempotency_conflict','此幂等键对应其他内容或已删除的记忆。',409)
                    return self._view(c,old['id']),False
            c.execute("""INSERT INTO memories(id,text,terms,speaker_id,subject,allow_cloud,valid_from,valid_until,idempotency_key,request_hash,created_at,updated_at)
                VALUES(%s,%s,to_tsvector('simple',%s),%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (memory_id,data['text'],terms(data['text']),data.get('speaker_id','user'),data.get('subject','user'),
                 data.get('allow_cloud',True),start,until,idempotency_key,digest,at,at))
            c.execute("INSERT INTO events(id,memory_id,revision,operation,content,created_at) VALUES(%s,%s,1,'create',%s,%s)",(uuid4(),memory_id,data['text'],at))
            self._enqueue(c,memory_id,1)
            result = self._view(c,memory_id)
        return result,True

    def get(self,memory_id,history=False):
        with self.pool.connection() as c:
            return self._view(c,memory_id,history)

    def listing(self,limit=20,offset=0):
        with self.pool.connection() as c:
            total=c.execute('SELECT count(*) AS n FROM memories WHERE deleted_at IS NULL').fetchone()['n']
            ids=c.execute('SELECT id FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC,id LIMIT %s OFFSET %s',(limit,offset)).fetchall()
            return {'total':total,'items':[self._view(c,r['id']) for r in ids],'limit':limit,'offset':offset}

    def correct(self,memory_id,expected_revision,changes):
        tokens.cache_clear()
        with self.pool.connection() as c:
            self._state_lock(c)
            result=self._correct(c,memory_id,expected_revision,changes)
            c.execute('UPDATE system_state SET epoch=epoch+1 WHERE id=1')
        return result

    def _correct(self,c,memory_id,expected_revision,changes,preserve_experiences=False):
        row=c.execute('SELECT * FROM memories WHERE id=%s FOR UPDATE',(memory_id,)).fetchone()
        if not row or row['deleted_at']:
            raise MemoryError('not_found','记忆不存在或已删除。',404)
        if row['revision'] != expected_revision:
            raise MemoryError('revision_conflict','记忆已被修改，请读取最新版本后重试。',409)
        current={**row,**changes}
        if current['valid_until'] and current['valid_until']<=current['valid_from']:
            raise MemoryError('invalid_validity','结束时间必须晚于生效时间。',422)
        revision=row['revision']+1
        c.execute("""UPDATE memories SET text=%s,terms=to_tsvector('simple',%s),subject=%s,speaker_id=%s,
            allow_cloud=%s,valid_from=%s,valid_until=%s,kind=%s,revision=%s,updated_at=%s WHERE id=%s""",
            (current['text'],terms(current['text']),current['subject'],current['speaker_id'],current['allow_cloud'],
             current['valid_from'],current['valid_until'],current['kind'],revision,self.clock.now(),memory_id))
        c.execute("INSERT INTO events(id,memory_id,revision,operation,content,created_at) VALUES(%s,%s,%s,'correct',%s,%s)",(uuid4(),memory_id,revision,current['text'],self.clock.now()))
        c.execute('DELETE FROM projections WHERE memory_id=%s',(memory_id,))
        c.execute("UPDATE outbox SET status='cancelled',lease_token=NULL,last_error=NULL WHERE memory_id=%s AND status IN ('pending','running','failed')",(memory_id,))
        c.execute('DELETE FROM messages WHERE %s::uuid=ANY(source_ids)',(memory_id,))
        self.invalidate_context(c,[('memory',memory_id)],stale_experiences=not preserve_experiences,
                                block_cloud=not current['allow_cloud'])
        self._enqueue(c,memory_id,revision)
        return self._view(c,memory_id)

    def _apply_delete(self,c,r):
        target=r.get('target_id')
        if r['target_type']=='memory':
            self.invalidate_context(c,[('memory',target)],erase=True)
            c.execute("UPDATE memories SET text='',terms=''::tsvector,request_hash='',deleted_at=COALESCE(deleted_at,%s),updated_at=%s WHERE id=%s",(r['deleted_at'],self.clock.now(),target))
            c.execute("UPDATE events SET content='' WHERE memory_id=%s",(target,))
            c.execute("UPDATE memory_evidence SET quote='' WHERE memory_id=%s",(target,))
            c.execute('DELETE FROM projections WHERE memory_id=%s',(target,))
            c.execute("UPDATE outbox SET status='cancelled',lease_token=NULL,last_error=NULL WHERE memory_id=%s",(target,))
            c.execute('DELETE FROM messages WHERE %s::uuid=ANY(source_ids)',(target,))
        elif r['target_type'] in ('experience','self'):
            self.invalidate_context(c,[(r['target_type'],target)],erase=True)
        elif r['target_type']=='conversation':
            roots=c.execute('SELECT id FROM experiences WHERE conversation_id=%s AND recorded_at<=%s',
                            (target,r['deleted_at'])).fetchall()
            self.invalidate_context(c,[('experience',row['id']) for row in roots],erase=True)
            c.execute('DELETE FROM messages WHERE conversation_id=%s AND created_at<=%s',(target,r['deleted_at']))
        else:
            c.execute('DELETE FROM messages WHERE created_at<%s',(r['cutoff'],))
        c.execute('INSERT INTO deletion_ledger(id,target_type,target_id,cutoff,epoch,deleted_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                  (r['id'],r['target_type'],target,r.get('cutoff'),r['epoch'],r['deleted_at']))

    def _delete(self,target_type,target_id=None,cutoff=None):
        tokens.cache_clear()
        with self.pool.connection() as c:
            epoch=self._state_lock(c)+1
            if target_type=='memory':
                row=c.execute('SELECT deleted_at FROM memories WHERE id=%s FOR UPDATE',(target_id,)).fetchone()
                if not row:
                    raise MemoryError('not_found','记忆不存在。',404)
                if row['deleted_at']:
                    return {'deleted':True,'id':target_id,'already_deleted':True}
            if target_type in ('experience','self'):
                table='experiences' if target_type=='experience' else 'self_observations'
                row=c.execute(sql.SQL('SELECT deleted_at FROM {} WHERE id=%s FOR UPDATE').format(sql.Identifier(table)),(target_id,)).fetchone()
                if not row:
                    raise MemoryError('not_found','经历或内部认识不存在。',404)
                if row['deleted_at']:
                    return {'deleted':True,'id':target_id,'already_deleted':True}
            r={'id':str(uuid4()),'target_type':target_type,'target_id':str(target_id) if target_id else None,
               'cutoff':cutoff.isoformat() if cutoff else None,'epoch':epoch,'deleted_at':self.clock.now().isoformat()}
            # The independent journal is a durable deletion intent. A rolled-back
            # database transaction is conservatively replayed on the next startup.
            self.journal.append(r)
            self._apply_delete(c,r)
            c.execute('UPDATE system_state SET epoch=%s WHERE id=1',(epoch,))
        return {'deleted':True,'id':target_id,'epoch':epoch}

    def forget(self,memory_id):
        return self._delete('memory',memory_id)

    def clear_conversation(self,conversation_id):
        return self._delete('conversation',conversation_id)

    def replay_deletions(self,required=False):
        records=self.journal.records(required=required)
        with self.pool.connection() as c:
            epoch=self._state_lock(c)
            for r in records:
                known=c.execute('SELECT 1 FROM deletion_ledger WHERE id=%s',(r['id'],)).fetchone()
                self._apply_delete(c,r)
                epoch=max(epoch,int(r['epoch']))
                if not known:
                    epoch+=1
            c.execute('UPDATE system_state SET epoch=%s WHERE id=1',(epoch,))
        return len(records)

    def expire_history(self):
        cutoff=self.clock.now()-timedelta(days=self.settings.history_days)
        with self.pool.connection() as c:
            exists=c.execute('SELECT 1 FROM messages WHERE created_at<%s LIMIT 1',(cutoff,)).fetchone()
        if exists:
            self._delete('expiry',cutoff=cutoff)

    def search(self,query,vector=None,limit=8,cloud_only=False,subject=None):
        filters='m.deleted_at IS NULL AND m.valid_from<=%s AND (m.valid_until IS NULL OR m.valid_until>%s)'
        args=[self.clock.now(),self.clock.now()]
        if cloud_only:
            filters+=' AND m.allow_cloud=true'
        if subject:
            filters+=' AND m.subject=%s'
            args.append(subject)
        ranked={}
        with self.pool.connection() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            keyword=ts_query(query)
            if keyword:
                rows=c.execute(f"""SELECT m.id,ts_rank_cd(m.terms,to_tsquery('simple',%s)) AS rank FROM memories m
                    WHERE {filters} AND m.terms@@to_tsquery('simple',%s) ORDER BY rank DESC,m.updated_at DESC LIMIT 40""",
                    (keyword,*args,keyword)).fetchall()
                for rank,r in enumerate(rows,1):
                    ranked[r['id']]={'score':1/(60+rank),'methods':['keyword']}
            if vector is not None:
                literal=vector_literal(vector)
                rows=c.execute(f"""SELECT m.id,max(1-(p.embedding<=>%s::vector)) AS similarity FROM projections p
                    JOIN memories m ON m.id=p.memory_id AND m.revision=p.revision WHERE {filters} AND p.model=%s
                    GROUP BY m.id HAVING max(1-(p.embedding<=>%s::vector))>=%s ORDER BY similarity DESC LIMIT 40""",
                    (literal,*args,self.settings.embedding_model,literal,self.settings.min_similarity)).fetchall()
                for rank,r in enumerate(rows,1):
                    hit=ranked.setdefault(r['id'],{'score':0,'methods':[]})
                    hit['score']+=1/(60+rank)
                    hit['methods'].append('semantic')
                    hit['similarity']=float(r['similarity'])
            return [{**self._view(c,mid),**hit} for mid,hit in sorted(ranked.items(),key=lambda pair:pair[1]['score'],reverse=True)[:limit]]

    def claim_job(self):
        with self.pool.connection() as c:
            return c.execute("""WITH selected AS (SELECT id FROM outbox WHERE
                (status='pending' AND available_at<=now()) OR (status='running' AND lease_until<now())
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE outbox o SET status='running',attempts=attempts+1,lease_token=%s,
                lease_until=now()+interval '120 seconds',updated_at=now() FROM selected s WHERE o.id=s.id RETURNING o.*""",(uuid4(),)).fetchone()

    def job_source(self,job):
        with self.pool.connection() as c:
            return c.execute('SELECT * FROM memories WHERE id=%s AND deleted_at IS NULL AND revision=%s',(job['memory_id'],job['revision'])).fetchone()

    def finish_index(self,job,texts,vectors):
        if len(texts)!=len(vectors):
            raise ValueError('incomplete_embeddings')
        with self.pool.connection() as c:
            self._state_lock(c)
            lease=c.execute('SELECT status,lease_token FROM outbox WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
            row=c.execute('SELECT revision,deleted_at FROM memories WHERE id=%s',(job['memory_id'],)).fetchone()
            if lease['status']!='running' or lease['lease_token']!=job['lease_token']:
                return False
            if row['deleted_at'] or row['revision']!=job['revision']:
                c.execute("UPDATE outbox SET status='cancelled',lease_token=NULL WHERE id=%s",(job['id'],))
                return False
            c.execute('DELETE FROM projections WHERE memory_id=%s',(job['memory_id'],))
            for index,(text,vector) in enumerate(zip(texts,vectors)):
                c.execute('INSERT INTO projections(memory_id,revision,chunk_index,content,embedding,model) VALUES(%s,%s,%s,%s,%s::vector,%s)',
                          (job['memory_id'],job['revision'],index,text,vector_literal(vector),self.settings.embedding_model))
            c.execute("UPDATE outbox SET status='done',lease_token=NULL,lease_until=NULL,last_error=NULL,updated_at=now() WHERE id=%s",(job['id'],))
        return True

    def fail_job(self,job,code='embedding_unavailable'):
        with self.pool.connection() as c:
            c.execute("""UPDATE outbox SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,
                available_at=now()+interval '10 seconds',lease_token=NULL,lease_until=NULL,last_error=%s,updated_at=now()
                WHERE id=%s AND lease_token=%s""",(code,job['id'],job['lease_token']))

    def reindex(self,memory_id):
        with self.pool.connection() as c:
            self._state_lock(c)
            row=self._view(c,memory_id)
            c.execute("""INSERT INTO outbox(memory_id,revision) VALUES(%s,%s) ON CONFLICT(memory_id,revision)
                DO UPDATE SET status='pending',attempts=0,available_at=now(),lease_token=NULL,lease_until=NULL,last_error=NULL""",(memory_id,row['revision']))
        return {'queued':True,'id':memory_id}

    def epoch(self):
        with self.pool.connection() as c:
            return c.execute('SELECT epoch FROM system_state WHERE id=1').fetchone()['epoch']

    def context_memories(self,ids):
        """Resolve conversational references without exposing expired/private records."""
        with self.pool.connection() as c:
            rows=c.execute('''SELECT id FROM memories WHERE id=ANY(%s::uuid[])
                AND deleted_at IS NULL AND allow_cloud AND valid_from<=%s AND (valid_until IS NULL OR valid_until>%s)
                ORDER BY updated_at DESC LIMIT 8''',(ids,self.clock.now(),self.clock.now())).fetchall()
            return [self._view(c,row['id']) for row in rows]

    def history(self,conversation_id,limit=12,offset=0,cloud_only=True):
        with self.pool.connection() as c:
            filter_cloud="""AND NOT EXISTS (SELECT 1 FROM memories m WHERE m.id=ANY(messages.source_ids)
                  AND (m.deleted_at IS NOT NULL OR NOT m.allow_cloud OR m.valid_from>%s
                       OR (m.valid_until IS NOT NULL AND m.valid_until<=%s)))
                AND NOT EXISTS (SELECT 1 FROM experiences e WHERE e.id=messages.experience_id
                    AND (e.deleted_at IS NOT NULL OR NOT e.allow_cloud OR e.context_stale))""" if cloud_only else ''
            rows=c.execute(f"""SELECT role,content,source_ids,created_at,experience_id FROM messages WHERE conversation_id=%s
                AND created_at>=%s
                {filter_cloud} ORDER BY id DESC LIMIT %s OFFSET %s""",
                (conversation_id,self.clock.now()-timedelta(days=self.settings.history_days),
                 *((self.clock.now(),self.clock.now()) if cloud_only else ()),limit,offset)).fetchall()
            return list(reversed(rows))

    @contextmanager
    def conversation_lock(self,conversation_id):
        with self.pool.connection() as c:
            locked=c.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked',(str(conversation_id),)).fetchone()['locked']
            c.commit()
            if not locked:
                raise MemoryError('conversation_busy','此会话正在回答，请稍后重试。',409)
            try:
                yield
            finally:
                c.rollback()
                c.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(str(conversation_id),))

    def start_call(self,conversation_id,source_ids,phase='answer',source_refs=None,estimated_input_tokens=None,policy_version=None,estimator_version=None):
        call_id=uuid4()
        with self.pool.connection() as c:
            c.execute("""INSERT INTO model_calls(id,conversation_id,source_ids,model,status,phase,source_refs,
                estimated_input_tokens,policy_version,created_at,estimator_version) VALUES(%s,%s,%s,%s,'running',%s,%s,%s,%s,%s,%s)""",
                (call_id,conversation_id,source_ids,self.settings.model_name,phase,Jsonb(json.loads(json.dumps(source_refs or [],default=str))),
                 estimated_input_tokens,policy_version,self.clock.now(),estimator_version))
        return call_id

    def finish_call(self,call_id,status,usage=None):
        usage=usage or {}
        with self.pool.connection() as c:
            c.execute('UPDATE model_calls SET status=%s,input_tokens=%s,output_tokens=%s WHERE id=%s',(status,usage.get('input_tokens'),usage.get('output_tokens'),call_id))

    def save_answer(self,conversation_id,question,answer,source_ids,expected_epoch,auto_actions=None,retain_turn=True,
                    experience_description=None,self_updates=None,context_roots=None,episode_proposals=None,rehearsals=None):
        tokens.cache_clear()
        actions=auto_actions or []
        with self.pool.connection() as c:
            if self._state_lock(c)!=expected_epoch:
                raise MemoryError('context_changed','回答期间记忆或会话已变更，请重新提问。',409)
            valid=c.execute("""SELECT count(*) AS n FROM memories WHERE id=ANY(%s::uuid[])
                AND deleted_at IS NULL AND allow_cloud AND valid_from<=%s
                AND (valid_until IS NULL OR valid_until>%s)""",(source_ids,self.clock.now(),self.clock.now())).fetchone()['n']
            if valid!=len(set(source_ids)):
                raise MemoryError('context_changed','引用的记忆已变更或过期，请重新提问。',409)
            # Validate every target before writing a deletion intent. A later SQL
            # failure can roll back the batch; deletion intents remain conservative.
            for action in actions:
                start=action.valid_from or self.clock.now()
                if action.operation!='forget' and action.valid_until and action.valid_until<=start:
                    raise MemoryError('memory_decision_invalid','记忆有效期已结束；本轮未写入，请重试。',502)
                if action.operation=='create':continue
                row=c.execute('SELECT revision,deleted_at,allow_cloud FROM memories WHERE id=%s',(action.target_id,)).fetchone()
                if not row or row['deleted_at'] or not row['allow_cloud'] or row['revision']!=action.expected_revision:
                    raise MemoryError('context_changed','要变更的记忆已改变，请重试。',409)
            changes=[]
            source_ids=list(source_ids)
            forgotten=set()
            for action in actions:
                if action.operation=='forget':
                    r={'id':str(uuid4()),'target_type':'memory','target_id':str(action.target_id),
                       'epoch':expected_epoch+1,'deleted_at':self.clock.now().isoformat(),'cutoff':None}
                    self.journal.append(r)
                    self._apply_delete(c,r)
                    forgotten.add(action.target_id)
                    changes.append({'operation':'forget','id':action.target_id})
                    continue
                start=action.valid_from or self.clock.now()
                if action.operation=='update':
                    item=self._correct(c,action.target_id,action.expected_revision,{
                        'text':action.text.strip(),'subject':action.subject,'kind':action.kind,
                        'valid_from':start,'valid_until':action.valid_until},
                        preserve_experiences=action.change_mode=='evolution')
                else:
                    # Exact duplicates are also suppressed under the global lock,
                    # including retried requests before the vector worker catches up.
                    duplicate=c.execute('''SELECT id FROM memories WHERE deleted_at IS NULL AND text=%s AND subject=%s
                        AND allow_cloud AND valid_until IS NOT DISTINCT FROM %s::timestamptz
                        AND (valid_until IS NULL OR valid_until>%s) LIMIT 1''',
                        (action.text.strip(),action.subject,action.valid_until,self.clock.now())).fetchone()
                    if duplicate:
                        source_ids.append(duplicate['id'])
                        continue
                    mid=uuid4()
                    c.execute('''INSERT INTO memories(id,text,terms,subject,valid_from,valid_until,request_hash,origin,kind,created_at,updated_at)
                        VALUES(%s,%s,to_tsvector('simple',%s),%s,%s,%s,%s,'automatic',%s,%s,%s)''',
                        (mid,action.text.strip(),terms(action.text),action.subject,start,action.valid_until,
                         sha256(action.text.encode()).hexdigest(),action.kind,self.clock.now(),self.clock.now()))
                    c.execute("INSERT INTO events(id,memory_id,revision,operation,content,created_at) VALUES(%s,%s,1,'create',%s,%s)",
                              (uuid4(),mid,action.text.strip(),self.clock.now()))
                    self._enqueue(c,mid,1)
                    item=self._view(c,mid)
                c.execute('INSERT INTO memory_evidence(memory_id,revision,conversation_id,quote,created_at) VALUES(%s,%s,%s,%s,%s)',
                          (item['id'],item['revision'],conversation_id,action.quote,self.clock.now()))
                source_ids.append(item['id'])
                changes.append({'operation':action.operation,'id':item['id'],'revision':item['revision'],
                                'text':item['text'],'kind':item['kind'],'valid_until':item['valid_until']})
            self_changes=[]
            changed_episodes=any((p.get('target_id') if isinstance(p,dict) else p.target_id) for p in (episode_proposals or []))
            committed_epoch=expected_epoch+int(bool(changes or ((self_updates or changed_episodes) and retain_turn and not forgotten)))
            if committed_epoch!=expected_epoch:c.execute('UPDATE system_state SET epoch=%s WHERE id=1',(committed_epoch,))
            source_ids=list(dict.fromkeys(mid for mid in source_ids if mid not in forgotten))
            # A forgetting turn itself may repeat the forgotten information. Keep
            # no content for this turn, so a later prompt cannot resurrect it.
            if forgotten or not retain_turn:
                return {'epoch':committed_epoch,'changes':changes,'history_saved':False,'experience_id':None,'self_changes':[],'episode_ids':[]}
            roots=[('memory',mid) for mid in source_ids]+list(context_roots or [])
            experience_id=self._archive_turn(c,conversation_id,question,answer,experience_description,roots)
            episode_ids=self._save_episodes(c,experience_id,episode_proposals,
                [rid for kind,rid in roots if kind=='episode'])
            self._save_rehearsals(c,experience_id,rehearsals)
            self_changes=self._save_self_updates(c,experience_id,self_updates or [])
            for role,content in [('user',question),('assistant',answer)]:
                c.execute('INSERT INTO messages(conversation_id,role,content,source_ids,experience_id,created_at) VALUES(%s,%s,%s,%s,%s,%s)',(conversation_id,role,content,source_ids,experience_id,self.clock.now()))
        return {'epoch':committed_epoch,'changes':changes,'history_saved':True,'experience_id':experience_id,'self_changes':self_changes,'episode_ids':[eid for eid in episode_ids if eid]}

    def health(self):
        with self.pool.connection() as c:
            jobs=c.execute('SELECT status,count(*) AS n FROM outbox GROUP BY status').fetchall()
            total=c.execute('SELECT count(*) AS n FROM memories WHERE deleted_at IS NULL').fetchone()['n']
            experience_count=c.execute('SELECT count(*) AS n FROM experiences WHERE deleted_at IS NULL').fetchone()['n']
            experience_jobs=c.execute('SELECT status,count(*) AS n FROM experience_jobs GROUP BY status').fetchall()
            episode_count=c.execute('SELECT count(*) AS n FROM episodes WHERE deleted_at IS NULL').fetchone()['n']
            episode_jobs=c.execute('SELECT status,count(*) AS n FROM episode_jobs GROUP BY status').fetchall()
            return {'database':'ok','version':'0.4.0','automatic_memory':True,'memories':total,'episodes':episode_count,
                    'episode_index_jobs':{r['status']:r['n'] for r in episode_jobs},
                    'experiences':experience_count,'experience_retention':'until_deleted',
                    'experience_index_jobs':{r['status']:r['n'] for r in experience_jobs},
                    'index_jobs':{r['status']:r['n'] for r in jobs},'model':self.settings.model_name}
