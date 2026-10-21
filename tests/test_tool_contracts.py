from copy import deepcopy

import pytest
from conftest import with_audit_records

from evidence_review.agent import ContextTools, review, review_tools
from evidence_review.project import Project
from evidence_review.sbfl import file_candidates


def make_context(tmp_path, lines=51, provided=51):
    path = tmp_path / "sample.py"
    path.write_text("value = 1\n" * lines)
    context = ContextTools(Project(path), 20000)
    context.emit("candidate", {"diagnostics": []})
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": provided})
    return context


def review_arguments():
    return {"verdict": "likely_issue", "summary": "测试客户端结论", "missing_evidence": [],
            "findings": [{"file": "sample.py", "line": 51, "title": "测试问题", "explanation": "测试说明",
                          "condition": "测试条件", "suggestion": "人工复核"}]}


def test_file_review_binds_only_already_provided_source(tmp_path):
    context = make_context(tmp_path)
    arguments = review_arguments()
    original = deepcopy(arguments)
    result = context.validate_review(arguments, file_audit=True)
    assert result["evidence_ids"] == ["E2"]
    assert result["findings"][0]["evidence_ids"] == ["E2"]
    assert result["findings"][0]["evidence_origin"] == "source_location"
    assert arguments == original  # The raw model request remains auditable.


def test_overall_citations_are_derived_from_validated_findings(tmp_path):
    context = make_context(tmp_path)
    arguments = review_arguments()
    arguments["evidence_ids"] = ["E1"]
    arguments["findings"][0]["evidence_ids"] = ["E2"]
    result = context.validate_review(arguments, file_audit=True)
    assert result["evidence_ids"] == ["E1", "E2"]


@pytest.mark.parametrize("change", [{"file": "other.py"}, {"line": 52}, {"line": 0}, {"line": True}])
def test_automatic_binding_rejects_invented_locations(tmp_path, change):
    context = make_context(tmp_path)
    arguments = review_arguments()
    arguments["findings"][0].update(change)
    with pytest.raises(ValueError):
        context.validate_review(arguments, file_audit=True)


def test_location_in_file_but_outside_returned_evidence_is_rejected(tmp_path):
    context = make_context(tmp_path, lines=100, provided=20)
    with pytest.raises(ValueError, match="没有已提供的源码证据"):
        context.validate_review(review_arguments(), file_audit=True)


def test_read_past_eof_is_bounded_and_reuses_existing_source(tmp_path):
    context = make_context(tmp_path)
    used = context.used
    result = context.call("read_lines", {"file": "sample.py", "start": 1, "end": 80})
    assert result["start"] == 1 and result["end"] == 51
    assert result["requested_range"] == {"start": 1, "end": 80}
    assert result["range_adjusted"] and result["reused"]
    assert result["evidence_id"] == "E2" and context.used == used
    assert result["code"].splitlines()[-1] == "51: value = 1"


def test_large_read_is_paginated_and_invalid_start_is_actionable(tmp_path):
    context = make_context(tmp_path, lines=200, provided=1)
    result = context.call("read_lines", {"file": "sample.py", "start": 2, "end": 999})
    assert result["end"] == 81 and result["next_start"] == 82
    assert len(result["code"].splitlines()) == 80
    with pytest.raises(ValueError, match="共 200 行"):
        context.call("read_lines", {"file": "sample.py", "start": 201, "end": 220})
    with pytest.raises(ValueError, match="本次导入"):
        context.call("read_lines", {"file": "../../secret.py", "start": 1, "end": 10})


def test_schema_removes_redundant_queries_and_citation_bookkeeping(tmp_path):
    context = make_context(tmp_path)
    tools = review_tools(context, "adaptive", True)
    assert [tool["function"]["name"] for tool in tools] == ["submit_review"]
    schema = tools[0]["function"]["parameters"]
    assert "evidence_ids" not in schema["properties"]
    finding = schema["properties"]["findings"]["items"]["properties"]
    assert "evidence_ids" not in finding
    assert finding["file"]["enum"] == ["sample.py"]
    assert finding["line"]["maximum"] == 51
    assert "enum" not in finding["title"]  # No shared schema alias contamination.
    assert "enum" not in finding["condition"]


def test_tools_refresh_after_remaining_source_is_read(tmp_path):
    context = make_context(tmp_path, lines=100, provided=80)
    assert "read_lines" in [t["function"]["name"] for t in review_tools(context, "adaptive", True)]
    context.call("read_lines", {"file": "sample.py", "start": 81, "end": 100})
    assert [t["function"]["name"] for t in review_tools(context, "adaptive", True)] == ["submit_review"]


def test_file_audit_binds_citations_and_verifies_findings_without_model_evidence_ids(tmp_path):
    context = make_context(tmp_path)

    class Client:
        def chat(self, messages, tools):
            assert [tool["function"]["name"] for tool in tools] == ["submit_review"]
            return {"tool_calls": [{"function": {"name": "submit_review", "arguments": with_audit_records(messages, review_arguments())}}]}

    result = review(context.project, file_candidates(context.project, [])[0], Client(), max_rounds=2)
    assert result["status"] == "completed"
    assert len(result["trace"]) == 2
    assert result["review"]["findings"][0]["evidence_ids"] == ["E2"]


def test_clean_file_summary_uses_actual_source_and_not_a_fake_finding(tmp_path):
    context = make_context(tmp_path)
    arguments = {"verdict": "inconclusive", "summary": "未发现足够证据", "missing_evidence": [], "findings": []}
    result = context.validate_review(arguments, file_audit=True)
    assert result["evidence_ids"] == ["E2"] and not result["findings"]
    arguments["verdict"] = "likely_issue"
    with pytest.raises(ValueError, match="至少列出一个"):
        context.validate_review(arguments, file_audit=True)
