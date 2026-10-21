from __future__ import annotations

import json
import math
from pathlib import Path

from .project import Project


def load_spectra(path: Path, project: Project):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("tests"), list):
        raise ValueError("覆盖文件必须使用 schema_version=1 并包含 tests 数组")
    if data.get("source_hashes") != project.hashes():
        raise ValueError("覆盖数据的源码哈希与本次导入文件不一致，请重新采集覆盖数据")
    seen = set()
    for test in data["tests"]:
        if not isinstance(test, dict):
            raise ValueError("tests 中的每个测试必须是对象")
        if not isinstance(test.get("name"), str) or test["name"] in seen:
            raise ValueError("测试名称必须是唯一字符串")
        seen.add(test["name"])
        if test.get("outcome") not in {"pass", "fail", "error"}:
            raise ValueError("outcome 必须是 pass、fail 或 error")
        if not isinstance(test.get("covered", {}), dict):
            raise ValueError("covered 必须是文件到行号数组的映射")
        for file, lines in test.get("covered", {}).items():
            if file not in project.sources or not isinstance(lines, list):
                raise ValueError(f"覆盖数据包含未知源码或无效行号数组: {file}")
            count = len(project.sources[file].splitlines())
            if any(type(line) is not int or line < 1 or line > count for line in lines):
                raise ValueError(f"覆盖行号超出源码范围: {file}")
    return data


def score_spectra(data: dict | None):
    if data is None:
        return {"status": "not_provided", "lines": [], "passed": 0, "failed": 0, "errors": 0}
    tests = data["tests"]
    failed = sum(t["outcome"] == "fail" for t in tests)
    passed = sum(t["outcome"] == "pass" for t in tests)
    result = {"status": "completed" if failed else "no_failing_tests", "lines": [],
              "failed": failed, "passed": passed,
              "errors": sum(t["outcome"] == "error" for t in tests),
              "origin": data.get("origin", "unspecified")}
    if not failed:
        return result
    counts: dict[tuple[str, int], list[int]] = {}
    for test in tests:
        if test["outcome"] == "error":
            continue
        for file, lines in test.get("covered", {}).items():
            for line in set(lines):
                values = counts.setdefault((file, line), [0, 0])
                values[0 if test["outcome"] == "fail" else 1] += 1
    for (file, line), (ef, ep) in counts.items():
        if ef:
            result["lines"].append({"file": file, "line": line, "failed_covered": ef,
                                    "passed_covered": ep, "score": ef / math.sqrt(failed * (ef + ep))})
    result["lines"].sort(key=lambda row: (-row["score"], row["file"], row["line"]))
    return result


def rank_candidates(project: Project, diagnostics: list[dict], sbfl: dict):
    candidates = {}

    def get(file, line):
        function = project.at(file, line)
        key = function.id if function else f"{file}:{line}"
        if key not in candidates:
            candidates[key] = {"id": key, "function_id": function.id if function else None,
                               "file": file, "line": line, "language": project.languages[file], "name": function.name if function else "<文件级>",
                               "diagnostics": [], "sbfl_lines": [], "sbfl_score": 0.0}
        return candidates[key]

    for diagnostic in diagnostics:
        get(diagnostic["file"], diagnostic["line"])["diagnostics"].append(diagnostic)
    for line in sbfl["lines"]:
        candidate = get(line["file"], line["line"])
        candidate["sbfl_lines"].append(line)
        candidate["sbfl_score"] = max(candidate["sbfl_score"], line["score"])
    severity = {"error": 4, "warning": 3, "performance": 2, "portability": 1, "style": 1}
    static = sorted((c for c in candidates.values() if c["diagnostics"]), key=lambda c: (
        -max(severity.get(d["severity"], 0) for d in c["diagnostics"]), c["id"]))
    dynamic = sorted((c for c in candidates.values() if c["sbfl_score"] > 0),
                     key=lambda c: (-c["sbfl_score"], c["id"]))
    # Reciprocal rank fusion combines ranks, not incomparable probabilities.
    # Ties share a rank so alphabetical order cannot change the fusion score.
    for candidate in candidates.values():
        candidate["priority"] = 0.0
    for channel, ordered in (("static_rank", static), ("sbfl_rank", dynamic)):
        last_value, rank = None, 0
        for position, candidate in enumerate(ordered, 1):
            value = (max(severity.get(d["severity"], 0) for d in candidate["diagnostics"])
                     if channel == "static_rank" else candidate["sbfl_score"])
            if value != last_value:
                rank = position
                last_value = value
            candidate[channel] = rank
            candidate["priority"] += 1 / (60 + rank)
    return sorted(candidates.values(), key=lambda c: (-c["priority"], c["id"]))


def file_candidates(project: Project, candidates: list[dict]):
    """Include every imported file, even when scanners emitted no diagnostics."""
    files = {file: {"id": f"{file}:file-audit", "function_id": None,
                    "file": file, "line": 1, "language": project.languages[file],
                    "name": "<整文件审查>", "scope": "file", "diagnostics": [],
                    "sbfl_lines": [], "sbfl_score": 0.0, "priority": 0.0}
             for file in project.sources}
    for candidate in candidates:
        item = files[candidate["file"]]
        item["diagnostics"].extend(candidate["diagnostics"])
        item["sbfl_lines"].extend(candidate["sbfl_lines"])
        item["sbfl_score"] = max(item["sbfl_score"], candidate["sbfl_score"])
        item["priority"] = max(item["priority"], candidate["priority"])
    return sorted(files.values(), key=lambda c: (-c["priority"], c["id"]))
