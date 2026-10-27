from __future__ import annotations

import html
import json
from pathlib import Path


def write_report(report: dict, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    esc = lambda value: html.escape(str(value))
    stats = report["summary"]
    diagnostics = report["scanner"]["diagnostics"]
    reviews = {r["candidate_id"]: r for r in report["reviews"]}
    cards = []
    for candidate in report["candidates"]:
        review = reviews.get(candidate["id"])
        conclusion = review.get("review") if review else None
        status = review["status"] if review else "未进入本次上下文预算"
        if conclusion:
            title = {"likely_issue": "疑似问题", "likely_false_positive": "疑似误报", "inconclusive": "无法判断"}[conclusion["verdict"]]
            body = f'<p><strong>{title}</strong> · {esc(conclusion["summary"])}</p><p>建议：{esc(conclusion["suggestion"])}</p>'
            body += f'<p>引用：{esc(", ".join(conclusion["evidence_ids"]))} · 缺失证据：{esc("；".join(conclusion["missing_evidence"]))}</p>'
            for finding in conclusion.get("findings", []):
                binding_note = " · 引用由程序按文件和行号关联" if finding.get("evidence_origin") == "source_location" else ""
                origin_note = '程序校正规范结论，静态见证未经目标代码执行验证' if finding.get('evidence_origin') == 'program_verified' else '模型推断，未经执行验证'
                body += (f'<div class="finding"><h3>{esc(finding["file"])}:{finding["line"]} · {esc(finding["title"])}</h3>'
                         f'<p>{esc(finding["explanation"])}</p><p><strong>触发条件：</strong>{esc(finding["condition"])}</p>'
                         f'<p>建议：{esc(finding["suggestion"])}</p><p class="muted">{origin_note} · 引用：'
                         f'{esc(", ".join(finding["evidence_ids"]))}{binding_note}</p></div>')
            for finding in conclusion.get("withdrawn_findings", []):
                body += (f'<details><summary>复核撤回 · {esc(finding["file"])}:{finding["line"]} · {esc(finding["title"])}</summary>'
                         f'<p>{esc(finding["withdraw_reason"])}</p></details>')
        else:
            label = {"not_run": "未启用模型", "error": "模型调用失败",
                     "budget_exhausted": "模型在预算内未完成有效提交，不能据此判断没有问题"}.get(status, status)
            body = f'<p class="muted">尚无模型复核结论 · {esc(label)}</p>'
        if review and review.get("error"):
            body += f'<p class="warning">{esc(review["error"])}</p>'
        if review and review.get("source_coverage"):
            coverage = review["source_coverage"]
            body += f'<p>提供源码：{coverage["provided_lines"]} / {coverage["total_lines"]} 行（提供不等于逐行检查正确）。</p>'
            if not coverage["complete"]:
                body += '<p class="warning">源码未完整提供，本次只审查了部分内容，剩余部分不能视为无问题。</p>'
        if review and review.get("audit"):
            audit = review["audit"]
            uncertain = sum(item["verdict"] == "inconclusive" for item in audit["diagnostic_reviews"] + audit["function_reviews"])
            body += (f'<h3>逐项审查记录</h3><p>静态诊断 {len(audit["diagnostic_reviews"])} / {audit["diagnostic_total"]} 条；'
                     f'函数、模块及操作单元 {len(audit["function_reviews"])} / {audit["function_total"]} 个（循环下标 {audit.get("loop_index_total", 0)} 个）；'
                     f'补查：{"已请求" if audit["followup_performed"] else "未请求"}；待确认意见 {uncertain} 条。处理意见不证明判断正确。</p>')
            for field, label in (('program_reconciliations', '程序证据校正记录（保留模型原意见）'),
                                 ('blocked_withdrawals', '被拦截的矛盾撤回操作')):
                if audit.get(field):
                    body += f'<details><summary>{label}</summary><pre>{esc(json.dumps(audit[field], ensure_ascii=False, indent=2))}</pre></details>'
            if audit.get("program_conflicts"):
                body += f'<p class="warning">存在程序与模型结论冲突，需要复核：{esc(json.dumps(audit["program_conflicts"], ensure_ascii=False))}</p>'
            if uncertain:
                body += '<p class="warning">部分处理意见仍为待确认。记录齐全不代表这些问题已得到确定判断，请查看具体意见和原始诊断。</p>'
            audit_rows = []
            verdict_labels = {"likely_issue": "认可风险", "likely_false_positive": "不支持此报警",
                              "inconclusive": "待确认", "issue_found": "发现问题", "no_issue_found": "未发现问题"}
            units = {unit["function_id"]: unit for unit in audit.get("units", [])}
            for field, identity in (("diagnostic_reviews", "diagnostic_id"), ("function_reviews", "function_id")):
                for item in audit[field]:
                    basis = " · 依据：" + ", ".join(item["basis_diagnostics"]) if item.get("basis_diagnostics") else ""
                    unit = units.get(item[identity], {}) if field == "function_reviews" else {}
                    detail = f'<br><code>{esc(unit["access"])}</code>' if unit.get("kind") == "loop_index" else ""
                    audit_rows.append(f'<tr><td>{esc(item[identity])}{detail}</td><td>{esc(verdict_labels[item["verdict"]])}</td><td>{esc(item["reason"] + basis)}</td></tr>')
            body += '<details><summary>诊断与审查单元处理意见</summary><table><tr><th>对象</th><th>结论</th><th>理由</th></tr>' + ''.join(audit_rows) + '</table></details>'
            if not audit["complete"]:
                body += f'<p class="warning">逐项审查或补查尚未完成：{esc(json.dumps(audit["pending"], ensure_ascii=False))}</p>'
        for evidence in review["evidence"] if review else []:
            if evidence["kind"] == "source":
                body += f'<details><summary>{esc(evidence["evidence_id"])} · {esc(evidence["file"])}:{evidence["start"]}–{evidence["end"]}</summary><pre>{esc(evidence["code"])}</pre></details>'
            else:
                body += f'<details><summary>{esc(evidence["evidence_id"])} · {esc(evidence["kind"])}</summary><pre>{esc(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre></details>'
        if review and review["trace"]:
            body += f'<details><summary>工具调用记录（{len(review["trace"])} 次）</summary><pre>{esc(json.dumps(review["trace"], ensure_ascii=False, indent=2))}</pre></details>'
        cards.append(f'<article><div class="eyebrow">{esc(candidate.get("language", ""))} · {esc(candidate["file"])}:{candidate["line"]}</div><h2>{esc(candidate["name"])}</h2><div class="tags">RRF 优先级 {candidate["priority"]:.5f} · SBFL 可疑度 {candidate["sbfl_score"]:.3f} · 静态诊断 {len(candidate["diagnostics"])}</div>{body}</article>')
    rows = "".join(f'<tr><td>{esc(d["id"])}</td><td>{esc(d["tool"])}</td><td>{esc(d["file"])}:{d["line"]}</td><td>{esc(d["severity"])} / {esc(d["rule"])}</td><td>{esc(d["message"])}</td></tr>' for d in diagnostics)
    scanner_rows = "".join(f'<tr><td>{esc(tool["tool"])}</td><td>{esc(", ".join(tool["languages"]))}</td><td>{esc(tool["status"])}</td><td>{len(tool["diagnostics"])}</td><td>{esc(tool["version"] or "不可用")}</td></tr>' for tool in report["scanner"].get("tools", []))
    notes = "".join(f"<li>{esc(note)}</li>" for note in report["limitations"] + report["scanner"]["notes"] + report["index_warnings"])
    origin = report["sbfl"].get("origin", "未提供覆盖数据")
    banner = '<div class="warning">本报告使用人工构造的教学覆盖数据，未实际运行测试。请勿将其作为动态验证证据。</div>' if origin == "synthetic-demo" else ""
    if report["scanner"]["status"] != "completed" or report["index_warnings"]:
        banner += '<div class="warning">静态扫描或 AST 索引不完整。语法错误可能阻止后续规则检查；请查看扫描器状态与运行提示。模型补充意见不代表静态检查已成功。</div>'
    if stats["selected"] < stats["candidates"]:
        banner += f'<div class="warning">本次仅选择 {stats["selected"]} / {stats["candidates"]} 个审查单元；未选择部分尚未进行模型审查，可增大 --top-k。</div>'
    if report["model"] and any(r["status"] != "completed" for r in report["reviews"]):
        banner += '<div class="warning">部分模型审查未完成有效提交。问题数只统计已接受的结论，不能将未完成的审查视为没有问题。</div>'
    audit_note = (f'审查范围：{"整文件多问题审查" if report.get("review_scope") == "files" else "候选报警复核"} · '
                  f'模型列出问题：{stats.get("model_findings", 0)}（独立于原始静态诊断，可能重合）')
    last_stat = (f'<div class="stat">模型问题（未经验证）<strong>{stats.get("model_findings", 0)}</strong></div>'
                 if report.get("review_scope") == "files" and report["model"] else
                 f'<div class="stat">证据字符数<strong>{stats["context_chars"]}</strong></div>')
    program_section = ""
    if report.get("program_analysis"):
        analysis = report["program_analysis"]
        labels = {"conditional_safe": "有前提的边界安全", "conditional_violation": "有前提的越界风险",
                  "unknown": "待确认", "verified_safe": "限定范围安全", "verified_violation": "程序支持的越界"}
        program_rows = []
        for claim in analysis["claims"]:
            detail = {key: claim[key] for key in ("assumptions", "derivation", "witness", "missing", "facts", "depends_on") if key in claim}
            program_rows.append(f'<tr><td>{esc(claim["file"])}:{claim["line"]}:{claim["column"]}<br><code>{esc(claim["expression"])}</code></td>'
                f'<td>{esc(labels.get(claim["verification_result"], claim["verification_result"]))}</td>'
                f'<td>{esc(claim["reason"])} · {esc(claim["stop_reason"])}'
                f'<details><summary>前提、推导与事实</summary><pre>{esc(json.dumps(detail, ensure_ascii=False, indent=2))}</pre></details></td></tr>')
        program_section = (f'<article><h2>知识库判定与证据补查</h2><p>策略：{esc(analysis["policy"])}；'
            f'访问 {len(analysis["claims"])} 处，动作 {analysis["summary"]["actions"]} 次，'
            f'相对成本 {analysis["summary"]["estimated_cost"]}。程序结果独立于模型意见；条件结论依赖列出的前提，未经真实执行复现。</p>'
            f'<p>检查范围：{esc(json.dumps(analysis.get("coverage", {}), ensure_ascii=False))}</p>'
            '<div class="table-wrap"><table><tr><th>访问位置（UTF-8 字节列）</th><th>程序结果</th><th>依据及停止原因</th></tr>'
            + ''.join(program_rows) + '</table></div>'
            f'<details><summary>补查动作与成本</summary><pre>{esc(json.dumps(analysis["trace"], ensure_ascii=False, indent=2))}</pre></details>'
            f'<p>解析未完成文件：{esc(json.dumps(analysis["parse_errors"], ensure_ascii=False))}</p></article>')
    page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Evidence Review · 程序分析报告</title><style>
:root{{color-scheme:light;--ink:#17283d;--muted:#63738a;--line:#dbe3eb;--accent:#13736e}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fa;color:var(--ink);font:15px/1.7 system-ui,-apple-system,sans-serif}}main{{max-width:1120px;margin:auto;padding:42px 24px 80px}}header{{border-bottom:1px solid var(--line);padding-bottom:24px}}h1{{font-size:32px;letter-spacing:-1px;margin:8px 0}}h2{{font-size:20px;margin:6px 0}}.eyebrow{{color:var(--accent);font-size:12px;font-weight:700;letter-spacing:1px}}.muted,.tags{{color:var(--muted)}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:24px 0}}.stat,article{{background:white;border:1px solid var(--line);border-radius:12px;padding:22px}}.stat strong{{display:block;font-size:30px}}article{{margin:16px 0}}.warning{{background:#fff2d5;color:#795013;border:1px solid #efd59c;border-radius:8px;padding:14px;margin:18px 0}}details{{border-top:1px solid var(--line);padding:10px 0}}summary{{cursor:pointer;color:var(--accent)}}pre{{background:#132237;color:#d9e7f4;padding:18px;border-radius:8px;overflow:auto;max-height:520px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}.table-wrap{{overflow-x:auto}}footer{{margin-top:28px;color:var(--muted);font-size:13px}}@media(max-width:650px){{.stats{{grid-template-columns:repeat(2,1fr)}}main{{padding:24px 16px}}h1{{font-size:27px}}}}
</style></head><body><main><header><div class="eyebrow">EVIDENCE REVIEW / 分析记录</div><h1>从候选问题到可追溯证据</h1><div class="muted">{esc(report["project"])} · {esc(report["created_at"])}<br>扫描：{esc(report["scanner"]["status"])} · 模型：{esc(report["model"] or "未启用")} · 上下文策略：{esc(report["context_mode"])}</div></header>
{banner}<div class="stats"><div class="stat">导入文件<strong>{stats["files"]}</strong></div><div class="stat">原始诊断<strong>{stats["diagnostics"]}</strong></div><div class="stat">候选 / 已选<strong>{stats["candidates"]} / {stats["selected"]}</strong></div>{last_stat}</div>
<p class="muted">{audit_note}<br>SBFL：{esc(report["sbfl"]["status"])} · 来源：{esc(origin)} · 通过 {report["sbfl"]["passed"]} / 失败 {report["sbfl"]["failed"]} / 采集错误 {report["sbfl"]["errors"]}<br>优先级和可疑度均不是缺陷概率；模型结论仅为复核意见。</p>
<article><h2>扫描器运行状态</h2><div class="table-wrap"><table><thead><tr><th>工具</th><th>语言</th><th>状态</th><th>诊断数</th><th>版本</th></tr></thead><tbody>{scanner_rows}</tbody></table></div></article>
{program_section}
{"".join(cards) or '<article>没有产生候选问题。这不代表程序没有缺陷。</article>'}
<article><h2>保留的全部静态诊断</h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>工具</th><th>位置</th><th>级别 / 规则</th><th>说明</th></tr></thead><tbody>{rows or '<tr><td colspan="5">没有静态诊断；请结合扫描状态判断。</td></tr>'}</tbody></table></div></article>
<article><h2>分析边界与运行提示</h2><ul>{notes}</ul></article><footer>完整结构化结果与工具调用记录见同目录 report.json。证据编号在每个候选问题内部有效。</footer></main></body></html>'''
    (output / "report.html").write_text(page, encoding="utf-8")
    return output / "report.html"
