import json

import pytest

from conftest import with_audit_records
from evidence_review.agent import ContextTools, Ollama, review, review_tools
from evidence_review.audit import AuditTracker
from evidence_review.project import Project
from evidence_review.scanner import scan, scan_pyright
from evidence_review.sbfl import file_candidates


def setup_audit(tmp_path, code="def first():\n    return missing\ndef second():\n    return 1 + 'x'\n"):
    path = tmp_path / "sample.py"
    path.write_text(code)
    project = Project(path)
    candidate = file_candidates(project, [])[0]
    candidate["diagnostics"] = [{"id": "D1", "file": "sample.py", "line": 2, "rule": "F821"}]
    return project, candidate


def submission(line=None):
    return {"verdict": "likely_issue" if line else "inconclusive", "summary": "测试客户端",
            "missing_evidence": [], "findings": [] if not line else [
                {"file": "sample.py", "line": line, "title": f"测试问题 {line}",
                 "explanation": "测试说明", "condition": "调用时", "suggestion": "人工复核"}]}


def respond(arguments):
    return {"tool_calls": [{"function": {"name": "submit_review", "arguments": arguments}}]}


def test_missing_checklists_trigger_targeted_followup_and_preserve_initial_findings(tmp_path):
    project, candidate = setup_audit(tmp_path)
    events = []

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return respond(submission(2))  # Findings alone cannot finish the audit.
            assert events[0]["response"]["complete"] is False
            feedback = json.loads(messages[-1]["content"])
            assert feedback["pending"]["diagnostics"] == ["D1"]
            assert len(feedback["pending"]["functions"]) == 2
            arguments = with_audit_records(messages, submission(4))
            arguments["function_reviews"][0]["verdict"] = "issue_found"
            return respond(arguments)

    client = Client()
    result = review(project, candidate, client, trace_callback=events.append)
    assert result["status"] == "completed" and client.calls == 2
    assert {f["line"] for f in result["review"]["findings"]} == {2, 4}
    assert result["audit"]["complete"] and result["audit"]["followup_performed"]
    assert len(result["audit"]["diagnostic_reviews"]) == 1
    assert len(events) == 2


def test_import_only_prefix_does_not_create_a_phantom_function_task(tmp_path):
    project, candidate = setup_audit(tmp_path, "import pickle\nimport random\ndef run(value):\n    return value\n")
    tracker = AuditTracker(project, candidate)
    assert len(tracker.units) == 1
    assert next(iter(tracker.units.values()))["name"] == "run"
    assert tracker.diagnostics["D1"]["line"] == 2


def test_round_limit_keeps_partial_findings_without_claiming_completion(tmp_path):
    project, candidate = setup_audit(tmp_path)

    class Client:
        def chat(self, messages, tools):
            return respond(submission(2))

    result = review(project, candidate, Client(), max_rounds=1)
    assert result["status"] == "budget_exhausted"
    assert len(result["review"]["findings"]) == 1
    assert not result["audit"]["complete"]
    assert not result["audit"]["followup_performed"]


def test_invalid_checklist_does_not_erase_valid_findings_between_rounds(tmp_path):
    project, candidate = setup_audit(tmp_path)

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                arguments = with_audit_records(messages, submission(2))
                arguments["function_reviews"][0]["verdict"] = "invalid-status"
                return respond(arguments)
            if self.calls == 2:
                assert "允许值" in json.loads(messages[-1]["content"])["error"]
            arguments = with_audit_records(messages, submission(4))
            arguments["function_reviews"][0]["verdict"] = "issue_found"
            return respond(arguments)

    result = review(project, candidate, Client())
    assert result["status"] == "completed"
    assert {f["line"] for f in result["review"]["findings"]} == {2, 4}
    assert result["trace"][0]["arguments"]["function_reviews"][0]["verdict"] == "invalid-status"


def test_no_issue_units_get_one_additional_check_and_never_require_fake_findings(tmp_path):
    project, candidate = setup_audit(tmp_path, "def okay():\n    return 1\n")
    candidate["diagnostics"] = []

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 2:
                assert json.loads(messages[-1]["content"])["recheck_functions"]
            return respond(with_audit_records(messages, submission()))

    client = Client()
    result = review(project, candidate, client)
    assert result["status"] == "completed" and client.calls == 2
    assert not result["review"]["findings"]
    assert result["audit"]["complete"]


def test_followup_can_withdraw_a_false_positive_with_a_reason(tmp_path):
    project, candidate = setup_audit(tmp_path, "def first(value):\n    return value + 1\n")
    candidate["diagnostics"] = []

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return respond(with_audit_records(messages, submission(2)))
            feedback = json.loads(messages[-1]["content"])
            assert feedback["verify_findings"][0]["finding_id"] == "F1"
            arguments = with_audit_records(messages, submission())
            arguments["withdraw_findings"] = [{"finding_id": "F1", "reason": "正常数值输入没有类型错误，先前假定错误参数缺少依据"}]
            return respond(arguments)

    result = review(project, candidate, Client())
    assert result["status"] == "completed" and result["audit"]["complete"]
    assert not result["review"]["findings"]
    assert result["review"]["withdrawn_findings"][0]["finding_id"] == "F1"
    assert result["audit"]["withdrawn_findings"][0]["reason"]


def test_withdrawal_cannot_reference_an_unknown_finding(tmp_path):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    with pytest.raises(ValueError, match="撤回需要已有"):
        tracker.accept({**submission(), "evidence_ids": ["E1"]},
                       {"withdraw_findings": [{"finding_id": "F999", "reason": "测试"}]}, context)


def test_incremental_issue_verdict_can_refer_to_already_saved_findings(tmp_path):
    project, candidate = setup_audit(tmp_path)

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return respond(with_audit_records(messages, submission(2)))
            arguments = with_audit_records(messages, submission())
            arguments["verdict"] = "likely_issue"
            arguments["function_reviews"][0]["verdict"] = "issue_found"
            return respond(arguments)

    result = review(project, candidate, Client())
    assert result["status"] == "completed"
    assert len(result["review"]["findings"]) == 1
    assert result["trace"][1]["arguments"]["verdict"] == "likely_issue"
    assert result["trace"][1]["arguments"]["findings"] == []


def test_hallucinated_diagnostic_records_cannot_create_targets_when_there_are_none(tmp_path):
    project, candidate = setup_audit(tmp_path, "def okay():\n    return 1\n")
    candidate["diagnostics"] = []

    class Client:
        def chat(self, messages, tools):
            arguments = with_audit_records(messages, submission())
            arguments["diagnostic_reviews"] = [{"diagnostic_id": "D999", "verdict": "likely_issue", "reason": "测试误填"}]
            return respond(arguments)

    result = review(project, candidate, Client())
    assert result["status"] == "completed" and not result["review"]["findings"]
    assert result["audit"]["diagnostic_total"] == 0
    assert result["audit"]["diagnostic_reviews"] == []
    assert result["audit"]["ignored_records"]
    assert result["trace"][0]["arguments"]["diagnostic_reviews"][0]["diagnostic_id"] == "D999"


def test_hallucinated_function_records_cannot_create_targets_in_import_only_file(tmp_path):
    project, candidate = setup_audit(tmp_path, "import math\n")
    candidate["diagnostics"] = []

    class Client:
        def chat(self, messages, tools):
            arguments = with_audit_records(messages, submission())
            arguments["function_reviews"] = [{"function_id": "invented-function", "verdict": "issue_found", "reason": "测试误填"}]
            return respond(arguments)

    result = review(project, candidate, Client())
    assert result["status"] == "completed" and not result["review"]["findings"]
    assert result["audit"]["function_total"] == 0
    assert result["audit"]["function_reviews"] == []
    assert result["audit"]["ignored_records"]


def test_known_finding_metadata_can_be_echoed_without_losing_source_validation(tmp_path):
    project, candidate = setup_audit(tmp_path)

    class Client:
        calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            arguments = with_audit_records(messages, submission(2))
            if self.calls > 1:
                arguments["findings"][0].update(finding_id="F1", evidence_origin="source_location")
            return respond(arguments)

    result = review(project, candidate, Client())
    assert result["status"] == "completed"
    assert len(result["review"]["findings"]) == 1
    assert result["trace"][1]["arguments"]["findings"][0]["finding_id"] == "F1"


def test_ollama_uses_constrained_json_only_for_complete_file_submission(tmp_path, monkeypatch):
    project, candidate = setup_audit(tmp_path)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    tracker = AuditTracker(project, candidate)
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"message": {"content": json.dumps(submission())}, "eval_count": 12,
                               "prompt_eval_count": 100, "done_reason": "stop"}).encode()

    def request(req, timeout):
        captured.append(json.loads(req.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", request)
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]
    original = json.loads(json.dumps(messages))
    client = Ollama("qwen3:8b")
    output = client.chat(messages, review_tools(context, "adaptive", True, tracker))
    assert captured[0]["format"]["additionalProperties"] is False
    assert "function_reviews" in captured[0]["format"]["required"]
    assert "tools" not in captured[0] and captured[0]["think"] is False
    assert output["tool_calls"][0]["function"]["arguments"] == submission()
    assert output["_transport"]["mode"] == "json_schema"
    assert client.usage["structured_requests"] == 1
    assert messages == original

    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 1})
    client.chat(messages, review_tools(context, "adaptive", True, tracker))
    assert "tools" in captured[1] and "format" not in captured[1]
    assert "think" not in captured[1]


@pytest.mark.parametrize("change", [
    {"diagnostic_id": "D999"}, {"verdict": "confirmed"}, {"reason": ""},
])
def test_bad_diagnostic_dispositions_are_atomic(tmp_path, change):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    arguments = {"diagnostic_reviews": [{"diagnostic_id": "D1", "verdict": "inconclusive",
                                         "reason": "未确认", **change}]}
    with pytest.raises(ValueError):
        tracker.accept({**submission(), "evidence_ids": ["E1"]}, arguments, context)
    assert tracker.conclusion is None and not tracker.diagnostic_reviews


def test_unread_function_cannot_be_marked_checked(tmp_path):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 1})
    arguments = {"function_reviews": [{"function_id": next(iter(tracker.units)),
                                       "verdict": "no_issue_found", "reason": "未见问题"}]}
    with pytest.raises(ValueError, match="尚未完整提供"):
        tracker.accept({**submission(), "evidence_ids": ["E1"]}, arguments, context)


def test_unsupported_issue_found_record_preserves_uncertainty(tmp_path):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    arguments = {"function_reviews": [{"function_id": next(iter(tracker.units)),
                                       "verdict": "issue_found", "reason": "存在问题"}]}
    tracker.accept({**submission(), "evidence_ids": ["E1"]}, arguments, context)
    row = next(iter(tracker.function_reviews.values()))
    assert row["verdict"] == "inconclusive" and row["record_origin"] == "program_consistency_check"


def test_existing_finding_cannot_be_hidden_by_a_no_issue_disposition(tmp_path):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    arguments = {"function_reviews": [{"function_id": next(iter(tracker.units)),
                                       "verdict": "no_issue_found", "reason": "未发现问题"}]}
    tracker.accept({**submission(2), "evidence_ids": ["E1"]}, arguments, context)
    assert tracker.conclusion["findings"]
    row = next(iter(tracker.function_reviews.values()))
    assert row["verdict"] == "inconclusive" and row["record_origin"] == "program_consistency_check"


def test_function_issue_record_can_reference_an_explicitly_supported_diagnostic(tmp_path):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    arguments = {"function_reviews": [{"function_id": next(iter(tracker.units)), "verdict": "issue_found", "reason": "认可函数中的诊断"}],
                 "diagnostic_reviews": [{"diagnostic_id": "D1", "verdict": "likely_issue", "reason": "未定义名称"}]}
    tracker.accept({**submission(), "evidence_ids": ["E1"]}, arguments, context)
    row = next(iter(tracker.function_reviews.values()))
    assert row["verdict"] == "issue_found" and row["basis_diagnostics"] == ["D1"]


def test_pyright_detects_string_numeric_operator_without_annotations_or_target_config(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("def describe(values):\n    average = sum(values) / len(values)\n    return 'mean=' + average\n")
    (tmp_path / "pyrightconfig.json").write_text(json.dumps({"exclude": ["**"], "reportOperatorIssue": "none"}))
    result = scan_pyright(Project(path), 30)
    assert result["status"] == "completed", result["notes"]
    assert any(d["rule"] == "reportOperatorIssue" and d["line"] == 3 for d in result["diagnostics"])
    assert not result["configuration"]["target_config_loaded"]


def test_pyright_reports_attribute_errors_for_known_list_types_without_execution(tmp_path):
    marker = tmp_path / "executed"
    path = tmp_path / "sample.py"
    path.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
                    "def append(values: list[int]):\n    values.push(1)\n")
    result = scan_pyright(Project(path), 30)
    assert result["status"] == "completed"
    assert any(d["rule"] == "reportAttributeAccessIssue" for d in result["diagnostics"])
    assert not marker.exists()


def test_low_bandit_findings_retain_security_warning_and_native_level(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("def secret(user):\n    assert user.is_admin\n    return 'private'\n")
    result = scan(Project(path))
    finding = next(d for d in result["diagnostics"] if d["rule"] == "B101")
    assert finding["severity"] == "warning" and finding["native_severity"] == "LOW"


def test_missing_type_checker_marks_scan_partial(tmp_path, monkeypatch):
    from evidence_review import scanner
    path = tmp_path / "sample.py"
    path.write_text("x = missing\n")
    original = scanner.find_tool
    monkeypatch.setattr(scanner, "find_tool", lambda name: None if name == "node" else original(name))
    result = scan(Project(path))
    assert result["status"] == "partial"
    assert next(t for t in result["tools"] if t["tool"] == "pyright")["status"] == "unavailable"


@pytest.mark.parametrize("new_verdict", ["likely_false_positive", "inconclusive"])
@pytest.mark.parametrize("max_rounds", [2, 3])
def test_regression_diagnostic_revision_reopens_stale_function(tmp_path, new_verdict, max_rounds):
    project, candidate = setup_audit(tmp_path)

    class Client:
        calls = 0
        first = None

        def chat(self, messages, tools):
            self.calls += 1
            plan = json.loads(messages[1]["content"])["audit_plan"]
            self.first, second = [unit["function_id"] for unit in plan["functions"]]
            args = submission()
            if self.calls == 1:
                args["diagnostic_reviews"] = [{"diagnostic_id": "D1", "verdict": "likely_issue", "reason": "认可诊断"}]
                args["function_reviews"] = [
                    {"function_id": self.first, "verdict": "issue_found", "reason": "依据 D1"},
                    {"function_id": second, "verdict": "no_issue_found", "reason": "暂未发现"}]
            elif self.calls == 2:
                args["diagnostic_reviews"] = [{"diagnostic_id": "D1", "verdict": new_verdict, "reason": "修订意见"}]
                args["function_reviews"] = [{"function_id": second, "verdict": "no_issue_found", "reason": "完成复查"}]
            else:
                assert self.first in json.loads(messages[-1]["content"])["pending"]["functions"]
                args["diagnostic_reviews"] = []
                args["function_reviews"] = [{"function_id": self.first, "verdict": "inconclusive", "reason": "旧依据不再成立"}]
            return respond(args)

    client = Client()
    result = review(project, candidate, client, max_rounds=max_rounds)
    assert client.calls == max_rounds
    assert result["trace"][1]["response"]["complete"] is False
    if max_rounds == 2:
        assert result["status"] == "budget_exhausted"
        assert client.first in result["audit"]["pending"]["functions"]
        assert not result["audit"]["complete"]
        return
    assert result["status"] == "completed"
    row = next(row for row in result["audit"]["function_reviews"] if row["function_id"] == client.first)
    assert row["verdict"] == "inconclusive" and "basis_diagnostics" not in row


def test_regression_stale_function_at_round_limit_remains_pending(tmp_path):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    first, second = tracker.units
    tracker.accept({**submission(), "evidence_ids": ["E1"]}, {
        "diagnostic_reviews": [{"diagnostic_id": "D1", "verdict": "likely_issue", "reason": "认可"}],
        "function_reviews": [{"function_id": first, "verdict": "issue_found", "reason": "依据诊断"},
                             {"function_id": second, "verdict": "no_issue_found", "reason": "暂未发现"}]}, context)
    tracker.begin_followup()
    tracker.accept({**submission(), "evidence_ids": ["E1"]}, {
        "diagnostic_reviews": [{"diagnostic_id": "D1", "verdict": "likely_false_positive", "reason": "修订"}],
        "function_reviews": [{"function_id": second, "verdict": "no_issue_found", "reason": "复查"}]}, context)
    assert first in tracker.report()["pending"]["functions"]
    assert tracker.report()["complete"] is False


@pytest.mark.parametrize("independent_finding", [False, True])
def test_regression_diagnostic_revision_preserves_remaining_support(tmp_path, independent_finding):
    project, candidate = setup_audit(tmp_path)
    candidate["diagnostics"].append({"id": "D2", "file": "sample.py", "line": 2, "rule": "test"})
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    first = next(iter(tracker.units))
    tracker.accept({**submission(2 if independent_finding else None), "evidence_ids": ["E1"]}, {
        "diagnostic_reviews": [{"diagnostic_id": d, "verdict": "likely_issue", "reason": "认可"} for d in ("D1", "D2")],
        "function_reviews": [{"function_id": first, "verdict": "issue_found", "reason": "发现问题"}]}, context)
    tracker.accept({**submission(), "evidence_ids": ["E1"]}, {
        "diagnostic_reviews": [{"diagnostic_id": "D1", "verdict": "likely_false_positive", "reason": "修订"}]}, context)
    row = tracker.function_reviews[first]
    assert row["verdict"] == "issue_found"
    assert row.get("basis_diagnostics", []) == ([] if independent_finding else ["D2"])


def test_regression_new_finding_reopens_old_no_issue_record(tmp_path):
    project, candidate = setup_audit(tmp_path)
    tracker = AuditTracker(project, candidate)
    context = ContextTools(project, 10000)
    context.call("read_lines", {"file": "sample.py", "start": 1, "end": 4})
    first = next(iter(tracker.units))
    tracker.accept({**submission(), "evidence_ids": ["E1"]}, {
        "function_reviews": [{"function_id": first, "verdict": "no_issue_found", "reason": "暂未发现"}]}, context)
    tracker.accept({**submission(2), "evidence_ids": ["E1"]}, {}, context)
    assert first in tracker.pending()["functions"]
    assert tracker.visible_review()["findings"]
