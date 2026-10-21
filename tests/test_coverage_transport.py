import io
import json
import subprocess
from pathlib import Path

from evidence_review import coverage
from evidence_review.agent import Ollama
from evidence_review.sbfl import load_spectra, score_spectra


def test_collector_uses_separate_profiles_and_real_oracle(demo_project, tmp_path, monkeypatch):
    """Exercise collector orchestration with instrumented-tool stand-ins, not a real LLVM run."""
    monkeypatch.setattr(coverage, "resolve_tool", lambda name: name)
    commands = []
    profiles = []

    def checked(command, cwd, timeout=60, env=None):
        commands.append(command)
        if "--version" in command:
            return "test stand-in 1.0\n"
        if command[0] == "llvm-cov":
            return f"SF:{demo_project.root}/fees.c\nDA:5,1\nDA:8,1\nend_of_record\n"
        return ""

    def run(command, **kwargs):
        profile = Path(kwargs["env"]["LLVM_PROFILE_FILE"])
        profiles.append(str(profile))
        profile.write_text("test profile")
        amount = command[-1]
        return subprocess.CompletedProcess(command, 0, "0\n" if amount == "101" else "10\n", "")

    monkeypatch.setattr(coverage, "checked", checked)
    monkeypatch.setattr(coverage.subprocess, "run", run)
    result = coverage.collect(demo_project, demo_project.root / "tests.json")
    assert [t["outcome"] for t in result["tests"]] == ["pass", "pass", "fail"]
    assert len(set(profiles)) == 3
    assert len([c for c in commands if c[0] == "llvm-profdata"]) == 4  # version + 3 merges
    path = tmp_path / "spectra.json"
    path.write_text(json.dumps(result))
    loaded = load_spectra(path, demo_project)
    assert score_spectra(loaded)["failed"] == 1


def test_missing_profile_is_error_not_test_failure(demo_project, monkeypatch):
    monkeypatch.setattr(coverage, "resolve_tool", lambda name: name)
    monkeypatch.setattr(coverage, "checked", lambda *args, **kwargs: "test stand-in\n")
    monkeypatch.setattr(coverage.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, -11, "", "crash"))
    result = coverage.collect(demo_project, demo_project.root / "tests.json")
    assert all(t["outcome"] == "error" for t in result["tests"])
    assert score_spectra(result)["failed"] == 0


def test_ollama_request_and_usage(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data)
        return io.BytesIO(json.dumps({"message": {"role": "assistant", "tool_calls": []},
                                      "prompt_eval_count": 100, "eval_count": 12}).encode())

    monkeypatch.setattr("evidence_review.agent.urllib.request.urlopen", urlopen)
    client = Ollama("test-model")
    client.chat([{"role": "user", "content": "test"}], [])
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["payload"]["stream"] is False
    assert client.usage == {"requests": 1, "prompt_tokens": 100, "completion_tokens": 12, "structured_requests": 0}
