"""Conservative Python list bounds rules. Never import or execute target code.

Parameter annotations are assumptions. Unknown effects/control flow prevent affine proof;
a separately budgeted closed AST search can establish a concrete counterexample.
All offsets use UTF-8 bytes and refer to Project's immutable input snapshot.
"""
from __future__ import annotations

import ast
import hashlib

from . import LIST_BOUNDS


def walk_scope(node):
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from walk_scope(child)


def integer(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = integer(node.operand)
        return -value if value is not None else None
    return None


def affine(node, symbol):
    if isinstance(node, ast.Name) and node.id == symbol:
        return 0
    if isinstance(node, ast.BinOp) and isinstance(node.left, ast.Name) and node.left.id == symbol:
        value = integer(node.right)
        if value is not None and isinstance(node.op, (ast.Add, ast.Sub)):
            return value if isinstance(node.op, ast.Add) else -value
    return None


def length(node, symbol):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len"
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.Name) and node.args[0].id == symbol)


class PythonBounds:
    def __init__(self, project, files=None):
        self.project = project
        self.allow_counterexample_helpers = True
        self.accesses, self.nodes, self.parents, self.modules = [], {}, {}, {}
        self.errors = []
        for file in files if files is not None else project.sources:
            if project.languages[file] != "python":
                continue
            source = project.sources[file]
            try:
                decoded = source.decode('utf-8-sig')
                module = ast.parse(source)
                # Context errors (e.g. break outside a loop) survive ast.parse.
                # Compile only for validation; never execute the resulting code.
                compile(module, file, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, UnicodeError) as exc:
                self.errors.append({"file": file, "reason": "syntax_error", "message": str(exc)})
                continue
            self.modules[file] = module
            self.parents[file] = {child: parent for parent in ast.walk(module) for child in ast.iter_child_nodes(parent)}
            offsets = [0]
            for line in source.splitlines(keepends=True):
                offsets.append(offsets[-1] + len(line))
            annotation_nodes = set()
            for item in ast.walk(module):
                for annotation in (getattr(item, "annotation", None), getattr(item, "returns", None)):
                    if annotation is not None:
                        annotation_nodes.update(ast.walk(annotation))
            for node in ast.walk(module):
                if not isinstance(node, ast.Subscript) or node in annotation_nodes:
                    continue
                bom = 3 if source.startswith(b'\xef\xbb\xbf') else 0
                start = offsets[node.lineno - 1] + node.col_offset + (bom if node.lineno == 1 else 0)
                end = offsets[node.end_lineno - 1] + node.end_col_offset + (bom if node.end_lineno == 1 else 0)
                digest = project.hashes()[file]
                identity = hashlib.sha256(f"{file}:{digest}:{start}:{end}:v1".encode()).hexdigest()[:20]
                access = {"access_id": identity, "rule": LIST_BOUNDS["id"], "file": file,
                          "line": node.lineno, "column": node.col_offset + 1,
                          "end_line": node.end_lineno, "end_column": node.end_col_offset + 1,
                          "start_byte": start, "end_byte": end, "source_hash": digest,
                          "expression": ast.get_source_segment(decoded, node) or ast.unparse(node)}
                self.nodes[identity] = node
                self.accesses.append(access)
        self.accesses.sort(key=lambda a: (a["file"], a["start_byte"]))

    def ancestors(self, access):
        node = self.nodes[access["access_id"]]
        chain = []
        while node in self.parents[access["file"]]:
            node = self.parents[access["file"]][node]
            chain.append(node)
        return chain

    def scope(self, access):
        return next((n for n in self.ancestors(access) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))),
                    self.modules[access["file"]])

    def guard_inputs(self, access):
        """Conditions dominating this access, plus simple earlier exits.

        Only an if containing a single return/continue with no else is an exit
        guard. Statements before the exit may themselves contain accesses.
        """
        node = self.nodes[access["access_id"]]
        conditions, exits = [], set()
        chain = self.ancestors(access)
        passed_iteration = False
        for ancestor in chain:
            if isinstance(ancestor, (ast.For, ast.ListComp)):
                passed_iteration = True
            if isinstance(ancestor, ast.If):
                if passed_iteration:
                    return [], set(), True
                if node not in {n for statement in ancestor.body for n in ast.walk(statement)}:
                    return [], set(), True
                conditions.append((ancestor.test, False))
            elif isinstance(ancestor, ast.ListComp) and len(ancestor.generators) == 1:
                conditions.extend((test, False) for test in ancestor.generators[0].ifs)
            if ancestor is self.scope(access):
                break
        child = node
        parents = self.parents[access["file"]]
        while child in parents:
            parent = parents[child]
            for field in ("body", "orelse"):
                block = getattr(parent, field, [])
                if not isinstance(block, list) or child not in block:
                    continue
                for statement in block[:block.index(child)]:
                    if (isinstance(statement, ast.If) and not statement.orelse and len(statement.body) == 1
                            and isinstance(statement.body[0], (ast.Return, ast.Continue))):
                        conditions.append((statement.test, True))
                        exits.add(statement.body[0])
            if parent is self.scope(access) or isinstance(parent, (ast.For, ast.ListComp)):
                break
            child = parent
        return conditions, exits, False

    def guard_atoms(self, test, negate=False):
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return self.guard_atoms(test.operand, not negate)
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) and not negate:
            atoms = [self.guard_atoms(value) for value in test.values]
            return None if any(part is None for part in atoms) else [atom for part in atoms for atom in part]
        if not isinstance(test, ast.Compare) or negate and len(test.ops) != 1:
            return None
        invert = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt}
        pairs = zip([test.left, *test.comparators[:-1]], test.ops, test.comparators)
        result = []
        for left, op, right in pairs:
            if type(op) not in invert:
                return None
            result.append((left, invert[type(op)]() if negate else op, right))
        return result

    def guard_property(self, access, atom):
        """Accept only guards directly establishing an integer list bound."""
        node = self.nodes[access["access_id"]]
        if not isinstance(node.value, ast.Name):
            return None
        container = node.value.id
        left, op, right = atom
        reverse = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE}
        if ast.dump(right) == ast.dump(node.slice):
            left, right, op = right, left, reverse[type(op)]()
        if ast.dump(left) != ast.dump(node.slice):
            return None
        if isinstance(op, ast.Lt) and length(right, container):
            return "upper"
        if (isinstance(op, ast.LtE) and isinstance(right, ast.BinOp) and isinstance(right.op, ast.Sub)
                and length(right.left, container) and integer(right.right) == 1):
            return "upper"
        if isinstance(op, ast.GtE) and (integer(right) == 0 or
                isinstance(right, ast.UnaryOp) and isinstance(right.op, ast.USub) and length(right.operand, container)):
            return "lower"
        return None

    def guard_summary(self, access):
        conditions, exits, else_path = self.guard_inputs(access)
        properties, supported_nodes = [], set()
        unsupported = else_path
        for test, negate in conditions:
            atoms = self.guard_atoms(test, negate)
            kinds = [self.guard_property(access, atom) for atom in atoms] if atoms is not None else [None]
            if any(kind is None for kind in kinds):
                unsupported = True
            else:
                properties.extend(kinds)
                supported_nodes.update(ast.walk(test))
        return {"upper_guard": "upper" in properties, "lower_guard": "lower" in properties,
                "unsupported_guard": unsupported,
                "conditions": [ast.dump(test) + (" [negated exit]" if negate else "") for test, negate in conditions]}, exits, supported_nodes

    def fact(self, access, kind, status="supported", **data):
        return {"kind": kind, "status": status, "origin": "deterministic_analysis",
                "source_hash": access["source_hash"], "file": access["file"],
                "access_id": access["access_id"], **data}

    def collect(self, access, kind):
        node = self.nodes[access["access_id"]]
        scope = self.scope(access)
        container = node.value.id if isinstance(node.value, ast.Name) else None
        chain = self.ancestors(access)
        if kind == "counterexample":
            from .python_witness import search
            result = search(self.modules[access["file"]], scope, node, allow_helpers=self.allow_counterexample_helpers)
            status = result.pop("status")
            return self.fact(access, kind, status, **result)
        if kind == "bindings":
            scopes = [scope, self.modules[access["file"]]]
            for region in scopes:
                for item in walk_scope(region):
                    if (isinstance(item, (ast.Name, ast.arg)) and
                        ((isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)) and item.id in {"len", "range"})
                         or isinstance(item, ast.arg) and item.arg in {"len", "range"})):
                        return self.fact(access, kind, "unsupported", reason="shadowed_builtin")
                    if isinstance(item, (ast.Import, ast.ImportFrom)):
                        for alias in item.names:
                            if alias.name == "*" or (alias.asname or alias.name.split('.')[0]) in {"range", "len"}:
                                return self.fact(access, kind, "unsupported", reason="shadowed_builtin")
                if any(isinstance(item, (ast.FunctionDef, ast.ClassDef)) and item.name in {"len", "range"}
                       for item in getattr(region, "body", [])):
                    return self.fact(access, kind, "unsupported", reason="shadowed_builtin")
            if any(isinstance(n, (ast.ClassDef, ast.Lambda, ast.AsyncFunctionDef)) or
                   isinstance(n, ast.FunctionDef) and n is not scope for n in chain):
                return self.fact(access, kind, "unsupported", reason="unsupported_scope")
            return self.fact(access, kind, assumptions=["标准 Python 内置 len/range 未被运行环境动态替换"])
        if kind == "container":
            if not container or isinstance(node.slice, ast.Slice):
                return self.fact(access, kind, "unsupported", reason="non_list_or_slice")
            for arg in getattr(getattr(scope, "args", None), "posonlyargs", []) + getattr(getattr(scope, "args", None), "args", []) + getattr(getattr(scope, "args", None), "kwonlyargs", []):
                annotation = arg.annotation
                if arg.arg == container and (isinstance(annotation, ast.Name) and annotation.id == "list" or
                    isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name) and annotation.value.id == "list"):
                    return self.fact(access, kind, assumptions=[f"{container} 在此路径按普通内置 list 处理；类型注解不是运行时保证"])
            for item in getattr(scope, 'body', []):
                if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.lineno < node.lineno:
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    if any(isinstance(t, ast.Name) and t.id == container for t in targets) and isinstance(item.value, ast.List):
                        if all(isinstance(e, ast.Constant) for e in item.value.elts):
                            return self.fact(access, kind, initial_length=len(item.value.elts), assumptions=[])
            return self.fact(access, kind, "unsupported", reason="container_type_unknown")
        if kind == "domain":
            context = next((n for n in chain if isinstance(n, (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp))), None)
            if isinstance(context, ast.For):
                target, iterator = context.target, context.iter
            elif isinstance(context, ast.ListComp) and len(context.generators) == 1 and not context.generators[0].is_async:
                target, iterator = context.generators[0].target, context.generators[0].iter
                if node not in set(ast.walk(context.elt)):
                    return self.fact(access, kind, "unsupported", reason="access_in_comprehension_filter")
            else:
                return self.fact(access, kind, "unsupported", reason="unsupported_iteration")
            if not (isinstance(target, ast.Name) and isinstance(iterator, ast.Call) and isinstance(iterator.func, ast.Name)
                    and iterator.func.id == "range" and len(iterator.args) == 1 and not iterator.keywords):
                return self.fact(access, kind, "unsupported", reason="unsupported_range")
            stop, trim = iterator.args[0], 0
            if isinstance(stop, ast.BinOp) and isinstance(stop.op, ast.Sub) and integer(stop.right) is not None:
                trim, stop = integer(stop.right), stop.left
            if trim < 0 or not length(stop, container):
                return self.fact(access, kind, "unsupported", reason="unrelated_or_unsupported_length")
            offset = affine(node.slice, target.id)
            constant = integer(node.slice)
            if offset is None and constant is None:
                return self.fact(access, kind, "unsupported", reason="unsupported_index")
            if isinstance(context, ast.For):
                if node not in {n for statement in context.body for n in ast.walk(statement)}:
                    return self.fact(access, kind, "unsupported", reason="access_outside_loop_body")
                for statement in context.body:
                    for item in ast.walk(statement):
                        if isinstance(item, ast.Name) and item.id == target.id and isinstance(item.ctx, (ast.Store, ast.Del)):
                            return self.fact(access, kind, "unsupported", reason="index_reassigned")
            return self.fact(access, kind, trim=trim, offset=offset, constant=constant, counter=target.id,
                             context_line=context.lineno)
        if kind == "guards":
            summary, _, _ = self.guard_summary(access)
            return self.fact(access, kind, **summary)
        if kind == "effects":
            calls = []
            writes = [n for n in walk_scope(scope) if isinstance(n, (ast.Assign, ast.AnnAssign))
                      and any(isinstance(t, ast.Name) and t.id == container for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
            if len(writes) > 1 or any(n not in getattr(scope, 'body', []) for n in writes):
                return self.fact(access, kind, "unsupported", reason="container_rebound")
            for item in walk_scope(scope):
                if isinstance(item, ast.Name) and item.id == container and isinstance(item.ctx, (ast.Store, ast.Del)):
                    parent = self.parents[access["file"]].get(item)
                    if not isinstance(parent, (ast.Assign, ast.AnnAssign)):
                        return self.fact(access, kind, "unsupported", reason="container_rebound")
                if isinstance(item, (ast.Import, ast.ImportFrom)) and any(
                        alias.name == "*" or (alias.asname or alias.name.split('.')[0]) == container for alias in item.names):
                    return self.fact(access, kind, "unsupported", reason="container_rebound")
                if isinstance(item, (ast.Global, ast.Nonlocal, ast.NamedExpr, ast.Delete, ast.AugAssign)):
                    return self.fact(access, kind, "unsupported", reason="mutation_or_dynamic_binding")
                if isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id == container:
                            if not (item.lineno < node.lineno and isinstance(item.value, ast.List)
                                    and all(isinstance(e, ast.Constant) for e in item.value.elts)):
                                return self.fact(access, kind, "unsupported", reason="container_rebound")
                        elif not isinstance(target, ast.Name):
                            return self.fact(access, kind, "unsupported", reason="complex_assignment_or_write")
                    if item.value and any(isinstance(n, ast.Name) and n.id == container for n in ast.walk(item.value)):
                        # Reading an element is not aliasing the list itself.
                        if isinstance(item.value, ast.Name):
                            return self.fact(access, kind, "unsupported", reason="container_alias")
                if not isinstance(item, ast.Call):
                    continue
                if isinstance(item.func, ast.Name) and item.func.id in {"len", "range"}:
                    continue
                if isinstance(item.func, ast.Attribute):
                    if isinstance(item.func.value, ast.Name) and item.func.value.id == container:
                        return self.fact(access, kind, "unsupported", reason="container_method_call")
                    receiver = item.func.value.id if isinstance(item.func.value, ast.Name) else None
                    initializers = [s for s in getattr(scope, "body", []) if isinstance(s, ast.Assign)
                                    and any(isinstance(t, ast.Name) and t.id == receiver for t in s.targets)]
                    stores = [n for n in walk_scope(scope) if isinstance(n, ast.Name) and n.id == receiver and isinstance(n.ctx, ast.Store)]
                    if (item.func.attr == "append" and receiver != container and len(initializers) == 1 and len(stores) == 1
                            and initializers[0].lineno < item.lineno and isinstance(initializers[0].value, ast.List)
                            and not initializers[0].value.elts):
                        continue
                    return self.fact(access, kind, "unsupported", reason="unknown_method_effect")
                calls.append(ast.unparse(item.func))
            return self.fact(access, kind, calls=list(dict.fromkeys(calls)))
        if kind.startswith("call:"):
            name = kind.split(':', 1)[1]
            matches = [n for n in self.modules[access["file"]].body if isinstance(n, ast.FunctionDef) and n.name == name]
            if len(matches) != 1 or matches[0].decorator_list:
                return self.fact(access, kind, "unsupported", reason="unresolved_call")
            helper = matches[0]
            # Only a truly empty helper is certified; arbitrary effect summaries are not inferred by LLM.
            empty = all(isinstance(s, ast.Pass) or isinstance(s, ast.Return) and (s.value is None or isinstance(s.value, ast.Constant))
                        or isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str)
                        for s in helper.body)
            calls = [n for n in walk_scope(scope) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]
            simple_args = all(all(isinstance(a, (ast.Name, ast.Constant)) for a in c.args) and not c.keywords for c in calls)
            names = {n.id for n in walk_scope(scope) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            if isinstance(scope, ast.FunctionDef):
                names.update(a.arg for a in [*scope.args.args, *scope.args.posonlyargs, *scope.args.kwonlyargs])
            module_conflict = any(
                isinstance(n, (ast.Assign, ast.AnnAssign)) and name in {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}
                or isinstance(n, ast.ClassDef) and n.name == name
                or isinstance(n, (ast.Import, ast.ImportFrom)) and any(a.name == "*" or (a.asname or a.name.split('.')[0]) == name for a in n.names)
                for n in self.modules[access["file"]].body)
            local_conflict = any(isinstance(n, (ast.Import, ast.ImportFrom)) and any(a.name == "*" or (a.asname or a.name.split('.')[0]) == name for a in n.names)
                                 for n in walk_scope(scope))
            args = [*helper.args.posonlyargs, *helper.args.args]
            arity = all(len(c.args) == len(args) for c in calls) and not helper.args.kwonlyargs and not helper.args.vararg
            if not empty or not simple_args or not arity or name in names or module_conflict or local_conflict:
                return self.fact(access, kind, "unsupported", reason="unknown_call_effect")
            return self.fact(access, kind, assumptions=[f"{name} 调用绑定到当前文件的无副作用函数；无运行时替换"])
        if kind == "reachability":
            if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and n is not scope
                   for n in ast.walk(scope)):
                return self.fact(access, kind, "unsupported", reason="nested_scope_in_analysis_region")
            guards, early_exits, guard_nodes = self.guard_summary(access)
            if guards["unsupported_guard"]:
                early_exits, guard_nodes = set(), set()
            bad = (ast.Try, ast.With, ast.Raise, ast.Break, ast.While, ast.Match, ast.IfExp,
                   ast.AsyncFor, ast.Await, ast.Yield, ast.YieldFrom, ast.Assert)
            if any(isinstance(n, bad) or isinstance(n, ast.Continue) and n not in early_exits
                   or isinstance(n, ast.BoolOp) and n not in guard_nodes for n in walk_scope(scope)):
                return self.fact(access, kind, "unsupported", reason="unsupported_control_flow")
            if isinstance(scope, ast.FunctionDef) and scope.decorator_list:
                return self.fact(access, kind, "unsupported", reason="decorated_function")
            # Earlier unrelated expressions may fail. Do not certify their reachability.
            for item in walk_scope(scope):
                before = (getattr(item, "lineno", node.lineno), getattr(item, "col_offset", node.col_offset)) < (node.lineno, node.col_offset)
                if (isinstance(item, (ast.Return, ast.Expr, ast.Assign, ast.AnnAssign)) and before
                        and item not in chain and item not in early_exits):
                    if isinstance(item, ast.Return) or isinstance(item, ast.Expr) and not isinstance(item.value, (ast.Constant, ast.Call)):
                        return self.fact(access, kind, "unsupported", reason="earlier_statement")
                if isinstance(item, ast.BinOp) and not (isinstance(item.op, (ast.Add, ast.Sub)) and integer(item.right) is not None):
                    return self.fact(access, kind, "unsupported", reason="earlier_expression_may_raise")
            loops = [n for n in chain if isinstance(n, (ast.For, ast.ListComp))]
            if len(loops) != 1:
                return self.fact(access, kind, "unsupported", reason="nested_or_missing_loop")
            return self.fact(access, kind, assumptions=["执行到此分析入口且普通整数/列表操作正常完成；未建模资源耗尽、并发或异步修改"])
        raise ValueError(f"未知证据工具：{kind}")

    def counterexample_result(self, access, facts, reason):
        # Acquire the finite search as a separate, budgeted evidence action.
        for prerequisite in ("bindings", "container"):
            if prerequisite not in facts:
                return {"verdict": "UNDECIDED", "verification_result": "unknown", "terminal": False,
                        "missing": [prerequisite], "reason": "missing_facts", "assumptions": []}
            if facts[prerequisite]["status"] != "supported":
                return {"verdict": "UNDECIDED", "verification_result": "unknown", "terminal": True,
                        "missing": [], "reason": facts[prerequisite]["reason"], "assumptions": []}
        if "counterexample" not in facts:
            return {"verdict": "UNDECIDED", "verification_result": "unknown", "terminal": False,
                    "missing": ["counterexample"], "reason": reason, "assumptions": []}
        found = facts["counterexample"]
        if found["status"] != "supported":
            return {"verdict": "UNDECIDED", "verification_result": "unknown", "terminal": True,
                    "missing": [], "reason": reason, "assumptions": []}
        return {"verdict": "VIOLATION", "verification_result": "conditional_violation", "terminal": True,
                "missing": [], "reason": "bounded_counterexample", "assumptions": found["assumptions"],
                "witness": found["witness"], "derivation": found["derivation"]}

    def verify(self, access, facts):
        obligations = LIST_BOUNDS["obligations"]
        unsupported = [f for f in facts.values() if f["status"] == "unsupported"]
        if unsupported:
            return self.counterexample_result(access, facts, unsupported[0]["reason"])
        missing = [kind for kind in obligations if kind not in facts]
        if missing:
            return {"verdict": "UNDECIDED", "verification_result": "unknown", "terminal": False,
                    "missing": missing, "reason": "missing_facts", "assumptions": []}
        call_gaps = [f"call:{name}" for name in facts["effects"].get("calls", []) if f"call:{name}" not in facts]
        if call_gaps:
            return {"verdict": "UNDECIDED", "verification_result": "unknown", "terminal": False,
                    "missing": call_gaps, "reason": "unknown_call_effect", "assumptions": []}
        if facts["guards"]["unsupported_guard"]:
            return self.counterexample_result(access, facts, "unsupported_guard")
        trim, offset = facts["domain"]["trim"], facts["domain"]["offset"]
        # Across n >= trim+1 and 0 <= i < n-trim, extrema are exact.
        known_length = facts["container"].get("initial_length")
        if known_length is not None and known_length <= trim:
            return {"verdict": "SAFE", "verification_result": "conditional_safe", "terminal": True,
                    "missing": [], "reason": "empty_iteration", "assumptions": ["标准内置与普通执行环境"],
                    "derivation": {"length": known_length, "iterations": 0}}
        constant = facts["domain"].get("constant")
        minimum_length = known_length if known_length is not None else trim + 1
        lower_safe = (constant if constant is not None else offset) >= -minimum_length or facts["guards"].get("lower_guard", False)
        upper_safe = ((constant < minimum_length) if constant is not None else offset <= trim) or facts["guards"]["upper_guard"]
        safe = lower_safe and upper_safe
        assumptions = list(dict.fromkeys(a for f in facts.values() for a in f.get("assumptions", [])))
        result = {"verdict": "SAFE" if safe else "VIOLATION",
                  "verification_result": "conditional_safe" if safe else "conditional_violation",
                  "terminal": True, "missing": [], "reason": "bounds_rule", "assumptions": assumptions,
                  "derivation": {"rule": "affine_range_v1", "length": "n", "iteration": f"0 <= i < n - {trim}",
                                 "index": str(constant) if constant is not None else f"i + ({offset})", "valid": "-n <= index < n",
                                 "upper_guard": facts["guards"]["upper_guard"],
                                 "lower_guard": facts["guards"].get("lower_guard", False)}}
        if not safe:
            n = known_length if known_length is not None else trim + 1
            i = n - trim - 1 if not upper_safe and constant is None else 0
            k = constant if constant is not None else i + offset
            if facts["guards"]["upper_guard"] and k >= n:
                result.update(verdict="UNDECIDED", verification_result="unknown", reason="no_supported_witness")
            else:
                result["witness"] = {"length": n, "counter": i, "index": k,
                                     "kind": "symbolic_bounds_witness_not_execution"}
                assert k < -n or k >= n
        return result
