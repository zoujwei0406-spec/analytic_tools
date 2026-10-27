import json

import pytest

from evidence_review.agent import ContextTools, review
from evidence_review.sbfl import rank_candidates


def candidate(project):
    return rank_candidates(project, [{"file": "a.c", "line": 3, "severity": "warning", "message": "sample"}], {"lines": []})[0]


def response(name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def conclusion(ids=None):
    return {"verdict": "likely_issue", "summary": "测试客户端的示例结论，不是真实模型分析", "evidence_ids": ids or ["E2"],
            "missing_evidence": ["运行时证据"], "suggestion": "人工复核"}


class ScriptedClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def chat(self, messages, tools):
        self.messages.append(json.loads(json.dumps(messages)))
        return next(self.responses)


def test_real_tool_dispatch_and_feedback(project):
    fid = project.at("a.c", 3).id
    client = ScriptedClient([response("find_callers", {"function_id": fid}),
                             response("get_function", {"function_id": project.at("b.c", 3).id}),
                             response("submit_review", conclusion(["E2", "E4"]))])
    result = review(project, candidate(project), client)
    assert result["status"] == "completed"
    assert result["evidence"][2]["functions"][0]["name"] == "caller"
    assert result["evidence"][3]["file"] == "b.c"
    assert client.messages[1][-1]["role"] == "tool"


def test_unknown_citation_rejected_then_repaired(project):
    client = ScriptedClient([response("submit_review", conclusion(["E999"])),
                             response("submit_review", conclusion())])
    result = review(project, candidate(project), client)
    assert result["status"] == "completed"
    assert "error" in result["trace"][0]["response"]


def test_arbitrary_command_tool_rejected(project):
    client = ScriptedClient([response("run_shell", {"command": "echo ignored"})])
    result = review(project, candidate(project), client, max_rounds=1)
    assert result["status"] == "budget_exhausted"
    assert "未知工具" in result["trace"][0]["response"]["error"]


def test_source_budget_and_scope(project):
    tools = ContextTools(project, 10)
    assert "error" in tools.call("get_function", {"function_id": project.at("a.c", 3).id})
    assert tools.evidence == []
    with pytest.raises(ValueError):
        tools.call("read_lines", {"file": "../../secret", "start": 1, "end": 1})


def test_tool_call_budget_is_enforced(project):
    client = ScriptedClient([{"tool_calls": [
        {"function": {"name": "find_callers", "arguments": {"function_id": project.at("a.c", 3).id}}},
        {"function": {"name": "submit_review", "arguments": conclusion()}},
    ]}])
    result = review(project, candidate(project), client, max_calls=1)
    assert result["status"] == "budget_exhausted"
    assert len(result["trace"]) == 1


def test_fixed_mode_adds_context_without_model(project):
    function = review(project, candidate(project), mode="function")
    fixed = review(project, candidate(project), mode="fixed")
    assert function["status"] == fixed["status"] == "not_run"
    assert function["review"] is None
    assert len(fixed["evidence"]) > len(function["evidence"])


def test_transport_failure_remains_visible(project):
    class BrokenClient:
        def chat(self, messages, tools):
            raise ValueError("连接失败")
    result = review(project, candidate(project), BrokenClient())
    assert result["status"] == "error"
    assert result["review"] is None


@pytest.mark.parametrize("mode", ["function", "fixed", "adaptive"])
def test_regression_long_candidate_seeds_alarm_before_function_prefix(tmp_path, mode):
    from evidence_review.project import Project

    path = tmp_path / "long.py"
    path.write_text("def example():\n" + "    pass\n" * 99 + "    return missing\n")
    project = Project(path)
    diagnostic = {"file": "long.py", "line": 101, "severity": "error", "message": "missing"}
    target = rank_candidates(project, [diagnostic], {"lines": []})[0]
    # A tight budget still has space for the alarm window, but not the full prefix.
    result = review(project, target, mode=mode, budget=1400)
    assert any(e["kind"] == "source" and e["start"] <= 101 <= e["end"] for e in result["evidence"])


@pytest.mark.parametrize("verdict", ["likely_issue", "likely_false_positive"])
def test_regression_candidate_rejects_citation_that_misses_alarm(project, verdict):
    target = candidate(project)
    context = ContextTools(project, 10000, candidate=target)
    prefix = context.call("read_lines", {"file": "a.c", "start": 1, "end": 2})
    args = {**conclusion([prefix["evidence_id"]]), "verdict": verdict}
    with pytest.raises(ValueError, match="候选位置.*a.c:3"):
        context.validate_review(args)
    alarm = context.call("read_lines", {"file": "a.c", "start": 3, "end": 3})
    # Merely delivering the alarm is insufficient if the submission does not cite it.
    with pytest.raises(ValueError, match="候选位置"):
        context.validate_review(args)
    args["evidence_ids"] = [alarm["evidence_id"]]
    assert context.validate_review(args)["verdict"] == verdict
    args.update(verdict="inconclusive", evidence_ids=[], missing_evidence=["需要更多源码"])
    assert context.validate_review(args)["verdict"] == "inconclusive"


def test_regression_candidate_requires_all_aggregated_diagnostic_and_sbfl_locations(tmp_path):
    from evidence_review.project import Project

    path = tmp_path / "long.py"
    path.write_text("def example():\n" + "    pass\n" * 98 + "    return 0\n")
    project = Project(path)
    diagnostics = [{"file": "long.py", "line": n, "severity": "warning", "message": "test"}
                   for n in (2, 90)]
    target = rank_candidates(project, diagnostics, {"lines": [{"file": "long.py", "line": 100, "score": 1.0}]})[0]
    context = ContextTools(project, 10000, candidate=target)
    prefix = context.call("get_function", {"function_id": target["function_id"]})
    args = conclusion([prefix["evidence_id"]])
    with pytest.raises(ValueError, match="long.py:90.*long.py:100"):
        context.validate_review(args)
    end = context.call("read_lines", {"file": "long.py", "start": 81, "end": 100})
    args["evidence_ids"].append(end["evidence_id"])
    assert context.validate_review(args)["verdict"] == "likely_issue"


def test_regression_review_rejects_other_file_citation_then_accepts_target(project):
    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return response("read_lines", {"file": "b.c", "start": 1, "end": 3})
            if self.calls == 2:
                return response("submit_review", conclusion(["E3"]))
            assert "候选位置" in json.loads(messages[-1]["content"])["error"]
            return response("submit_review", conclusion(["E2"]))

    result = review(project, candidate(project), Client())
    assert result["status"] == "completed"
    assert "error" in result["trace"][1]["response"]
    assert result["review"]["evidence_ids"] == ["E2"]
