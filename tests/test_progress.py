import importlib.util
import io
import json
from pathlib import Path
from threading import Event

import pytest
from conftest import with_audit_records

from evidence_review.agent import Ollama, review
from evidence_review.sbfl import file_candidates


def load_suite():
    path = Path(__file__).resolve().parents[1] / "test" / "run_suite.py"
    spec = importlib.util.spec_from_file_location("fixture_suite", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_progress_is_visible_while_model_is_still_waiting(project, monkeypatch):
    monkeypatch.setattr("evidence_review.progress.WAIT_INTERVAL", 0.01)
    waiting = Event()
    messages = []

    def progress(message):
        messages.append(message)
        if "已等待" in message:
            waiting.set()

    class SlowClient:
        def chat(self, messages, tools):
            assert waiting.wait(2), "No progress appeared while the request was blocked"
            return {"tool_calls": [{"function": {"name": "submit_review", "arguments": with_audit_records(messages, {
                "verdict": "inconclusive", "summary": "测试客户端", "evidence_ids": ["E2"],
                "missing_evidence": ["运行结果"], "suggestion": "人工复核", "findings": []})}}]}

    result = review(project, file_candidates(project, [])[0], SlowClient(), progress=progress)
    assert result["status"] == "completed"
    assert any("第 1/6 轮" in message for message in messages)
    assert any("收到响应" in message for message in messages)
    assert "结论已接受" in messages[-1]


def test_timeout_has_an_actionable_message(monkeypatch):
    def timeout(request, timeout):
        raise TimeoutError("test timeout")

    monkeypatch.setattr("urllib.request.urlopen", timeout)
    with pytest.raises(ValueError, match="超过 7 秒"):
        Ollama("test-client", timeout=7).chat([], [])


def test_live_log_flushes_before_the_analysis_finishes(tmp_path):
    suite = load_suite()
    console = io.StringIO()
    path = tmp_path / "run.log"
    with path.open("w") as log:
        live = suite.LiveLog(console, log)
        print("request still running", file=live)
        assert path.read_text() == console.getvalue() == "request still running\n"


def test_suite_preserves_live_log_on_keyboard_interrupt(tmp_path, monkeypatch, capsys):
    suite = load_suite()

    def interrupted(command):
        output = Path(command[command.index("--output") + 1])
        log = output / "run.log"
        assert "开始分析" in log.read_text()
        print("模型正在等待", flush=True)
        assert "模型正在等待" in log.read_text()
        assert command[command.index("--model-timeout") + 1] == "7"
        raise KeyboardInterrupt

    monkeypatch.setattr(suite, "analyze", interrupted)
    result = suite.main(["--language", "python", "--group", "defects", "--model", "test-client",
                         "--model-timeout", "7", "--output", str(tmp_path)])
    assert result == 130
    log = tmp_path / "python" / "defects" / "run.log"
    assert "分析已中断" in log.read_text()
    assert "模型正在等待" in capsys.readouterr().out


def test_cli_timeout_is_passed_to_model_client(project, tmp_path, monkeypatch):
    from evidence_review.cli import main

    class Client:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}

        def __init__(self, model, url, timeout):
            assert timeout == 7

        def chat(self, messages, tools):
            return {"tool_calls": [{"function": {"name": "submit_review", "arguments": with_audit_records(messages, {
                "verdict": "inconclusive", "summary": "测试客户端", "evidence_ids": ["E2"],
                "missing_evidence": [], "findings": []})}}]}

    monkeypatch.setattr("evidence_review.cli.Ollama", Client)
    assert main(["analyze", str(project.root / "a.c"), "--model", "test-client",
                 "--review-scope", "files", "--model-timeout", "7", "--output", str(tmp_path)]) == 0


def test_suite_flags_model_findings_in_clean_control(tmp_path, monkeypatch):
    suite = load_suite()

    def analyzer(command):
        output = Path(command[command.index("--output") + 1])
        (output / "report.json").write_text(json.dumps({
            "scanner": {"status": "completed", "diagnostics": [], "tools": []},
            "summary": {"model_findings": 1}, "reviews": [{"status": "completed"}], "index_warnings": [],
        }))
        return 0

    monkeypatch.setattr(suite, "analyze", analyzer)
    assert suite.main(["--language", "python", "--group", "clean", "--model", "test-client", "--output", str(tmp_path)]) == 1
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["control_cases_with_diagnostics"] == 0
    assert summary["control_cases_with_model_findings"] == 1
