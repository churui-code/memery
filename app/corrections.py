"""Explicit source-range corrections; raw archives are never rewritten."""
from copy import deepcopy
import re
from hashlib import sha256
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field
from app.errors import MemoryError
from app.source_units import source_units
from app.text import terms


class EpisodeCorrection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_id: UUID
    expected_revision: int = Field(ge=1)
    experience_id: UUID
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    old_quote: str | None = Field(default=None, min_length=1, max_length=8000,
        description='被否定的旧主张，逐字取自选中旧detail；只选错误分句，不带未被纠正的另一笔金额。')
    quote: str = Field(min_length=2, max_length=8000,
        description='当前用户明确纠正旧错误的逐字原话；不是后来真实变化。')


def correction_targets(payload):
    """Name only user details in this exact frozen, cloud-visible context."""
    targets = {}
    seen = set()
    for episode in payload.get('experience_evidence', []):
        if episode.get('legacy_projection'):
            continue
        for detail in episode.get('details', []):
            if detail.get('role') != 'user':
                continue
            target = {'target_id': str(episode['id']), 'expected_revision': episode['revision'],
                      'experience_id': str(detail['experience_id']),
                      'start': detail['start'], 'end': detail['end']}
            identity = tuple(target.values())
            if identity in seen:
                continue
            seen.add(identity)
            targets[f'c{len(targets)+1:04d}'] = {**target, 'quote': detail['quote']}
    return targets


def validate_correction_appends(episodes, corrections):
    targets={str(p.target_id) for p in corrections}
    if any(str(p.get('target_id') if isinstance(p,dict) else p.target_id) in targets for p in episodes):
        raise ValueError('correction_and_append_conflict')


def validate_corrections(proposals, text, episodes):
    available = {str(e['id']): e for e in episodes}
    seen = set()
    for p in proposals:
        episode = available.get(str(p.target_id))
        if not episode or episode['revision'] != p.expected_revision or p.quote not in text:
            raise ValueError('correction_source_unavailable')
        detail = next((d for d in episode.get('details', []) if str(d.get('experience_id'))==str(p.experience_id)
                       and d.get('role')=='user' and d.get('start')==p.start and d.get('end')==p.end), None)
        if detail is None:
            raise ValueError('correction_detail_unavailable')
        if p.old_quote is not None:
            offset = detail['quote'].find(p.old_quote)
            if offset<0 or detail['quote'].find(p.old_quote,offset+1)>=0:
                raise ValueError('correction_old_quote_ambiguous')
            p.start += offset
            p.end = p.start+len(p.old_quote)
        if any(str(p.experience_id)==source and p.start<end and p.end>start for source,start,end in seen):
            raise ValueError('correction_ranges_overlap')
        seen.add((str(p.experience_id),p.start,p.end))


def overlaps(row, mask):
    return (str(row.get('experience_id')) == str(mask['experience_id'])
            and row.get('role') == 'user' and row.get('start', 0) < mask['end']
            and row.get('end', 0) > mask['start'])


def sanitize_episode(item, masks, *, discard_metadata=False):
    """Keep complete uncorrected source sentences; never splice quotations."""
    relevant = [m for m in masks if str(m['experience_id']) in
                {str(x) for x in item.get('source_experience_ids', [])}]
    if not relevant and not discard_metadata:
        return item
    item = deepcopy(item)
    def safe_parts(row):
        if row.get('role')!='user':
            return []
        if not any(overlaps(row,m) for m in relevant):
            return [row]
        parts=[]
        # Preserve complete clauses, including original punctuation; never join across a correction.
        for unit in source_units(row['quote']):
            for match in re.finditer(r'[^，,。！？!?；;\n]+[，,。！？!?；;\n]*[”’」』）)\]】]*',unit['text']):
                part={**row,'quote':match.group(),'start':row['start']+unit['start']+match.start(),
                      'end':row['start']+unit['start']+match.end()}
                if not any(overlaps(part,m) for m in relevant):
                    part.pop('id',None)
                    parts.append(part)
        return parts
    retained=[]
    for detail in item.get('details',[]):
        for part in safe_parts(detail):
            if 'id' not in part:
                part.update(id='span:'+sha256(str((part.get('experience_id'),part['start'],part['end'])).encode()).hexdigest()[:24],
                            meaning='未被纠正的原始分句',importance_reason='',emotional_reason='')
                part.pop('covered_detail_ids',None)
            # Metadata may paraphrase corrected claims outside this retained quote.
            part.update(meaning='纠错后保留的用户原话',importance_reason='',emotional_reason='')
            retained.append(part)
    item['details']=retained
    item['cues']=[{**d,'text':d['quote'][:300]} for d in item.get('cues',[])
                  if d.get('role')=='user' and not any(overlaps(d,m) for m in relevant)]
    evidence=[part for row in item.get('evidence',[]) for part in safe_parts(row)]
    item['evidence'] = evidence
    # Old summaries can repeat the superseded claim even when selected details do not.
    item['title'] = '已纠错事件的有效原文'
    item['gist'] = '\n'.join(dict.fromkeys(d['quote'] for d in item.get('details', []))) or '部分旧细节已纠正。'
    item['importance_reason'] = item['emotional_reason'] = ''
    item['participants'] = []
    time_quote = item.get('time_quote')
    for row in item.get('details', []) + item.get('evidence', []):
        if time_quote and time_quote in row.get('quote', ''):
            offset = row['quote'].find(time_quote)
            span = {**row, 'start':row['start']+offset, 'end':row['start']+offset+len(time_quote)}
            if any(overlaps(span,m) for m in relevant):
                item.update(time_quote=None,occurred_at=None,occurred_until=None,time_precision='unknown')
    # If the time phrase disappeared with the corrected span, do not retain its derived date.
    if time_quote and not any(time_quote in row.get('quote','') for row in evidence):
        item.update(time_quote=None,occurred_at=None,occurred_until=None,time_precision='unknown')
    item['matched_cues'] = []
    item['correction_filtered'] = True
    return item


class CorrectionRepository:
    @staticmethod
    def _correction_masks(c, source_ids):
        # Tombstones remain effective even when the replacement is deleted.
        return c.execute('''SELECT experience_id,start_offset AS start,end_offset AS end FROM episode_corrections
            WHERE experience_id=ANY(%s::uuid[])''',(list(source_ids),)).fetchall()

    def correction_context(self, proposals):
        if not proposals:
            return [], [], []
        with self.pool.connection() as c:
            self._check_corrections(c, proposals)
            masks = [p.model_dump() for p in proposals]
            roots = [('experience', p.experience_id) for p in proposals]
            affected = self.context_descendants(c, roots)
            originals = {p.experience_id for p in proposals}
            return masks, affected, list(originals)

    def _check_corrections(self, c, proposals):
        seen=[]
        for p in proposals:
            if any(p.experience_id==sid and p.start<end and p.end>start for sid,start,end in seen):
                raise MemoryError('memory_decision_invalid','同一轮纠错范围不能重叠。',502)
            seen.append((p.experience_id,p.start,p.end))
            row = c.execute('SELECT * FROM episodes WHERE id=%s FOR UPDATE',(p.target_id,)).fetchone()
            source = c.execute('SELECT * FROM experiences WHERE id=%s',(p.experience_id,)).fetchone()
            if (not row or row['revision'] != p.expected_revision or row['deleted_at'] or row['context_stale']
                    or not row['allow_cloud'] or not source or source['deleted_at'] or source['context_stale']
                    or not source['allow_cloud'] or p.end > len(source['user_text']) or p.end <= p.start
                    or (p.old_quote is not None and source['user_text'][p.start:p.end]!=p.old_quote)):
                raise MemoryError('context_changed','纠错来源已改变，请重新读取。',409)
            if not c.execute('''SELECT 1 FROM episode_evidence WHERE episode_id=%s AND experience_id=%s
                AND role='user' AND start_offset<=%s AND end_offset>=%s''',
                (p.target_id,p.experience_id,p.start,p.end)).fetchone():
                raise MemoryError('context_changed','纠错原文范围不存在。',409)
            if any(overlaps({'experience_id':p.experience_id,'role':'user','start':p.start,'end':p.end},m)
                   for m in self._correction_masks(c,[p.experience_id])):
                raise MemoryError('context_changed','该旧细节已经纠正，请使用当前证据。',409)

    def _prepare_corrections(self, c, proposals):
        if not proposals:
            return {'episodes':[],'independent':[]}
        self._check_corrections(c, proposals)
        source_ids = list({p.experience_id for p in proposals})
        # Any episode representing the same original span must be sanitized too.
        ids = [r['episode_id'] for r in c.execute('''SELECT DISTINCT v.episode_id FROM episode_evidence v JOIN episodes e ON e.id=v.episode_id
            WHERE v.experience_id=ANY(%s::uuid[]) AND e.deleted_at IS NULL AND NOT e.context_stale''',(source_ids,)).fetchall()]
        affected = self.context_descendants(c, [('experience',sid) for sid in source_ids])
        # Context dependencies taint old assistant answers, not independent user testimony.
        independent = [r['id'] for r in c.execute('''SELECT e.id FROM episodes e
            WHERE e.id=ANY(%s::uuid[]) AND NOT e.context_stale AND e.deleted_at IS NULL
            AND NOT EXISTS(SELECT 1 FROM episode_evidence v WHERE v.episode_id=e.id
                AND (v.role<>'user' OR v.experience_id=ANY(%s::uuid[])))''',
            ([r['id'] for r in affected if r['kind']=='episode' and r['id'] not in ids],source_ids)).fetchall()]
        return {'episodes':ids,'independent':independent}

    def _apply_corrections(self, c, proposals, replacement_id, prepared):
        if not proposals:
            return
        # The transaction lock and source/version checks precede any fact invalidation.
        source_ids = list({p.experience_id for p in proposals})
        ids = list(prepared['episodes'])
        independent = prepared['independent']
        affected = self.context_descendants(c, [('experience',sid) for sid in source_ids])
        affected_experiences = [r['id'] for r in affected if r['kind']=='experience']
        self.invalidate_context(c, [('experience',sid) for sid in source_ids])
        c.execute('UPDATE experiences SET correction_history_blocked=true WHERE id=ANY(%s::uuid[])',
                  (affected_experiences,))
        for eid in independent:
            quotes = c.execute('SELECT DISTINCT quote FROM episode_evidence WHERE episode_id=%s',(eid,)).fetchall()
            gist = '\n'.join(r['quote'] for r in quotes)
            c.execute("""UPDATE episodes SET context_stale=false,title='独立用户原文',gist=%s,
                participants='[]',importance_reason='',emotional_reason='' WHERE id=%s""",(gist,eid))
            c.execute("""UPDATE episode_details SET meaning='独立用户原文',importance_reason='',emotional_reason=''
                WHERE episode_id=%s""",(eid,))
            c.execute('''UPDATE episode_cues u SET text=left(p.quote,300) FROM episode_evidence p
                WHERE u.evidence_id=p.id AND u.episode_id=%s''',(eid,))
            c.execute('''UPDATE experiences SET context_stale=false WHERE id IN
                (SELECT experience_id FROM episode_evidence WHERE episode_id=%s)''',(eid,))
        ids.extend(independent)
        c.execute('UPDATE experiences SET context_stale=false WHERE id=ANY(%s::uuid[])',(source_ids,))
        for p in proposals:
            c.execute('''INSERT INTO episode_corrections(id,episode_id,episode_revision,experience_id,
                start_offset,end_offset,replacement_experience_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                (uuid4(),p.target_id,p.expected_revision,p.experience_id,p.start,p.end,replacement_id,self.clock.now()))
        # Raw originals remain local and inspectable, but their complete chat/index must not go to cloud.
        c.execute('DELETE FROM messages WHERE experience_id=ANY(%s::uuid[])',(source_ids,))
        c.execute('DELETE FROM experience_chunks WHERE experience_id=ANY(%s::uuid[])',(source_ids,))
        c.execute("UPDATE experience_jobs SET status='cancelled',lease_token=NULL WHERE experience_id=ANY(%s::uuid[])",(source_ids,))
        for eid in ids:
            c.execute('UPDATE episodes SET context_stale=false WHERE id=%s',(eid,))
            safe = self._episode_view(c,eid)
            digest = sha256((safe['source_hash']+str(replacement_id)).encode()).hexdigest()
            searchable = '\n'.join([safe['title'],safe['gist'],*(e['quote'] for e in safe['evidence'])])
            c.execute('''UPDATE episodes SET title=%s,gist=%s,terms=to_tsvector('simple',%s),
                revision=revision+1,source_hash=%s,updated_at=%s,
                occurred_at=%s,occurred_until=%s,time_quote=%s,time_precision=%s WHERE id=%s''',
                (safe['title'],safe['gist'],terms(searchable),digest,self.clock.now(),
                 safe.get('occurred_at'),safe.get('occurred_until'),safe.get('time_quote'),safe.get('time_precision','unknown'),eid))
            c.execute('DELETE FROM episode_chunks WHERE episode_id=%s',(eid,))
            c.execute("DELETE FROM episode_associations WHERE (source_id=%s OR target_id=%s) AND kind<>'correction'",(eid,eid))
            c.execute('''INSERT INTO episode_jobs(episode_id,revision,source_hash)
                SELECT id,revision,source_hash FROM episodes WHERE id=%s
                ON CONFLICT(episode_id) DO UPDATE SET revision=EXCLUDED.revision,source_hash=EXCLUDED.source_hash,
                status='pending',attempts=0,available_at=now(),lease_token=NULL,last_error=NULL''',(eid,))
            c.execute('''INSERT INTO episode_versions(episode_id,revision,title,gist,importance,importance_reason,
                emotional_salience,emotional_reason,source_hash,created_at,source_reliability,encoding_version,
                time_precision,occurred_at,occurred_until,time_quote)
                SELECT id,revision,title,gist,importance,'',emotional_salience,'',source_hash,updated_at,
                source_reliability,encoding_version,time_precision,occurred_at,occurred_until,time_quote
                FROM episodes WHERE id=%s''',(eid,))
        self._link(c, [('experience',sid) for sid in source_ids], 'experience', replacement_id)

    @staticmethod
    def _link_correction_episodes(c, proposals, episode_ids):
        for p in proposals:
            # All prior corrections of this explicitly identified event point to
            # the latest source, so recall does not need unbounded association hops.
            related = c.execute("""WITH RECURSIVE related(id) AS (
                SELECT %s::uuid UNION
                SELECT CASE WHEN a.source_id=r.id THEN a.target_id ELSE a.source_id END
                FROM related r JOIN episode_associations a ON (a.source_id=r.id OR a.target_id=r.id)
                WHERE a.kind='correction') SELECT id FROM related""",(p.target_id,)).fetchall()
            for row in related:
                for eid in episode_ids:
                    if eid and eid!=row['id']:
                        c.execute("""INSERT INTO episode_associations(source_id,target_id,kind)
                            VALUES(%s,%s,'correction') ON CONFLICT DO NOTHING""",(row['id'],eid))
