from pathlib import Path
import json

import pytest

from evidence_review.project import Project


def with_audit_records(messages, arguments):
    """Scripted test clients explicitly implement the file-audit protocol."""
    plan = json.loads(messages[1]["content"])["audit_plan"]
    arguments["diagnostic_reviews"] = [{"diagnostic_id": d["diagnostic_id"],
                                        "verdict": "inconclusive", "reason": "测试客户端未作语义判断"}
                                       for d in plan["diagnostics"]]
    arguments["function_reviews"] = [{"function_id": f["function_id"],
                                      "verdict": "issue_found" if any(
                                          any(a <= finding["line"] <= b for a, b in f["ranges"])
                                          for finding in arguments["findings"]) else "no_issue_found",
                                      "reason": "测试客户端的处理记录"}
                                     for f in plan["functions"]]
    return arguments


@pytest.fixture
def project(tmp_path):
    (tmp_path / "a.c").write_text(
        'static int helper(int x) { return x + 1; }\n'
        'int target(int n) {\n'
        '    return helper(n);\n'
        '}\n', encoding="utf-8")
    (tmp_path / "b.c").write_text(
        'int target(int);\n'
        'static int helper(int x) { return x - 1; }\n'
        'int caller(void) { return target(3) + helper(2); }\n', encoding="utf-8")
    return Project(tmp_path)


@pytest.fixture
def demo_project():
    return Project(Path(__file__).resolve().parents[1] / "src/evidence_review/demo_project")
