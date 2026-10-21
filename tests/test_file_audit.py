import json

import pytest
from conftest import with_audit_records

from evidence_review.agent import ContextTools, review
from evidence_review.cli import main
from evidence_review.project import Project
from evidence_review.sbfl import file_candidates


def make_project(tmp_path, code):
    path = tmp_path / "sample.py"
    path.write_text(code)
    return Project(path)


def result_with_findings(findings):
    return {"verdict": "likely_issue", "summary": "测试客户端返回的示例问题列表",
            "evidence_ids": ["E2"], "missing_evidence": [], "suggestion": "人工复核",
            "findings": findings}


def finding(line, title="测试示例"):
    return {"file": "sample.py", "line": line, "title": title, "explanation": "测试说明",
            "condition": "修复前置语法错误后", "suggestion": "人工复核", "evidence_ids": ["E2"]}


def test_broken_ast_does_not_truncate_small_file_to_diagnostic_window(tmp_path):
    project = make_project(tmp_path, "def broken(xs)\n    return xs[1]\n" + "\n" * 12 + "broken([])\n")
    assert not project.functions
    candidate = file_candidates(project, [
        {"file": "sample.py", "diagnostics": [{"rule": "invalid-syntax"}],
         "sbfl_lines": [], "sbfl_score": 0.0, "priority": 1.0}
    ])[0]
    output = review(project, candidate)
    source = output["evidence"][1]
    assert "15: broken([])" in source["code"]
    assert source["start"] == 1 and source["end"] == 15
    assert output["source_coverage"]["complete"]


def test_file_review_includes_no_diagnostic_files(tmp_path):
    project = make_project(tmp_path, "xs = [1]\nprint(xs[2])\n")
    candidates = file_candidates(project, [])
    assert len(candidates) == 1 and candidates[0]["diagnostics"] == []
    assert candidates[0]["scope"] == "file"


def test_multiple_findings_and_schema_are_preserved(tmp_path):
    project = make_project(tmp_path, "def broken(xs)\n    return xs[1]\n")

    class Client:
        def chat(self, messages, tools):
            params = tools[-1]["function"]["parameters"]
            assert "findings" in params["required"]
            return {"tool_calls": [{"function": {"name": "submit_review", "arguments":
                    with_audit_records(messages, result_with_findings([finding(1, "语法"), finding(2, "边界")]))}}]}

    output = review(project, file_candidates(project, [])[0], Client())
    assert output["status"] == "completed"
    assert len(output["review"]["findings"]) == 2


def test_file_findings_do_not_require_duplicate_overall_suggestion(tmp_path):
    project = make_project(tmp_path, "print(missing)\n")
    context = ContextTools(project, 5000)
    context.emit("candidate", {})
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 1})
    arguments = result_with_findings([finding(1)])
    del arguments["suggestion"]
    assert context.validate_review(arguments, file_audit=True)["findings"] == arguments["findings"]
    arguments["findings"][0]["evidence_ids"] = ["E1"]
    with pytest.raises(ValueError, match="可用编号.*E2"):
        context.validate_review(arguments, file_audit=True)


def test_missing_required_fields_receive_actionable_feedback(tmp_path):
    project = make_project(tmp_path, "print(1)\n")
    context = ContextTools(project, 5000)
    arguments = result_with_findings([])
    del arguments["summary"]
    with pytest.raises(ValueError, match="缺少.*summary"):
        context.validate_review(arguments, file_audit=True)


@pytest.mark.parametrize("bad", [
    {"line": 3}, {"file": "outside.py"}, {"evidence_ids": ["E1"]},
    {"evidence_ids": ["E999"]}, {"condition": ""}, {"line": True},
])
def test_findings_must_cite_their_actual_source_range(tmp_path, bad):
    project = make_project(tmp_path, "x = 1\nprint(x)\nprint(x + 1)\n")
    context = ContextTools(project, 5000)
    context.emit("candidate", {})
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 2})
    with pytest.raises(ValueError):
        context.validate_review(result_with_findings([{**finding(1), **bad}]), file_audit=True)


def test_large_file_reports_partial_source_coverage(tmp_path):
    project = make_project(tmp_path, "x = 1\n" * 100)
    output = review(project, file_candidates(project, [])[0])
    assert output["source_coverage"] == {
        "file": "sample.py", "total_lines": 100, "provided_lines": 80, "complete": False}
    empty = review(project, file_candidates(project, [])[0], budget=1)
    assert empty["source_coverage"]["provided_lines"] == 0


def test_file_mode_cli_reports_findings_separately_from_static_diagnostics(tmp_path, monkeypatch):
    project = make_project(tmp_path, "xs = [1]\nprint(xs[2])\n")

    class Client:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}

        def __init__(self, *args):
            pass

        def chat(self, messages, tools):
            return {"tool_calls": [{"function": {"name": "submit_review", "arguments":
                    with_audit_records(messages, result_with_findings([finding(2, "<示例问题>")]))}}]}

    monkeypatch.setattr("evidence_review.cli.Ollama", Client)
    output = tmp_path / "reports"
    assert main(["analyze", str(project.root / "sample.py"), "--model", "test-client",
                 "--review-scope", "files", "--output", str(output)]) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["summary"]["diagnostics"] == 0
    assert report["summary"]["model_findings"] == 1
    html = (output / "report.html").read_text()
    assert "&lt;示例问题&gt;" in html and "触发条件" in html
    assert "2 / 2 行" in html
