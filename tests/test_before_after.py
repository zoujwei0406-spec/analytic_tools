"""Guard against misleading end-to-end comparison metrics."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('before_after', ROOT / 'test/run_before_after.py')
comparison = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT / 'test'))
try:
    spec.loader.exec_module(comparison)
finally:
    sys.path.pop(0)


def test_unreviewed_alert_does_not_become_a_true_positive():
    result = comparison.score([{'id': 'x'}], {})
    assert result['status'] == 'pending_adjudication'
    assert 'tp' not in result


def test_no_alerts_still_counts_all_missed_bugs():
    result = comparison.score([], {})
    assert result['tp'] == result['fp'] == 0
    assert result['fn'] == 3 and result['recall'] == 0
    assert result['precision'] is None


def test_cross_origin_duplicates_and_safe_false_alerts():
    alerts = [{'id': k} for k in ('model', 'program', 'fp1', 'fp2')]
    decisions = {
        'model': {'status': 'tp', 'labels': ['A'], 'reason': 'correct bounds'},
        'program': {'status': 'tp', 'labels': ['A'], 'reason': 'same bounds'},
        'fp1': {'status': 'fp', 'labels': ['B'], 'issue_key': 'B-bounds', 'reason': 'guard is valid'},
        'fp2': {'status': 'fp', 'labels': ['B'], 'issue_key': 'B-bounds', 'reason': 'duplicate'},
    }
    result = comparison.score(alerts, decisions)
    assert (result['tp'], result['fp'], result['fn']) == (1, 1, 2)
    assert result['false_positive_rate'] == pytest.approx(1 / 3)


@pytest.mark.parametrize('decisions', [
    {'x': {'status': 'tp', 'labels': ['B'], 'reason': 'cannot call a safe label TP'}},
    {'x': {'status': 'tp', 'labels': ['A'], 'reason': ''}},
    {'x': {'status': 'fp', 'labels': ['A'], 'issue_key': 'wrong', 'reason': 'wrong label'}},
    {'stale': {'status': 'out_of_scope', 'reason': 'does not exist'}},
])
def test_invalid_adjudication_is_rejected(decisions):
    with pytest.raises(ValueError):
        comparison.score([{'id': 'x'}], decisions)


def test_report_union_keeps_model_false_alert_despite_program_safe():
    report = {'scanner': {'diagnostics': []}, 'reviews': [{'review': {
        'findings': [{'line': 7, 'title': 'bounds'}], 'withdrawn_findings': [{'line': 15}]}}],
        'program_analysis': {'claims': [{'verdict': 'SAFE', 'line': 7}]}}
    alerts = comparison.artifacts(report)
    assert len(alerts) == 1 and alerts[0]['id'] == 'model:0:0'


def test_pair_reports_regressions_as_well_as_improvements():
    before = {'status': 'scored', 'detected': ['A', 'C'], 'false_controls': ['B']}
    after = {'status': 'scored', 'detected': ['A', 'E'], 'false_controls': ['F']}
    assert comparison.paired_changes(before, after) == {
        'newly_detected': ['E'], 'lost_detections': ['C'],
        'removed_false_controls': ['B'], 'added_false_controls': ['F']}


def test_same_model_budget_source_and_scope_in_both_arms(tmp_path):
    args = SimpleNamespace(model='same-model', ollama_url='http://127.0.0.1:11435', max_rounds=6, model_timeout=180, model_context=40960)
    before = comparison.command_for('checklist', tmp_path, args)
    after = comparison.command_for('evidence', tmp_path, args)
    before[before.index('--analysis-strategy') + 1] = 'evidence'
    assert before == after[:len(before)]
    assert after[len(before):] == ['--evidence-policy', 'cost', '--max-evidence-actions', '1000', '--evidence-budget', '2000']


def test_independent_labels_still_point_to_expected_source_locations():
    tree = ast.parse(comparison.SOURCE.read_text())
    functions = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    for label in comparison.LABELS.values():
        function = functions[label['function']]
        accesses = [n for n in ast.walk(function) if isinstance(n, ast.Subscript)
                    and isinstance(n.value, ast.Name) and n.value.id == 'values']
        assert len(accesses) == 1 and accesses[0].lineno == label['line']


def test_same_target_efficiency_does_not_compare_unmatched_detections():
    def row(detected, first, requests):
        return {'complete': True, 'quality': {'status': 'scored', 'detected': detected},
                'measurement': {'attempts': requests},
                'first_detection': {'model': first, 'system': first, 'rounds_adjudicated': True}}
    before = row(['E'], {'E': 2}, 4)
    after = row(['A', 'C', 'E'], {'A': 1, 'C': 1, 'E': 1}, 4)
    result = comparison.equal_target_efficiency(before, after)
    assert result['common_targets'] == ['E']
    assert (result['before_model_round'], result['after_model_round']) == (2, 1)
    assert result['same_final_true_positive_set'] is False
    assert result['before_fixed_target_reached'] is False
    assert result['after_fixed_target_reached'] is True
    assert result['before_final_review_rounds'] == result['after_final_review_rounds'] == 4


def test_program_round_zero_is_distinct_from_model_detection():
    report = {'reviews': [{'trace': [{'round': 1, 'tool': 'submit_review'}]}]}
    result = comparison.first_detection_rounds(report,
        [{'id': 'program:one', 'origin': 'program'}],
        {'program:one': {'status': 'tp', 'labels': ['E']}},
        {'0': {'labels': [], 'reason': 'no correct formal finding'}})
    assert result['model'] == {} and result['system'] == {'E': 0}
    assert result['rounds_adjudicated'] is True


def test_incomplete_or_unreviewed_rounds_cannot_claim_speedup():
    assert comparison.equal_target_efficiency({'complete': False}, {'complete': True}) == {'status': 'incomplete'}
    report = {'reviews': [{'trace': [{'round': 1, 'tool': 'submit_review'}]}]}
    assert comparison.first_detection_rounds(report, [], {}, {})['rounds_adjudicated'] is False


def test_explicit_model_context_is_sent_to_service(monkeypatch):
    import io
    import json
    from evidence_review.agent import Ollama
    captured = []
    def respond(request, timeout):
        captured.append(json.loads(request.data))
        return io.BytesIO(b'{"message": {"role": "assistant", "content": "ok"}}')
    monkeypatch.setattr('evidence_review.agent.urllib.request.urlopen', respond)
    Ollama('test-model', context_length=40960).chat([], [])
    Ollama('test-model').chat([], [])
    assert captured[0]['options']['num_ctx'] == 40960
    assert 'num_ctx' not in captured[1]['options']
    with pytest.raises(ValueError):
        Ollama('test-model', context_length=0)


def test_context_overflow_is_not_counted_as_an_efficiency_improvement():
    assert comparison.equal_target_efficiency(
        {'complete': True, 'input_context_valid': False},
        {'complete': True, 'input_context_valid': True}) == {'status': 'invalid_context'}


def test_summary_excludes_failed_runs_and_rejects_modified_reports(tmp_path):
    import hashlib
    import json
    report = {'reviews': []}
    report_path = tmp_path / 'report.json'
    report_path.write_text(json.dumps(report))
    comparison.save(tmp_path / 'runs.json', {'runs': [{
        'id': 'one', 'repeat': 1, 'strategy': 'checklist', 'complete': False,
        'report': 'report.json', 'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
        'alerts': [], 'measurement': {'wall_seconds': 1, 'model_seconds': 1, 'attempts': 1},
        'usage': {'prompt_tokens': 0, 'completion_tokens': 0}, 'tool_calls': 0, 'program_actions': 0,
    }]})
    comparison.save(tmp_path / 'adjudication.json', {'one': {'final': {}, 'rounds': {}}})
    comparison.summarize(tmp_path)
    result = json.loads((tmp_path / 'summary.json').read_text())
    assert result['aggregate'] == {}
    assert '待核/未完成' in (tmp_path / 'summary.md').read_text()
    report_path.write_text('{}')
    with pytest.raises(ValueError, match='报告变化'):
        comparison.summarize(tmp_path)


def test_empty_input_regression_has_normal_path_and_real_bug_control():
    proof = comparison.empty_input_proof(comparison.SOURCE.read_text())
    assert proof['empty_length'] == 0 and proof['empty_iterations'] == []
    # Native list semantics oracle only; the target file is never executed.
    values = [1]
    i = next(iter(range(len(values))))
    with pytest.raises(IndexError):
        values[i + 1]
    assert proof['positive_witness']['index'] == 1


@pytest.mark.parametrize('change', [
    lambda s: s.replace('def touch(values):\n    pass', 'def touch(values):\n    values.pop()'),
    lambda s: s.replace('def case_e(values: list[int]):\n', 'def case_e(values: list[int]):\n    raise RuntimeError()\n'),
    lambda s: s + '\ndef len(values):\n    return 1\n',
])
def test_empty_path_proof_rejects_changed_assumptions(change):
    with pytest.raises(ValueError, match='源码已改变'):
        comparison.empty_input_proof(change(comparison.SOURCE.read_text()))


def fp_regression_scores(before_fp=True, after_fp=False, after_tp=True):
    labels = comparison.FALSE_POSITIVE_LABELS
    before_alerts = [{'id': 'true'}] + ([{'id': 'false'}] if before_fp else [])
    before_decisions = {'true': {'status': 'tp', 'labels': ['NONEMPTY_BOUNDS'], 'reason': 'real nonempty bounds'}}
    if before_fp:
        before_decisions['false'] = {'status': 'fp', 'labels': ['EMPTY_INPUT'], 'issue_key': 'empty-input', 'reason': 'empty is valid'}
    after_alerts, after_decisions = [], {}
    if after_tp:
        after_alerts.append({'id': 'true'})
        after_decisions['true'] = before_decisions['true']
    if after_fp:
        after_alerts.append({'id': 'false'})
        after_decisions['false'] = {'status': 'fp', 'labels': ['EMPTY_INPUT'], 'issue_key': 'empty-input', 'reason': 'still wrong'}
    return comparison.score(before_alerts, before_decisions, labels), comparison.score(after_alerts, after_decisions, labels)


def test_known_false_positive_removed_with_positive_retained():
    before, after = fp_regression_scores()
    change = comparison.false_positive_change(before, after)
    assert (before['fp'], after['fp']) == (1, 0)
    assert before['tp'] == after['tp'] == 1
    assert change['target_regression_passed'] is True


@pytest.mark.parametrize('options', [
    {'after_tp': False}, {'before_fp': False}, {'after_fp': True},
])
def test_regression_cannot_pass_by_suppressing_all_alerts_or_without_baseline_fp(options):
    before, after = fp_regression_scores(**options)
    assert comparison.false_positive_change(before, after)['target_regression_passed'] is False


def test_unreviewed_false_positive_regression_remains_pending():
    assert comparison.false_positive_change(
        {'status': 'pending_adjudication'}, {'status': 'scored'})['status'] == 'pending_adjudication'


def test_saved_regression_rejects_incomplete_context_before_reading_report(tmp_path):
    with pytest.raises(ValueError, match='输入有效'):
        comparison.check_saved_regression_run(tmp_path, {}, {'complete': True, 'input_context_valid': False})


def test_saved_fp_reports_are_bound_to_bytes_source_and_model(tmp_path):
    import hashlib
    import json
    source_hashes = {comparison.SOURCE.name: hashlib.sha256(comparison.SOURCE.read_bytes()).hexdigest()}
    report = {'source_hashes': source_hashes, 'scanner': {'status': 'completed', 'diagnostics': []},
              'reviews': [{'status': 'completed', 'review': {'findings': []}}]}
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(report))
    data = {'model': {'digest': 'fixed-model'}, 'model_context': 40960}
    row = {'complete': True, 'input_context_valid': True, 'report': 'report.json',
           'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
           'source_hashes': source_hashes, 'alerts': [],
           'runtime_model': {'models': [{'digest': 'fixed-model', 'context_length': 40960}]}}
    assert comparison.check_saved_regression_run(tmp_path, data, row) == report
    row['runtime_model']['models'][0]['digest'] = 'different-model'
    with pytest.raises(ValueError, match='模型摘要'):
        comparison.check_saved_regression_run(tmp_path, data, row)
    row['runtime_model']['models'][0]['digest'] = 'fixed-model'
    row['alerts'] = [{'id': 'forged'}]
    with pytest.raises(ValueError, match='缓存报警'):
        comparison.check_saved_regression_run(tmp_path, data, row)
    row['alerts'] = []
    path.write_text('{}')
    with pytest.raises(ValueError, match='原报告变化'):
        comparison.check_saved_regression_run(tmp_path, data, row)


def test_report_omission_does_not_establish_causal_correction():
    before = {'status': 'scored', 'false_controls': ['EMPTY_INPUT'], 'detected': ['NONEMPTY_BOUNDS']}
    after = {'status': 'scored', 'false_controls': [], 'detected': ['NONEMPTY_BOUNDS']}
    assert comparison.false_positive_change(before, after)['causal_attribution'] == 'not_established'


def test_frozen_start_calls_real_model_only_after_replay():
    class Client:
        def chat(self, messages, tools):
            return {'content': 'real'}
    client = comparison.FrozenFirstClient(Client(), {'findings': [{'line': 7}]})
    first = client.chat([], [])
    first['tool_calls'][0]['function']['arguments']['findings'].clear()
    assert client.submission['findings'] == [{'line': 7}]
    assert client.requests == []
    assert client.chat([], []) == {'content': 'real'}
    assert len(client.requests) == 1


def test_only_program_intervention_fields_can_differ():
    import json
    before = {'messages': [
        {'role': 'user', 'content': json.dumps({'task': 'same', 'audit_plan': {'functions': []}})},
        {'role': 'tool', 'content': json.dumps({'program_conflicts': [], 'task': 'same'})}], 'tools': []}
    after = {'messages': [
        {'role': 'user', 'content': json.dumps({'task': 'same', 'audit_plan': {'functions': [], 'program_analysis': {}},
            'program_analysis': {}, 'program_analysis_rules': 'conditional'})},
        {'role': 'tool', 'content': json.dumps({'program_conflicts': [{'finding_id': 'F2'}], 'task': 'same'})}], 'tools': []}
    normalize = comparison.correction_request_without_intervention
    assert normalize(before) == normalize(after)
    after['messages'][1]['content'] = json.dumps({'task': 'unfair extra hint'})
    assert normalize(before) != normalize(after)


def test_correction_needs_explicit_withdrawal_and_no_replacement_claim():
    result = {'status': 'completed', 'review': {
        'findings': [{'finding_id': 'F1'}], 'withdrawn_findings': []}, 'trace': []}
    assert not comparison.correction_outcome(result)['false_positive_withdrawn']
    result['review']['withdrawn_findings'] = [{'finding_id': 'F2', 'withdraw_reason': 'safe bounds'}]
    assert comparison.correction_outcome(result)['false_positive_withdrawn']
    result['review']['findings'].append({'finding_id': 'F3'})
    assert comparison.correction_outcome(result)['status'] == 'pending_adjudication'


def test_both_arms_correcting_does_not_credit_new_scheme():
    before = {'status': 'scored', 'false_positive_withdrawn': True, 'true_positive_retained': True,
              'conflict_feedback_observed': False}
    after = {**before, 'conflict_feedback_observed': True}
    assert comparison.correction_pair(before, after, True) == 'both_corrected_no_incremental_benefit'
    assert comparison.correction_pair(before, after, False) == 'invalid_or_unadjudicated'
    before['false_positive_withdrawn'] = False
    assert comparison.correction_pair(before, after, True) == 'incremental_correction_observed'
    after['true_positive_retained'] = False
    assert comparison.correction_pair(before, after, True) == 'true_positive_regression'


def test_incomplete_correction_cannot_claim_success():
    assert comparison.correction_outcome({'status': 'budget_exhausted'}) == {'status': 'incomplete'}
    assert comparison.correction_pair({'status': 'incomplete'}, {'status': 'scored'}, True) == 'invalid_or_unadjudicated'


def test_baseline_dropping_true_issue_is_not_treatment_regression_or_fp_gain():
    before = {'status': 'scored', 'false_positive_withdrawn': True, 'true_positive_retained': False,
              'conflict_feedback_observed': False}
    after = {**before, 'true_positive_retained': True, 'conflict_feedback_observed': True}
    assert comparison.correction_pair(before, after, True) == 'both_corrected_baseline_lost_true_positive'
