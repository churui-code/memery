"""Pure unit tests: runnable with unittest without Docker or a database."""
from copy import deepcopy
import unittest

from app.errors import MemoryError
from app.reflection_scope import reflection_scope_issues, validate_reflection_scope


def payload(*quotes):
    return {'episodes': [{'pending': True, 'evidence': [
        {'id': str(index), 'quote': quote, 'role': 'user', 'start': 9}
    ]} for index, quote in enumerate(quotes)], 'self_state': []}


def proposal(text, reason='这项认识仍需后续材料检验。', evidence_ids=('0',)):
    return {'updates': [{'id': 'p1', 'text': text, 'reason': reason,
                         'evidence_ids': list(evidence_ids)}]}


class ReflectionScopeTests(unittest.TestCase):
    def codes(self, update, sources):
        return {item['code'] for item in reflection_scope_issues(update, sources)}

    def test_new_exclusive_rule_is_refused_for_unrelated_topic(self):
        sources = payload('样品尚未收到实验室确认；不要将电话转告作为结论。')
        update = proposal('只有实验室发来的书面确认才能确定样品结果。')
        self.assertIn('unlocated_restriction', self.codes(update, sources))

    def test_prerequisite_cannot_be_added_to_wait_instruction(self):
        sources = payload('等我下次带来测量结果再讨论。')
        for text in ('待所有数据齐备后再讨论。', '等全部结果补齐再继续。', '必须补齐所有数据才可开始讨论。'):
            with self.subTest(text=text):
                self.assertIn('unlocated_restriction', self.codes(proposal(text), sources))

    def test_operator_from_another_source_clause_cannot_launder_condition(self):
        sources = payload('只有本人可以签字。结果出来后就讨论。')
        self.assertIn('unlocated_restriction', self.codes(proposal('只有全部结果出来后才能讨论。'), sources))

    def test_original_restriction_is_allowed_when_exactly_located(self):
        text = '只有拿到本人授权后才能提交申请。'
        update, sources = proposal(text), payload(text)
        self.assertIs(validate_reflection_scope(update, sources), update)

    def test_restriction_in_reason_is_not_exempt(self):
        update = proposal('当前先保留结果。', reason='必须完成所有记录才能讨论。')
        issues = reflection_scope_issues(update, payload('下次带来新记录后再讨论。'))
        self.assertTrue(any(i['field'] == 'reason' and i['code'] == 'unlocated_restriction' for i in issues))

    def test_limited_frequency_cannot_be_rewritten_as_tendency(self):
        sources = payload('忙碌时，我偶尔想散步，不希望被打断。')
        for text in ('忙碌时倾向于散步。', '用户忙碌时想散步。'):
            with self.subTest(text=text):
                self.assertIn('lost_local_qualifier', self.codes(proposal(text), sources))

    def test_qualifier_on_different_predicate_does_not_license_first(self):
        sources = payload('我有时想散步。', '另一些时候我想看书。')
        update = proposal('用户想散步、另一些时候有时想看书。', evidence_ids=('0', '1'))
        self.assertIn('lost_local_qualifier', self.codes(update, sources))

    def test_qualifier_loss_in_reason_is_detected(self):
        sources = payload('我偶尔想散步。')
        update = proposal('用户偶尔想散步。', reason='原文说明用户想散步。')
        issues = reflection_scope_issues(update, sources)
        self.assertTrue(any(i['field'] == 'reason' and i['code'] == 'lost_local_qualifier' for i in issues))

    def test_local_qualifier_is_preserved(self):
        sources = payload('我有时想散步。')
        update = proposal('用户有时想散步。', reason='原文说用户有时想散步，具体频率未知。')
        self.assertEqual([], reflection_scope_issues(update, sources))

    def test_unrelated_summary_does_not_need_to_repeat_predicate(self):
        self.assertEqual([], reflection_scope_issues(proposal('需求会随情境变化。'), payload('我有时想散步。')))

    def test_uncertainty_remains_local(self):
        self.assertEqual([], reflection_scope_issues(proposal('用户可能想散步。'), payload('我有时想散步。')))

    def test_foreign_date_in_reason_requires_its_evidence(self):
        sources = payload('2023年4月7日提交蓝色文件。', '2023年8月9日提交白色文件。')
        update = proposal('白色文件已提交。', '这与4月7日的记录不同。', ('1',))
        issues = reflection_scope_issues(update, sources)
        self.assertTrue(any(i['field'] == 'reason' and i['code'] == 'unbound_source_anchor' for i in issues))
        self.assertTrue(any(i['evidence_ids'] == ['0'] for i in issues))

    def test_binding_both_sources_allows_exact_cross_event_summary(self):
        sources = payload('2023年4月7日提交蓝色文件。', '2023年8月9日提交白色文件。')
        update = proposal('蓝色文件与白色文件均已提交。', '记录分别发生于4月7日和8月9日。', ('0', '1'))
        self.assertEqual([], reflection_scope_issues(update, sources))

    def test_foreign_amount_requires_source_binding(self):
        sources = payload('维修费为268元。', '运输费为83元。')
        self.assertIn('unbound_source_anchor', self.codes(proposal('维修费为268元。', evidence_ids=('1',)), sources))

    def test_foreign_phrase_without_numbers_requires_source_binding(self):
        sources = payload('记录包含橙色旅行箱。', '记录包含红色雨伞。')
        self.assertIn('unbound_source_fragment', self.codes(proposal('橙色旅行箱与当前记录不同。', evidence_ids=('1',)), sources))

    def test_common_fragment_is_not_treated_as_foreign_proof(self):
        sources = payload('需要认真核对；其余暂缓。', '这次需要认真核对。')
        self.assertEqual([], reflection_scope_issues(proposal('需要认真核对。'), sources))

    def test_source_locations_point_to_original_text(self):
        sources = payload('记录包含橙色旅行箱。', '记录包含红色雨伞。')
        update = proposal('橙色旅行箱值得再次核对。', evidence_ids=('1',))
        issue = next(i for i in reflection_scope_issues(update, sources) if i['code'] == 'unbound_source_fragment')
        location = issue['source_locations'][0]
        quote = sources['episodes'][0]['evidence'][0]['quote']
        self.assertEqual(update['updates'][0]['text'][issue['start']:issue['end']], quote[location['quote_start']:location['quote_end']])
        self.assertEqual(location['quote_start'] + 9, location['source_start'])

    def test_database_offsets_and_uuid_ids_have_same_diagnostics(self):
        from uuid import uuid4
        eid_a, eid_b = uuid4(), uuid4()
        sources = payload('记录包含橙色旅行箱。', '记录包含红色雨伞。')
        for event, eid in zip(sources['episodes'], (eid_a, eid_b)):
            evidence = event['evidence'][0]
            evidence['id'] = eid
            evidence['start_offset'] = evidence.pop('start')
        update = proposal('橙色旅行箱值得再次核对。', evidence_ids=(eid_b,))
        issue = next(i for i in reflection_scope_issues(update, sources) if i['code'] == 'unbound_source_fragment')
        location = issue['source_locations'][0]
        self.assertEqual([str(eid_a)], issue['evidence_ids'])
        self.assertEqual(location['quote_start'] + 9, location['source_start'])

    def test_v12_written_cross_event_reason_requires_missing_date_source(self):
        # Frozen synthetic report reproduction: factually correct cross-event
        # reasoning still needs both original sources bound to this update.
        sources = payload(
            '合成小说：2024年5月1日，林舟在星河公园对阿遥说“我爱你”，单膝跪地，亲吻她的手背，然后戴上紫藤戒指；小吃花了37元。',
            '合成小说：2024年6月2日，同样的林舟和阿遥在星河公园买银戒指，花了52元，没有告白或单膝跪地。')
        update = proposal(
            '在用户提供的合成小说设定中，2024年6月2日的林舟与阿遥在星河公园买银戒指，花费52元，没有告白也没有单膝跪地。',
            '新原文直接给出该版本的日期、地点、戒指材质与花费，并明确其缺少告白与单膝跪地情节；这是该设定版本的新增细节，与5月1日紫藤戒指版本的差异也由此可知。仍未知的是该版本中两人关系状态、戒指用途和花费分配等未被提及的内容。',
            ('1',))
        issues = reflection_scope_issues(update, sources)
        self.assertTrue(any(i['field'] == 'reason' and i['code'] == 'unbound_source_anchor'
                            and update['updates'][0]['reason'][i['start']:i['end']] == '5月1日'
                            and i['evidence_ids'] == ['0'] for i in issues))
        update['updates'][0]['evidence_ids'].append('0')
        self.assertEqual([], reflection_scope_issues(update, sources))

    def test_v12_written_reported_information_does_not_license_only_official_notice(self):
        sources = payload(
            '合成转述：小远告诉我，社团下周可能取消聚会，但他也只是听别人说的。',
            '合成对话：我还没有收到社团正式通知，不要把小远的转述当成确定消息。')
        update = proposal(
            '当消息只来自他人转述（含第二手转述）时，应把它当作未证实的线索，而不是确定事实；只有直接收到的正式通知才能确定社团聚会的安排。',
            '两段新原文都直接呈现转述链：小远说他也是听别人说的（reported_unverified），且用户明确表示尚未收到社团正式通知，不要把转述当成确定消息。原文中没有社团是否真的取消聚会的证实，也未说明最终安排如何，这些仍然未知。',
            ('0', '1'))
        issues = reflection_scope_issues(update, sources)
        self.assertTrue(any(i['field'] == 'text' and i['code'] == 'unlocated_restriction'
                            and update['updates'][0]['text'][i['start']:i['end']].startswith('只有直接收到')
                            for i in issues))

    def test_v12_written_needs_summary_loses_sometimes_in_text_and_reason(self):
        sources = payload(
            '合成对话：疲惫时，我有时想独处，不想被连续追问。',
            '合成对话：另一些时候我又想有人陪着。不要把一次表达当成我永远不变的需要。')
        update = proposal(
            '用户明确表示其对独处与陪伴的需求会随情境和状态变化：在疲惫时倾向于独处、不想被连续追问；另一些时候又想有人陪着。用户并提醒不应把某一次表达当成他永远不变的需要。',
            '两段待反思原文均为用户第一人称陈述，分别提到疲惫时想独处、以及有时又想有人陪着，并自我提醒不要把一次表达视为恒定需要，共同支持“需求波动”这一有界认识。仍未知的是触发波动的具体条件、频率与用户如何自我识别当下处于哪一种状态。',
            ('0', '1'))
        issues = reflection_scope_issues(update, sources)
        for field, phrase in (('text', '在疲惫时倾向于独处'), ('reason', '分别提到疲惫时想独处')):
            self.assertTrue(any(i['field'] == field and i['code'] == 'lost_local_qualifier'
                                and update['updates'][0][field][i['start']:i['end']] == phrase for i in issues))

    def test_ids_without_frozen_quotes_fail_closed(self):
        sources = {'episodes': [{'evidence': [{'id': '0'}]}]}
        self.assertEqual({'source_quote_unavailable'}, self.codes(proposal('保留有限认识。'), sources))

    def test_missing_unbound_quote_also_prevents_cross_source_check(self):
        sources = payload('首条记录。')
        sources['episodes'].append({'evidence': [{'id': '1'}]})
        self.assertIn('source_quote_unavailable', self.codes(proposal('保留有限认识。'), sources))

    def test_conflicting_duplicate_evidence_fails_closed(self):
        sources = payload('原始内容。', '其他内容。')
        sources['episodes'][1]['evidence'][0]['id'] = '0'
        self.assertIn('source_quote_unavailable', self.codes(proposal('保留有限认识。'), sources))

    def test_self_state_cannot_supply_original_evidence(self):
        sources = payload('提交白色文件。', '提交蓝色文件。')
        sources['self_state'] = [{'text': '提交蓝色文件。'}]
        self.assertIn('unbound_source_fragment', self.codes(proposal('提交蓝色文件。'), sources))

    def test_empty_proposal_requires_no_sources(self):
        self.assertEqual([], reflection_scope_issues({'updates': []}, {}))

    def test_missing_background_does_not_prove_never_having_a_prior_view(self):
        sources = payload('合成小说：两人在公园交换戒指。')
        sources['self_state'] = []
        sources['target_versions'] = []
        claim = proposal('我保留虚构边界。', '此前没有任何认识，因此首次形成。')
        self.assertIn('unlocated_restriction', self.codes(claim, sources))

    def test_validation_preserves_inputs_and_reports_structured_issues(self):
        update, sources = proposal('只有全部信息齐备才能继续。'), payload('下次再继续。')
        before = deepcopy((update, sources))
        with self.assertRaises(MemoryError) as caught:
            validate_reflection_scope(update, sources)
        self.assertEqual('reflection_scope_unsupported', caught.exception.code)
        self.assertTrue(caught.exception.scope_issues)
        self.assertEqual(before, (update, sources))

    def test_model_dump_interface_is_supported(self):
        class Model:
            def model_dump(self, mode):
                return proposal('保留有限认识。')
        model = Model()
        self.assertIs(validate_reflection_scope(model, payload('一条普通记录。')), model)

    def test_semantically_equivalent_paraphrase_can_be_conservatively_refused(self):
        sources = payload('只有得到授权才能提交。')
        self.assertIn('unlocated_restriction', self.codes(proposal('提交必须先获许可。'), sources))

    def test_synonym_only_borrowing_is_explicitly_outside_proven_subset(self):
        sources = payload('我读了一部长篇故事。', '我买了一只箱子。')
        # No shared date, quantity or four-character fragment: passing here
        # does not establish source support and semantic review is still needed.
        self.assertEqual([], reflection_scope_issues(proposal('读过小说。', evidence_ids=('1',)), sources))


if __name__ == '__main__':
    unittest.main()
