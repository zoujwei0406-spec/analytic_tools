from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .project import Project


def find_tool(name):
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() else shutil.which(name)


def find_cppcheck():
    return find_tool("cppcheck")


def parse_xml(text: str, project: Project):
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<results")
    if start < 0:
        raise ValueError("Cppcheck 未返回 XML 结果")
    root = ET.fromstring(text[start:])
    findings, notes = [], []
    for error in root.findall("./errors/error"):
        locations = []
        for location in error.findall("location"):
            raw = location.get("file", "")
            try:
                path = project.safe_path(raw).relative_to(project.root).as_posix()
            except ValueError:
                continue
            line = int(location.get("line", "0"))
            if path in project.sources and 1 <= line <= len(project.sources[path].splitlines()):
                locations.append({"file": path, "line": line, "info": location.get("info", "")})
        rule = error.get("id", "unknown")
        if not locations or error.get("severity") == "information":
            notes.append(f"{rule}: {error.get('msg', '')}")
            continue
        findings.append({
            "id": f"D{len(findings) + 1}", "tool": "cppcheck", "rule": rule,
            "severity": error.get("severity", "warning"),
            "message": error.get("msg", ""), "detail": error.get("verbose", ""),
            "inconclusive": error.get("inconclusive") == "true", "locations": locations,
            "file": locations[0]["file"], "line": locations[0]["line"],
        })
    return findings, notes


def scan_cppcheck(project: Project, compile_commands: str | None = None, timeout: int = 120):
    executable = find_cppcheck()
    if not executable:
        return {"status": "unavailable", "diagnostics": [], "notes": ["未找到 cppcheck"], "version": None}
    command = [executable, "--xml", "--xml-version=2", "--enable=warning,style,performance,portability", "--inconclusive"]
    if compile_commands:
        database = project.safe_path(compile_commands)
        if not database.is_file():
            raise ValueError(f"编译数据库不存在: {compile_commands}")
        command.append(f"--project={database}")
    else:
        command.extend(str(project.safe_path(name)) for name in project.sources if project.languages[name] in {"c", "cpp"})
    try:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                 timeout=10, check=True).stdout.strip()
        result = subprocess.run(command, cwd=project.root, capture_output=True, text=True,
                                timeout=timeout, errors="replace")
        if result.returncode != 0:
            raise ValueError(f"Cppcheck 退出码 {result.returncode}: {result.stderr[:1000]}")
        findings, notes = parse_xml(result.stderr, project)
        return {"status": "completed", "version": version, "command": command,
                "diagnostics": findings, "notes": notes}
    except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError) as exc:
        return {"status": "error", "version": None, "diagnostics": [], "notes": [str(exc)]}


def diagnostic(project, tool, file, line, rule, severity, message, **extra):
    try:
        file = project.safe_path(file).relative_to(project.root).as_posix()
    except ValueError:
        return None
    if file not in project.sources or not isinstance(line, int) or not 1 <= line <= len(project.sources[file].splitlines()):
        return None
    return {"tool": tool, "rule": rule, "severity": severity, "message": message,
            "file": file, "line": line, "inconclusive": False,
            "locations": [{"file": file, "line": line, "info": ""}], **extra}


def scan_python(project, tool, timeout):
    executable = find_tool(tool)
    if not executable:
        return {"status": "unavailable", "version": None, "diagnostics": [], "notes": [f"未找到 {tool}，请安装项目 Python 依赖"]}
    files = [str(project.safe_path(file)) for file, language in project.languages.items() if language == "python"]
    if tool == "bandit":
        command = [executable, "--quiet", "--ignore-nosec", "--format", "json", *files]
    else:
        command = [executable, "check", "--isolated", "--no-cache", "--ignore-noqa", "--output-format", "json",
                   "--select", "E9,F63,F7,F82", *files]
    try:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10, check=True).stdout.strip().splitlines()[0]
        result = subprocess.run(command, cwd=project.root, capture_output=True, text=True, timeout=timeout, errors="replace")
        if result.returncode not in {0, 1}:
            raise ValueError(f"{tool} 退出码 {result.returncode}: {(result.stderr or result.stdout)[:1000]}")
        data = json.loads(result.stdout)
        findings, notes, status = [], [], "completed"
        if tool == "bandit":
            for row in data.get("results", []):
                finding = diagnostic(project, tool, row["filename"], row["line_number"], row["test_id"],
                                     {"HIGH": "error", "MEDIUM": "warning", "LOW": "warning"}.get(row["issue_severity"], "warning"),
                                     row["issue_text"], native_severity=row["issue_severity"], confidence=row["issue_confidence"])
                if finding:
                    findings.append(finding)
            if data.get("errors"):
                status = "error"
                notes.extend(f"{e.get('filename')}: {e.get('reason')}" for e in data["errors"])
        else:
            for row in data:
                finding = diagnostic(project, tool, row["filename"], row["location"]["row"], row.get("code") or "invalid-syntax",
                                     "error", row["message"])
                if finding:
                    findings.append(finding)
        return {"status": status, "version": version, "command": command, "diagnostics": findings, "notes": notes}
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as exc:
        return {"status": "error", "version": None, "diagnostics": [], "notes": [str(exc)]}


def scan_pyright(project, timeout):
    node = find_tool("node")
    runner = Path(__file__).resolve().parents[2] / "node_modules/pyright/index.js"
    if not node or not runner.is_file():
        return {"status": "unavailable", "version": None, "diagnostics": [],
                "notes": ["Pyright 不可用；请安装 Node.js 并在工具目录运行 npm ci --ignore-scripts"]}
    try:
        # Analyze only imported snapshots with our own configuration. Do not load
        # the target project's pyrightconfig, plugins, or interpreter startup code.
        with tempfile.TemporaryDirectory(prefix="evidence-pyright-") as directory:
            root = Path(directory)
            files = []
            for file, source in project.sources.items():
                if project.languages[file] == "python":
                    snapshot = root / file
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.write_bytes(source)
                    files.append(file)
            config = root / "pyrightconfig.json"
            config.write_text(json.dumps({"include": files, "typeCheckingMode": "basic",
                                         "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}",
                                         "analyzeUnannotatedFunctions": True, "autoSearchPaths": False,
                                         "useLibraryCodeForTypes": False, "reportMissingImports": "none"}))
            command = [node, str(runner), "--outputjson", "--project", str(config)]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                    timeout=timeout, errors="replace")
            if result.returncode not in {0, 1}:
                raise ValueError(f"Pyright 退出码 {result.returncode}: {(result.stderr or result.stdout)[:1000]}")
            data = json.loads(result.stdout)
            findings = []
            for row in data["generalDiagnostics"]:
                relative = Path(row["file"]).relative_to(root).as_posix()
                finding = diagnostic(project, "pyright", relative, row["range"]["start"]["line"] + 1,
                                     row.get("rule") or "invalid-syntax", row["severity"], row["message"])
                if finding:
                    findings.append(finding)
            return {"status": "completed", "version": data["version"], "command": command,
                    "diagnostics": findings, "notes": [],
                    "configuration": {"mode": "basic", "source": "imported-snapshots",
                                      "target_config_loaded": False, "library_code_inference": False}}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return {"status": "error", "version": None, "diagnostics": [], "notes": [str(exc)]}


def scan_eslint(project, timeout):
    node = find_tool("node")
    if not node:
        return {"status": "unavailable", "version": None, "diagnostics": [], "notes": ["未找到 Node.js；请安装 Node.js 并在本工具目录运行 npm ci"]}
    command = [node, str(Path(__file__).with_name("eslint_runner.mjs"))]
    payload = {"root": str(project.root), "files": [
        {"path": str(project.safe_path(file)), "code": source.decode("utf-8", errors="replace")}
        for file, source in project.sources.items() if project.languages[file] in {"javascript", "typescript"}
    ]}
    try:
        result = subprocess.run(command, input=json.dumps(payload), cwd=project.root, capture_output=True,
                                text=True, errors="replace", timeout=timeout)
        if result.returncode:
            missing = "Cannot find package" in result.stderr or "Cannot find module" in result.stderr
            return {"status": "unavailable" if missing else "error", "version": None, "diagnostics": [],
                    "notes": [f"ESLint 未能运行：{result.stderr[:1200]}", "在本工具安装目录运行 npm ci --ignore-scripts"]}
        data = json.loads(result.stdout)
        findings, notes = [], []
        for row in data["results"]:
            for message in row["messages"]:
                finding = diagnostic(project, "eslint", row["filePath"], message.get("line"),
                                     message.get("ruleId") or "parse-error", "error" if message["severity"] == 2 else "warning",
                                     message["message"])
                if finding:
                    findings.append(finding)
                else:
                    notes.append(f"{row['filePath']}: {message['message']}")
        return {"status": "error" if notes else "completed", "version": data["version"], "command": command,
                "diagnostics": findings, "notes": notes}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return {"status": "error", "version": None, "diagnostics": [], "notes": [str(exc)]}


def scan(project: Project, compile_commands: str | None = None, timeout: int = 120):
    languages = set(project.languages.values())
    tools = []
    if languages & {"c", "cpp"}:
        tools.append({"tool": "cppcheck", "languages": sorted(languages & {"c", "cpp"}),
                      **scan_cppcheck(project, compile_commands, timeout)})
    elif compile_commands:
        raise ValueError("--compile-commands 仅适用于包含 C/C++ 的输入")
    if "python" in languages:
        for tool in ("bandit", "ruff"):
            tools.append({"tool": tool, "languages": ["python"], **scan_python(project, tool, timeout)})
        tools.append({"tool": "pyright", "languages": ["python"], **scan_pyright(project, timeout)})
    if languages & {"javascript", "typescript"}:
        tools.append({"tool": "eslint", "languages": sorted(languages & {"javascript", "typescript"}), **scan_eslint(project, timeout)})
    findings, notes = [], []
    for tool in tools:
        notes.extend(f"{tool['tool']}: {note}" for note in tool["notes"])
        for finding in tool["diagnostics"]:
            finding["id"] = f"D{len(findings) + 1}"
            finding["language"] = project.languages[finding["file"]]
            findings.append(finding)
    states = {tool["status"] for tool in tools}
    status = ("completed" if states == {"completed"} else "partial" if "completed" in states
              else "unavailable" if states == {"unavailable"} else "error")
    return {"status": status, "diagnostics": findings, "notes": notes, "tools": tools,
            "version": tools[0]["version"] if len(tools) == 1 else None,
            "versions": {tool["tool"]: tool["version"] for tool in tools}}
