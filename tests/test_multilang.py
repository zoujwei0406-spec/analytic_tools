import json
from pathlib import Path

import pytest

from evidence_review import scanner
from evidence_review.agent import review
from evidence_review.cli import main
from evidence_review.project import Project
from evidence_review.sbfl import rank_candidates, score_spectra


DEMO = Path(__file__).resolve().parents[1] / 'src/evidence_review/demo_multilang'


def test_all_languages_have_real_ast_functions():
    project = Project(DEMO)
    assert set(project.languages.values()) == {'c', 'cpp', 'python', 'javascript', 'typescript'}
    assert not project.warnings
    assert project.at('bounds.cpp', 6).name == 'read'
    assert project.at('unsafe.js', 2).name == 'parseInput'
    assert project.at('unsafe.ts', 6).name == 'handleInput'
    assert project.at('unsafe.py', 5).name == 'parse_input'


def test_nested_python_scopes_and_decorators(tmp_path):
    (tmp_path / 'module.py').write_text('@decorate\ndef outer():\n    def inner():\n        return hidden()\n    return visible()\n')
    project = Project(tmp_path)
    outer, inner = project.at('module.py', 1), project.at('module.py', 4)
    assert outer.name == 'outer'
    assert inner.name == 'inner'
    assert outer.calls == ['visible']
    assert inner.calls == ['hidden']
    assert outer.code.startswith('@decorate')


def test_language_families_do_not_cross(tmp_path):
    (tmp_path / 'a.c').write_text('int target(void) { return 1; }\n')
    (tmp_path / 'b.py').write_text('def caller():\n    return target()\n')
    project = Project(tmp_path)
    assert project.callers(project.at('a.c', 1).id) == []


def test_js_to_ts_call_candidates_and_arrow_functions(tmp_path):
    (tmp_path / 'a.ts').write_text('export const target = (n: number) => n + 1;\n')
    (tmp_path / 'b.js').write_text('export function caller() { return target(2); }\n')
    project = Project(tmp_path)
    assert [f.name for f in project.callers(project.at('a.ts', 1).id)] == ['caller']


def test_cpp_static_members_are_not_file_local(tmp_path):
    (tmp_path / 'a.hpp').write_text('class A { public: static int target() { return 1; } };\n')
    (tmp_path / 'b.cpp').write_text('int caller() { return A::target(); }\n')
    project = Project(tmp_path)
    target = project.at('a.hpp', 1)
    assert not target.static
    assert [f.name for f in project.callers(target.id)] == ['caller']


def test_cpp_pointer_return_and_same_line_overloads(tmp_path):
    (tmp_path / 'a.cpp').write_text('int f(int x){return x;} double f(double x){return x;}\nchar *get(){return nullptr;}\n')
    project = Project(tmp_path)
    assert len(project.functions) == 3
    assert project.at('a.cpp', 2).name == 'get'


@pytest.mark.parametrize('extension,code', [
    ('jsx', 'export const View = () => <button>OK</button>;\n'),
    ('tsx', 'export const View = (p: {label: string}) => <button>{p.label}</button>;\n'),
])
def test_jsx_tsx_parse(tmp_path, extension, code):
    path = tmp_path / f'view.{extension}'
    path.write_text(code)
    project = Project(path)
    assert not project.warnings
    assert project.at(path.name, 1).name == 'View'


def test_single_file_and_unsupported_extension(tmp_path):
    path = tmp_path / 'a.py'
    path.write_text('def test():\n    return 1\n')
    assert list(Project(path).sources) == ['a.py']
    (tmp_path / 'a.java').write_text('class A {}')
    with pytest.raises(ValueError, match='不支持'):
        Project(tmp_path / 'a.java')


def test_dependency_and_model_dirs_are_pruned(tmp_path):
    for directory in ('models', 'runtime', 'node_modules', '.venv'):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'ignored.py').write_text('def ignore(): pass\n')
    (tmp_path / 'selected.py').write_text('def selected(): pass\n')
    assert list(Project(tmp_path).sources) == ['selected.py']


def test_real_multilanguage_scanners_and_source_ids():
    project = Project(DEMO)
    result = scanner.scan(project)
    assert result['status'] == 'completed', result['notes']
    tools = {t['tool'] for t in result['tools']}
    assert tools == {'cppcheck', 'bandit', 'ruff', 'pyright', 'eslint'}
    rules = {(d['file'], d['rule']) for d in result['diagnostics']}
    assert {('bounds.c', 'arrayIndexOutOfBounds'), ('bounds.cpp', 'arrayIndexOutOfBounds'),
            ('unsafe.py', 'B307'), ('unsafe.py', 'F821'), ('unsafe.js', 'no-eval'), ('unsafe.ts', 'no-eval')} <= rules
    ids = [d['id'] for d in result['diagnostics']]
    assert len(ids) == len(set(ids))
    cpp_command = result['tools'][0]['command']
    assert not any(arg.endswith(('.py', '.js', '.ts')) for arg in cpp_command)


def test_python_file_level_diagnostics_get_source_evidence(tmp_path):
    (tmp_path / 'a.py').write_text('import pickle\n')
    project = Project(tmp_path)
    result = scanner.scan(project)
    candidates = rank_candidates(project, result['diagnostics'], score_spectra(None))
    assert candidates and candidates[0]['function_id'] is None
    output = review(project, candidates[0])
    assert output['evidence'][1]['language'] == 'python'


def test_missing_scanner_is_partial_not_clean(tmp_path, monkeypatch):
    (tmp_path / 'a.py').write_text('def broken():\n    return missing\n')
    original = scanner.find_tool
    monkeypatch.setattr(scanner, 'find_tool', lambda name: None if name == 'bandit' else original(name))
    result = scanner.scan(Project(tmp_path))
    assert result['status'] == 'partial'
    assert result['tools'][0]['status'] == 'unavailable'
    assert any(d['rule'] == 'F821' for d in result['diagnostics'])


def test_syntax_error_is_reported_not_silently_clean(tmp_path):
    (tmp_path / 'broken.py').write_text('def broken(:\n    pass\n')
    result = scanner.scan(Project(tmp_path))
    assert result['status'] == 'partial'
    assert any(d['tool'] == 'ruff' for d in result['diagnostics'])


def test_eslint_does_not_execute_target_config(tmp_path):
    marker = tmp_path / 'executed'
    (tmp_path / 'eslint.config.mjs').write_text(f'import fs from "node:fs"; fs.writeFileSync({json.dumps(str(marker))}, "bad"); export default [];\n')
    (tmp_path / 'a.js').write_text('export function parse(text) { return eval(text); }\n')
    result = scanner.scan(Project(tmp_path, ['a.js']))
    assert result['status'] == 'completed', result['notes']
    assert any(d['rule'] == 'no-eval' for d in result['diagnostics'])
    assert not marker.exists()


def test_multilanguage_cli_report(tmp_path):
    assert main(['demo', '--suite', 'multilang', '--output', str(tmp_path)]) == 0
    report = json.loads((tmp_path / 'report.json').read_text())
    assert len(report['summary']['languages']) == 5
    assert report['sbfl']['status'] == 'not_provided'
    html = (tmp_path / 'report.html').read_text()
    assert '扫描器运行状态' in html and 'bandit' in html and 'eslint' in html
