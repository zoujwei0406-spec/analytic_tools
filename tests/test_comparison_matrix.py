"""Validation for the multi-group experiment, independent of model inference."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_review.evidence import analyze_evidence
from evidence_review.project import Project

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('matrix', ROOT/'test/run_comparison_matrix.py')
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_groups_are_fixed_balanced_and_hash_checked():
    manifest = json.loads(matrix.MANIFEST.read_text())
    assert len(manifest['groups']) == 5
    for group in manifest['groups']:
        source = matrix.ROOT/group['source']
        assert matrix.digest(source) == group['sha256']
        functions = {n.name: n for n in ast.parse(source.read_text()).body if isinstance(n, ast.FunctionDef)}
        assert sorted(v['bug'] for v in group['labels'].values()) == [False, True]
        for label in group['labels'].values():
            accesses = [n for n in ast.walk(functions[label['function']]) if isinstance(n, ast.Subscript)
                        and isinstance(n.value, ast.Name) and n.value.id == 'values']
            assert len(accesses) == 1 and accesses[0].lineno == label['line']


def test_same_settings_only_strategy_and_evidence_options_differ(tmp_path):
    args = SimpleNamespace(model='m', ollama_url='http://127.0.0.1:11435', max_rounds=4,
                           model_context=40960, model_timeout=300)
    group = {'source': 'cases/evidence_bounds/matrix/offset.py'}
    before = matrix.command_for(group, 'checklist', tmp_path, args)
    after = matrix.command_for(group, 'evidence', tmp_path, args)
    before[before.index('--analysis-strategy')+1] = 'evidence'
    assert before == after[:len(before)]
    assert after[len(before):] == ['--evidence-policy','cost','--max-evidence-actions','1000','--evidence-budget','2000']
    assert 'manifest.json' not in ' '.join(after)


def test_context_rejects_wrong_model_and_capacity_or_failed_request():
    row = {'runtime_model': {'models': [{'digest': 'd', 'context_length': 40960}]},
           'measurement': {'responses': [{'prompt_tokens': 14000}]}}
    assert matrix.context_valid(row, {'digest': 'd'}, 40960)
    assert not matrix.context_valid(row, {'digest': 'other'}, 40960)
    row['measurement']['responses'][0]['prompt_tokens'] = 40960
    assert not matrix.context_valid(row, {'digest': 'd'}, 40960)
    row['measurement']['responses'][0]['prompt_tokens'] = 0
    assert not matrix.context_valid(row, {'digest': 'd'}, 40960)


def test_independent_length_semantics_include_small_inputs():
    def invalid(n, trim, offset):
        return [i for i in range(n-trim) if not -n <= i+offset < n]
    for n in range(100):
        assert bool(invalid(n, 1, 2)) == (n >= 2)
        assert invalid(n, 2, 2) == []
        assert bool(invalid(n, 0, -2)) == (n == 1)
        assert all(-n <= -1 < n for _ in range(n))
        assert all(0 <= i+1 < n for i in range(n) if i+1 < n)
    assert list(range(len([]))) == []
    assert not -1 <= -2 < 1


def test_program_results_match_independent_fixture_labels():
    manifest = json.loads(matrix.MANIFEST.read_text())
    for group in manifest['groups']:
        analysis = analyze_evidence(Project(matrix.ROOT/group['source']), max_actions=1000, max_cost=2000)
        expected = {label['line']: 'VIOLATION' if label['bug'] else 'SAFE' for label in group['labels'].values()}
        assert {c['line']: c['verdict'] for c in analysis['claims']} == expected


def test_unfinished_matrix_cannot_be_plotted_as_full_result(tmp_path):
    import sys
    spec = importlib.util.spec_from_file_location('matrix_plots', ROOT/'test/plot_comparison_matrix.py')
    plots = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plots)
    manifest = json.loads(matrix.MANIFEST.read_text())
    (tmp_path/'runs.json').write_text(json.dumps({'manifest': manifest, 'repeats': 2, 'runs': []}))
    (tmp_path/'adjudication.json').write_text('{}')
    sys.path.insert(0, str(ROOT/'test'))
    try:
        import pytest
        with pytest.raises(ValueError, match='运行矩阵不完整'):
            plots.load_scored(tmp_path)
    finally:
        sys.path.pop(0)


def test_program_cannot_cancel_a_model_false_claim():
    import sys
    sys.path.insert(0, str(ROOT/'test'))
    try:
        from run_before_after import score
        labels = {'bad': {'bug': True}, 'safe': {'bug': False}}
        result = score([{'id': 'program'}, {'id': 'wrong-model'}], {
            'program': {'status': 'tp', 'labels': ['bad'], 'reason': 'valid symbolic witness'},
            'wrong-model': {'status': 'fp', 'labels': [], 'issue_key': 'wrong-condition',
                            'reason': 'condition includes an unreachable empty-list access'}}, labels)
        assert (result['tp'], result['fp'], result['fn']) == (1, 1, 0)
    finally:
        sys.path.pop(0)


def test_correction_replay_rejects_treatment_report_as_baseline(tmp_path):
    import sys
    import pytest
    report = tmp_path/'report.json'
    report.write_text('{}')
    (tmp_path/'runs.json').write_text(json.dumps({'runs': [
        {'id': 'r', 'report': 'report.json', 'strategy': 'evidence', 'complete': True, 'input_context_valid': True}]}))
    (tmp_path/'adjudication.json').write_text('{}')
    sys.path.insert(0, str(ROOT/'test'))
    try:
        with pytest.raises(ValueError, match='旧流程误报'):
            matrix.replay(SimpleNamespace(output=tmp_path, replay_reference=report))
    finally:
        sys.path.pop(0)


def test_final_system_totals_keep_detection_errors_and_complete_runs_separate():
    import sys
    spec = importlib.util.spec_from_file_location('matrix_plots_final', ROOT/'test/plot_comparison_matrix.py')
    plots = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plots)
    sys.path.insert(0, str(ROOT/'test'))
    try:
        from run_before_after import score
        labels = {'bad': {'bug': True}, 'safe': {'bug': False}}
        groups = {'g': {'labels': labels}}
        tp = {'status': 'tp', 'labels': ['bad'], 'reason': 'verified counterexample'}
        fp = {'status': 'fp', 'labels': [], 'issue_key': 'wrong-condition', 'reason': 'invalid trigger'}
        rows = []
        for annotations, model_ids in [({'program': tp, 'model': fp}, {'model'}),
                                       ({'program': tp, 'model': tp}, {'model'}),
                                       ({}, set())]:
            alerts = [{'id': key} for key in annotations]
            rows.append({'group': 'g',
                'quality': score(alerts, annotations, labels),
                'model_quality': score([a for a in alerts if a['id'] in model_ids],
                    {k: v for k, v in annotations.items() if k in model_ids}, labels),
                'false_claim_categories': {'wrong_trigger_condition': 1} if annotations.get('model') == fp else {}})
        totals = plots.summarize_results(rows, groups)
        assert (totals['tp'], totals['fn'], totals['fp']) == (2, 1, 1)
        assert totals['model_tp'] == 1
        assert totals['fully_correct_runs'] == 1
        assert totals['runs'] == totals['positive_opportunities'] == totals['safe_opportunities'] == 3
        assert totals['wrong_trigger_condition'] == 1
    finally:
        sys.path.pop(0)


def test_timing_uses_full_wall_time_and_retains_request_costs():
    spec = importlib.util.spec_from_file_location('matrix_plots_timing', ROOT/'test/plot_comparison_matrix.py')
    plots = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plots)
    assert plots.summarize_timing([]) is None
    rows = [{'measurement': {'wall_seconds': wall, 'model_seconds': model, 'attempts': requests},
             'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
             'stage_timings': {'scanner_seconds': .5, 'evidence_seconds': .01}}
            for wall, model, requests in [(10, 8, 2), (6, 4, 1)]]
    measured = plots.summarize_timing(rows)
    assert measured['mean_seconds'] == measured['median_seconds'] == 8
    assert measured['total_seconds'] == 16 and measured['model_seconds'] == 12
    assert measured['non_model_seconds'] == 4 and measured['requests'] == 3
    assert measured['stage_seconds']['scanner_seconds'] == 1
    assert measured['prompt_tokens'] == 200 and measured['completion_tokens'] == 40


def test_program_corrected_model_text_is_not_scored_as_independent_model_detection():
    import sys
    sys.path.insert(0, str(ROOT/'test'))
    try:
        from run_before_after import artifacts, score
        report = {'scanner': {'diagnostics': []}, 'reviews': [{'review': {'findings': [
            {'file': 'x.py', 'line': 1, 'evidence_origin': 'program_verified'}]}}],
            'program_analysis': {'claims': [{'verdict': 'VIOLATION', 'access_id': 'A',
                'file': 'x.py', 'line': 1, 'expression': 'x[i+1]', 'reason': 'bounds_rule',
                'assumptions': ['ordinary list'], 'witness': {'length': 1, 'index': 1}}]}}
        alerts = artifacts(report)
        assert len(alerts) == 2 and {alert['origin'] for alert in alerts} == {'program'}
        labels = {'bad': {'bug': True}}
        decisions = {alert['id']: {'status': 'tp', 'labels': ['bad'], 'reason': 'same verified access'} for alert in alerts}
        assert score(alerts, decisions, labels)['tp'] == 1
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize('metric', ['program_fn', 'safe_as_violation', 'unsafe_as_safe', 'boundary_decisions'])
def test_real_runs_are_blocked_until_known_regressions_are_fixed(monkeypatch, metric):
    import sys
    sys.path.insert(0, str(ROOT/'test'))
    try:
        import run_evidence_reliability
        metrics = dict(expectation_matches=40, program_fn=0, safe_as_violation=0, unsafe_as_safe=0, boundary_decisions=0)
        metrics[metric] = 1
        monkeypatch.setattr(run_evidence_reliability, 'run', lambda policies: {'cases': {'accesses': 40}, 'runs': [{'metrics': metrics}]})
        with pytest.raises(ValueError, match='先修复漏检/误报'):
            matrix.run(SimpleNamespace())
    finally:
        sys.path.pop(0)
