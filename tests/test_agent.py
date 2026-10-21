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
