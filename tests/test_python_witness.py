"""Replay closed-interpreter witnesses using only test-owned finite Python."""
import ast
from copy import deepcopy

import pytest

from evidence_review.evidence import analyze_evidence
from evidence_review.knowledge.python_witness import search
from evidence_review.project import Project
from evidence_review.audit import AuditTracker
from evidence_review.sbfl import file_candidates


def analyze(tmp_path, source, **options):
    path = tmp_path / 'owned.py'
    path.write_text(source)
    return Project(path), analyze_evidence(Project(path), **options)


TEMPLATES = {
    'mutation': '    for i in range(len(values)):\n        values.pop()\n        item = values[i + ({offset})]\n',
    'alias': '    alias = values\n    for i in range(len(values)):\n        alias.pop()\n        item = values[i + ({offset})]\n',
    'helper': '    for i in range(len(values)):\n        shrink(values)\n        item = values[i + ({offset})]\n',
    'nested': '    for i in range(len(values)):\n        for j in range(len(values)):\n            item = values[j + ({offset})]\n',
    'square': '    for i in range(len(values)):\n        item = values[i*i + ({offset})]\n',
    'step': '    for i in range(0, len(values), 2):\n        item = values[i + ({offset})]\n',
    'branch': '    for i in range(len(values)):\n        if i >= 0:\n            item = values[i + ({offset})]\n',
    'reassigned': '    for i in range(len(values)):\n        i = 2\n        item = values[i + ({offset})]\n',
    'compound_exit': '    for i in range(len(values)):\n        if i + ({offset}) >= len(values):\n            item = values[i + ({offset})]\n            continue\n',
    'stale_if': '    i = 0\n    if i + ({offset}) < len(values):\n        for i in range(len(values)):\n            item = values[i + ({offset})]\n',
    'stale_exit': '    i = 0\n    if i + ({offset}) >= len(values):\n        return None\n    for i in range(len(values)):\n        item = values[i + ({offset})]\n',
    'ternary': '    for i in range(len(values)):\n        item = values[i + ({offset})] if i >= 0 else None\n',
    'filtered_comprehension': '    return [values[i*i + ({offset})] for i in range(len(values)) if i >= 0]\n',
    'short_circuit': '    for i in range(len(values)):\n        if i < 0 or i >= 0:\n            item = values[i + ({offset})]\n',
}


@pytest.mark.parametrize('template', TEMPLATES)
def test_search_matches_native_reachability_and_replays_witness(template):
    # Compile/execute only these fixed test-owned templates. The analyzer never
    # calls exec/eval, including when analyzing files supplied by a user.
    for offset in range(-3, 4):
        source = 'def shrink(values):\n    values.pop()\n\ndef f(values: list[int]):\n' + TEMPLATES[template].format(offset=offset)
        module = ast.parse(source)
        scope = module.body[-1]
        target = next(n for n in ast.walk(scope) if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and isinstance(n.value, ast.Name) and n.value.id == 'values')
        namespace = {}
        exec(compile(source, '<owned-finite-witness-oracle>', 'exec'), namespace)
        unsafe = False
        for n in range(9):
            try:
                namespace['f'](list(range(n)))
            except IndexError:
                unsafe = True
        result = search(module, scope, target)
        assert (result['status'] == 'supported') == unsafe, (template, offset, result)
        if unsafe:
            witness = result['witness']
            assert witness['kind'] == 'bounded_ast_counterexample'
            assert not -witness['length'] <= witness['index'] < witness['length']
            with pytest.raises(IndexError):
                namespace['f'](**deepcopy(witness['entry_inputs']))
            assert result['total_steps'] <= 100001


@pytest.mark.parametrize('body', [
    '    for i in range(0, len(values), 2):\n        if i + 1 < len(values):\n            item = values[i+1]\n',
    '    for i in range(len(values)):\n        values.pop()\n        if i < len(values):\n            item = values[i]\n',
    '    return [values[i*i] for i in range(len(values)) if i*i < len(values)]\n',
    '    for i in range(len(values)):\n        return\n        item = values[i+1]\n',
    '    for i in range(len(values)):\n        item = values[i+1] if False else None\n',
    '    for i in range(len(values)):\n        break\n        item = values[i+1]\n',
    '    for i in range(len(values)):\n        values.pop()\n        continue\n        item = values[i]\n',
    '    for i in range(len(values)):\n        1 // 0\n        item = values[i+1]\n',
    '    for i in range(len(values)):\n        missing_name\n        item = values[i+1]\n',
    '    for i in range(len(values)):\n        callback(values)\n        item = values[i+1]\n',
])
def test_safe_or_blocked_paths_never_become_violations(tmp_path, body):
    _, result = analyze(tmp_path, 'def f(values: list[int]):\n' + body)
    assert all(c['verdict'] != 'VIOLATION' for c in result['claims'])


@pytest.mark.parametrize('prefix,body', [
    ('def shrink(values):\n    values.pop()\nshrink = external\n', 'shrink(values)'),
    ('def shrink(values, range):\n    for j in range(len(values)):\n        values.pop()\n', 'shrink(values, unknown)'),
    ('def shrink(values):\n    range = external\n    for j in range(len(values)):\n        values.pop()\n', 'shrink(values)'),
    ('def shrink(values):\n    shrink(values)\n', 'shrink(values)'),
    ('@external\ndef shrink(values):\n    values.pop()\n', 'shrink(values)'),
    ('def shrink(values):\n    import os\n    values.pop()\n', 'shrink(values)'),
])
def test_dynamic_helpers_are_not_executed_or_assumed(tmp_path, prefix, body):
    source = prefix + '\ndef f(values: list[int]):\n    for i in range(len(values)):\n        ' + body + '\n        item = values[i]\n'
    _, result = analyze(tmp_path, source)
    assert result['claims'][-1]['verdict'] == 'UNDECIDED'


def test_mutation_witness_reports_entry_and_access_length_separately(tmp_path):
    project, result = analyze(tmp_path, 'def f(values: list[int]):\n' + TEMPLATES['alias'].format(offset=0))
    claim = result['claims'][0]
    assert claim['verdict'] == 'VIOLATION'
    witness = claim['witness']
    assert witness['entry_inputs'] == {'values': [0]} and witness['length'] == 0
    assert witness['index'] == 0 and any(e.get('effect') == 'pop' for e in witness['path'])
    tracker = AuditTracker(project, file_candidates(project, [])[0], program_analysis=result)
    finding = {'file': claim['file'], 'line': claim['line'], 'title': '越界', 'issue_kind': 'index_bounds', 'access_id': claim['access_id']}
    rendered = tracker.reconcile_findings({'findings': [finding]})['findings'][0]
    assert '入口参数' in rendered['condition'] and '执行到该访问时' in rendered['condition']
    assert 'AST 路径见证' in rendered['explanation']


def test_search_budget_and_no_result_do_not_prove_safety():
    source = 'def f(values: list[int]):\n    for i in range(1000000):\n        item = values[i*i]\n'
    module = ast.parse(source)
    target = next(n for n in ast.walk(module) if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == 'values')
    result = search(module, module.body[0], target)
    assert result['status'] == 'unsupported' and result['reason'] == 'no_bounded_counterexample'


@pytest.mark.parametrize('policy,verdict', [('rules', 'UNDECIDED'), ('fixed', 'VIOLATION'), ('gaps', 'VIOLATION'), ('cost', 'VIOLATION')])
def test_local_policy_cannot_enter_helpers_through_fallback(tmp_path, policy, verdict):
    source = ('def shrink(values):\n    values.pop()\n\ndef f(values: list[int]):\n'
              '    alias = values\n    for i in range(len(values)):\n'
              '        shrink(alias)\n        item = values[i]\n')
    _, result = analyze(tmp_path, source, policy=policy)
    assert result['claims'][0]['verdict'] == verdict
    assert any(e['tool'] == 'counterexample' for e in result['trace'])


@pytest.mark.parametrize('source', [
    'def f(values: list[int]):\n    for i in range(len(values)):\n        values.pop()\n        item = values[i]\n    yield 1\n',
    'def lazy(values):\n    values.pop()\n    yield 1\n\ndef f(values: list[int]):\n    for i in range(len(values)):\n        lazy(values)\n        item = values[i]\n',
])
def test_generator_body_is_not_treated_as_eager_call(tmp_path, source):
    _, result = analyze(tmp_path, source)
    assert result['claims'][-1]['verdict'] == 'UNDECIDED'


@pytest.mark.parametrize('tail', ['    break\n', '    continue\n', '    nonlocal absent\n'])
def test_context_invalid_ast_never_produces_a_witness(tmp_path, tail):
    source = 'def f(values: list[int]):\n    values.pop()\n    item = values[0]\n' + tail
    # ast.parse accepts these; Python rejects them before the function can run.
    ast.parse(source)
    with pytest.raises(SyntaxError):
        compile(source, '<owned-invalid-context>', 'exec')
    _, result = analyze(tmp_path, source)
    assert result['claims'] == [] and result['parse_errors'][0]['reason'] == 'syntax_error'
