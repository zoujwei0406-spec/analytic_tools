import json

import pytest

from evidence_review.cli import main
from evidence_review.scanner import find_cppcheck, scan


@pytest.mark.skipif(not find_cppcheck(), reason="需要安装 cppcheck")
def test_real_scanner_finds_example_overflow(demo_project):
    result = scan(demo_project)
    assert result["status"] == "completed", result
    assert any(d["file"] == "memory.c" and d["rule"] == "arrayIndexOutOfBounds" for d in result["diagnostics"])


@pytest.mark.skipif(not find_cppcheck(), reason="需要安装 cppcheck")
def test_cli_demo_outputs_honest_report(tmp_path):
    assert main(["demo", "--output", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["scanner"]["status"] == "completed"
    assert report["sbfl"]["origin"] == "synthetic-demo"
    assert report["model"] is None
    assert all(r["status"] == "not_run" for r in report["reviews"])
    html = (tmp_path / "report.html").read_text()
    assert "人工构造" in html
    assert "没有静态诊断" not in html


def test_invalid_spectra_returns_actionable_error(demo_project, tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text('{"schema_version":1,"tests":[],"source_hashes":{}}')
    assert main(["analyze", str(demo_project.root), "--spectra", str(path), "--output", str(tmp_path)]) == 2
    assert "哈希" in capsys.readouterr().err
