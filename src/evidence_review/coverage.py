from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .project import Project


def resolve_tool(name: str):
    found = shutil.which(name)
    if found:
        return found
    if shutil.which("xcrun"):
        result = subprocess.run(["xcrun", "--find", name], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    raise ValueError(f"缺少 {name}；请安装包含 clang、llvm-profdata 和 llvm-cov 的 LLVM 工具链")


def checked(command: list[str], cwd: Path, timeout=60, env=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            timeout=timeout, env=env, errors="replace")
    if result.returncode:
        raise ValueError(f"命令失败 ({Path(command[0]).name}): {(result.stderr or result.stdout)[:2000]}")
    return result.stdout


def parse_lcov(text: str, project: Project):
    coverage: dict[str, set[int]] = {}
    file = None
    for line in text.splitlines():
        if line.startswith("SF:"):
            try:
                name = project.safe_path(line[3:]).relative_to(project.root).as_posix()
                file = name if name in project.sources else None
            except ValueError:
                file = None
        elif line.startswith("DA:") and file:
            number, count, *_ = line[3:].split(",")
            if int(count) > 0:
                coverage.setdefault(file, set()).add(int(number))
        elif line == "end_of_record":
            file = None
    return {file: sorted(lines) for file, lines in sorted(coverage.items())}


def collect(project: Project, manifest_path: Path, timeout: int = 10):
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError("测试清单必须是 JSON 对象")
    sources = manifest.get("sources", [])
    tests = manifest.get("tests", [])
    if not sources or not isinstance(sources, list) or any(not isinstance(s, str) or s not in project.sources or not s.endswith(".c") for s in sources):
        raise ValueError("manifest.sources 必须列出本次导入的 .c 文件")
    if not tests or not isinstance(tests, list):
        raise ValueError("manifest.tests 必须是非空测试数组")
    names = set()
    for test in tests:
        if not isinstance(test, dict):
            raise ValueError("tests 中的每个测试必须是对象")
        if not isinstance(test.get("name"), str) or test["name"] in names:
            raise ValueError("测试名称必须唯一")
        names.add(test["name"])
        if not isinstance(test.get("args", []), list) or any(not isinstance(x, str) for x in test.get("args", [])):
            raise ValueError("测试 args 必须是字符串数组")
        if not isinstance(test.get("expected_stdout"), str) or type(test.get("expected_exit", 0)) is not int:
            raise ValueError("每个测试需要 expected_stdout 字符串和可选 expected_exit 整数")
    clang, profdata, cov = (resolve_tool(t) for t in ("clang", "llvm-profdata", "llvm-cov"))
    versions = {name: checked([exe, "--version"], project.root).splitlines()[0]
                for name, exe in (("clang", clang), ("llvm-profdata", profdata), ("llvm-cov", cov))}
    result = {"schema_version": 1, "origin": "llvm-per-test-execution", "source_hashes": project.hashes(),
              "tool_versions": versions, "tests": []}
    with tempfile.TemporaryDirectory(prefix="evidence-coverage-") as temporary:
        work = Path(temporary)
        binary = work / "program"
        compile_command = [clang, "-std=c11", "-O0", "-g", "-fprofile-instr-generate", "-fcoverage-mapping",
                           "-I", str(project.root), *(str(project.safe_path(s)) for s in sources), "-o", str(binary)]
        checked(compile_command, project.root)
        result["compile_command"] = compile_command
        for index, test in enumerate(tests):
            raw = work / f"test-{index}.profraw"
            merged = work / f"test-{index}.profdata"
            row = {"name": test["name"], "outcome": "error", "covered": {}, "args": test.get("args", []),
                   "expected_stdout": test["expected_stdout"], "expected_exit": test.get("expected_exit", 0)}
            try:
                # The explicit collect command executes user-supplied C locally, not in a sandbox.
                execution = subprocess.run([str(binary), *row["args"]], cwd=work,
                                           env={**os.environ, "LLVM_PROFILE_FILE": str(raw)},
                                           capture_output=True, text=True, errors="replace", timeout=timeout)
                row.update(stdout=execution.stdout[:8000], stderr=execution.stderr[:8000], exit_code=execution.returncode)
                if not raw.exists():
                    raise ValueError("没有生成覆盖文件（例如进程崩溃）；此测试不计入 SBFL")
                checked([profdata, "merge", "-sparse", str(raw), "-o", str(merged)], project.root)
                exported = checked([cov, "export", str(binary), f"-instr-profile={merged}", "-format=lcov"], project.root)
                row["covered"] = parse_lcov(exported, project)
                if not row["covered"]:
                    raise ValueError("未采集到导入源码的覆盖信息；此测试不计入 SBFL")
                row["outcome"] = "pass" if (execution.returncode == row["expected_exit"]
                                             and execution.stdout == row["expected_stdout"]) else "fail"
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                row["error"] = str(exc)
            result["tests"].append(row)
    project.check_unchanged()
    return result
