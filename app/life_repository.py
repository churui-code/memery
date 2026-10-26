"""Permanent autobiographical evidence, retrieval, and versioned self development."""
from uuid import uuid4
from psycopg.types.json import Jsonb
from app.errors import MemoryError
from app.life import observation_view
from app.episodes import source_hash, normalize_event_time
from app.text import chunks, terms, ts_query, tokens


class LifeRepository:
    @staticmethod
    def _link(c, roots, target_type, target_id):
        for source_type, source_id in set(roots):
            c.execute('''INSERT INTO context_links(source_type,source_id,target_type,target_id)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (source_type, source_id, target_type, target_id))

    @staticmethod
    def context_descendants(c, roots):
        if not roots:
            return []
        return c.execute('''WITH RECURSIVE affected(kind,id) AS (
            SELECT * FROM unnest(%s::text[],%s::uuid[])
            UNION
            SELECT l.target_type,l.target_id FROM context_links l
            JOIN affected a ON a.kind=l.source_type AND a.id=l.source_id
        ) SELECT kind,id FROM affected''',
            ([r[0] for r in roots], [r[1] for r in roots])).fetchall()

    def affected_context(self, memory_ids):
        with self.pool.connection() as c:
            return self.context_descendants(c, [('memory',mid) for mid in memory_ids])

    def invalidate_context(self, c, roots, erase=False, stale_experiences=True, block_cloud=False):
        rows = self.context_descendants(c, roots)
        experiences = [r['id'] for r in rows if r['kind'] == 'experience']
        observations = [r['id'] for r in rows if r['kind'] == 'self']
        episodes = [r['id'] for r in rows if r['kind'] == 'episode']
        at = self.clock.now()
        if erase:
            c.execute('''UPDATE experiences SET user_text='',assistant_text='',title='',summary='',
                participants='[]',anchors='[]',time_quote=NULL,terms=''::tsvector,source_hash='',
                deleted_at=COALESCE(deleted_at,%s) WHERE id=ANY(%s::uuid[])''', (at,experiences))
            c.execute('DELETE FROM experience_chunks WHERE experience_id=ANY(%s::uuid[])', (experiences,))
            c.execute("UPDATE experience_jobs SET status='cancelled',lease_token=NULL,last_error=NULL WHERE experience_id=ANY(%s::uuid[])", (experiences,))
            c.execute('''UPDATE self_observations SET text='',reason='',evidence_quote='',
                deleted_at=COALESCE(deleted_at,%s),invalidated_at=COALESCE(invalidated_at,%s)
                WHERE id=ANY(%s::uuid[])''', (at,at,observations))
            c.execute('''UPDATE episodes SET title='',gist='',importance_reason='',emotional_reason='',
                participants='[]',terms=''::tsvector,time_quote=NULL,source_hash='',last_rehearsed_at=NULL,
                source_reliability='{}',occurred_at=NULL,occurred_until=NULL,time_precision='unknown',
                deleted_at=COALESCE(deleted_at,%s) WHERE id=ANY(%s::uuid[])''',(at,episodes))
            c.execute('''UPDATE episode_versions SET title='',gist='',importance_reason='',emotional_reason='',
                source_hash='',source_reliability='{}',time_quote=NULL,occurred_at=NULL,occurred_until=NULL,
                time_precision='unknown' WHERE episode_id=ANY(%s::uuid[])''',(episodes,))
            for table in ('episode_details','episode_cues','episode_chunks'):
                # Table names are constants, never user input.
                c.execute(f'DELETE FROM {table} WHERE episode_id=ANY(%s::uuid[])',(episodes,))
            c.execute('DELETE FROM episode_evidence WHERE episode_id=ANY(%s::uuid[])',(episodes,))
            c.execute('DELETE FROM episode_associations WHERE source_id=ANY(%s::uuid[]) OR target_id=ANY(%s::uuid[])',(episodes,episodes))
            c.execute('DELETE FROM episode_rehearsals WHERE episode_id=ANY(%s::uuid[]) OR experience_id=ANY(%s::uuid[])',(episodes,experiences))
            c.execute('''UPDATE episodes e SET last_rehearsed_at=(SELECT max(r.recalled_at) FROM episode_rehearsals r
                WHERE r.episode_id=e.id) WHERE e.deleted_at IS NULL AND e.last_rehearsed_at IS NOT NULL''')
            c.execute("UPDATE episode_jobs SET status='cancelled',lease_token=NULL,last_error=NULL WHERE episode_id=ANY(%s::uuid[])",(episodes,))
            # Disposable messages derived from recalled experiences must also go.
            c.execute('''DELETE FROM messages m USING experiences e
                WHERE e.id=ANY(%s::uuid[]) AND m.experience_id=e.id''', (experiences,))
        else:
            if stale_experiences:
                c.execute('UPDATE experiences SET context_stale=true WHERE id=ANY(%s::uuid[])', (experiences,))
                c.execute('UPDATE episodes SET context_stale=true WHERE id=ANY(%s::uuid[])',(episodes,))
                c.execute("UPDATE episode_jobs SET status='cancelled',lease_token=NULL,last_error=NULL WHERE episode_id=ANY(%s::uuid[])",(episodes,))
            if block_cloud:
                c.execute('UPDATE experiences SET allow_cloud=false WHERE id=ANY(%s::uuid[])', (experiences,))
                c.execute('UPDATE episodes SET allow_cloud=false WHERE id=ANY(%s::uuid[])',(episodes,))
            c.execute('''UPDATE self_observations SET invalidated_at=COALESCE(invalidated_at,%s)
                WHERE id=ANY(%s::uuid[])''', (at,observations))
            c.execute('DELETE FROM messages WHERE experience_id=ANY(%s::uuid[])',(experiences,))

    def _archive_turn(self, c, conversation_id, question, answer, description, roots):
        experience_id = uuid4()
        data = description.model_dump(mode='json') if description else {}
        title = data.get('title', '一次对话')
        summary = data.get('summary', '')
        anchors = data.get('anchors', [])
        recorded_at=self.clock.now()
        event_time=normalize_event_time(data.get('time_quote'),recorded_at)
        # Tokenize each chunk so a late key phrase survives even in a long turn.
        searchable = '\n'.join([title, summary, *(a['quote'] for a in anchors), question, answer])
        index_terms = ' '.join(terms(part) for part in chunks(searchable))
        c.execute('''INSERT INTO experiences(id,conversation_id,user_text,assistant_text,title,summary,
            context_type,participants,anchors,significance,terms,occurred_at,time_quote,recorded_at,source_hash)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s,%s,%s,%s)''',
            (experience_id, conversation_id, question, answer, title, summary,
             data.get('context_type', 'conversation'), Jsonb(data.get('participants', [])),
             Jsonb(anchors), data.get('significance', 0.2), index_terms,
             event_time['occurred_at'],data.get('time_quote'),recorded_at,source_hash(question,answer)))
        self._link(c, roots, 'experience', experience_id)
        c.execute('INSERT INTO experience_jobs(experience_id) VALUES(%s)', (experience_id,))
        return experience_id

    def _save_self_updates(self, c, experience_id, updates):
        changes = []
        for update in updates:
            previous = c.execute('''SELECT * FROM self_observations WHERE domain=%s AND state_key=%s
                ORDER BY revision DESC LIMIT 1''', (update.domain, update.key)).fetchone()
            revision = previous['revision'] + 1 if previous else 1
            oid = uuid4()
            c.execute('''INSERT INTO self_observations(id,domain,state_key,revision,text,reason,evidence_quote,
                confidence,intensity,half_life_hours,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (oid, update.domain, update.key, revision, update.text, update.reason, update.quote,
                 update.confidence, update.intensity, update.half_life_hours,self.clock.now()))
            roots = [('experience', experience_id)]
            roots.extend(('experience', eid) for eid in update.source_experience_ids)
            for eid in getattr(update,'source_episode_ids',[]):
                episode = c.execute('SELECT id FROM episodes WHERE id=%s AND deleted_at IS NULL AND NOT context_stale AND allow_cloud',(eid,)).fetchone()
                if not episode:
                    raise MemoryError('context_changed','内部认识的事件证据已失效。',409)
                roots.append(('episode',eid))
                roots.extend(('experience',r['experience_id']) for r in c.execute(
                    'SELECT DISTINCT experience_id FROM episode_evidence WHERE episode_id=%s',(eid,)).fetchall())
            if previous and not previous['invalidated_at'] and not previous['deleted_at']:
                roots.append(('self', previous['id']))
            self._link(c, roots, 'self', oid)
            changes.append({'id': oid, 'domain': update.domain, 'key': update.key,
                            'revision': revision, 'text': update.text,
                            'epistemic_status': 'internal_interpretation'})
        return changes

    def self_state(self, per_domain=6,query=''):
        with self.pool.connection() as c:
            rows = c.execute('''WITH latest AS (
                SELECT DISTINCT ON (domain,state_key) * FROM self_observations
                ORDER BY domain,state_key,revision DESC
            ), ranked AS (
                SELECT *,row_number() OVER (PARTITION BY domain ORDER BY
                    (SELECT count(*) FROM unnest(%s::text[]) AS word
                     WHERE position(word IN text)>0 OR position(word IN state_key)>0) DESC,
                    created_at DESC,id) AS rn
                FROM latest WHERE invalidated_at IS NULL AND deleted_at IS NULL
            ) SELECT * FROM ranked WHERE rn<=%s ORDER BY domain,rn''', (list(tokens(query)),per_domain)).fetchall()
            for row in rows:
                row.pop('rn')
            return [observation_view(row,at=self.clock.now()) for row in rows]

    def self_history(self, domain=None, key=None, limit=50, offset=0):
        with self.pool.connection() as c:
            rows = c.execute('''SELECT * FROM self_observations WHERE deleted_at IS NULL
                AND (%s::text IS NULL OR domain=%s) AND (%s::text IS NULL OR state_key=%s)
                ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s''',
                (domain, domain, key, key, limit, offset)).fetchall()
            for row in rows:
                row['sources'] = c.execute('''SELECT source_type,source_id FROM context_links
                    WHERE target_type='self' AND target_id=%s ORDER BY source_type,source_id''', (row['id'],)).fetchall()
            return [observation_view(row,at=self.clock.now()) for row in rows]

    def forget_experience(self, experience_id):
        return self._delete('experience',experience_id)

    def forget_observation(self, observation_id):
        return self._delete('self',observation_id)

    def experience(self, experience_id):
        with self.pool.connection() as c:
            row = c.execute('SELECT * FROM experiences WHERE id=%s AND deleted_at IS NULL', (experience_id,)).fetchone()
            if not row:
                raise MemoryError('not_found', '经历不存在或已删除。', 404)
            row.pop('terms', None)
            row['sources'] = c.execute('''SELECT source_type,source_id FROM context_links
                WHERE target_type='experience' AND target_id=%s ORDER BY source_type,source_id''', (experience_id,)).fetchall()
            return row

    def search_experiences(self, query='', vector=None, limit=8, cloud_only=False,
                           after=None, before=None, include_stale=False, conversation_id=None,time_basis='recorded_at'):
        from app.repository import vector_literal
        filters = 'e.deleted_at IS NULL'
        args = []
        if cloud_only:
            filters += ' AND e.allow_cloud'
        if not include_stale:
            filters += ' AND NOT e.context_stale'
        if time_basis not in ('recorded_at','occurred_at'):
            raise ValueError('invalid time basis')
        for value, clause in [(after, f'e.{time_basis}>=%s'), (before, f'e.{time_basis}<%s'),
                              (conversation_id, 'e.conversation_id=%s')]:
            if value is not None:
                filters += ' AND ' + clause
                args.append(value)
        ranked = {}
        with self.pool.connection() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            keyword = ts_query(query)
            if keyword:
                rows = c.execute(f'''SELECT e.id,ts_rank_cd(e.terms,to_tsquery('simple',%s)) AS rank
                    FROM experiences e WHERE {filters} AND e.terms@@to_tsquery('simple',%s)
                    ORDER BY rank DESC,e.significance DESC,e.recorded_at DESC LIMIT 40''',
                    (keyword, *args, keyword)).fetchall()
                for rank, row in enumerate(rows, 1):
                    ranked[row['id']] = {'score': 1 / (60 + rank), 'methods': ['keyword']}
            if vector is not None:
                literal = vector_literal(vector)
                rows = c.execute(f'''SELECT e.id,max(1-(p.embedding<=>%s::vector)) AS similarity
                    FROM experience_chunks p JOIN experiences e ON e.id=p.experience_id
                    WHERE {filters} AND p.model=%s GROUP BY e.id
                    HAVING max(1-(p.embedding<=>%s::vector))>=%s ORDER BY similarity DESC LIMIT 40''',
                    (literal, *args, self.settings.embedding_model, literal, self.settings.experience_min_similarity)).fetchall()
                for rank, row in enumerate(rows, 1):
                    hit = ranked.setdefault(row['id'], {'score': 0, 'methods': []})
                    hit['score'] += 1 / (60 + rank)
                    hit['methods'].append('semantic')
            if not query.strip():
                rows = c.execute(f'''SELECT e.id FROM experiences e WHERE {filters}
                    ORDER BY e.recorded_at DESC,e.id LIMIT %s''', (*args, limit)).fetchall()
                ranked = {r['id']: {'score': 1 / (60 + i), 'methods': ['timeline']} for i, r in enumerate(rows, 1)}
            result = []
            for eid, hit in sorted(ranked.items(), key=lambda pair: pair[1]['score'], reverse=True)[:limit]:
                row = c.execute('SELECT * FROM experiences WHERE id=%s', (eid,)).fetchone()
                row.pop('terms', None)
                result.append({**row, **hit})
            return result

    def claim_experience_job(self):
        with self.pool.connection() as c:
            return c.execute('''WITH selected AS (
                SELECT experience_id FROM experience_jobs WHERE
                (status='pending' AND available_at<=now()) OR (status='running' AND lease_until<now())
                ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE experience_jobs j SET status='running',attempts=attempts+1,
                lease_token=%s,lease_until=now()+interval '120 seconds' FROM selected s
                WHERE j.experience_id=s.experience_id RETURNING j.*''', (uuid4(),)).fetchone()

    def experience_job_source(self, job):
        with self.pool.connection() as c:
            return c.execute('SELECT * FROM experiences WHERE id=%s AND deleted_at IS NULL', (job['experience_id'],)).fetchone()

    def finish_experience_index(self, job, texts, vectors):
        from app.repository import vector_literal
        if len(texts) != len(vectors):
            raise ValueError('incomplete_embeddings')
        with self.pool.connection() as c:
            self._state_lock(c)
            lease = c.execute('SELECT * FROM experience_jobs WHERE experience_id=%s FOR UPDATE', (job['experience_id'],)).fetchone()
            source = c.execute('SELECT deleted_at FROM experiences WHERE id=%s', (job['experience_id'],)).fetchone()
            if not lease or lease['status'] != 'running' or lease['lease_token'] != job['lease_token']:
                return
            if not source or source['deleted_at']:
                c.execute("UPDATE experience_jobs SET status='cancelled',lease_token=NULL WHERE experience_id=%s", (job['experience_id'],))
                return
            c.execute('DELETE FROM experience_chunks WHERE experience_id=%s', (job['experience_id'],))
            for i, (text, vector) in enumerate(zip(texts, vectors)):
                c.execute('''INSERT INTO experience_chunks(experience_id,chunk_index,content,embedding,model)
                    VALUES(%s,%s,%s,%s::vector,%s)''', (job['experience_id'], i, text, vector_literal(vector), self.settings.embedding_model))
            c.execute("UPDATE experience_jobs SET status='done',lease_token=NULL,last_error=NULL WHERE experience_id=%s", (job['experience_id'],))

    def fail_experience_job(self, job):
        with self.pool.connection() as c:
            c.execute('''UPDATE experience_jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,
                available_at=now()+interval '10 seconds',lease_token=NULL,last_error='embedding_unavailable'
                WHERE experience_id=%s AND status='running' AND lease_token=%s''', (job['experience_id'], job['lease_token']))
