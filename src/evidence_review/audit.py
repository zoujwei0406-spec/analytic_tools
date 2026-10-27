"""Track review coverage separately from source delivery and issue counts."""

from copy import deepcopy
import ast

from .languages import parser_for, text, walk


def loop_index_units(project, file):
    """Recovery syntax supplies review targets, never a proved bounds violation."""
    if project.languages[file] != "python":
        return []
    tree = parser_for("python").parse(project.sources[file])
    units = []
    for node in walk(tree.root_node):
        if node.type != "subscript":
            continue
        loops = []
        parent = node.parent
        while parent:
            if parent.type in {"function_definition", "lambda", "class_definition"}:
                break
            if parent.type in {"for_statement", "while_statement"}:
                loops.append(parent)
            parent = parent.parent
        if not loops:
            continue
        start, end = node.start_point.row + 1, node.end_point.row + 1
        controls = [{"line": loop.start_point.row + 1,
                     "kind": loop.type,
                     "target": text(loop.child_by_field_name("left"))[:160],
                     "expression": text(loop.child_by_field_name("right") or
                                        loop.child_by_field_name("condition"))[:160]}
                    for loop in loops]
        units.append({"function_id": f"{file}:{start}:{node.start_point.column + 1}:loop-index",
                      "name": "循环下标访问", "kind": "loop_index",
                      "ranges": [[start, end]],
                      "context_ranges": [[loops[-1].start_point.row + 1, loops[-1].end_point.row + 1]],
                      "access": text(node)[:160], "loop_controls": controls,
                      "syntax_recovered": tree.root_node.has_error,
                      "check": "独立核对本处访问：容器类型、循环首次/末次索引及偏移、空输入、保护条件、提前退出与容器变化。"
                               "reason 简述实际边界或保护依据；不能以同函数的其他问题替代本处结论。"
                               "区分 Python 负索引、切片和字典键语义。类型或可达性未知时注明条件或 inconclusive，"
                               "语法错误后的风险须说明修复前置错误后才可能触发。"})
    return units


VERDICTS = ["likely_issue", "likely_false_positive", "inconclusive"]
UNIT_VERDICTS = ["issue_found", "no_issue_found", "inconclusive"]


class AuditTracker:
    def __init__(self, project, candidate, program_analysis=None):
        self.program_analysis = program_analysis
        hashes = project.hashes()
        self.resolved_claims = {claim['access_id']: claim for claim in (program_analysis or {}).get('claims', [])
            if claim.get('access_id') and claim.get('source_hash') == hashes.get(claim.get('file'))
            and claim.get('terminal') is True and claim.get('stop_reason') == 'resolved'
            and claim.get('verdict') in {'SAFE', 'VIOLATION'}}
        self.evidence_complete = bool(self.resolved_claims) and len(self.resolved_claims) == len((program_analysis or {}).get('claims', []))
        self.program_reconciliations = []
        self.blocked_withdrawals = []
        self.file = candidate["file"]
        self.diagnostics = {d["id"]: d for d in candidate["diagnostics"]}
        self.units = {}
        occupied = set()
        for function in project.functions.values():
            if function.file != self.file:
                continue
            self.units[function.id] = {"function_id": function.id, "name": function.name,
                                      "ranges": [[function.start, function.end]]}
            occupied.update(range(function.start, function.end + 1))
        if project.languages[self.file] == "python":
            try:
                module = ast.parse(project.sources[self.file])
            except SyntaxError:
                module = None
            for node in module.body if module else []:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    # Import security warnings already have diagnostic tasks.
                    occupied.update(range(node.lineno, node.end_lineno + 1))
        # Imports and statements outside functions also need a disposition.
        module_lines = [n for n, line in enumerate(project.sources[self.file].splitlines(), 1)
                        if n not in occupied and line.strip()]
        if module_lines:
            ranges = []
            for line in module_lines:
                if ranges and line == ranges[-1][1] + 1:
                    ranges[-1][1] = line
                else:
                    ranges.append([line, line])
            fid = self.file + ":module"
            self.units[fid] = {"function_id": fid, "name": "模块级语句", "ranges": ranges}
        # Each access gets its own disposition even when recovery parsing lost
        # the enclosing function, or that function already has another issue.
        for unit in loop_index_units(project, self.file):
            self.units[unit["function_id"]] = unit
        self.diagnostic_reviews = {}
        self.function_reviews = {}
        self.conclusion = None
        self.withdrawals = {}
        self.ignored_records = []
        self.followup_performed = False
        self.recheck_pending = set()
        self.program_units = {}
        for key, unit in self.units.items():
            matches = [claim for claim in self.resolved_claims.values()
                if unit.get('kind') == 'loop_index' and claim['file'] == self.file
                and claim['line'] == unit['ranges'][0][0]
                and claim['expression'].strip() == unit['access'].strip()]
            if len(matches) == 1:
                claim = matches[0]
                self.program_units[key] = claim['access_id']
                self.function_reviews[key] = {'function_id': key,
                    'verdict': 'issue_found' if claim['verdict'] == 'VIOLATION' else 'no_issue_found',
                    'reason': '程序条件判定：' + claim['verdict'] + '；仅覆盖此处列表边界，前提见程序证据。',
                    'record_origin': 'deterministic_analysis', 'access_id': claim['access_id']}

    def plan(self):
        return {"diagnostics": [{"diagnostic_id": key, "line": d["line"], "rule": d["rule"]}
                                for key, d in self.diagnostics.items()],
                "functions": [unit for key, unit in self.units.items() if key not in self.program_units],
                "program_resolved_units": [{'function_id': key, 'access_id': access_id}
                                           for key, access_id in self.program_units.items()],
                "checks": ["名称与类型、运算符的操作数是否兼容", "边界与空输入", "状态共享与返回值",
                           "安全检查是否可绕过、输入是否可信", "注释中的业务约定、条件边界"]}

    def schema(self):
        pending = self.pending()

        def entries(key, ids, verdicts, required):
            # Focus constrained retries on the missing records. When there are
            # no missing records, keep optional corrections to known IDs open.
            choices = list(required) or list(ids)
            identity = {"type": "string"}
            if choices:
                identity["enum"] = choices
            return {"type": "array", "minItems": len(required), "maxItems": len(choices),
                    "items": {"type": "object", "additionalProperties": False,
                    "properties": {key: identity, "verdict": {"type": "string", "enum": verdicts},
                                   "reason": {"type": "string"}},
                    "required": [key, "verdict", "reason"]}}
        ids = [f["finding_id"] for f in (self.visible_review() or {}).get("findings", [])
               if f.get('evidence_origin') != 'program_verified']
        model_units = {key: unit for key, unit in self.units.items() if key not in self.program_units}
        return {"diagnostic_reviews": entries("diagnostic_id", self.diagnostics, VERDICTS, pending["diagnostics"]),
                "function_reviews": entries("function_id", model_units, UNIT_VERDICTS, pending["functions"]),
                "withdraw_findings": {"type": "array", **({"maxItems": 0} if not ids else {}), "items": {"type": "object", "additionalProperties": False,
                    "properties": {"finding_id": {"type": "string", **({"enum": ids} if ids else {})},
                                   "reason": {"type": "string"}}, "required": ["finding_id", "reason"]}}}

    def accept(self, conclusion, arguments, context):
        """Accept incremental coverage only after validating the whole submission."""
        updates = []
        ignored = []
        for field, key, expected, verdicts in (
            ("diagnostic_reviews", "diagnostic_id", self.diagnostics, VERDICTS),
            ("function_reviews", "function_id", self.units, UNIT_VERDICTS),
        ):
            rows = arguments.get(field, [])
            if not isinstance(rows, list):
                raise ValueError(f"{field} 必须是数组")
            if not expected and rows:
                # There are no real targets to lose. Keep raw arguments in the
                # trace, but do not let invented objects create audit targets.
                ignored.append({"field": field, "count": len(rows), "reason": "本次没有此类审查对象，多余记录不作为证据"})
                continue
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != {key, "verdict", "reason"}:
                    raise ValueError(f"{field} 每项仅包含 {key}、verdict、reason；收到 {row!r}")
                if not isinstance(row[key], str) or row[key] not in expected:
                    raise ValueError(f"{field} 的 {key}={row[key]!r} 无效；有效 ID：{list(expected)}")
                if row[key] in seen:
                    raise ValueError(f"{field} 重复提交 {row[key]}，每项只提交一次")
                if row["verdict"] not in verdicts:
                    raise ValueError(f"{field} 的 {row[key]} 使用 verdict={row['verdict']!r}；允许值：{verdicts}")
                if not isinstance(row["reason"], str) or not row["reason"].strip():
                    raise ValueError(f"{field} 的 {row[key]} 需要非空 reason")
                seen.add(row[key])
                if row["verdict"] != "inconclusive":
                    ranges = (self.units[row[key]].get("context_ranges", self.units[row[key]]["ranges"]) if key == "function_id"
                              else [[expected[row[key]]["line"], expected[row[key]]["line"]]])
                    if any(not context.source_evidence(self.file, n)
                           for start, end in ranges for n in range(start, end + 1)):
                        raise ValueError(f"{row[key]} 源码尚未完整提供，请读取剩余源码或标记 inconclusive")
                updates.append((field, deepcopy(row)))
        merged = self.merge_findings(conclusion)
        withdrawals = dict(self.withdrawals)
        withdrawal_rows = arguments.get("withdraw_findings", [])
        if not isinstance(withdrawal_rows, list):
            raise ValueError("withdraw_findings 必须为数组")
        known = {f["finding_id"] for f in merged["findings"]}
        for row in withdrawal_rows:
            if (not isinstance(row, dict) or set(row) != {"finding_id", "reason"}
                    or not isinstance(row["finding_id"], str) or row["finding_id"] not in known
                    or not isinstance(row["reason"], str) or not row["reason"].strip()):
                raise ValueError(f"撤回需要已有 finding_id 和非空 reason；有效 ID：{sorted(known)}")
            finding = next(f for f in merged['findings'] if f['finding_id'] == row['finding_id'])
            if finding.get('evidence_origin') == 'program_verified' or any(
                    phrase in row['reason'] for phrase in ('无需撤回', '不应撤回', '仍有效', '继续保留')):
                self.blocked_withdrawals.append({**deepcopy(row),
                    'reason_blocked': '撤回操作与程序验证或“保留”理由矛盾，保留已有问题；不能撤回程序风险。'})
                continue
            withdrawals[row["finding_id"]] = row["reason"]
        diagnostic_reviews = {**self.diagnostic_reviews, **{row["diagnostic_id"]: row for field, row in updates if field == "diagnostic_reviews"}}
        submitted_functions = {row["function_id"] for field, row in updates if field == "function_reviews"}
        function_reviews = deepcopy(self.function_reviews)
        function_reviews.update({row["function_id"]: row for field, row in updates if field == "function_reviews"})
        # Reconcile retained records too: incremental submissions can revise a
        # diagnostic or a finding without resubmitting every dependent function.
        for key, row in list(function_reviews.items()):
            if key in self.program_units:
                function_reviews[key] = deepcopy(self.function_reviews[key])
                continue
            ranges = self.units[key]["ranges"]
            has_finding = any(
                f["finding_id"] not in withdrawals and f["file"] == self.file
                and any(start <= f["line"] <= end for start, end in ranges)
                for f in merged["findings"]
            )
            program_support = [claim['access_id'] for claim in self.resolved_claims.values()
                               if claim['file'] == self.file and claim['verdict'] == 'VIOLATION'
                               and any(a <= claim['line'] <= b for a, b in ranges)]
            row.pop('basis_program', None)
            if program_support:
                row['basis_program'] = program_support
            supported = [identity for identity, diagnostic in self.diagnostics.items()
                         if diagnostic_reviews.get(identity, {}).get("verdict") == "likely_issue"
                         and any(a <= diagnostic["line"] <= b for a, b in ranges)]
            # Never retain references to diagnostics whose opinion has changed.
            row.pop("basis_diagnostics", None)
            if row["verdict"] == "issue_found" and not has_finding:
                if program_support:
                    pass  # Program-supported risk does not require a duplicate model finding.
                elif supported:
                    row["basis_diagnostics"] = supported
                elif key not in submitted_functions:
                    del function_reviews[key]  # Missing record re-enters pending().
                    continue
                else:
                    row.update(verdict="inconclusive", record_origin="program_consistency_check",
                               reason="程序一致性检查：模型标记发现问题，但没有函数内的问题位置或认可的静态诊断；保留不确定性。原意见：" + row["reason"])
            if row["verdict"] == "no_issue_found" and (has_finding or supported or program_support):
                if key not in submitted_functions:
                    del function_reviews[key]
                    continue
                row.update(verdict="inconclusive", record_origin="program_consistency_check",
                           reason="程序一致性检查：模型的无问题记录与其问题清单或认可的静态诊断矛盾；保留已有发现并标为待确认。原意见：" + row["reason"])
        self.conclusion = merged
        self.withdrawals = withdrawals
        self.ignored_records.extend(ignored)
        self.diagnostic_reviews = diagnostic_reviews
        self.function_reviews = function_reviews
        self.recheck_pending.difference_update(submitted_functions)

    def merge_findings(self, conclusion):
        """Merge already source-validated findings independently of checklist validity."""
        conclusion = self.reconcile_findings(conclusion)
        merged = deepcopy(self.conclusion or conclusion)
        existing = {(f["file"], f["line"], f["title"]): f for f in merged["findings"]}
        for finding in conclusion["findings"]:
            key = (finding["file"], finding["line"], finding["title"])
            if key in existing:
                # A validated recheck may correct conditions or advice. Keep
                # the stable identity and the original submission in the trace.
                identity = existing[key].get("finding_id")
                existing[key].update(deepcopy(finding))
                if identity:
                    existing[key]["finding_id"] = identity
            else:
                saved = deepcopy(finding)
                merged["findings"].append(saved)
                existing[key] = saved
        merged["missing_evidence"] = list(dict.fromkeys(merged["missing_evidence"] + conclusion["missing_evidence"]))
        merged["evidence_ids"] = list(dict.fromkeys(merged["evidence_ids"] + conclusion["evidence_ids"]))
        merged["verdict"] = "likely_issue" if merged["findings"] else conclusion["verdict"]
        for number, finding in enumerate(merged["findings"], 1):
            finding.setdefault("finding_id", f"F{number}")
        merged["summary"] = f"逐项审查累计列出 {len(merged['findings'])} 个问题；详见问题与审查记录。"
        return merged

    def reconcile_findings(self, conclusion):
        """Resolve only a bound property at one exact, current verified access."""
        result = deepcopy(conclusion)
        retained = []
        for finding in result['findings']:
            if finding.get('evidence_origin') == 'program_verified':
                retained.append(finding)
                continue
            kind = finding.get('issue_kind')
            # Old saved clients lack typed references. Such observations remain
            # visible and receive conflict feedback rather than automatic deletion.
            claim = self.resolved_claims.get(finding.get('access_id')) if kind == 'index_bounds' else None
            if claim is None or claim['file'] != finding['file'] or claim['line'] != finding['line']:
                retained.append(finding)
                continue
            decision = {'original': deepcopy(finding), 'access_id': claim['access_id'],
                'verdict': claim['verdict'], 'assumptions': claim.get('assumptions', []),
                'resolution': 'safe_under_assumptions' if claim['verdict'] == 'SAFE' else 'condition_from_program_witness'}
            if decision not in self.program_reconciliations:
                self.program_reconciliations.append(decision)
            if claim['verdict'] == 'SAFE':
                continue
            witness = claim.get('witness')
            if not witness:
                retained.append(finding)
                continue
            length, index = witness['length'], witness['index']
            condition = f"在程序列明的前提下，列表长度={length}，下标={index}"
            if 'counter' in witness:
                condition += f"，循环变量={witness['counter']}"
            evidence_type = '有界 AST 路径见证' if witness.get('kind') == 'bounded_ast_counterexample' else '符号见证'
            if 'entry_inputs' in witness:
                condition += f"；入口参数={witness['entry_inputs']}；长度为执行到该访问时的长度"
            retained.append({**finding, 'title': '列表下标存在条件越界',
                'explanation': f"程序规则为 {claim['expression']} 给出{evidence_type}：下标 {index} 不在 [{-length}, {length}) 内；这不是动态执行复现。",
                'condition': condition + '。',
                'suggestion': '核对循环范围与下标偏移，确保执行访问时下标满足 -len(列表) <= 下标 < len(列表)。',
                'evidence_origin': 'program_verified', 'program_access_id': claim['access_id']})
        result['findings'] = retained
        if not retained:
            result['verdict'] = 'inconclusive'
        return result

    def visible_review(self):
        if self.conclusion is None:
            return None
        result = deepcopy(self.conclusion)
        result["withdrawn_findings"] = [{**f, "withdraw_reason": self.withdrawals[f["finding_id"]]}
                                        for f in result["findings"] if f["finding_id"] in self.withdrawals]
        result["findings"] = [f for f in result["findings"] if f["finding_id"] not in self.withdrawals]
        result["verdict"] = "likely_issue" if result["findings"] else "inconclusive"
        result["summary"] = f"逐项审查列出 {len(result['findings'])} 个问题，复核撤回 {len(result['withdrawn_findings'])} 项；详见处理记录。"
        return result

    def pending(self):
        return {"diagnostics": [key for key in self.diagnostics if key not in self.diagnostic_reviews],
                "functions": [key for key in self.units if key not in self.function_reviews or key in self.recheck_pending]}

    def begin_followup(self):
        if not self.followup_performed:
            self.recheck_pending.update((self.followup() or {}).get("recheck_functions", []))
        self.followup_performed = True

    def program_conflicts(self):
        conflicts = []
        for finding in (self.visible_review() or {}).get("findings", []):
            description = (finding.get("title", "") + " " + finding.get("explanation", "")).lower()
            if not any(word in description for word in ("越界", "indexerror", "out of bounds", "安全索引", "边界检查充分")):
                continue
            claims = [c for c in (self.program_analysis or {}).get("claims", [])
                      if c["file"] == finding["file"] and c["line"] == finding["line"]]
            if len(claims) == 1 and claims[0]["verdict"] == "SAFE":
                conflicts.append({"finding_id": finding["finding_id"], "access_id": claims[0]["access_id"],
                                  "reason": "模型报告越界，但程序在列出的前提下判定该访问安全；请核对前提或撤回误报。",
                                  "assumptions": claims[0].get("assumptions", []),
                                  "derivation": claims[0].get("derivation", {})})
        return conflicts

    def followup(self):
        pending = self.pending()
        if self.evidence_complete:
            # Evidence-guided review repeats unresolved work, not every completed
            # function. A verified subscript never certifies the whole function.
            recheck = [] if self.followup_performed else [key for key, row in self.function_reviews.items()
                if key not in self.program_units and row['verdict'] == 'inconclusive']
            verify = [] if self.followup_performed else [finding for finding in (self.visible_review() or {}).get('findings', [])
                if finding.get('evidence_origin') != 'program_verified']
        else:
            recheck = [] if self.followup_performed else [key for key, row in self.function_reviews.items()
                if key not in self.program_units and (row['verdict'] != 'issue_found' or self.units[key].get('kind') == 'loop_index')]
            verify = [] if self.followup_performed else [finding for finding in (self.visible_review() or {}).get('findings', [])
                if finding.get('evidence_origin') != 'program_verified']
        if not any(pending.values()) and not recheck and not verify:
            return None
        return {"program_conflicts": self.program_conflicts(),
                "conflict_instructions": "program_conflicts 列出了模型与程序的条件结论冲突。逐一核对；如果没有具体不同前提，"
                                         "将误报 finding_id 填入 withdraw_findings 撤回。安全说明不应作为新问题放入 findings。",
                "task": "补查尚未逐项处理的诊断和函数；对无问题结论再核对一次。已有 findings 保留，只提交新增问题。"
                        "同时复核已有问题，位置校验不证明判断正确。将误判填入 withdraw_findings（finding_id、reason）撤回。"
                        "不能凭空要求函数接受任意错误类型，也不能把正常的显式容器修改或空结果当成漏洞。"
                        "逐个核对操作数类型、空输入、状态和安全约定。循环下标任务须独立计算首次/末次索引与偏移，"
                        "检查保护条件和容器变化，不能只重复其他报警。对于越界意见，写出实际索引与长度的比较："
                        "len(a)-1 是合法最大正索引，len(a) 才越过正向边界；range(len(a)) 中 a[i] 在长度不变时合法。"
                        "撤回把合法边界或切片误判为越界的发现。也独立检查实际调用的元素类型和更早的阻断错误。"
                        "用简短 reason 记录结果；不能用未读源码得出无问题结论。",
                "pending": pending, "recheck_functions": recheck,
                "verify_findings": verify,
                "requested_function_records": [self.units[key] for key in dict.fromkeys(pending["functions"] + recheck)],
                "submission_rules": "返回 findings、diagnostic_reviews、function_reviews 等提交字段；"
                                    "审查记录可只包含本次补充的条目，ID 必须来自 audit_plan。"
                                    "无新增问题时 findings=[]，verdict=inconclusive，不要重复已有问题。"}

    def report(self):
        pending = self.pending()
        return {**({"program_analysis": self.program_analysis, "program_conflicts": self.program_conflicts()}
                    if self.program_analysis is not None else {}),
                "diagnostic_reviews": list(self.diagnostic_reviews.values()),
                "function_reviews": list(self.function_reviews.values()),
                "diagnostic_total": len(self.diagnostics), "function_total": len(self.units),
                "loop_index_total": sum(unit.get("kind") == "loop_index" for unit in self.units.values()),
                "units": list(self.units.values()),
                "pending": pending, "complete": not any(pending.values()) and self.followup() is None,
                "followup_performed": self.followup_performed,
                "program_reconciliations": self.program_reconciliations,
                "blocked_withdrawals": self.blocked_withdrawals,
                "program_resolved_units": self.program_units,
                "withdrawn_findings": [{"finding_id": key, "reason": value} for key, value in self.withdrawals.items()],
                "ignored_records": self.ignored_records,
                "limitation": "记录仅证明已逐项提交处理意见，不证明判断正确或没有遗漏。"}
