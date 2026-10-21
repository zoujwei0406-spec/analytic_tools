"""Analyze labelled fixtures without importing or executing their source code."""

import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import json
from pathlib import Path
import sys
from threading import Lock
import time

from evidence_review.cli import main as analyze, positive


ROOT = Path(__file__).resolve().parent


class LiveLog:
    """Write immediately to both the terminal and the current case's log."""

    def __init__(self, console, log):
        self.console, self.log = console, log
        self.lock = Lock()

    def write(self, value):
        with self.lock:
            self.console.write(value)
            self.log.write(value)
            self.console.flush()
            self.log.flush()
        return len(value)

    def flush(self):
        with self.lock:
            self.console.flush()
            self.log.flush()


def matched_diagnostics(issue, diagnostics):
    """Match a labelled rule and an actual reported location, not just proximity."""
    return [d["id"] for d in diagnostics if d["rule"] in issue["static_rules"] and any(
        location["file"] == Path(issue["file"]).name and location["line"] == issue["line"]
        for location in d["locations"])]


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行 test/cases 中的教学用例；不会执行用例代码")
    parser.add_argument("--language", action="append", choices=["c", "cpp", "python", "javascript", "typescript"])
    parser.add_argument("--group", action="append", choices=["defects", "logic", "syntax_error", "clean"])
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    parser.add_argument("--model", help="可选本地模型；默认仅运行静态扫描，建议先只选一个用例")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11435")
    parser.add_argument("--max-rounds", type=positive, default=6)
    parser.add_argument("--model-timeout", type=positive, default=180, help="每轮模型请求超时秒数")
    args = parser.parse_args(argv)
    manifest = json.loads((ROOT / "expected.json").read_text())
    cases = [c for c in manifest["cases"] if (not args.language or c["language"] in args.language)
             and (not args.group or c["group"] in args.group)]
    if not cases:
        parser.error("没有匹配用例")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for case_number, case in enumerate(cases, 1):
        destination = output / case["language"] / case["group"]
        destination.mkdir(parents=True, exist_ok=True)
        command = ["analyze", str(ROOT / "cases" / case["file"]), "--review-scope", "files",
                   "--output", str(destination)]
        if args.model:
            command += ["--model", args.model, "--ollama-url", args.ollama_url,
                        "--max-rounds", str(args.max_rounds), "--model-timeout", str(args.model_timeout)]
        report_path = destination / "report.json"
        # A failed invocation must not silently reuse a previous run's report.
        report_path.unlink(missing_ok=True)
        report_path.with_suffix(".html").unlink(missing_ok=True)
        started = time.monotonic()
        with (destination / "run.log").open("w", encoding="utf-8") as log:
            live = LiveLog(sys.stdout, log)
            with redirect_stdout(live), redirect_stderr(live):
                print(f"[{case_number}/{len(cases)}] 开始分析 {case['file']}；日志实时写入 {destination / 'run.log'}", flush=True)
                if args.model:
                    print(f"模型 {args.model}；最多 {args.max_rounds} 轮，每轮请求超时 {args.model_timeout} 秒。等待时每 15 秒显示状态。", flush=True)
                try:
                    exit_code = analyze(command)
                except KeyboardInterrupt:
                    print("分析已中断；已保留本轮日志，尚未完成的报告不会视为成功。", flush=True)
                    return 130
                print(f"本用例分析结束：退出码 {exit_code}，耗时 {time.monotonic() - started:.1f} 秒。", flush=True)
        if not report_path.exists():
            print(f"{case['file']}: 未生成本轮报告，退出码 {exit_code}；详情见 {destination / 'run.log'}", flush=True)
            return 2
        report = json.loads(report_path.read_text())
        diagnostics = report["scanner"]["diagnostics"]
        labels = [{**issue, "matched_static_diagnostics": matched_diagnostics(issue, diagnostics)}
                  for issue in case["issues"]]
        syntax_reported = any(d["rule"] in {"invalid-syntax", "parse-error", "syntaxError", "internalAstError"}
                              for d in diagnostics)
        unexpected_error = any(tool["status"] != "completed" and not (
            case["group"] == "syntax_error" and syntax_reported and tool["status"] == "error"
            and any(word in " ".join(tool["notes"]).lower() for word in ("syntax", "parsing", "parse", "ast")))
            for tool in report["scanner"]["tools"])
        unexpected_error |= any(r["status"] != "completed" for r in report["reviews"]) if args.model else False
        row = {"file": case["file"], "group": case["group"], "language": case["language"],
               "scanner_status": report["scanner"]["status"], "cli_exit": exit_code,
               "diagnostics": len(diagnostics), "model_findings": report["summary"]["model_findings"],
               "label_count": len(labels), "matched_label_count": sum(bool(i["matched_static_diagnostics"]) for i in labels),
               "unexpected_tool_error": unexpected_error,
               "index_warnings": report["index_warnings"],
               "control_has_diagnostics": case["group"] == "clean" and bool(diagnostics),
               "control_has_model_findings": case["group"] == "clean" and bool(report["summary"]["model_findings"]),
               "report": str((destination / "report.html").relative_to(output)), "labels": labels}
        results.append(row)
        print(f"{case['file']}: 扫描 {row['scanner_status']}，静态诊断 {len(diagnostics)}，"
              f"模型问题 {row['model_findings']}，标签规则/位置匹配 {row['matched_label_count']}/{len(labels)}", flush=True)

    summary = {"schema_version": 1, "mode": "local-model" if args.model else "static-only", "model": args.model,
               "cases": len(results), "languages": dict(Counter(r["language"] for r in results)),
               "expected_labels": sum(r["label_count"] for r in results),
               "static_diagnostics": sum(r["diagnostics"] for r in results),
               "matched_static_labels": sum(r["matched_label_count"] for r in results),
               "model_findings": sum(r["model_findings"] for r in results),
               "unexpected_tool_errors": sum(r["unexpected_tool_error"] for r in results),
               "control_cases_with_diagnostics": sum(r["control_has_diagnostics"] for r in results),
               "control_cases_with_model_findings": sum(r["control_has_model_findings"] for r in results),
               "notes": ["用例代码没有执行；没有生成覆盖数据或动态复现证据。",
                         "标签为人工设计；规则和行号匹配仅作为静态检出的保守代理，不证明诊断含义完全一致。",
                         "模型问题未自动与预期标签作语义匹配，不能据问题数量声称准确率或召回率。",
                         "未列出规则的标签不做自动匹配；原始全部静态诊断保留在各用例报告中。",
                         "语法错误组预期可能出现部分扫描失败，仍保留原始状态与退出码。"],
               "results": results}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 多语言用例运行记录", "", f"模式：{summary['mode']}；模型：{args.model or '未调用'}。",
             "", "用例源码未执行，没有覆盖采集或动态复现。", "",
             "| 用例 | 扫描状态 | 静态诊断 | 静态规则/位置匹配标签 | 模型问题 | 报告 |",
             "|---|---|---:|---:|---:|---|"]
    for row in results:
        lines.append(f"| {row['file']} | {row['scanner_status']} | {row['diagnostics']} | "
                     f"{row['matched_label_count']}/{row['label_count']} | {row['model_findings']} | [打开]({row['report']}) |")
    lines += ["", "规则/位置匹配数不等于完整检出率；未匹配标签需要结合诊断和源码人工复核。",
              "模型问题数量可能包含重复、误报或漏报，未进行自动语义评分。", "",
              f"正常对照出现静态报警：{summary['control_cases_with_diagnostics']} 个；出现模型报警：{summary['control_cases_with_model_findings']} 个。", "",
              "## 索引提示", ""]
    for row in results:
        for warning in row["index_warnings"]:
            lines.append(f"- {row['file']} · {warning}")
    lines += ["",
              "## 静态规则未匹配的预期标签（不含模型语义匹配）", "",
              "下面的标签可能已经被模型发现，需要查看对应模型报告确认。", ""]
    for row in results:
        for issue in row["labels"]:
            if not issue["matched_static_diagnostics"]:
                lines.append(f"- {issue['id']} · {issue['file']}:{issue['line']} · {issue['title']}：{issue['condition']}")
    (output / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"汇总：{output / 'summary.md'}")
    return 2 if summary["unexpected_tool_errors"] else 1 if (summary["control_cases_with_diagnostics"] or summary["control_cases_with_model_findings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
