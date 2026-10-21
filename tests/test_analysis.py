import json

import pytest

from evidence_review.cli import example_spectra
from evidence_review.coverage import parse_lcov
from evidence_review.project import Project
from evidence_review.sbfl import load_spectra, rank_candidates, score_spectra
from evidence_review.scanner import parse_xml


def test_ast_cross_file_callers_and_static_scope(project):
    target = project.at("a.c", 3)
    assert target.name == "target"
    assert [f.name for f in project.callers(target.id)] == ["caller"]
    helper = project.at("a.c", 1)
    assert [f.name for f in project.callers(helper.id)] == ["target"]
    assert [f.file for f in project.callees(target.id)] == ["a.c"]
    assert "3:     return helper(n);" in project.lines("a.c", 2, 4)


def test_pointer_return_and_indirect_call(tmp_path):
    (tmp_path / "a.c").write_text('char *get(void) { return 0; }\nint f(void) { return (*callback)(1); }\n')
    project = Project(tmp_path)
    assert project.at("a.c", 1).name == "get"
    assert project.at("a.c", 2).indirect_calls == 1


def test_selected_inputs_and_path_escape(project):
    subset = Project(project.root, ["a.c"])
    assert set(subset.sources) == {"a.c"}
    with pytest.raises(ValueError, match="项目目录"):
        Project(project.root, ["../"])
    with pytest.raises(ValueError, match="导入"):
        subset.lines("b.c", 1, 2)


def test_changed_source_detected(project):
    (project.root / "a.c").write_text("int changed;\n")
    with pytest.raises(ValueError, match="发生变化"):
        project.check_unchanged()


def test_cppcheck_xml_keeps_locations_and_escapes(project):
    text = '''Checking a.c ...
<?xml version="1.0"?><results><errors>
<error id="testRule" severity="warning" msg="x &lt; y" inconclusive="true">
<location file="a.c" line="3"/><location file="b.c" line="3"/>
</error><error id="missingInclude" severity="information" msg="missing"/>
</errors></results>'''
    findings, notes = parse_xml(text, project)
    assert findings[0]["message"] == "x < y"
    assert len(findings[0]["locations"]) == 2
    assert findings[0]["inconclusive"] is True
    assert notes == ["missingInclude: missing"]


def test_ochiai_counts_per_test_not_visits():
    data = {"tests": [
        {"outcome": "fail", "covered": {"a.c": [1, 1, 2]}},
        {"outcome": "fail", "covered": {"a.c": [1]}},
        {"outcome": "pass", "covered": {"a.c": [2]}},
        {"outcome": "error", "covered": {"a.c": [1]}},
    ]}
    result = score_spectra(data)
    assert result["lines"][0]["score"] == 1.0
    assert result["lines"][1]["score"] == 0.5
    assert result["errors"] == 1


def test_no_failing_tests_is_not_safe_verdict():
    result = score_spectra({"tests": [{"outcome": "pass", "covered": {"a.c": [1]}}]})
    assert result["status"] == "no_failing_tests"
    assert result["lines"] == []


def test_spectrum_hash_validation_and_invalid_lines(demo_project, tmp_path):
    data = example_spectra(demo_project)
    path = tmp_path / "spectra.json"
    path.write_text(json.dumps(data))
    assert load_spectra(path, demo_project)["origin"] == "synthetic-demo"
    data["tests"][0]["covered"]["fees.c"] = [999]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="行号"):
        load_spectra(path, demo_project)
    data["source_hashes"] = {}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="哈希"):
        load_spectra(path, demo_project)


def test_ranking_retains_unflagged_sbfl_candidates(project):
    diagnostic = {"file": "a.c", "line": 3, "severity": "warning", "id": "D1"}
    sbfl = {"lines": [{"file": "b.c", "line": 3, "score": 0.9},
                       {"file": "a.c", "line": 3, "score": 0.8}]}
    candidates = rank_candidates(project, [diagnostic], sbfl)
    assert candidates[0]["name"] == "target"
    assert len(candidates) == 2
    assert candidates[1]["diagnostics"] == []


def test_ranking_ties_have_equal_priority(project):
    sbfl = {"lines": [{"file": "b.c", "line": 3, "score": 0.9},
                       {"file": "a.c", "line": 3, "score": 0.9}]}
    candidates = rank_candidates(project, [], sbfl)
    assert candidates[0]["priority"] == candidates[1]["priority"]


def test_lcov_merges_counts_ignores_foreign_files(project):
    text = f'SF:{project.root}/a.c\nDA:1,0\nDA:2,1\nDA:3,7\nend_of_record\nSF:/outside/file.c\nDA:99,9\nend_of_record\n'
    assert parse_lcov(text, project) == {"a.c": [2, 3]}
