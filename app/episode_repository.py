"""Source-bound event projections and their local retrieval indexes."""
from hashlib import sha256
import json
import re
from uuid import uuid4
from psycopg.types.json import Jsonb
from app.errors import MemoryError
from app.episodes import locate_quote, validate_episode_proposals, source_reliability, normalize_event_time
from app.text import chunks, terms, ts_query, tokens


class EpisodeRepository:
    @staticmethod
    def _episode_evidence(c, episode_id, source, role, quote, occurrence=0, start=None):
        if start is None:
            start,end=locate_quote(source[role + '_text'], quote, occurrence)
        else:
            end=start+len(quote)
            if source[role+'_text'][start:end]!=quote:
                raise ValueError('episode_span_does_not_match')
        return c.execute('''INSERT INTO episode_evidence(id,episode_id,experience_id,role,quote,
            start_offset,end_offset,source_revision,source_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(episode_id,experience_id,role,start_offset,end_offset) DO UPDATE SET quote=EXCLUDED.quote
            RETURNING id''', (uuid4(),episode_id,source['id'],role,quote,start,end,
                              source['revision'],source['source_hash'])).fetchone()['id']

    def _save_episodes(self, c, experience_id, proposals=None, allowed_existing=()):
        source = c.execute('SELECT * FROM experiences WHERE id=%s', (experience_id,)).fetchone()
        if proposals:
            proposals = validate_episode_proposals(proposals,source['user_text'],allowed_existing)
            return [self._write_episode(c,source,p.model_dump(mode='python')) for p in proposals]
        return [self._fallback_episode(c,source)]

    def _fallback_episode(self, c, source):
        role = 'user' if source['user_text'] else 'assistant'
        text = source[role + '_text']
        if not text:
            text = source['title'] or '一次对话'
            # No invented evidence is indexed for an empty migrated source.
            return None
        matches = list(re.finditer(r'[^。！？\n]+[。！？]?',text))
        sentences = [match.group().strip() for match in matches if match.group().strip()]
        gist = source['summary'] or next((s for s in sentences if len(s)<=2000),'保存了一段完整对话，可通过原文检索。')
        details=[]
        for match in matches:
            sentence=match.group().strip()
            if not sentence:
                continue
            start=match.start()+len(match.group())-len(match.group().lstrip())
            details.append({'quote':sentence,'_start':start,'kind':'fact','meaning':'原始对话片段',
                            'importance':0.2,'emotional_salience':0})
        for anchor in source['anchors']:
            if anchor['quote'] in text:
                details.append({'quote':anchor['quote'],'occurrence':anchor.get('occurrence',0),
                                'kind':'speech','meaning':anchor['meaning'],
                                'importance':source['significance'],'emotional_salience':0})
        data = {'title':source['title'] or '一次对话','gist':gist,'quote':text,'context_type':source['context_type'],
                'importance':source['significance'],'importance_reason':'本地归档表示，尚未细分事件',
                'emotional_salience':0,'emotional_reason':'','participants':source['participants'],
                'occurred_at':source['occurred_at'],'time_quote':source['time_quote'],
                'details':details,'cues':[{'kind':'phrase','text':a['quote'][:300],'quote':a['quote'],
                                         'occurrence':a.get('occurrence',0)}
                                        for a in source['anchors'] if a['quote'] in text]}
        return self._write_episode(c,source,data,role=role,local_fallback=True)

    def _write_episode(self, c, source, data, role='user', local_fallback=False):
        target = data.get('target_id')
        at = self.clock.now()
        previous = None
        if target:
            previous = c.execute('SELECT * FROM episodes WHERE id=%s FOR UPDATE',(target,)).fetchone()
            if not previous or previous['deleted_at'] or previous['context_stale'] or not previous['allow_cloud']:
                raise MemoryError('context_changed','要补充的经历已失效，请重新读取。',409)
            if previous['context_type'] != data['context_type']:
                raise MemoryError('memory_decision_invalid','不同真实性类别的事件不能合并。',502)
        eid = previous['id'] if previous else uuid4()
        revision = previous['revision']+1 if previous else 1
        reliability=source_reliability(data['context_type'],role)
        encoding='legacy-local-v1' if local_fallback else 'episode-encoding-v1'
        event_time=normalize_event_time(data.get('time_quote'),source['recorded_at'])
        event_time_quote=data.get('time_quote')
        if previous and event_time['time_precision']=='unknown':
            event_time={key:previous[key] for key in ('occurred_at','occurred_until','time_precision')}
            event_time_quote=previous['time_quote']
        if previous:
            # Changes to an event invalidate interpretations derived from its old
            # representation. Original autobiographical sources stay available.
            self.invalidate_context(c,[('episode',eid)],stale_experiences=False)
            c.execute('''UPDATE episodes SET title=%s,gist=%s,importance=%s,importance_reason=%s,
                emotional_salience=%s,emotional_reason=%s,participants=%s,
                occurred_at=%s,occurred_until=%s,time_precision=%s,time_quote=%s,
                source_reliability=%s,encoding_version=%s,updated_at=%s,revision=%s,local_fallback=false WHERE id=%s''',
                (data['title'],data['gist'],data['importance'],data.get('importance_reason',''),
                 data['emotional_salience'],data.get('emotional_reason',''),Jsonb(data.get('participants',[])),
                 event_time['occurred_at'],event_time['occurred_until'],event_time['time_precision'],event_time_quote,
                 Jsonb(reliability),encoding,at,revision,eid))
        else:
            c.execute('''INSERT INTO episodes(id,title,gist,context_type,importance,importance_reason,
                emotional_salience,emotional_reason,participants,terms,occurred_at,time_quote,recorded_at,
                updated_at,source_hash,local_fallback,allow_cloud,context_stale,occurred_until,time_precision,source_reliability,encoding_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,''::tsvector,%s,%s,%s,%s,'',%s,%s,%s,%s,%s,%s,%s)''',
                (eid,data['title'],data['gist'],data['context_type'],data['importance'],data.get('importance_reason',''),
                 data['emotional_salience'],data.get('emotional_reason',''),Jsonb(data.get('participants',[])),
                 event_time['occurred_at'],event_time_quote,source['recorded_at'],at,local_fallback,
                 source['allow_cloud'],source['context_stale'],event_time['occurred_until'],event_time['time_precision'],Jsonb(reliability),encoding))
        self._episode_evidence(c,eid,source,role,data['quote'],data.get('occurrence',0))
        for detail in data.get('details',[]):
            evidence = self._episode_evidence(c,eid,source,role,detail['quote'],detail.get('occurrence',0),detail.get('_start'))
            c.execute('''INSERT INTO episode_details(id,episode_id,evidence_id,kind,meaning,importance,emotional_salience,
                importance_reason,emotional_reason,source_reliability) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (uuid4(),eid,evidence,detail['kind'],detail.get('meaning',''),detail['importance'],detail['emotional_salience'],
                 detail.get('importance_reason') or detail.get('meaning',''),detail.get('emotional_reason') or detail.get('meaning',''),Jsonb(reliability)))
        for cue in data.get('cues',[]):
            evidence = self._episode_evidence(c,eid,source,role,cue['quote'],cue.get('occurrence',0))
            c.execute('INSERT INTO episode_cues(id,episode_id,evidence_id,kind,text) VALUES(%s,%s,%s,%s,%s)',
                      (uuid4(),eid,evidence,cue['kind'],cue['text']))
        self._link(c,[('experience',source['id'])],'episode',eid)
        evidence = c.execute('''SELECT DISTINCT experience_id,source_revision,source_hash,quote FROM episode_evidence
            WHERE episode_id=%s ORDER BY experience_id,source_revision,source_hash,quote''',(eid,)).fetchall()
        digest = sha256(json.dumps(evidence,default=str,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        cue_text = ' '.join(r['text'] for r in c.execute('SELECT text FROM episode_cues WHERE episode_id=%s',(eid,)).fetchall())
        searchable = '\n'.join([data['title'],data['gist'],cue_text,*(r['quote'] for r in evidence)])
        index_terms = ' '.join(terms(part) for part in chunks(searchable))
        c.execute("UPDATE episodes SET source_hash=%s,terms=to_tsvector('simple',%s) WHERE id=%s",(digest,index_terms,eid))
        c.execute('''INSERT INTO episode_versions(episode_id,revision,title,gist,importance,importance_reason,
            emotional_salience,emotional_reason,source_hash,created_at,source_reliability,encoding_version,
            time_precision,occurred_at,occurred_until,time_quote)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (eid,revision,data['title'],data['gist'],data['importance'],data.get('importance_reason',''),
             data['emotional_salience'],data.get('emotional_reason',''),digest,at,Jsonb(reliability),encoding,
             event_time['time_precision'],event_time['occurred_at'],event_time['occurred_until'],event_time_quote))
        c.execute('DELETE FROM episode_chunks WHERE episode_id=%s',(eid,))
        c.execute('''INSERT INTO episode_jobs(episode_id,revision,source_hash) VALUES(%s,%s,%s)
            ON CONFLICT(episode_id) DO UPDATE SET revision=EXCLUDED.revision,source_hash=EXCLUDED.source_hash,
            status='pending',attempts=0,available_at=now(),lease_until=NULL,lease_token=NULL,last_error=NULL''',
            (eid,revision,digest))
        # Exact entity/phrase agreement creates bounded, inspectable associations.
        c.execute('''INSERT INTO episode_associations(source_id,target_id,kind)
            SELECT DISTINCT %s,b.episode_id,'shared_' || a.kind FROM episode_cues a
            JOIN episode_cues b ON a.text=b.text AND a.kind=b.kind AND a.episode_id<>b.episode_id
            JOIN episodes other ON other.id=b.episode_id
            WHERE a.episode_id=%s AND a.kind IN ('person','place','object','phrase')
            AND length(a.text)>=2 AND other.deleted_at IS NULL AND NOT other.context_stale
            ORDER BY b.episode_id LIMIT 40 ON CONFLICT DO NOTHING''',(eid,eid))
        return eid

    def backfill_episodes(self, limit=100):
        """Local-only, restartable projection creation for surviving v3 archives."""
        with self.pool.connection() as c:
            self._state_lock(c)
            sources = c.execute('''SELECT e.* FROM experiences e WHERE e.deleted_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM episode_evidence p WHERE p.experience_id=e.id)
                AND (e.user_text<>'' OR e.assistant_text<>'')
                ORDER BY e.recorded_at,e.id LIMIT %s FOR UPDATE SKIP LOCKED''',(limit,)).fetchall()
            for source in sources:
                self._fallback_episode(c,source)
            return len(sources)

    def _episode_view(self, c, episode_id):
        row = c.execute('SELECT * FROM episodes WHERE id=%s AND deleted_at IS NULL',(episode_id,)).fetchone()
        if not row:
            raise MemoryError('not_found','事件不存在或已删除。',404)
        row.pop('terms',None)
        evidence = c.execute('''SELECT p.id,p.experience_id,p.role,p.quote,p.start_offset AS start,p.end_offset AS end,
            p.source_revision,p.source_hash,x.recorded_at AS source_recorded_at FROM episode_evidence p
            JOIN experiences x ON x.id=p.experience_id WHERE p.episode_id=%s ORDER BY x.recorded_at,p.start_offset,p.id''',
            (episode_id,)).fetchall()
        row['evidence'] = evidence
        row['source_experience_ids'] = list(dict.fromkeys(r['experience_id'] for r in evidence))
        row['source_revisions'] = {str(r['experience_id']):r['source_revision'] for r in evidence}
        row['details'] = c.execute('''SELECT d.id,d.kind,d.meaning,d.importance,d.emotional_salience,
            d.importance_reason,d.emotional_reason,d.source_reliability,
            p.quote,p.experience_id,p.role,p.start_offset AS start,p.end_offset AS end,p.source_revision,p.source_hash,
            x.recorded_at AS source_recorded_at FROM episode_details d JOIN episode_evidence p ON p.id=d.evidence_id
            JOIN experiences x ON x.id=p.experience_id
            WHERE d.episode_id=%s ORDER BY x.recorded_at,p.start_offset,d.id''',(episode_id,)).fetchall()
        row['cues'] = c.execute('''SELECT u.id,u.kind,u.text,p.quote,p.experience_id,p.role,
            p.start_offset AS start,p.end_offset AS end,p.source_revision,p.source_hash,x.recorded_at AS source_recorded_at
            FROM episode_cues u JOIN episode_evidence p ON p.id=u.evidence_id JOIN experiences x ON x.id=p.experience_id
            WHERE u.episode_id=%s ORDER BY u.kind,u.text,u.id''',(episode_id,)).fetchall()
        row['rehearsal_count'] = c.execute('SELECT count(*) AS n FROM episode_rehearsals WHERE episode_id=%s',(episode_id,)).fetchone()['n']
        from app.corrections import sanitize_episode
        return sanitize_episode(row, self._correction_masks(c,row['source_experience_ids']))

    def episode(self, episode_id):
        with self.pool.connection() as c:
            if not c.execute('SELECT 1 FROM episodes WHERE id=%s',(episode_id,)).fetchone():
                legacy=self._legacy_episode_view(c,episode_id)
                if legacy:
                    return legacy
            return self._episode_view(c,episode_id)

    def _legacy_episode_view(self, c, experience_id):
        source=c.execute('''SELECT * FROM experiences e WHERE e.id=%s AND e.deleted_at IS NULL
            AND NOT EXISTS(SELECT 1 FROM episode_evidence p WHERE p.experience_id=e.id)''',(experience_id,)).fetchone()
        if not source:
            return None
        role='user' if source['user_text'] else 'assistant'
        quote=source[role+'_text']
        reliability=source_reliability(source['context_type'],role)
        event_time=normalize_event_time(source['time_quote'],source['recorded_at'])
        evidence={'experience_id':source['id'],'role':role,'quote':quote,'start':0,'end':len(quote),
                  'source_revision':source['revision'],'source_hash':source['source_hash'],
                  'source_recorded_at':source['recorded_at']}
        details=[]
        cues=[]
        for index,anchor in enumerate(source['anchors']):
            if anchor['quote'] not in quote:
                continue
            start,end=locate_quote(quote,anchor['quote'],anchor.get('occurrence',0))
            item={**evidence,'quote':anchor['quote'],'start':start,'end':end}
            details.append({**item,'id':str(source['id'])+':'+str(index),'kind':'speech',
                            'meaning':anchor['meaning'],'importance':source['significance'],'emotional_salience':0,
                            'importance_reason':anchor['meaning'],'emotional_reason':'本地兼容表示不推测情绪',
                            'source_reliability':reliability})
            cues.append({**item,'kind':'phrase','text':anchor['quote'][:300]})
        if not details and quote:
            details=[{**evidence,'id':str(source['id'])+':raw','kind':'fact','meaning':'尚未细分的原始记录',
                      'importance':source['significance'],'emotional_salience':0,'importance_reason':'历史兼容表示',
                      'emotional_reason':'本地兼容表示不推测情绪','source_reliability':reliability}]
        return {'id':source['id'],'title':source['title'] or '历史对话','gist':source['summary'] or '保存了完整的历史对话，可查阅原文。',
                'context_type':source['context_type'],'importance':source['significance'],'importance_reason':'历史兼容表示',
                'emotional_salience':0,'emotional_reason':'','recorded_at':source['recorded_at'],
                **event_time,'time_quote':source['time_quote'],'source_reliability':reliability,'encoding_version':'legacy-local-v1',
                'updated_at':source['recorded_at'],'last_rehearsed_at':None,
                'revision':source['revision'],'source_hash':source['source_hash'],'participants':source['participants'],
                'allow_cloud':source['allow_cloud'],'context_stale':source['context_stale'],'deleted_at':None,
                'details':details,'cues':cues,'evidence':[evidence],'source_experience_ids':[source['id']],
                'source_revisions':{str(source['id']):source['revision']},
                'rehearsal_count':0,'legacy_projection':True,'local_fallback':True}

    @staticmethod
    def _source_details(item, query):
        """Expand matching original sentences without model guesses or writes."""
        query_terms=set(tokens(query))
        if not query_terms:
            return item
        covered={(str(d['experience_id']),d['role'],d['start'],d['end']) for d in item['details']}
        candidates=[]
        for evidence in item.get('evidence',[]):
            for match in re.finditer(r'[^。！？\n]+[。！？]?',evidence['quote']):
                quote=match.group()
                matching=len(query_terms.intersection(tokens(quote)))
                if not matching:
                    continue
                start=evidence['start']+match.start()
                end=evidence['start']+match.end()
                identity=(str(evidence['experience_id']),evidence['role'],start,end)
                if identity in covered:
                    continue
                covered.add(identity)
                kind='amount' if re.search(r'\d+(?:\.\d+)?\s*(元|块|美元|人民币)',quote) else 'fact'
                detail={key:evidence[key] for key in ('experience_id','role','source_revision','source_hash','source_recorded_at')}
                detail.update(id='span:'+sha256(str(identity).encode()).hexdigest()[:24],quote=quote,start=start,end=end,
                              kind=kind,meaning='按当前线索展开的原始片段',importance=0.2,emotional_salience=0,
                              importance_reason='本地按线索展开，不推断长期意义',emotional_reason='本地展开不推测情绪',
                              source_reliability=source_reliability(item['context_type'],evidence['role']))
                candidates.append((matching,detail))
        candidates.sort(key=lambda pair:(-pair[0],pair[1]['start']))
        item['details'].extend(detail for _,detail in candidates[:16])
        return item

    def episodes(self, limit=50, offset=0, cloud_only=False):
        with self.pool.connection() as c:
            rows = c.execute('''SELECT id FROM episodes WHERE deleted_at IS NULL
                AND (%s=false OR (allow_cloud AND NOT context_stale))
                ORDER BY recorded_at DESC,id LIMIT %s OFFSET %s''',(cloud_only,limit,offset)).fetchall()
            return [self._episode_view(c,r['id']) for r in rows]

    def episode_candidates(self, query, vector=None, limit=80, cloud_only=True, after=None,
                           before=None, time_basis='recorded_at', conversation_id=None, exact=False):
        from app.repository import vector_literal
        if time_basis not in ('recorded_at','occurred_at'):
            raise ValueError('invalid_time_basis')
        filters = 'e.deleted_at IS NULL AND NOT e.context_stale'
        args = []
        if cloud_only:
            filters += ' AND e.allow_cloud'
        if after is not None and time_basis=='occurred_at':
            filters+=' AND (e.occurred_until>%s OR (e.occurred_until IS NULL AND e.occurred_at>=%s))'
            args.extend((after,after))
        for value,clause in [(after if time_basis!='occurred_at' else None,f'e.{time_basis}>=%s'),(before,f'e.{time_basis}<%s')]:
            if value is not None:
                filters += ' AND ' + clause
                args.append(value)
        if conversation_id is not None:
            filters += ''' AND EXISTS(SELECT 1 FROM episode_evidence ev JOIN experiences x ON x.id=ev.experience_id
                WHERE ev.episode_id=e.id AND x.conversation_id=%s)'''
            args.append(conversation_id)
        ranked = {}
        legacy_ids = set()
        def add(eid, method, score=0, **extra):
            hit = ranked.setdefault(eid,{'score':0.0,'methods':[],'similarity':0.0,'matched_cues':[]})
            if method not in hit['methods']:
                hit['methods'].append(method)
                hit['score'] += score
            hit.update(extra)
        with self.pool.connection() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            keyword = ts_query(query)
            if keyword:
                rows = c.execute(f'''SELECT e.id,ts_rank_cd(e.terms,to_tsquery('simple',%s)) AS rank
                    FROM episodes e WHERE {filters} AND e.terms@@to_tsquery('simple',%s)
                    ORDER BY rank DESC,e.recorded_at DESC,e.id LIMIT %s''',(keyword,*args,keyword,limit)).fetchall()
                for i,row in enumerate(rows,1):
                    add(row['id'],'keyword',1/(60+i))
                cues = c.execute(f'''SELECT u.episode_id,u.kind,u.text,p.quote,
                    (SELECT count(DISTINCT f.episode_id) FROM episode_cues f JOIN episodes z ON z.id=f.episode_id
                     JOIN episode_evidence fp ON fp.id=f.evidence_id
                     WHERE f.text=u.text AND z.deleted_at IS NULL AND NOT z.context_stale AND (%s=false OR z.allow_cloud)
                     AND NOT EXISTS(SELECT 1 FROM episode_corrections correction
                         WHERE correction.experience_id=fp.experience_id AND fp.role='user'
                         AND correction.start_offset<fp.end_offset AND correction.end_offset>fp.start_offset)) AS frequency
                    FROM episode_cues u JOIN episodes e ON e.id=u.episode_id JOIN episode_evidence p ON p.id=u.evidence_id
                    WHERE {filters} AND NOT EXISTS(SELECT 1 FROM episode_corrections correction
                        WHERE correction.experience_id=p.experience_id AND p.role='user'
                        AND correction.start_offset<p.end_offset AND correction.end_offset>p.start_offset)
                    AND (position(lower(u.text) IN lower(%s))>0
                     OR (length(%s)>=2 AND position(lower(%s) IN lower(u.text))>0))
                    ORDER BY frequency,e.recorded_at DESC,u.id LIMIT %s''',
                    (cloud_only,*args,query,query,query,limit*4)).fetchall()
                for cue in cues:
                    add(cue['episode_id'],'cue',1/60)
                    ranked[cue['episode_id']]['matched_cues'].append({k:cue[k] for k in ('kind','text','quote','frequency')})
            if vector is not None:
                literal = vector_literal(vector)
                if exact:
                    c.execute('SET LOCAL enable_indexscan=off')
                # ORDER BY distance directly permits HNSW; visibility and source
                # revision are checked before returning any candidate.
                rows = c.execute(f'''SELECT e.id,1-(p.embedding<=>%s::vector) AS similarity
                    FROM episode_chunks p JOIN episodes e ON e.id=p.episode_id AND e.revision=p.revision
                    AND e.source_hash=p.source_hash WHERE {filters} AND p.model=%s
                    ORDER BY p.embedding<=>%s::vector LIMIT %s''',
                    (literal,*args,self.settings.embedding_model,literal,max(limit*4,100))).fetchall()
                for i,row in enumerate(rows,1):
                    if row['similarity'] >= self.settings.experience_min_similarity:
                        old = ranked.get(row['id'],{}).get('similarity',0)
                        add(row['id'],'semantic',1/(60+i),similarity=max(old,float(row['similarity'])))
            # Recent events supply high-baseline candidates even with weak cues.
            recent_limit = limit if not query.strip() else min(20,limit)
            rows = c.execute(f'''SELECT e.id FROM episodes e WHERE {filters}
                ORDER BY e.recorded_at DESC,e.id LIMIT %s''',(*args,recent_limit)).fetchall()
            for i,row in enumerate(rows,1):
                add(row['id'],'recent',1/(120+i))
            legacy_filters='e.deleted_at IS NULL AND NOT e.context_stale AND NOT EXISTS(SELECT 1 FROM episode_evidence p WHERE p.experience_id=e.id)'
            legacy_args=[]
            if cloud_only:
                legacy_filters+=' AND e.allow_cloud'
            for value,clause in [(after,f'e.{time_basis}>=%s'),(before,f'e.{time_basis}<%s'),
                                 (conversation_id,'e.conversation_id=%s')]:
                if value is not None:
                    legacy_filters+=' AND '+clause
                    legacy_args.append(value)
            if keyword:
                old=c.execute(f'''SELECT e.id,ts_rank_cd(e.terms,to_tsquery('simple',%s)) AS rank
                    FROM experiences e WHERE {legacy_filters} AND e.terms@@to_tsquery('simple',%s)
                    ORDER BY rank DESC,e.recorded_at DESC,e.id LIMIT %s''',(keyword,*legacy_args,keyword,limit)).fetchall()
                for i,row in enumerate(old,1):
                    legacy_ids.add(row['id'])
                    add(row['id'],'keyword',1/(60+i))
            old=c.execute(f'''SELECT e.id FROM experiences e WHERE {legacy_filters}
                ORDER BY e.recorded_at DESC,e.id LIMIT %s''',(*legacy_args,recent_limit)).fetchall()
            for i,row in enumerate(old,1):
                legacy_ids.add(row['id'])
                add(row['id'],'recent',1/(120+i))
            if time_basis=='occurred_at' and (after is not None or before is not None):
                for eid in legacy_ids:
                    legacy=self._legacy_episode_view(c,eid)
                    start,end=legacy['occurred_at'],legacy['occurred_until']
                    if start is None or (after is not None and (end or start)<=after) or (before is not None and start>=before):
                        ranked.pop(eid,None)
            selected = sorted(ranked.items(),key=lambda p:(-p[1]['score'],str(p[0])))[:limit]
            return [{**self._source_details(self._legacy_episode_view(c,eid) if eid in legacy_ids else self._episode_view(c,eid),query),**hit}
                    for eid,hit in selected]

    def episode_associated(self, ids, cloud_only=True, limit=20):
        if not ids:
            return []
        with self.pool.connection() as c:
            rows = c.execute('''WITH neighbors AS (
                SELECT source_id AS root,target_id AS id,kind FROM episode_associations WHERE source_id=ANY(%s::uuid[])
                UNION SELECT target_id AS root,source_id AS id,kind FROM episode_associations WHERE target_id=ANY(%s::uuid[])
            ) SELECT n.id,array_agg(DISTINCT n.root) AS association_sources,array_agg(DISTINCT n.kind) AS association_kinds
            FROM neighbors n JOIN episodes e ON e.id=n.id JOIN episodes seed ON seed.id=n.root
            WHERE NOT (n.id=ANY(%s::uuid[])) AND e.deleted_at IS NULL AND NOT e.context_stale
             AND seed.deleted_at IS NULL AND NOT seed.context_stale
             AND (%s=false OR (e.allow_cloud AND seed.allow_cloud))
            GROUP BY n.id ORDER BY n.id LIMIT %s''',(ids,ids,ids,cloud_only,limit)).fetchall()
            return [{**self._episode_view(c,r['id']),**r,'methods':['association'],'similarity':0,
                     'matched_cues':[]} for r in rows]

    def _save_rehearsals(self, c, experience_id, rehearsals):
        at = self.clock.now()
        for item in rehearsals or []:
            eid = item.get('episode_id') if isinstance(item,dict) else item
            kind = item.get('kind','discussion') if isinstance(item,dict) else 'discussion'
            if kind not in ('discussion','confirmation','supplement','continuation','correction'):
                raise ValueError('invalid_rehearsal_kind')
            row = c.execute('SELECT * FROM episodes WHERE id=%s FOR UPDATE',(eid,)).fetchone()
            if not row or row['deleted_at'] or row['context_stale'] or not row['allow_cloud']:
                raise MemoryError('context_changed','回忆来源已变更，请重新提问。',409)
            if row['last_rehearsed_at'] and (at-row['last_rehearsed_at']).total_seconds()<86400:
                continue
            c.execute('''INSERT INTO episode_rehearsals(episode_id,experience_id,kind,recalled_at)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(eid,experience_id,kind,at))
            c.execute('UPDATE episodes SET last_rehearsed_at=%s WHERE id=%s',(at,eid))

    def claim_episode_job(self):
        with self.pool.connection() as c:
            return c.execute('''WITH selected AS (SELECT episode_id FROM episode_jobs
                WHERE (status='pending' AND available_at<=now()) OR (status='running' AND lease_until<now())
                ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE episode_jobs j SET status='running',attempts=attempts+1,lease_token=%s,
                lease_until=now()+interval '120 seconds' FROM selected s WHERE j.episode_id=s.episode_id RETURNING j.*''',
                (uuid4(),)).fetchone()

    def episode_job_source(self, job):
        with self.pool.connection() as c:
            row = c.execute('''SELECT id FROM episodes WHERE id=%s AND revision=%s AND source_hash=%s
                AND deleted_at IS NULL AND NOT context_stale''',(job['episode_id'],job['revision'],job['source_hash'])).fetchone()
            return self._episode_view(c,row['id']) if row else None

    def finish_episode_index(self, job, texts, vectors):
        from app.repository import vector_literal
        if len(texts)!=len(vectors):
            raise ValueError('incomplete_embeddings')
        with self.pool.connection() as c:
            self._state_lock(c)
            lease = c.execute('SELECT * FROM episode_jobs WHERE episode_id=%s FOR UPDATE',(job['episode_id'],)).fetchone()
            row = c.execute('SELECT * FROM episodes WHERE id=%s',(job['episode_id'],)).fetchone()
            if not lease or lease['status']!='running' or lease['lease_token']!=job['lease_token']:
                return False
            if not row or row['deleted_at'] or row['context_stale'] or row['revision']!=job['revision'] or row['source_hash']!=job['source_hash']:
                c.execute("UPDATE episode_jobs SET status='cancelled',lease_token=NULL WHERE episode_id=%s",(job['episode_id'],))
                return False
            c.execute('DELETE FROM episode_chunks WHERE episode_id=%s',(job['episode_id'],))
            for i,(text,vector) in enumerate(zip(texts,vectors)):
                c.execute('''INSERT INTO episode_chunks(episode_id,revision,source_hash,chunk_index,content,embedding,model)
                    VALUES(%s,%s,%s,%s,%s,%s::vector,%s)''',
                    (job['episode_id'],job['revision'],job['source_hash'],i,text,vector_literal(vector),self.settings.embedding_model))
            c.execute("UPDATE episode_jobs SET status='done',lease_token=NULL,last_error=NULL WHERE episode_id=%s",(job['episode_id'],))
            return True

    def fail_episode_job(self, job):
        with self.pool.connection() as c:
            c.execute('''UPDATE episode_jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,
                available_at=now()+interval '10 seconds',lease_token=NULL,last_error='embedding_unavailable'
                WHERE episode_id=%s AND status='running' AND lease_token=%s''',(job['episode_id'],job['lease_token']))
