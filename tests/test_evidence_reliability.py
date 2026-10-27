"""Independent semantic oracles and evidence/model boundary regressions."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from evidence_review.audit import AuditTracker
from evidence_review.evidence import analyze_evidence
from evidence_review.project import Project
from evidence_review.sbfl import file_candidates

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'test/cases/evidence_bounds/reliability'


def load_runner():
    spec = importlib.util.spec_from_file_location('reliability_runner', ROOT / 'test/run_evidence_reliability.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_project(tmp_path, source):
    path = tmp_path / 'case.py'
    path.write_text(source, encoding='utf-8')
    return Project(path)


@pytest.mark.parametrize('policy', ['rules', 'fixed', 'gaps', 'cost'])
def test_independently_labelled_reliability_corpus(policy):
    result = load_runner().run([policy])
    assert result['cases']['accesses'] == 40 and result['cases']['functions'] == 37
    row = result['runs'][0]
    assert row['metrics']['expectation_matches'] == 40
    assert row['metrics']['safe_as_violation'] == 0
    assert row['metrics']['unsafe_as_safe'] == 0
    assert row['metrics']['boundary_decisions'] == 0
    # Unknown actual defects are still misses; never count fallback as detection.
    assert row['metrics']['program_fn'] == (1 if policy == 'rules' else 0)


GUARDS = {
    'chained': '        if -len(values) <= {k} < len(values):\n            item = values[{k}]\n',
    'and': '        if {k} >= 0 and {k} < len(values):\n            item = values[{k}]\n',
    'continue': '        if {k} >= len(values):\n            continue\n        item = values[{k}]\n',
    'return': '        if {k} >= len(values):\n            return None\n        item = values[{k}]\n',
    'not': '        if not {k} < len(values):\n            return None\n        item = values[{k}]\n',
    'lower': '        if {k} >= -len(values):\n            item = values[{k}]\n',
    'upper': '        if {k} < len(values):\n            item = values[{k}]\n',
    'inclusive_upper': '        if {k} <= len(values) - 1:\n            item = values[{k}]\n',
}


@pytest.mark.parametrize('guard', list(GUARDS))
def test_guards_against_native_python_not_rule_reimplementation(tmp_path, guard):
    # Execute ONLY fixed, finite templates owned by this test, never user input
    # or the defect fixtures. CPython evaluates guards and indexing independently.
    for n in range(6):
        for trim in (0, 1, 2):
            for offset in range(-4, 5):
                index = f'i + ({offset})'
                source = ('def f():\n    values = ' + repr(list(range(n))) + '\n'
                          f'    for i in range(len(values) - {trim}):\n' + GUARDS[guard].format(k=index))
                project = make_project(tmp_path, source)
                claim = analyze_evidence(project)['claims'][0]
                namespace = {}
                exec(compile(source, '<owned-finite-bounds-oracle>', 'exec'), namespace)
                try:
                    namespace['f']()
                    truth = 'SAFE'
                except IndexError:
                    truth = 'VIOLATION'
                assert claim['verdict'] == truth, (guard, n, trim, offset, claim)
                if truth == 'VIOLATION':
                    witness = claim['witness']
                    assert witness['length'] == n
                    assert 0 <= witness['counter'] < len(range(n - trim))
                    assert witness['index'] == witness['counter'] + offset
                    with pytest.raises(IndexError):
                        list(range(n))[witness['index']]


@pytest.mark.parametrize('group', ['defects', 'safe_controls', 'capability_boundaries', 'other_properties'])
@pytest.mark.parametrize('options', [{'max_actions': 0}, {'max_cost': 2}, {'verify': False}])
def test_incomplete_analysis_never_suppresses_findings(group, options):
    project = Project(FIXTURES / f'{group}.py')
    analysis = analyze_evidence(project, **options)
    assert all(c['verdict'] == 'UNDECIDED' for c in analysis['claims'])
    tracker = AuditTracker(project, file_candidates(project, [])[0], program_analysis=analysis)
    assert not tracker.resolved_claims and not tracker.program_units and not tracker.evidence_complete
    findings = [{'file': c['file'], 'line': c['line'], 'title': '待复核的越界意见',
                 'issue_kind': 'index_bounds', 'access_id': c['access_id']} for c in analysis['claims']]
    assert tracker.reconcile_findings({'findings': findings})['findings'] == findings


@pytest.mark.parametrize('function', ['undefined_after_read', 'attribute_after_read', 'divide_after_read', 'distinct_same_line'])
def test_other_property_survives_access_correction(function):
    project = Project(FIXTURES / 'other_properties.py')
    analysis = analyze_evidence(project, max_actions=1000, max_cost=2000)
    claims = [c for c in analysis['claims'] if project.at(c['file'], c['line']).name == function]
    tracker = AuditTracker(project, file_candidates(project, [])[0], program_analysis=analysis)
    findings = [{'file': c['file'], 'line': c['line'], 'title': '模型越界意见',
                 'issue_kind': 'index_bounds', 'access_id': c['access_id']} for c in claims]
    other = {'file': claims[0]['file'], 'line': claims[0]['line'], 'title': function,
             'issue_kind': 'other', 'access_id': None}
    retained = tracker.reconcile_findings({'findings': findings + [other]})['findings']
    assert other in retained
    for claim in claims:
        matches = [f for f in retained if f['access_id'] == claim['access_id']]
        if claim['verdict'] == 'SAFE':
            assert matches == []
        elif claim['verdict'] == 'VIOLATION':
            assert len(matches) == 1 and matches[0]['evidence_origin'] == 'program_verified'
        else:
            assert matches == [findings[0]]
    assert not any(r['original']['issue_kind'] == 'other' for r in tracker.program_reconciliations)


def test_unknown_accesses_keep_model_tasks_and_model_followup():
    from evidence_review.agent import review
    from conftest import with_audit_records
    project = Project(FIXTURES / 'capability_boundaries.py')
    analysis = analyze_evidence(project, max_actions=1000, max_cost=2000)
    candidate = file_candidates(project, [])[0]
    tracker = AuditTracker(project, candidate, program_analysis=analysis)
    assert any(c['verdict'] == 'UNDECIDED' and c['access_id'] not in tracker.resolved_claims for c in analysis['claims'])
    assert any(unit.get('kind') == 'loop_index' for unit in tracker.plan()['functions'])
    class Client:
        calls = 0
        submissions = 0
        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {'tool_calls': [{'function': {'name': 'read_lines', 'arguments':
                    {'file': candidate['file'], 'start': 81, 'end': len(project.sources[candidate['file']].splitlines())}}}]}
            self.submissions += 1
            payload = with_audit_records(messages, {'verdict': 'inconclusive', 'summary': '能力边界需要复核',
                'missing_evidence': [], 'findings': []})
            return {'tool_calls': [{'function': {'name': 'submit_review', 'arguments': payload}}]}
    client = Client()
    result = review(project, candidate, client, program_analysis=analysis)
    assert result['status'] == 'completed'
    assert client.submissions == 2 and client.calls == 3 and result['audit']['followup_performed']
    # This fake client checks protocol only, not detection accuracy.


def test_reliability_comparison_rejects_different_inputs():
    runner = load_runner()
    result = runner.run(['cost'])
    for key in ('manifest_sha256', 'input_hashes', 'cases'):
        changed = deepcopy(result)
        changed[key] = 'tampered'
        with pytest.raises(ValueError, match='输入或标签不一致'):
            runner.compare(result, changed)
    assert runner.compare(result, deepcopy(result)) == result


@pytest.mark.parametrize('guard', list(GUARDS))
def test_parameterized_witness_executes_and_safe_proofs_survive_lengths(tmp_path, guard):
    # Parameter assumptions differ from known local lengths: check witnesses at
    # the length selected by the solver, not just where a random input fails.
    for trim in range(3):
        for offset in range(-4, 5):
            source = ('def f(values: list[int]):\n'
                      f'    for i in range(len(values) - {trim}):\n' + GUARDS[guard].format(k=f'i + ({offset})'))
            project = make_project(tmp_path, source)
            claim = analyze_evidence(project)['claims'][0]
            namespace = {}
            exec(compile(source, '<owned-parameter-witness-oracle>', 'exec'), namespace)
            if claim['verdict'] == 'SAFE':
                for n in range(13):
                    namespace['f'](list(range(n)))
            else:
                assert claim['verdict'] == 'VIOLATION'
                with pytest.raises(IndexError):
                    namespace['f'](list(range(claim['witness']['length'])))
