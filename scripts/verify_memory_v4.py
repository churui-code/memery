#!/usr/bin/env python3
"""Isolated v0.4 acceptance. Every stored record is from frozen synthetic fixtures.

Run with --real-model only when live-provider verification is authorized. The
default performs real local BGE retrieval and separately labelled full-text
scale measurements; --full-index indexes all scale distractors before timing.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo), 3)


def error_record(error):
    result = {'type': type(error).__name__, 'code': getattr(error, 'code', None)}
    cause = error.__cause__
    if cause is not None:
        if hasattr(cause, 'errors'):
            result['validation'] = [{'location': list(row['loc']), 'type': row['type']} for row in cause.errors(include_url=False, include_context=False, include_input=False)]
        elif isinstance(cause, ValueError):
            result['validation_code'] = str(cause)[:200]
    return result


def preserve_failed_report(destination):
    if not destination.exists():
        return
    try:
        previous = json.loads(destination.read_text())
    except (ValueError, UnicodeError):
        return
    if isinstance(previous, dict) and previous.get('passed') is False:
        history = destination.parent / 'verification-history'
        history.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        shutil.copy2(destination, history / f'{destination.stem}-{stamp}-{uuid4().hex}.json')


def inside(args):
    sys.path.insert(0, str(Path.cwd()))
    from psycopg import sql
    from app.clock import MutableClock
    from app.config import Settings
    from app.context import ESTIMATOR_VERSION
    from app.embeddings import LocalEmbeddings
    from app.lab_scenarios import SCENARIO_VERSION, encoded_event
    from app.model import MiniMax
    from app.recall import POLICY_VERSION, RecallEngine
    from app.repository import Repository
    from app.service import MemoryService
    from app.text import chunks
    from memory_v4_fixtures import EVENTS, FIXTURE_VERSION, FROZEN_SHA256, MODEL_CASES, QUERIES

    frozen = json.dumps({'events': EVENTS, 'queries': QUERIES, 'model_cases': MODEL_CASES},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    report = {'version': '0.4', 'started_at': datetime.now(timezone.utc).isoformat(),
        'isolation': 'random temporary PostgreSQL schema; separate temporary deletion journal; synthetic records only',
        'fixture_version': FIXTURE_VERSION, 'fixture_sha256': sha256(frozen.encode()).hexdigest(),
        'fixture_encoder_version': SCENARIO_VERSION, 'policy_version': POLICY_VERSION,
        'estimator_version': ESTIMATOR_VERSION, 'real_model_requested': args.real_model,
        'embedding_stage': {'cases': []}, 'performance_stage': {'scales': []},
        'generation_stage': {'enabled': args.real_model, 'cases': []}, 'failures': [], 'passed': False}
    if report['fixture_sha256'] != FROZEN_SHA256:
        report['failures'].append({'stage': 'fixture', 'reason': 'frozen_fixture_hash_changed', 'expected': FROZEN_SHA256})
    schema = 'memory_v4_qa_' + uuid4().hex
    repo = None
    with tempfile.TemporaryDirectory(prefix='memory-v4-qa-') as temporary:
        journal = Path(temporary) / 'deletions.jsonl'
        journal.touch()
        try:
            settings = replace(Settings(), db_schema=schema, deletion_journal=str(journal), lab_enabled=True)
            clock = MutableClock(datetime(2025, 1, 1, 1, tzinfo=timezone.utc))
            repo = Repository(settings, clock=clock)
            repo.migrate()
            embeddings = LocalEmbeddings(settings)
            report.update(embedding_model=settings.embedding_model, embedding_dimensions=embeddings.dimensions,
                model=settings.model_name, model_provider='MiniMax', environment='isolated Docker API image')
            expected = {}

            def index_pending():
                """Batch the exact production passage construction, not a new encoder."""
                completed = 0
                while True:
                    pending, passages = [], []
                    for _ in range(64):
                        job = repo.claim_episode_job()
                        if not job:
                            break
                        source = repo.episode_job_source(job)
                        if not source:
                            repo.finish_episode_index(job, [], [])
                            continue
                        text = '\n'.join([source['title'], source['gist'],
                            *(cue['text'] for cue in source['cues']), *(item['quote'] for item in source['evidence'])])
                        parts = chunks(text)
                        pending.append((job, parts, len(passages)))
                        passages.extend(parts)
                    if not pending:
                        return completed
                    try:
                        vectors = embeddings.embed(passages)
                        for job, parts, offset in pending:
                            repo.finish_episode_index(job, parts, vectors[offset:offset + len(parts)])
                            completed += 1
                    except Exception:
                        for job, _, _ in pending:
                            repo.fail_episode_job(job)
                        raise

            def save_fixture(event):
                description, episodes, _ = encoded_event(event)
                if event.get('occurred_at'):
                    at = datetime.fromisoformat(event['occurred_at'])
                    description = description.model_copy(update={'occurred_at': at, 'time_quote': event['time_quote']})
                    episodes = [value.model_copy(update={'occurred_at': at, 'time_quote': event['time_quote']}) for value in episodes]
                committed = repo.save_answer(uuid4(), event['text'], '已记录这段合成故事。', [], repo.epoch(),
                    experience_description=description, episode_proposals=episodes)
                return {'episode_ids': [str(eid) for eid in committed['episode_ids']],
                    'experience_ids': [str(committed['experience_id'])]}

            for event in EVENTS:
                expected[event['key']] = save_fixture(event)
            clock.advance(days=1)
            index_start = time.perf_counter()
            index_pending()
            with repo.pool.connection() as connection:
                initial_index = connection.execute("SELECT status,count(*) AS n FROM episode_jobs GROUP BY status").fetchall()
            initial_counts = {row['status']: row['n'] for row in initial_index}
            report['embedding_stage'].update(index_seconds=round(time.perf_counter()-index_start, 3),
                index_jobs=initial_counts, event_count=len(EVENTS), query_count=len(QUERIES),
                groups=dict(Counter(case['group'] for case in QUERIES)))
            if initial_counts.get('done', 0) != len(EVENTS):
                report['failures'].append({'stage': 'embedding', 'reason': 'fixture_index_incomplete', 'counts': initial_counts})

            def inspect_case(case, engine, exact=False):
                start = time.perf_counter()
                entry = {'id': case['id'], 'group': case['group'], 'query': case['query'],
                    'expected': expected[case['key']], 'required_quotes': case['required_quotes']}
                try:
                    result = engine.preview(case['query'], exact=exact)
                    actual_episodes = [str(item['id']) for item in result['evidence']]
                    actual_sources = sorted({str(source) for item in result['evidence']
                        for source in item.get('source_experience_ids', [])})
                    # Never count a keyword merely repeated in the query or
                    # candidate trace as if it reached the answering context.
                    text = json.dumps(result['payload']['experience_evidence'], ensure_ascii=False, default=str)
                    details = [detail['quote'] for item in result['evidence'] for detail in item.get('details', [])]
                    target = set(entry['expected']['episode_ids'])
                    source_hit = bool(target.intersection(actual_episodes))
                    candidate_hit = any(str(row['id']) in target for row in result['candidates'])
                    quote_checks = {quote: quote in text for quote in case['required_quotes']}
                    budget_ok = result['budget']['estimated_tokens'] <= result['budget']['effective_tokens']
                    rank = next((i + 1 for i, row in enumerate(result['candidates']) if str(row['id']) in target), None)
                    entry.update(actual_episode_ids=actual_episodes, actual_source_ids=actual_sources,
                        candidate_episode_ids=[str(row['id']) for row in result['candidates']],
                        actual_quotes=details, candidate_hit=candidate_hit, evidence_hit=source_hit,
                        target_evidence_fraction=len(target.intersection(actual_episodes))/max(1, len(actual_episodes)),
                        false_awakened_count=len(set(actual_episodes) - target),
                        candidate_rank=rank, quote_checks=quote_checks, budget=result['budget'],
                        budget_ok=budget_ok, embedding_status=result['embedding_status'],
                        passed=source_hit and all(quote_checks.values()) and budget_ok)
                except Exception as error:
                    entry.update(passed=False, error=error_record(error))
                entry['milliseconds'] = round((time.perf_counter()-start)*1000, 3)
                return entry

            engine = RecallEngine(repo, embeddings)
            for case in QUERIES:
                entry = inspect_case(case, engine)
                report['embedding_stage']['cases'].append(entry)
                if not entry['passed']:
                    report['failures'].append({'stage': 'embedding', 'case': case['id']})
            entries = report['embedding_stage']['cases']
            report['embedding_stage']['metrics'] = {
                'candidate_recall': sum(row.get('candidate_hit', False) for row in entries)/len(entries),
                'evidence_recall': sum(row.get('evidence_hit', False) for row in entries)/len(entries),
                'mean_target_evidence_fraction': sum(row.get('target_evidence_fraction', 0) for row in entries)/len(entries),
                'mean_false_awakened_count': sum(row.get('false_awakened_count', 0) for row in entries)/len(entries),
                'pass_count': sum(row['passed'] for row in entries),
                'p50_ms': percentile([row['milliseconds'] for row in entries], .5),
                'p95_ms': percentile([row['milliseconds'] for row in entries], .95)}
            report['metric_definitions'] = {
                'target_evidence_fraction': 'Frozen expected episode IDs divided by all selected evidence episode IDs.',
                'false_awakened_count': 'Selected episode IDs outside the frozen expected target set. Related comparison events can be legitimate context; this count does not prove false attribution in the generated answer.',
                'quote_checks': 'Quotes must occur in final episodic context, not merely in the query or retrieval trace.'}
            print('v0.4: frozen BGE queries completed', file=sys.stderr, flush=True)

            # Keep the provider stage separate and before performance clutter.
            # The first ten probes explicitly disable storing new conversation
            # evidence, so generated answers cannot teach later probes the key.
            if args.real_model:
                class AuditedModel(MiniMax):
                    proposals = None
                    def _structured(self, system, prompt, tool):
                        result = super()._structured(system, prompt, tool)
                        self.proposals.append({'tool': tool['name'], 'proposal': result['decision']})
                        return result
                model = AuditedModel(settings)
                service = MemoryService(repo, embeddings, model)
                for case in MODEL_CASES:
                    model.proposals = []
                    before_state = {str(row['id']) for row in repo.self_state(per_domain=100)}
                    with repo.pool.connection() as connection:
                        call_ids_before = {str(row['id']) for row in connection.execute('SELECT id FROM model_calls').fetchall()}
                    entry = {'id': case['id'], 'query': case['query'],
                        'expected': expected.get(case.get('key')), 'required': case.get('required', []),
                        'required_any': case.get('required_any', [])}
                    question = case['query']
                    if not case.get('state_created') and not case.get('state_used'):
                        question += ' 本轮只是合成记忆测试，请不要保存本轮的新记录。'
                    start = time.perf_counter()
                    try:
                        result = service.chat(question, conversation_id=uuid4())
                        answer = result['answer']
                        context = result.get('actual_context', {})
                        actual_sources = sorted({str(row['id']) for row in result.get('experience_sources', [])})
                        checks = {'required_words': all(word in answer for word in case.get('required', [])),
                            'required_any': not case.get('required_any') or any(word in answer for word in case['required_any']),
                            'bounded_calls': 2 <= len(result.get('calls', [])) <= 3}
                        if case.get('key'):
                            checks['source_hit'] = bool(set(expected[case['key']]['experience_ids']).intersection(actual_sources))
                        if case.get('no_personal_facts'):
                            checks['no_personal_facts'] = repo.listing()['total'] == 0
                        if case.get('state_created'):
                            states = repo.self_state(per_domain=100)
                            checks['new_sourced_state'] = any(str(row['id']) not in before_state and row['reason'] and row['evidence_quote'] for row in states)
                        if case.get('state_used'):
                            checks['prior_state_in_context'] = any(str(row.get('id')) in before_state for row in context.get('self_state', []))
                        entry.update(answer=answer, actual_source_ids=actual_sources, checks=checks,
                            actual_quotes=[detail.get('quote') for event in context.get('experience_evidence', []) for detail in event.get('details', [])],
                            context_budget=result.get('recall_trace', {}).get('budget'),
                            self_changes=result.get('self_changes', []), passed=all(checks.values()))
                    except Exception as error:
                        entry.update(passed=False, error=error_record(error))
                    with repo.pool.connection() as connection:
                        calls = connection.execute('''SELECT id,phase,status,input_tokens,output_tokens,estimated_input_tokens,model
                            FROM model_calls ORDER BY created_at,id''').fetchall()
                    entry['actual_calls'] = [dict(row) for row in calls if str(row['id']) not in call_ids_before]
                    entry['synthetic_proposals'] = model.proposals
                    entry['milliseconds'] = round((time.perf_counter()-start)*1000, 3)
                    report['generation_stage']['cases'].append(entry)
                    if not entry['passed']:
                        report['failures'].append({'stage': 'generation', 'case': case['id']})
                    print('v0.4: M3 case ' + case['id'] + ' completed', file=sys.stderr, flush=True)
                report['generation_stage']['pass_count'] = sum(row['passed'] for row in report['generation_stage']['cases'])
            else:
                report['generation_stage'].update(status='skipped_without_real_model_flag', planned_case_ids=[case['id'] for case in MODEL_CASES])

            inserted = 0
            for scale in (50, 500, 5000):
                insert_start = time.perf_counter()
                while inserted < scale:
                    number = inserted + 1
                    noise = {'key': f'noise-{number}', 'title': f'合成日常散步记录{number}',
                        'text': f'合成故事：路人{number}在普通公园散步，随后到商店买纸巾。普通收据编号为R{number:06d}。',
                        'importance': .05, 'emotional_salience': 0,
                        'details': [(f'R{number:06d}', 'object', .05)],
                        'cues': [(f'R{number:06d}', 'object')]}
                    save_fixture(noise)
                    inserted += 1
                if args.full_index:
                    index_pending()
                with repo.pool.connection() as connection:
                    index_counts = {row['status']: row['n'] for row in connection.execute('SELECT status,count(*) AS n FROM episode_jobs GROUP BY status').fetchall()}
                total_jobs = sum(index_counts.values())
                performance = {'distractor_count': scale, 'insertion_and_index_seconds': round(time.perf_counter()-insert_start, 3),
                    'mode': 'hybrid_all_indexes_requested' if args.full_index else 'fulltext_same_activation_policy',
                    'index_jobs': index_counts, 'index_ready_ratio': index_counts.get('done', 0)/max(1, total_jobs),
                    'ann_performance_claimed': args.full_index and index_counts.get('done', 0) == total_jobs,
                    'samples': [], 'exact_samples': [], 'ann_exact_comparisons': []}
                performance_engine = RecallEngine(repo, embeddings if args.full_index else None)
                for repeat in range(args.repeats):
                    for case in QUERIES:
                        entry = inspect_case(case, performance_engine)
                        entry['repeat'] = repeat + 1
                        performance['samples'].append(entry)
                        if args.full_index:
                            exact_entry = inspect_case(case, performance_engine, exact=True)
                            exact_entry['repeat'] = repeat + 1
                            performance['exact_samples'].append(exact_entry)
                            ann_set = set(entry.get('candidate_episode_ids', []))
                            exact_set = set(exact_entry.get('candidate_episode_ids', []))
                            performance['ann_exact_comparisons'].append({'case': case['id'], 'repeat': repeat + 1,
                                'candidate_overlap_recall': len(ann_set & exact_set)/max(1, len(exact_set)),
                                'ann_evidence_hit': entry.get('evidence_hit', False),
                                'exact_evidence_hit': exact_entry.get('evidence_hit', False),
                                'ann_missing_exact_episode_ids': sorted(exact_set - ann_set)})
                samples = performance['samples']
                performance.update(p50_ms=percentile([row['milliseconds'] for row in samples], .5),
                    p95_ms=percentile([row['milliseconds'] for row in samples], .95),
                    evidence_recall=sum(row.get('evidence_hit', False) for row in samples)/len(samples),
                    mean_target_evidence_fraction=sum(row.get('target_evidence_fraction', 0) for row in samples)/len(samples),
                    mean_false_awakened_count=sum(row.get('false_awakened_count', 0) for row in samples)/len(samples),
                    budget_compliance=sum(row.get('budget_ok', False) for row in samples)/len(samples),
                    max_estimated_tokens=max((row.get('budget', {}).get('estimated_tokens', 0) for row in samples), default=0))
                if args.full_index:
                    exact_samples = performance['exact_samples']
                    performance.update(exact_p50_ms=percentile([row['milliseconds'] for row in exact_samples], .5),
                        exact_p95_ms=percentile([row['milliseconds'] for row in exact_samples], .95),
                        exact_evidence_recall=sum(row.get('evidence_hit', False) for row in exact_samples)/len(exact_samples),
                        ann_vs_exact_candidate_recall=sum(row['candidate_overlap_recall'] for row in performance['ann_exact_comparisons'])/len(samples))
                    if index_counts.get('done', 0) != total_jobs:
                        report['failures'].append({'stage': 'performance', 'scale': scale, 'reason': 'full_index_incomplete'})
                report['performance_stage']['scales'].append(performance)
                # Semantic-only retrieval is intentionally unavailable in the
                # default full-text scale route. Report misses, don't pretend
                # they constitute a BGE quality result or silently omit them.
                required = samples if args.full_index else [row for row in samples if row['group'] != 'semantic_paraphrase']
                for row in required:
                    if not row['passed']:
                        report['failures'].append({'stage': 'performance', 'scale': scale, 'case': row['id'], 'repeat': row['repeat']})
                print(f'v0.4: scale {scale} completed', file=sys.stderr, flush=True)
            report['performance_stage']['latency_limits_enforced'] = False
            report['performance_stage']['note'] = 'P50/P95 are observed on this host; no hardware-independent latency promise.'
            report['passed'] = not report['failures']
        except Exception as error:
            report['failures'].append({'stage': 'fatal', **error_record(error)})
        finally:
            if repo is not None:
                try:
                    with repo.pool.connection() as connection:
                        connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
                    report['temporary_schema_removed'] = True
                except Exception as error:
                    report['failures'].append({'stage': 'cleanup', **error_record(error)})
                    report['passed'] = False
                finally:
                    repo.close()
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--real-model', action='store_true')
    parser.add_argument('--full-index', action='store_true')
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error('--repeats must be between 1 and 10')
    if args.inside:
        return inside(args)
    root = Path(__file__).resolve().parent.parent
    command = ['docker', 'compose', 'run', '-T', '--rm', '--no-deps', '-v', str(root/'scripts')+':/verification:ro',
        'api', 'python', '/verification/verify_memory_v4.py', '--inside', '--repeats', str(args.repeats)]
    if args.real_model:
        command.append('--real-model')
    if args.full_index:
        command.append('--full-index')
    result = subprocess.run(command, cwd=root, stdout=subprocess.PIPE, text=True, check=False)
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report = {'version': '0.4', 'passed': False, 'failures': [{'stage': 'container', 'exit_code': result.returncode,
            'reason': 'container_did_not_emit_a_valid_report'}]}
    report['process_exit_code'] = result.returncode
    destination = root/'docs'/'verification-v0.4.json'
    destination.parent.mkdir(exist_ok=True)
    preserve_failed_report(destination)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+'\n')
    print(json.dumps({'passed': report.get('passed', False), 'failures': len(report.get('failures', [])),
        'report': str(destination)}, ensure_ascii=False))
    return 0 if result.returncode == 0 and report.get('passed') else 1


if __name__ == '__main__':
    sys.exit(main())
