import json

import pytest

from conftest import with_audit_records
from evidence_review.agent import ContextTools, Ollama, review, review_tools
from evidence_review.audit import AuditTracker
from evidence_review.project import Project
from evidence_review.sbfl import file_candidates


def setup_loop(tmp_path, colon=":", access="values[index + 1]"):
    path = tmp_path / "sample.py"
    path.write_text(f"def neighbors(values){colon}\n"
                    "    for index in range(len(values)):\n"
                    "        current = values[index]\n"
                    f"        following = {access}\n"
                    "    return missing\n")
    project = Project(path)
    candidate = file_candidates(project, [])[0]
    return project, candidate, AuditTracker(project, candidate)


def empty_submission():
    return {"verdict": "inconclusive", "summary": "测试客户端", "findings": [], "missing_evidence": []}


def found(line):
    return {**empty_submission(), "verdict": "likely_issue", "findings": [
        {"file": "sample.py", "line": line, "title": "测试问题", "explanation": "测试说明",
         "condition": "修复前置错误后", "suggestion": "人工复核"}]}


@pytest.mark.parametrize("colon", [":", ""])
def test_index_targets_survive_syntax_errors_without_repairing_source(tmp_path, colon):
    project, _, tracker = setup_loop(tmp_path, colon)
    original = project.hashes()
    units = [u for u in tracker.units.values() if u.get("kind") == "loop_index"]
    assert [u["ranges"] for u in units] == [[[3, 3]], [[4, 4]]]
    assert units[1]["access"] == "values[index + 1]"
    assert units[1]["loop_controls"][0]["expression"] == "range(len(values))"
    assert units[1]["context_ranges"] == [[2, 4]]
    assert units[1]["syntax_recovered"] == (not colon)
    assert bool(project.functions) == bool(colon)
    project.check_unchanged()
    assert original == project.hashes()


def test_missing_index_records_cannot_be_satisfied_by_other_errors_in_same_module(tmp_path):
    project, candidate, _ = setup_loop(tmp_path, colon="")

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            arguments = with_audit_records(messages, found(1) if self.calls == 1 else empty_submission())
            if self.calls == 1:
                arguments["function_reviews"] = [row for row in arguments["function_reviews"]
                                                  if not row["function_id"].endswith(":loop-index")]
            else:
                feedback = json.loads(messages[-1]["content"])
                assert len(feedback["pending"]["functions"]) == 2
                assert all(key.endswith(":loop-index") for key in feedback["pending"]["functions"])
            return {"tool_calls": [{"function": {"name": "submit_review", "arguments": arguments}}]}

    result = review(project, candidate, Client())
    assert result["status"] == "completed"
    assert result["audit"]["loop_index_total"] == 2
    assert len(result["review"]["findings"]) == 1  # No artificial bounds findings.


def test_index_disposition_needs_loop_context_not_just_access_line(tmp_path):
    project, _, tracker = setup_loop(tmp_path)
    unit = next(u for u in tracker.units.values() if u.get("access") == "values[index + 1]")
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 4, "end": 4})
    arguments = {"function_reviews": [{"function_id": unit["function_id"],
                                       "verdict": "no_issue_found", "reason": "边界有效"}]}
    with pytest.raises(ValueError, match="尚未完整提供"):
        tracker.accept({**empty_submission(), "evidence_ids": ["E1"]}, arguments, context)


def test_issue_elsewhere_in_loop_cannot_stand_in_for_this_access(tmp_path):
    project, _, tracker = setup_loop(tmp_path)
    unit = next(u for u in tracker.units.values() if u.get("access") == "values[index + 1]")
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 5})
    tracker.accept({**found(3), "evidence_ids": ["E1"]}, {"function_reviews": [
        {"function_id": unit["function_id"], "verdict": "issue_found", "reason": "循环有问题"}]}, context)
    assert tracker.function_reviews[unit["function_id"]]["verdict"] == "inconclusive"


def test_requested_index_recheck_requires_a_new_record(tmp_path):
    project, candidate, _ = setup_loop(tmp_path)

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            arguments = with_audit_records(messages, found(4) if self.calls == 1 else empty_submission())
            if self.calls == 2:
                feedback = json.loads(messages[-1]["content"])
                assert any(key.endswith(":loop-index") for key in feedback["recheck_functions"])
                arguments["function_reviews"] = []  # Cannot silently skip rechecking.
            if self.calls == 3:
                assert json.loads(messages[-1]["content"])["pending"]["functions"]
            return {"tool_calls": [{"function": {"name": "submit_review", "arguments": arguments}}]}

    client = Client()
    result = review(project, candidate, client)
    assert client.calls == 3 and result["status"] == "completed"


def test_nested_function_does_not_inherit_outer_loop_scope(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("for item in items:\n    def inner(values):\n        return values[0]\n")
    project = Project(path)
    tracker = AuditTracker(project, file_candidates(project, [])[0])
    assert not any(unit.get("kind") == "loop_index" for unit in tracker.units.values())


def test_while_targets_keep_guards_and_distinguish_accesses_on_the_same_line(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("def pairs(values):\n    index = 0\n    result = []\n"
                    "    while index + 1 < len(values):\n"
                    "        result.append((values[index], values[index + 1]))\n"
                    "        index += 1\n    return result\n")
    project = Project(path)
    tracker = AuditTracker(project, file_candidates(project, [])[0])
    units = [unit for unit in tracker.units.values() if unit.get("kind") == "loop_index"]
    assert len({unit["function_id"] for unit in units}) == 2
    assert all(unit["loop_controls"][0]["expression"] == "index + 1 < len(values)" for unit in units)
    assert all(unit["context_ranges"] == [[4, 6]] for unit in units)


def test_qwen_loop_review_enables_reasoning_and_keeps_structured_submission(tmp_path, monkeypatch):
    project, _, tracker = setup_loop(tmp_path)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 5})
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"message": {"content": json.dumps(empty_submission())},
                               "eval_count": 100, "done_reason": "stop"}).encode()

    def request(req, timeout):
        captured.append(json.loads(req.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", request)
    result = Ollama("qwen3:8b").chat([{"role": "system", "content": "test"}],
                                     review_tools(context, "adaptive", True, tracker))
    assert captured[0]["think"] is True
    assert captured[0]["options"]["num_predict"] == 8192
    assert "format" in captured[0] and "tools" not in captured[0]
    assert result["_transport"]["thinking_requested"] is True


def test_retry_schema_targets_missing_records_instead_of_repeating_completed_ones(tmp_path):
    project, _, tracker = setup_loop(tmp_path, colon="")
    schema = tracker.schema()["function_reviews"]
    assert schema["minItems"] == schema["maxItems"] == 3
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 5})
    tracker.accept({**empty_submission(), "evidence_ids": ["E1"]}, {"function_reviews": [
        {"function_id": key, "verdict": "inconclusive", "reason": "尚未确认"}
        for key in tracker.units if key.endswith(":loop-index")]}, context)
    schema = tracker.schema()["function_reviews"]
    assert schema["minItems"] == schema["maxItems"] == 1
    assert schema["items"]["properties"]["function_id"]["enum"] == ["sample.py:module"]


def test_recheck_can_correct_conditions_without_duplicating_or_renumbering_findings(tmp_path):
    _, _, tracker = setup_loop(tmp_path)
    original = {**found(4), "evidence_ids": ["E1"]}
    original["findings"][0]["condition"] = "旧条件"
    tracker.conclusion = tracker.merge_findings(original)
    corrected = {**found(4), "evidence_ids": ["E1"]}
    corrected["findings"][0]["condition"] = "非空列表且执行到最后一轮"
    result = tracker.merge_findings(corrected)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["finding_id"] == "F1"
    assert result["findings"][0]["condition"] == "非空列表且执行到最后一轮"
    assert original["findings"][0]["condition"] == "旧条件"
