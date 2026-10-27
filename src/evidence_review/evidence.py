"""Evidence ledger and bounded acquisition policies over deterministic tools."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import time

from .knowledge import LIST_BOUNDS
from .knowledge.python_subscript import PythonBounds

# Relative acquisition costs, not measured seconds or calibrated probabilities.
COSTS = {"bindings": 1, "container": 1, "domain": 1, "guards": 1, "effects": 2, "reachability": 2, "counterexample": 8}
FIXED_ORDER = ["domain", "guards", "effects", "reachability", "bindings", "container"]


class EvidenceLedger:
    def __init__(self, hashes):
        self.hashes, self.facts, self.results = dict(hashes), {}, {}

    def add(self, access, fact):
        if (fact.get("origin") != "deterministic_analysis" or fact.get("access_id") != access["access_id"]
                or fact.get("file") != access["file"] or fact.get("source_hash") != self.hashes[access["file"]]):
            raise ValueError("拒绝非程序事实、错误对象或过期源码证据")
        saved = deepcopy(fact)
        saved["fact_id"] = hashlib.sha256(json.dumps(saved, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
        bucket = self.facts.setdefault(access["access_id"], {})
        if bucket.get(saved["kind"]) != saved:
            self.results.pop(access["access_id"], None)
        bucket[saved["kind"]] = saved
        return saved

    def view(self, access):
        return deepcopy(self.facts.get(access["access_id"], {}))

    def record(self, access, result):
        self.results[access["access_id"]] = {**deepcopy(result), "depends_on": [f["fact_id"] for f in self.view(access).values()]}


def choose_action(missing, policy):
    if policy == "fixed":
        return next(kind for kind in FIXED_ORDER + sorted(missing) if kind in missing)
    if policy == "gaps":
        return missing[0]
    # Priority weights describe decisive obligations, not model-reported confidence.
    weights = {"bindings": 3, "container": 3, "domain": 2, "guards": 1, "effects": 2, "reachability": 1}
    return min(missing, key=lambda k: (-weights.get(k, 3) / COSTS.get(k, 3), k))


def analyze_evidence(project, files=None, policy="cost", max_actions=100, max_cost=200, timeout=30, verify=True, access_ids=None):
    if policy not in {"rules", "fixed", "gaps", "cost"}:
        raise ValueError("证据策略必须为 rules/fixed/gaps/cost")
    if max_actions < 0 or max_cost < 0 or timeout <= 0:
        raise ValueError("分析预算无效")
    started = time.monotonic()
    extractor = PythonBounds(project, files)
    extractor.allow_counterexample_helpers = policy != "rules"
    if access_ids is not None:
        extractor.accesses = [a for a in extractor.accesses if a["access_id"] in access_ids]
    ledger = EvidenceLedger(project.hashes())
    trace, spent, attempts = [], 0, 0
    queue = list(extractor.accesses)
    stopped = {}
    while queue:
        access = queue.pop(0)
        facts = ledger.view(access)
        result = extractor.verify(access, facts)
        ledger.record(access, result)
        if result["terminal"]:
            stopped[access["access_id"]] = "resolved" if result["verdict"] != "UNDECIDED" else "unsupported"
            continue
        if time.monotonic() - started >= timeout:
            stopped[access["access_id"]] = "timeout"
            continue
        if policy == "rules":
            # Pure local-rule baseline: no follow-up into helper implementations.
            missing = [k for k in result["missing"] if not k.startswith("call:")]
            if not missing:
                stopped[access["access_id"]] = "local_rules_only"
                continue
        else:
            missing = result["missing"]
        action = choose_action(missing, "fixed" if policy == "rules" else policy)
        cost = COSTS.get(action, 3)
        if attempts >= max_actions or spent + cost > max_cost:
            stopped[access["access_id"]] = "budget_exhausted"
            continue
        event = {"access_id": access["access_id"], "tool": action, "missing_before": result["missing"],
                 "estimated_cost": cost, "selection_reason": f"{policy}: 补充 {action} 证据"}
        tick = time.monotonic()
        attempts += 1
        spent += cost
        try:
            fact = extractor.collect(access, action)
            saved = ledger.add(access, fact)
            event.update(fact_id=saved["fact_id"], status=saved["status"], reason=saved.get("reason"))
        except (ValueError, TypeError, KeyError) as exc:
            event.update(status="error", reason=str(exc))
            stopped[access["access_id"]] = "tool_error"
        event["elapsed_seconds"] = round(time.monotonic() - tick, 6)
        trace.append(event)
        if access["access_id"] not in stopped:
            queue.append(access)  # A fair pass over every access before deeper acquisition.
    claims = []
    for access in extractor.accesses:
        result = ledger.results[access["access_id"]]
        # Re-evaluate after final action, even if the budget was spent exactly.
        if stopped[access["access_id"]] != "tool_error":
            result = extractor.verify(access, ledger.view(access))
            ledger.record(access, result)
            result = ledger.results[access["access_id"]]
        if not verify:
            result = {**result, "verdict": "UNDECIDED", "verification_result": "unknown",
                      "reason": "verification_disabled", "terminal": False}
            result.pop("witness", None)
            result.pop("derivation", None)
        claims.append({**access, **result, "stop_reason": stopped[access["access_id"]],
                       "facts": list(ledger.view(access).values())})
    counts = dict(Counter(c["verification_result"] for c in claims))
    return {"schema_version": 1, "rule": LIST_BOUNDS, "policy": policy, "verification_enabled": verify,
            "claims": claims, "trace": trace, "parse_errors": extractor.errors,
            "source_hashes": project.hashes(), "selected_files": list(files) if files is not None else list(project.sources),
            "summary": {"accesses": len(claims), "results": counts, "actions": attempts, "estimated_cost": spent,
                        "elapsed_seconds": round(time.monotonic() - started, 6),
                        "model_requests": 0, "model_tokens": None,
                        "budget_exhausted": sum(c["stop_reason"] == "budget_exhausted" for c in claims)},
            "limitations": ["支持有限 Python 普通列表规则与有界 AST 反例搜索；不执行目标源码，搜索失败不能证明安全。",
                            "参数类型、运行环境与路径假设逐项列出；未知调用和不支持结构保持不确定。",
                            "cost 为可解释启发式成本策略，未实现训练式收益预测或组合动作搜索。"]}


def candidate_analysis(analysis, candidate, project):
    if not analysis:
        return None
    function = project.functions.get(candidate.get("function_id"))
    return {"policy": analysis["policy"], "claims": [c for c in analysis["claims"] if c["file"] == candidate["file"]
            and (candidate.get("scope") == "file" or function and function.start <= c["line"] <= function.end
                 or not function and c["line"] == candidate["line"])],
            "limitations": analysis["limitations"]}


def access_candidates(project, candidates, accesses):
    """Add no-alarm accesses without changing scanner diagnostics or RRF scores."""
    items = {c['id']: deepcopy(c) for c in candidates}
    for access in accesses:
        function = project.at(access['file'], access['line'])
        identity = function.id if function else f"{access['file']}:{access['line']}"
        items.setdefault(identity, {"id": identity, "function_id": function.id if function else None,
            "file": access['file'], "line": access['line'], "language": "python",
            "name": function.name if function else "<下标访问>", "diagnostics": [], "sbfl_lines": [],
            "sbfl_score": 0.0, "priority": 0.0})
    return sorted(items.values(), key=lambda c: (-c['priority'], c['id']))


def model_evidence(analysis):
    """Send conclusions and their assumptions once; keep full facts in the report."""
    if analysis is None:
        return None
    keys = ('access_id', 'rule', 'file', 'line', 'column', 'expression', 'source_hash',
            'verdict', 'verification_result', 'reason', 'assumptions', 'derivation',
            'witness', 'missing', 'stop_reason')
    return {'claims': [{key: deepcopy(claim[key]) for key in keys if key in claim}
                       for claim in analysis.get('claims', [])],
            'limitations': analysis.get('limitations', []),
            'submission_policy': '程序已判定的列表边界由程序负责。不要把安全说明列为问题；'
                '新增问题填写 issue_kind，边界问题绑定 access_id，其他问题 access_id=null。'
                '仍须独立检查函数内的名称、类型、算术、状态和安全问题。'
                '函数包含程序 VIOLATION 时 function_reviews 可标 issue_found，不必重复生成该边界问题。'}
