"""Host-only standard-library tests; no Docker, database, provider or credentials."""
from copy import deepcopy
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import verify_reflection_v5 as verification


def historical(calls,stamp='2026-10-26T10:00:00+00:00',run_id=None):
    value={'version':'0.5','real_model_requested':True,'fixture_sha256':verification.FROZEN_SHA256,
           'started_at':stamp,'cloud_calls':calls,'cases':[],'passed':False}
    if run_id:value['verification_run_id']=run_id
    return value


def write_report(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')
    return path


def reserve_in_process(path,index):
    ledger=verification.PersistentCallLedger(path)
    for _ in range(10):
        try:ledger.reserve('worker-'+str(index),10)
        except verification.BudgetLimitError:return


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.path=self.root/'budget'/'ledger.json'
        self.ledger=verification.PersistentCallLedger(self.path,create=True)
    def tearDown(self):
        self.directory.cleanup()

    def seed_history(self):
        first=write_report(self.root/'a.json',historical(68))
        second=write_report(self.root/'b.json',historical(74,'2026-10-26T11:00:00+00:00'))
        return first,second

    def test_backfill_142_once_despite_copied_reports_and_reruns(self):
        first,second=self.seed_history()
        copied=write_report(self.root/'history-copy.json',json.loads(first.read_text()))
        self.ledger.backfill([first,second,copied])
        restarted=verification.PersistentCallLedger(self.path)
        restarted.backfill([first,second,copied])
        summary=restarted.snapshot()
        self.assertEqual(summary['charged_calls'],142)
        self.assertEqual(summary['cumulative_limit'],96)
        self.assertEqual(summary['historical_runs'],2)
        self.assertEqual(len(summary['backfill_runs'][0]['reports'])+len(summary['backfill_runs'][1]['reports']),3)
        with self.assertRaises(verification.BudgetLimitError):restarted.reserve('new-run',96)
        self.assertEqual(restarted.snapshot()['charged_calls'],142)

    def test_only_explicit_total_limit_changes_persisted_ceiling(self):
        self.ledger.backfill(self.seed_history())
        with self.assertRaises(verification.BudgetLimitError):self.ledger.assert_available()
        self.ledger.set_explicit_limit(144)
        first=self.ledger.reserve('approved-run',96)
        self.ledger.finish(first,'failed',{'input_tokens':7,'output_tokens':2},'synthetic_failure')
        restarted=verification.PersistentCallLedger(self.path)
        restarted.reserve('new-process',96)
        with self.assertRaises(verification.BudgetLimitError):restarted.reserve('third-process',96)
        self.assertEqual(restarted.snapshot()['charged_calls'],144)
        self.assertEqual(restarted.snapshot()['cumulative_limit'],144)
        self.assertEqual(len(json.loads(self.path.read_text())['limit_changes']),1)

    def test_reservation_is_durable_before_provider_and_interruption_never_refunds(self):
        self.ledger.set_explicit_limit(1)
        identifier=self.ledger.reserve('interrupted',1)
        persisted=json.loads(self.path.read_text())
        self.assertEqual(persisted['reservations'][0]['id'],identifier)
        self.assertEqual(persisted['reservations'][0]['status'],'reserved')
        restarted=verification.PersistentCallLedger(self.path)
        with self.assertRaises(verification.BudgetLimitError):restarted.reserve('rerun',1)
        self.assertEqual(restarted.snapshot()['reserved_pending_calls'],1)
        self.assertFalse(restarted.snapshot()['provider_usage']['complete'])

    def test_failed_call_is_charged_and_provider_usage_recorded_separately(self):
        identifier=self.ledger.reserve('failure',2)
        self.ledger.finish(identifier,'failed',{'input_tokens':23,'output_tokens':5,'secret':'not retained'},'model_timeout')
        summary=self.ledger.snapshot()
        self.assertEqual(summary['charged_calls'],1)
        self.assertEqual(summary['provider_usage'],{'known_input_tokens':23,'known_output_tokens':5,'complete':True})
        self.assertNotIn('secret',self.path.read_text())
        with self.assertRaises(RuntimeError):self.ledger.finish(identifier,'completed',{})
        self.assertEqual(self.ledger.snapshot()['charged_calls'],1)

    def test_cross_process_flock_never_overspends(self):
        self.ledger.set_explicit_limit(7)
        ctx=multiprocessing.get_context('fork')
        children=[ctx.Process(target=reserve_in_process,args=(str(self.path),index)) for index in range(6)]
        for process in children:process.start()
        for process in children:
            process.join(10)
            self.assertEqual(process.exitcode,0)
        summary=self.ledger.snapshot()
        self.assertEqual(summary['charged_calls'],7)
        self.assertEqual(summary['reserved_pending_calls'],7)

    def test_reusing_historical_run_id_cannot_hide_new_charges(self):
        first=write_report(self.root/'history.json',historical(2,run_id='same-run'))
        self.ledger.backfill([first])
        self.ledger.set_explicit_limit(3)
        self.ledger.reserve('same-run',10)
        self.assertEqual(self.ledger.snapshot()['charged_calls'],3)
        self.assertEqual(self.ledger.run_calls('same-run'),3)
        with self.assertRaises(verification.BudgetLimitError):self.ledger.reserve('same-run',10)

    def test_new_ledger_reports_do_not_double_charge_reserved_calls(self):
        identifier=self.ledger.reserve('recorded-run',3)
        self.ledger.finish(identifier,'completed',{'input_tokens':9,'output_tokens':4})
        report=historical(1,run_id='recorded-run')
        report['cases']=[{'arms':{'on':{'calls':[{'input_tokens':9,'output_tokens':4}]}}}]
        path=write_report(self.root/'report.json',report)
        self.ledger.backfill([path,path])
        self.assertEqual(self.ledger.snapshot()['charged_calls'],1)
        self.assertEqual(self.ledger.snapshot()['provider_usage']['known_input_tokens'],9)

    def test_corrupt_or_missing_ledger_and_incomplete_history_fail_closed(self):
        with self.assertRaises(RuntimeError):verification.PersistentCallLedger(self.root/'missing'/'ledger.json')
        with self.assertRaises(RuntimeError):self.ledger.require_known_history()
        with self.assertRaises(RuntimeError):verification.PersistentCallBudget(self.ledger,'fresh',96)
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):verification.PersistentCallLedger(self.path)

    def test_history_fixture_and_counts_cannot_be_silently_ignored(self):
        wrong=historical(1);wrong['fixture_sha256']='wrong'
        with self.assertRaises(RuntimeError):self.ledger.backfill([write_report(self.root/'bad.json',wrong)])
        missing=historical(1);missing.pop('cloud_calls')
        with self.assertRaises(RuntimeError):self.ledger.backfill([write_report(self.root/'missing-count.json',missing)])
        self.assertEqual(self.ledger.snapshot()['charged_calls'],0)

    def test_failed_persistence_never_returns_a_reservation(self):
        with patch.object(self.ledger,'_write',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):self.ledger.reserve('blocked',3)
        self.assertEqual(self.ledger.snapshot()['charged_calls'],0)

    def test_host_exhausted_budget_stops_before_docker_and_preserves_original_report(self):
        docs=self.root/'docs'
        current=write_report(docs/'verification-v0.5.json',historical(74,'2026-10-26T11:00:00+00:00'))
        write_report(docs/'verification-history'/'verification-v0.5-first.json',historical(68))
        original=current.read_bytes()
        with patch.object(verification,'__file__',str(self.root/'scripts'/'verify_reflection_v5.py')), \
             patch('sys.argv',['verify_reflection_v5.py','--real-model']), \
             patch.object(verification.subprocess,'run',side_effect=AssertionError('Docker must not start')):
            self.assertEqual(verification.main(),1)
        self.assertEqual(current.read_bytes(),original)
        summary=json.loads((docs/'verification-v0.5-budget.json').read_text())
        self.assertEqual((summary['charged_calls'],summary['cumulative_limit']),(142,96))
        self.assertTrue(list((docs/'verification-history').glob('*budget-blocked*')))

    def test_every_previous_report_is_preserved_including_passed_runs(self):
        destination=write_report(self.root/'docs'/'verification-v0.5.json',{'passed':True,'synthetic':'immutable'})
        before=destination.read_bytes()
        verification.preserve_report(destination)
        copies=list((destination.parent/'verification-history').glob('*.json'))
        self.assertEqual(len(copies),1)
        self.assertEqual(copies[0].read_bytes(),before)

    def test_diagnostic_on_only_is_forwarded_without_changing_budget(self):
        self.ledger.backfill(self.seed_history())
        docs=self.root/'docs';docs.mkdir()
        fake={'passed':True,'cloud_calls':0,'diagnostic_on_only':True,'full_suite':False,'arms':['on']}
        with patch.object(verification,'__file__',str(self.root/'scripts'/'verify_reflection_v5.py')), \
             patch('sys.argv',['verify_reflection_v5.py','--on-only','--cases','shared-meaning']), \
             patch.object(verification.subprocess,'run') as run:
            run.return_value.stdout=json.dumps(fake)
            run.return_value.returncode=0
            self.assertEqual(verification.main(),0)
        self.assertIn('--on-only',run.call_args.args[0])
        actual=json.loads((docs/'verification-v0.5-mock.json').read_text())
        self.assertFalse(actual['full_suite'])
        self.assertEqual(actual['arms'],['on'])
        self.assertEqual(self.ledger.snapshot()['charged_calls'],142)


if __name__=='__main__':unittest.main()
