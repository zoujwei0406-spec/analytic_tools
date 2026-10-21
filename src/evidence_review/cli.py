from __future__ import annotations

import argparse
from collections import Counter
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .agent import Ollama, review
from .coverage import collect, resolve_tool
from .project import Project
from .languages import LABELS
from .report import write_report
from .sbfl import file_candidates, load_spectra, rank_candidates, score_spectra
from .scanner import find_cppcheck, find_tool, scan

LIMITATIONS = [
    "支持 C、C++、Python、JavaScript、TypeScript；AST 不执行预处理或完整类型/别名分析。成员调用、动态分派和同名函数均可能存在歧义。",
    "Python 使用 Bandit、Ruff 和 Pyright basic 类型检查；Pyright 仅分析导入源码快照，不加载目标配置，不推断第三方库实现；未知类型仍可能漏报。JS/TS 使用固定 ESLint 规则，TypeScript 尚未接入 tsc 类型检查。",
    "内置 collect 目前仅支持简单 C 程序；其他语言可导入符合格式的逐测试覆盖 JSON 进行 SBFL。",
    "candidates 模式只复核静态诊断和 SBFL 候选；files 模式也审查无报警文件。top-k、源码读取预算和模型能力均可能导致遗漏，源码提供不代表检查正确。",
    "SBFL 使用 Ochiai，可疑度不等于缺陷概率。RRF 是排序策略，其有效性尚需对照实验。",
    "模型引用校验仅验证证据确实已返回，不证明推理正确。模型不能将问题标记为已动态复现。",
    "整文件模式记录每条诊断及每个函数/模块单元的处理意见；Python 显式 for/while 循环中的下标访问另建独立任务并复查，语法错误时尝试使用恢复语法树。处理记录完整不代表判断正确，也不是新的静态越界证明。",
    "context_chars 统计返回的证据字符数，不是 Token 数，也不包含重复发送历史消息的费用。",
]


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def analyze(args):
    started = time.monotonic()
    project = Project(Path(args.project), args.inputs)
    print(f"[1/4] 索引完成：{len(project.sources)} 个文件，{len(project.functions)} 个函数", flush=True)
    language_counts = dict(sorted(Counter(project.languages.values()).items()))
    print("      语言：" + "、".join(f"{LABELS[lang]} {count}" for lang, count in language_counts.items()), flush=True)
    scanner = scan(project, args.compile_commands)
    print(f"[2/4] 静态扫描：{scanner['status']}，{len(scanner['diagnostics'])} 条诊断", flush=True)
    for tool in scanner["tools"]:
        print(f"      {tool['tool']}：{tool['status']}，{len(tool['diagnostics'])} 条诊断", flush=True)
    spectra = load_spectra(Path(args.spectra), project) if args.spectra else None
    sbfl = score_spectra(spectra)
    candidates = rank_candidates(project, scanner["diagnostics"], sbfl)
    if args.review_scope == "files":
        candidates = file_candidates(project, candidates)
    print(f"[3/4] SBFL：{sbfl['status']}，候选 {len(candidates)}，选择前 {min(args.top_k, len(candidates))} 个", flush=True)
    client = Ollama(args.model, args.ollama_url, args.model_timeout) if args.model else None
    trace_path = Path(args.output).resolve() / "model-trace.jsonl"
    if client:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("", encoding="utf-8")

    def trace_callback(event):
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    reviews = []
    for candidate in candidates[:args.top_k]:
        action = f"模型复核（{args.model}）" if client else "提取上下文（未启用模型）"
        print(f"      {action} {candidate['id']}", flush=True)
        reviews.append(review(project, candidate, client, args.context_mode, args.context_chars,
                              args.max_rounds, args.max_calls,
                              progress=lambda message: print(f"        {message}", flush=True),
                              trace_callback=trace_callback if client else None))
    project.check_unchanged()
    report = {
        "schema_version": 2, "project": str(project.root), "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model, "context_mode": args.context_mode, "review_scope": args.review_scope,
        "source_hashes": project.hashes(),
        "languages": project.languages,
        "summary": {"files": len(project.sources), "functions": len(project.functions),
                    "languages": language_counts,
                    "diagnostics": len(scanner["diagnostics"]), "candidates": len(candidates), "selected": len(reviews),
                    "model_findings": sum(len((r.get("review") or {}).get("findings", [])) for r in reviews),
                    "context_chars": sum(r["context_chars"] for r in reviews),
                    "source_chars": sum(len(data.decode("utf-8", errors="replace")) for data in project.sources.values()),
                    "elapsed_seconds": round(time.monotonic() - started, 3)},
        "scanner": scanner, "sbfl": sbfl, "candidates": candidates, "reviews": reviews,
        "model_usage": client.usage if client else None,
        "index_warnings": project.warnings, "limitations": LIMITATIONS,
    }
    output = write_report(report, Path(args.output).resolve())
    print(f"[4/4] 报告：{output}")
    if not client:
        print("未启用大模型：已完成真实静态扫描、候选排序与源码提取；没有生成模型结论。")
    elif args.review_scope == "files":
        print(f"整文件审查：模型列出 {report['summary']['model_findings']} 个问题（未经执行验证）；原始静态诊断 {len(scanner['diagnostics'])} 条。")
    if scanner["status"] != "completed":
        print("扫描未完整完成：请查看报告中的扫描器状态。语法错误可能阻止后续规则检查。")
    errors = scanner["status"] != "completed" or any(r["status"] in {"error", "budget_exhausted"} for r in reviews)
    return 2 if errors else 0


def example_spectra(project):
    # Deliberately synthetic: this command tests the SBFL pipeline, not C execution.
    return {"schema_version": 1, "origin": "synthetic-demo", "source_hashes": project.hashes(), "tests": [
        {"name": "below_threshold", "outcome": "pass", "covered": {"fees.c": [4, 5, 8], "main.c": [6, 9, 10, 11]}},
        {"name": "above_threshold", "outcome": "pass", "covered": {"fees.c": [4, 5, 6], "main.c": [6, 9, 10, 11]}},
        {"name": "at_threshold", "outcome": "fail", "covered": {"fees.c": [4, 5, 8], "main.c": [6, 9, 10, 11]}},
    ]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="多语言程序分析：静态扫描 + AST + 可选 SBFL + 有界 Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    analysis = commands.add_parser("analyze", help="分析一个项目或其中的文件；默认离线")
    analysis.add_argument("project", help="项目根目录或单个源码文件")
    analysis.add_argument("--inputs", nargs="+", help="相对项目根目录的文件或目录，可传多个")
    analysis.add_argument("--spectra", help="逐测试覆盖 JSON（路径相对当前工作目录）")
    analysis.add_argument("--compile-commands", help="相对项目根目录的 compile_commands.json")
    analysis.add_argument("--model", help="启用 Ollama 模型；源码证据将发送到指定服务")
    analysis.add_argument("--ollama-url", default="http://localhost:11434")
    analysis.add_argument("--model-timeout", type=positive, default=180, help="每轮模型请求等待超时秒数，不是整个分析的总时限")
    analysis.add_argument("--top-k", type=positive, default=5)
    analysis.add_argument("--review-scope", choices=["candidates", "files"], default="candidates",
                          help="candidates 复核报警位置；files 按文件审查多个问题，包含无报警文件；top-k 限制所选数量")
    analysis.add_argument("--context-mode", choices=["function", "fixed", "adaptive"], default="adaptive")
    analysis.add_argument("--context-chars", type=positive, default=14000, help="每个候选的证据字符预算")
    analysis.add_argument("--max-rounds", type=positive, default=6)
    analysis.add_argument("--max-calls", type=positive, default=10)
    analysis.add_argument("--output", default="reports/latest")
    coverage = commands.add_parser("collect", help="编译并在本机执行你信任的 C 测试，采集逐测试覆盖")
    coverage.add_argument("project")
    coverage.add_argument("--inputs", nargs="+")
    coverage.add_argument("--manifest", required=True)
    coverage.add_argument("--timeout", type=positive, default=10)
    coverage.add_argument("--output", default="reports/spectra.json")
    demo = commands.add_parser("demo", help="真实扫描示例，并使用明确标注的人工覆盖数据演示 SBFL")
    demo.add_argument("--output", default="reports/demo")
    demo.add_argument("--suite", choices=["c", "multilang"], default="c", help="c 使用教学 SBFL；multilang 运行多语言真实静态扫描")
    commands.add_parser("doctor", help="检查本机扫描器与覆盖工具链")
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            return analyze(args)
        if args.command == "collect":
            result = collect(Project(Path(args.project), args.inputs), Path(args.manifest), args.timeout)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"覆盖数据：{output.resolve()}")
            print(f"通过 {sum(t['outcome'] == 'pass' for t in result['tests'])} / 失败 {sum(t['outcome'] == 'fail' for t in result['tests'])} / 采集错误 {sum(t['outcome'] == 'error' for t in result['tests'])}")
            return 2 if any(t["outcome"] == "error" for t in result["tests"]) else 0
        if args.command == "demo":
            if args.suite == "multilang":
                directory = Path(__file__).resolve().parent / "demo_multilang"
                print("多语言示例：扫描器与 AST 实际运行；不使用模拟覆盖数据，未调用模型。", flush=True)
                return main(["analyze", str(directory), "--output", str(Path(args.output).resolve())])
            directory = Path(__file__).resolve().parent / "demo_project"
            output = Path(args.output).resolve()
            output.mkdir(parents=True, exist_ok=True)
            spectra = output / "synthetic-spectra.json"
            spectra.write_text(json.dumps(example_spectra(Project(directory)), indent=2))
            print("注意：Cppcheck 与 AST 实际运行；SBFL 使用人工构造的教学覆盖数据；未调用模型。", flush=True)
            return main(["analyze", str(directory), "--spectra", str(spectra), "--output", str(output)])
        if args.command == "doctor":
            executable = find_cppcheck()
            print("Cppcheck:", executable or "未安装")
            for name in ("bandit", "ruff", "node"):
                print(f"{name}: {find_tool(name) or '未安装'}")
            if find_tool("node"):
                probe = subprocess.run([find_tool("node"), str(Path(__file__).with_name("eslint_runner.mjs"))],
                                       input=json.dumps({"root": str(Path.cwd()), "files": []}),
                                       capture_output=True, text=True, timeout=30)
                print("ESLint:", json.loads(probe.stdout)["version"] if probe.returncode == 0 else "不可用，请在工具目录运行 npm ci --ignore-scripts")
            for name in ("clang", "llvm-profdata", "llvm-cov"):
                try:
                    path = resolve_tool(name)
                    result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
                    print(f"{name}: {result.stdout.splitlines()[0] if result.returncode == 0 else '不可用（请检查工具链安装）'}")
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    print(f"{name}: {exc}")
            return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
