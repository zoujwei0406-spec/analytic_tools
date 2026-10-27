"""Semantic controls and integration contracts for the evidence workflow."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from evidence_review.cli import main
from evidence_review.evidence import EvidenceLedger, analyze_evidence
from evidence_review.knowledge.python_subscript import PythonBounds
from evidence_review.project import Project

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'test/cases/evidence_bounds'
LABELS = json.loads((FIXTURES / 'expected.json').read_text())['functions']


def project_for(tmp_path, source):
    (tmp_path / 'case.py').write_text(source, encoding='utf-8')
    return Project(tmp_path / 'case.py')


@pytest.mark.parametrize('policy', ['rules', 'fixed', 'gaps', 'cost'])
def test_labelled_cases(policy):
    project = Project(FIXTURES / 'cases.py')
    analysis = analyze_evidence(project, policy=policy, max_actions=1000, max_cost=2000)
    actual = {}
    for claim in analysis['claims']:
        function = project.at(claim['file'], claim['line']).name
        actual.setdefault(function, []).append(claim['verdict'])
        assert claim['source_hash'] == project.hashes()[claim['file']]
        assert not claim['verification_result'].startswith('verified_')  # All model/environment assumptions explicit.
        if claim['verdict'] != 'UNDECIDED':
            assert claim['derivation'] and claim['depends_on'] and claim['assumptions']
    expected = deepcopy(LABELS)
    if policy == 'rules':
        expected['helper_unsafe'] = expected['helper_safe'] = ['UNDECIDED']
    assert actual == expected
    assert len({(e['access_id'], e['tool']) for e in analysis['trace']}) == len(analysis['trace'])


def test_same_line_unicode_and_annotations(tmp_path):
    p = project_for(tmp_path, 'def f(values: list[int]):\n    for i in range(len(values)):\n        中文 = values[i], values[i + 1]\n')
    result = analyze_evidence(p)
    assert [c['verdict'] for c in result['claims']] == ['SAFE', 'VIOLATION']
    assert len({c['access_id'] for c in result['claims']}) == 2
    for c in result['claims']:
        assert p.sources['case.py'][c['start_byte']:c['end_byte']].decode() == c['expression']


def test_syntax_error_is_not_proved():
    result = analyze_evidence(Project(FIXTURES / 'syntax_error.py'))
    assert result['claims'] == []
    assert result['parse_errors'][0]['reason'] == 'syntax_error'


@pytest.mark.parametrize('budget,actions', [(0,100), (100,0), (2,2)])
def test_budget_exhaustion_keeps_unknown(tmp_path, budget, actions):
    p = project_for(tmp_path, 'def f(values: list[int]):\n    return [values[i+1] for i in range(len(values))]\n')
    r = analyze_evidence(p, max_cost=budget, max_actions=actions)
    assert r['claims'][0]['verdict'] == 'UNDECIDED'
    assert r['claims'][0]['missing']
    assert r['claims'][0]['stop_reason'] == 'budget_exhausted'
    assert r['summary']['estimated_cost'] <= budget
    assert r['summary']['actions'] <= actions


def test_timeout_keeps_unknown(tmp_path):
    p = project_for(tmp_path, 'def f(values: list[int]):\n    return [values[i+1] for i in range(len(values))]\n')
    r = analyze_evidence(p, timeout=1e-20)
    assert r['claims'][0]['stop_reason'] == 'timeout'


def test_ledger_rejects_model_and_stale_facts_and_invalidates(tmp_path):
    p = project_for(tmp_path, 'def f(values: list[int]):\n    return [values[i] for i in range(len(values))]\n')
    extractor = PythonBounds(p)
    access = extractor.accesses[0]
    fact = extractor.collect(access, 'domain')
    ledger = EvidenceLedger(p.hashes())
    ledger.add(access, fact)
    ledger.record(access, {'verdict': 'SAFE'})
    for changes in ({'origin': 'llm_hypothesis'}, {'source_hash': 'stale'}, {'access_id': 'fake'}):
        with pytest.raises(ValueError):
            ledger.add(access, {**fact, **changes})
    ledger.add(access, {**fact, 'status': 'unsupported', 'reason': 'new_information'})
    assert access['access_id'] not in ledger.results


def test_rule_against_native_list_index_oracle(tmp_path):
    # Independent executable oracle for generated pure templates; no fixture code imported/executed.
    for n in range(7):
        for trim in range(3):
            for offset in range(-3, 4):
                for guarded in (False, True):
                    source = ('def f():\n    values = ' + repr(list(range(n))) + '\n'
                              f'    for i in range(len(values) - {trim}):\n')
                    if guarded:
                        source += f'        if i + ({offset}) < len(values):\n            item = values[i + ({offset})]\n'
                    else:
                        source += f'        item = values[i + ({offset})]\n'
                    p = project_for(tmp_path, source)
                    claim = analyze_evidence(p)['claims'][0]
                    unsafe = False
                    for i in range(n - trim):
                        if guarded and not i + offset < n:
                            continue
                        try:
                            list(range(n))[i + offset]
                        except IndexError:
                            unsafe = True
                    assert claim['verdict'] == ('VIOLATION' if unsafe else 'SAFE'), (n,trim,offset,guarded,claim)


@pytest.mark.parametrize('statement', [
    'values.pop()', 'alias = values', 'values = []', 'del values[0]',
    'i = 0', 'break', 'external(values)', 'assert i > 10', 'value = 1 / 0',
])
def test_unsupported_effects_never_certify_safety(tmp_path, statement):
    p = project_for(tmp_path, f'def f(values: list[int]):\n    for i in range(len(values)):\n        {statement}\n        item = values[i]\n')
    assert analyze_evidence(p)['claims'][-1]['verdict'] == ('VIOLATION' if statement in {'values.pop()', 'values = []'} else 'UNDECIDED')


def test_cli_no_alarm_entry_scope_and_report(tmp_path, monkeypatch):
    # Isolate entry behavior from externally installed scanner versions.
    monkeypatch.setattr('evidence_review.cli.scan', lambda *a: {'status':'completed','diagnostics':[],'tools':[],'notes':[]})
    p = project_for(tmp_path, 'def a(values: list[int]):\n    return [values[i+1] for i in range(len(values))]\n\ndef b(values: list[int]):\n    return [values[i] for i in range(len(values))]\n')
    output = tmp_path / 'report'
    assert main(['analyze', str(p.root / 'case.py'), '--analysis-strategy','evidence','--top-k','1','--output',str(output)]) == 0
    report = json.loads((output/'report.json').read_text())
    assert report['schema_version'] == 3
    assert report['scanner']['diagnostics'] == []
    assert report['program_analysis']['coverage'] == {'enumerated_accesses':2,'selected_accesses':1,'unselected_accesses':1}
    assert report['program_analysis']['claims'][0]['verdict'] == 'VIOLATION'
    assert report['reviews'][0]['program_analysis']['claims'][0]['verdict'] == 'VIOLATION'
    assert '知识库判定与证据补查' in (output/'report.html').read_text()
    # Default behavior is unchanged: no new candidate without static/SBFL evidence.
    assert main(['analyze', str(p.root / 'case.py'), '--output',str(output)]) == 0
    legacy = json.loads((output/'report.json').read_text())
    assert legacy['schema_version'] == 2 and 'program_analysis' not in legacy
    assert legacy['candidates'] == []
    assert not any('cost 为可解释' in note for note in legacy['limitations'])


def test_model_receives_but_cannot_overwrite_program_analysis(tmp_path):
    from evidence_review.agent import review
    from evidence_review.sbfl import file_candidates
    p = project_for(tmp_path, 'def f(values: list[int]):\n    return [values[i+1] for i in range(len(values))]\n')
    program = analyze_evidence(p)
    class Client:
        def chat(self, messages, tools):
            user = json.loads(messages[1]['content'])
            assert user['program_analysis']['claims'][0]['verdict'] == 'VIOLATION'
            # A malformed model attempt to declare verification must be rejected.
            return {'tool_calls':[{'function':{'name':'submit_review','arguments':{'verified_safe':True}}}]}
    result = review(p, file_candidates(p,[])[0], Client(), max_rounds=1, program_analysis=program)
    assert result['status'] == 'budget_exhausted'
    assert result['program_analysis']['claims'][0]['verdict'] == 'VIOLATION'
    assert 'error' in result['trace'][0]['response']


def test_ablation_metrics_and_equal_inputs():
    import importlib.util
    spec = importlib.util.spec_from_file_location('ablation', ROOT / 'test/run_evidence_ablation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run(budgets=[140,400])
    assert len(result['runs']) == 8
    assert result['labels'] == {'accesses':25,'positive':12,'safe':7,'capability_boundary':6}
    runs = {(r['budget'],r['policy']):r for r in result['runs']}
    for r in result['runs']:
        assert r['metrics']['unsafe_as_safe'] == r['metrics']['unsupported_decisions'] == r['metrics']['fp'] == 0
        assert r['costs']['estimated_cost'] <= r['budget']
        assert r['metrics']['tp'] + r['metrics']['fn'] == 12
        assert [p['access_id'] for p in r['predictions']] == [p['access_id'] for p in result['runs'][0]['predictions']]
    assert runs[400,'rules']['metrics']['fn'] == 1
    assert runs[400,'fixed']['metrics']['fn'] == 0
    # Verify actual re-query reaches a helper body rather than trusting an LLM assertion.
    r = analyze_evidence(Project(FIXTURES/'cases.py'),max_actions=1000,max_cost=2000)
    assert any(e['tool'] == 'call:touch' for e in r['trace'])


@pytest.mark.parametrize('setup', [
    'def touch(values):\n    pass\nfrom external import touch\n',
    'def touch(values):\n    pass\n',
])
def test_helper_binding_and_complex_assignment_are_conservative(tmp_path, setup):
    body = ('def f(values: list[int], sink):\n    for i in range(len(values)):\n'
            '        sink.target = values\n        touch(values)\n        item = values[i]\n')
    p = project_for(tmp_path, setup + body)
    assert analyze_evidence(p)['claims'][0]['verdict'] == 'UNDECIDED'


def test_conflicting_model_finding_is_flagged_not_silently_deleted(tmp_path):
    from evidence_review.audit import AuditTracker
    from evidence_review.sbfl import file_candidates
    p = project_for(tmp_path, 'def f(values: list[int]):\n    return [values[i] for i in range(len(values))]\n')
    program = analyze_evidence(p)
    tracker = AuditTracker(p, file_candidates(p, [])[0], program_analysis=program)
    tracker.conclusion = {'findings':[{'finding_id':'F1','file':'case.py','line':2,'title':'索引越界','explanation':'模型错误断言'}],
                          'verdict':'likely_issue','summary':'test','missing_evidence':[],'evidence_ids':[]}
    assert tracker.program_conflicts()[0]['finding_id'] == 'F1'
    feedback = tracker.followup()
    assert feedback['program_conflicts']
    assert tracker.visible_review()['findings']  # Model opinion retained; no silent rule override.
    tracker.withdrawals['F1'] = '模型确认误报'
    assert tracker.program_conflicts() == []


def test_output_receiver_rebinding_is_not_trusted(tmp_path):
    p = project_for(tmp_path, 'def f(values: list[int], attacker):\n    out = []\n    for i in range(len(values)):\n        out = attacker\n        out.append(values)\n        item = values[i]\n')
    assert analyze_evidence(p)['claims'][0]['verdict'] == 'UNDECIDED'


@pytest.mark.parametrize('source', [b'\xef\xbb\xbfvalues[0]\r\n', b'values[0]\r\n'])
def test_bom_and_crlf_byte_spans(tmp_path, source):
    (tmp_path/'case.py').write_bytes(source)
    p = Project(tmp_path/'case.py')
    c = PythonBounds(p).accesses[0]
    assert source[c['start_byte']:c['end_byte']] == b'values[0]'


def test_failed_tool_is_charged_and_stops(tmp_path, monkeypatch):
    p = project_for(tmp_path, 'def f(values: list[int]):\n    return [values[i] for i in range(len(values))]\n')
    def fail(*args):
        raise ValueError('controlled tool failure')
    monkeypatch.setattr(PythonBounds, 'collect', fail)
    result = analyze_evidence(p)
    assert result['summary']['actions'] == 1
    assert result['summary']['estimated_cost'] > 0
    assert result['claims'][0]['stop_reason'] == 'tool_error'
    assert result['claims'][0]['verdict'] == 'UNDECIDED'


def test_nested_definition_cannot_rebind_counter_unnoticed(tmp_path):
    p = project_for(tmp_path, 'def f(values: list[int]):\n    for i in range(len(values)):\n        def i():\n            pass\n        item = values[i]\n')
    assert analyze_evidence(p)['claims'][0]['verdict'] == 'UNDECIDED'


@pytest.mark.parametrize('safe', [True, False])
def test_typed_program_reconciliation_preserves_raw_model_and_exact_property(tmp_path, safe):
    from evidence_review.audit import AuditTracker
    from evidence_review.sbfl import file_candidates
    code = 'def f(values: list[int]):\n    return [values[i' + ('' if safe else '+1') + '] for i in range(len(values))]\n'
    p = project_for(tmp_path, code)
    program = analyze_evidence(p)
    tracker = AuditTracker(p, file_candidates(p, [])[0], program_analysis=program)
    claim = program['claims'][0]
    finding = {'file': 'case.py', 'line': 2, 'title': '安全索引访问' if safe else '越界',
        'explanation': '模型原意见', 'condition': '当长度小于3时', 'suggestion': '原建议',
        'issue_kind': 'index_bounds', 'access_id': claim['access_id'], 'evidence_ids': ['E1']}
    conclusion = {'findings': [finding], 'verdict': 'likely_issue', 'summary': 'test', 'missing_evidence': [], 'evidence_ids': ['E1']}
    tracker.conclusion = tracker.merge_findings(conclusion)
    visible = tracker.visible_review()['findings']
    assert tracker.program_reconciliations[0]['original'] == finding
    if safe:
        assert visible == []
        assert tracker.program_reconciliations[0]['resolution'] == 'safe_under_assumptions'
    else:
        assert visible[0]['evidence_origin'] == 'program_verified'
        assert '长度=1' in visible[0]['condition'] and '下标=1' in visible[0]['condition']
        assert '小于3' not in visible[0]['condition']


def test_safe_bounds_does_not_remove_other_issue_on_same_line(tmp_path):
    from evidence_review.audit import AuditTracker
    from evidence_review.sbfl import file_candidates
    p = project_for(tmp_path, 'def f(values: list[int]):\n    for i in range(len(values)):\n        item = values[i]; result = missing_name\n')
    program = analyze_evidence(p)
    assert program['claims'][0]['verdict'] == 'SAFE'
    tracker = AuditTracker(p, file_candidates(p, [])[0], program_analysis=program)
    finding = {'file': 'case.py', 'line': 3, 'title': '未定义名称', 'explanation': '非空输入时引用未定义名称',
        'condition': '非空列表', 'suggestion': '定义 missing_name', 'issue_kind': 'other', 'access_id': None}
    result = tracker.reconcile_findings({'findings': [finding]})
    assert result['findings'] == [finding] and not tracker.program_reconciliations


def test_unknown_or_stale_evidence_cannot_suppress_model_finding(tmp_path):
    from evidence_review.audit import AuditTracker
    from evidence_review.sbfl import file_candidates
    p = project_for(tmp_path, 'def f(values: list[int], change):\n    for i in range(len(values)):\n        change(values)\n        item = values[i+1]\n')
    program = analyze_evidence(p)
    assert program['claims'][0]['verdict'] == 'UNDECIDED'
    finding = {'file': 'case.py', 'line': 4, 'title': '条件越界', 'issue_kind': 'index_bounds',
               'access_id': program['claims'][0]['access_id']}
    for analysis in (program, {**program, 'claims': [{**program['claims'][0],
            'verdict': 'SAFE', 'terminal': True, 'stop_reason': 'resolved', 'source_hash': 'stale'}]}):
        tracker = AuditTracker(p, file_candidates(p, [])[0], program_analysis=analysis)
        assert not tracker.resolved_claims
        assert tracker.reconcile_findings({'findings': [finding]})['findings'] == [finding]


def test_evidence_loop_records_do_not_skip_other_function_checks(tmp_path):
    from evidence_review.agent import review
    from evidence_review.sbfl import file_candidates
    p = project_for(tmp_path, 'def f(values: list[int]):\n    for i in range(len(values)):\n        item = values[i]\n')
    program = analyze_evidence(p)
    class Client:
        calls = 0
        def chat(self, messages, tools):
            self.calls += 1
            plan = json.loads(messages[1]['content'])['audit_plan']
            assert len(plan['functions']) == len(plan['program_resolved_units']) == 1
            assert 'program_analysis' not in plan
            assert 'facts' not in json.loads(messages[1]['content'])['program_analysis']['claims'][0]
            assert not any(unit['function_id'].endswith(':loop-index') for unit in plan['functions'])
            args = {'verdict': 'inconclusive', 'summary': '逐项检查', 'missing_evidence': [], 'findings': [],
                'diagnostic_reviews': [], 'function_reviews': [
                    {'function_id': plan['functions'][0]['function_id'], 'verdict': 'no_issue_found', 'reason': '其他性质未发现问题'}]}
            return {'tool_calls': [{'function': {'name': 'submit_review', 'arguments': args}}]}
    client = Client()
    result = review(p, file_candidates(p, [])[0], client, program_analysis=program)
    assert result['status'] == 'completed' and client.calls == 1
    assert result['audit']['complete']
    assert len(result['audit']['function_reviews']) == 2
    assert not result['audit']['followup_performed']


def test_program_verified_finding_cannot_be_withdrawn(tmp_path):
    from evidence_review.agent import ContextTools
    from evidence_review.audit import AuditTracker
    from evidence_review.sbfl import file_candidates
    p = project_for(tmp_path, 'def f(values: list[int]):\n    return [values[i+1] for i in range(len(values))]\n')
    program = analyze_evidence(p)
    tracker = AuditTracker(p, file_candidates(p, [])[0], program_analysis=program)
    context = ContextTools(p, 10000)
    context.call('read_lines', {'file': 'case.py', 'start': 1, 'end': 2})
    conclusion = {'findings': [{'file': 'case.py', 'line': 2, 'title': '越界',
        'issue_kind': 'index_bounds', 'access_id': program['claims'][0]['access_id']}],
        'summary': 'test', 'verdict': 'likely_issue', 'missing_evidence': [], 'evidence_ids': ['E1']}
    tracker.conclusion = tracker.merge_findings(conclusion)
    tracker.accept({**conclusion, 'findings': []}, {'withdraw_findings': [
        {'finding_id': 'F1', 'reason': '原发现经复核仍有效，无需撤回'}]}, context)
    assert tracker.visible_review()['findings'] and not tracker.withdrawals
    assert tracker.blocked_withdrawals
    assert tracker.schema()['withdraw_findings']['maxItems'] == 0


@pytest.mark.parametrize('source', [
    'def f():\n    return 1\n',
    'def f(values: list[int], callback):\n    for i in range(len(values)):\n        callback(values)\n        item = values[i+1]\n',
])
def test_absent_or_unknown_program_evidence_keeps_original_followup(tmp_path, source):
    from conftest import with_audit_records
    from evidence_review.agent import review
    from evidence_review.sbfl import file_candidates
    p = project_for(tmp_path, source)
    program = analyze_evidence(p)
    assert all(claim['verdict'] == 'UNDECIDED' for claim in program['claims'])
    class Client:
        calls = 0
        def chat(self, messages, tools):
            self.calls += 1
            args = with_audit_records(messages, {'verdict': 'inconclusive', 'summary': '测试提交',
                'missing_evidence': [], 'findings': []})
            return {'tool_calls': [{'function': {'name': 'submit_review', 'arguments': args}}]}
    client = Client()
    result = review(p, file_candidates(p, [])[0], client, program_analysis=program)
    assert result['status'] == 'completed' and client.calls == 2
    assert result['audit']['followup_performed']
