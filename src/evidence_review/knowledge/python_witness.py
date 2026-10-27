"""Finite counterexamples in a closed AST subset; never execute target Python.

Only existence is established. Exhausting the search never establishes safety.
Every value is created here; arbitrary calls, imports and attributes are refused.
"""
from __future__ import annotations

import ast
from itertools import product


class Unsupported(Exception):
    pass


class Returned(Exception):
    def __init__(self, value):
        self.value = value


class Continued(Exception):
    pass


class Broken(Exception):
    pass


class Counterexample(Exception):
    def __init__(self, witness):
        self.witness = witness


def annotation_kind(node):
    if isinstance(node, ast.Name) and node.id in {'int', 'list'}:
        return node.id
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id == 'list' and isinstance(node.slice, ast.Name) and node.slice.id == 'int'):
        return 'list'
    return None


class Machine:
    """Bounded ordinary list/int semantics, with path-local alias identity."""
    def __init__(self, module, target, inputs, budget, allow_helpers=True):
        self.module, self.target, self.inputs, self.budget = module, target, inputs, budget
        self.allow_helpers = allow_helpers
        self.steps, self.depth, self.path = 0, 0, []
        self.helpers = {}
        for node in module.body:
            if isinstance(node, ast.FunctionDef):
                self.helpers.setdefault(node.name, []).append(node)
        self.scopes = []

    def tick(self, node):
        self.steps += 1
        self.budget[0] -= 1
        if self.steps > 3000 or self.budget[0] < 0:
            raise Unsupported('search_budget')

    def note(self, **event):
        self.path.append(event)
        if len(self.path) > 64:
            del self.path[0]

    def int_value(self, value):
        if type(value) is not int or abs(value) > 100000:
            raise Unsupported('integer_domain')
        return value

    def bind(self, target, value, env):
        if not isinstance(target, ast.Name):
            raise Unsupported('assignment_target')
        env[target.id] = value

    def expr(self, node, env):
        self.tick(node)
        if isinstance(node, ast.Constant) and type(node.value) in {int, bool, str, type(None)}:
            if type(node.value) is int:
                self.int_value(node.value)
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise Unsupported('unknown_name')
            return env[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            if len(node.elts) > 128:
                raise Unsupported('literal_size')
            values = [self.expr(n, env) for n in node.elts]
            return values if isinstance(node, ast.List) else tuple(values)
        if isinstance(node, ast.UnaryOp):
            value = self.expr(node.operand, env)
            if isinstance(node.op, ast.Not):
                return not self.truth(value)
            value = self.int_value(value)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
        if isinstance(node, ast.BinOp):
            left, right = self.int_value(self.expr(node.left, env)), self.int_value(self.expr(node.right, env))
            if isinstance(node.op, ast.Add):
                return self.int_value(left + right)
            if isinstance(node.op, ast.Sub):
                return self.int_value(left - right)
            if isinstance(node.op, ast.Mult):
                return self.int_value(left * right)
            if isinstance(node.op, ast.FloorDiv):
                return self.int_value(left // right)
            if isinstance(node.op, ast.Mod):
                return self.int_value(left % right)
            raise Unsupported('operator')
        if isinstance(node, ast.BoolOp):
            for value_node in node.values:
                value = self.expr(value_node, env)
                if isinstance(node.op, ast.And) and not self.truth(value):
                    return value
                if isinstance(node.op, ast.Or) and self.truth(value):
                    return value
            return value
        if isinstance(node, ast.Compare):
            left = self.expr(node.left, env)
            for op, right_node in zip(node.ops, node.comparators):
                right = self.expr(right_node, env)
                if type(left) not in {int, bool} or type(right) not in {int, bool}:
                    raise Unsupported('comparison_types')
                if isinstance(op, ast.Lt):
                    result = left < right
                elif isinstance(op, ast.LtE):
                    result = left <= right
                elif isinstance(op, ast.Gt):
                    result = left > right
                elif isinstance(op, ast.GtE):
                    result = left >= right
                elif isinstance(op, ast.Eq):
                    result = left == right
                elif isinstance(op, ast.NotEq):
                    result = left != right
                else:
                    raise Unsupported('comparison')
                if not result:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self.expr(node.body if self.truth(self.expr(node.test, env)) else node.orelse, env)
        if isinstance(node, ast.Subscript):
            # Python evaluates the receiver before the index expression.
            receiver, index = self.expr(node.value, env), self.expr(node.slice, env)
            if type(receiver) is not list or type(index) is not int:
                raise Unsupported('non_list_index')
            if not -len(receiver) <= index < len(receiver):
                if node is self.target:
                    raise Counterexample({'kind': 'bounded_ast_counterexample',
                        'entry_inputs': self.inputs, 'length': len(receiver), 'index': index,
                        'locals': {k: v for k, v in env.items() if type(v) is int},
                        'path': list(self.path), 'steps': self.steps})
                raise Unsupported('earlier_index_error')
            return receiver[index]
        if isinstance(node, ast.ListComp):
            local, output = dict(env), []
            def generate(index):
                if index == len(node.generators):
                    output.append(self.expr(node.elt, local))
                    return
                generator = node.generators[index]
                if generator.is_async:
                    raise Unsupported('async_comprehension')
                for value in self.iter_values(self.expr(generator.iter, local)):
                    self.tick(generator)
                    self.bind(generator.target, value, local)
                    if all(self.truth(self.expr(test, local)) for test in generator.ifs):
                        generate(index + 1)
            generate(0)
            return output
        if isinstance(node, ast.Call):
            if node.keywords or any(isinstance(n, ast.Starred) for n in node.args):
                raise Unsupported('call_arguments')
            if isinstance(node.func, ast.Attribute):
                receiver = self.expr(node.func.value, env)
                args = [self.expr(n, env) for n in node.args]
                if type(receiver) is not list:
                    raise Unsupported('method_receiver')
                if node.func.attr == 'pop' and len(args) <= 1:
                    index = self.int_value(args[0]) if args else -1
                    # A failing pop is an earlier exception, never a subscript witness.
                    value = receiver.pop(index)
                    self.note(line=node.lineno, effect='pop', resulting_length=len(receiver))
                    return value
                if node.func.attr == 'append' and len(args) == 1 and len(receiver) < 128:
                    receiver.append(args[0])
                    return None
                raise Unsupported('method')
            if not isinstance(node.func, ast.Name):
                raise Unsupported('callee')
            name = node.func.id
            if name in env or self.conflicts(self.module, name) or self.conflicts(self.scopes[-1], name):
                raise Unsupported('rebound_callee')
            args = [self.expr(n, env) for n in node.args]
            if name == 'len' and len(args) == 1 and type(args[0]) is list:
                return len(args[0])
            if name == 'range' and 1 <= len(args) <= 3:
                values = range(*(self.int_value(v) for v in args))
                if len(values) > 128:
                    raise Unsupported('range_size')
                return values
            if not self.allow_helpers:
                raise Unsupported('local_rules_only')
            helpers = self.helpers.get(name, [])
            if len(helpers) != 1:
                raise Unsupported('unresolved_call')
            self.note(line=node.lineno, call=name)
            return self.call(helpers[0], args)
        raise Unsupported('expression')

    @staticmethod
    def conflicts(region, name):
        # Static binding rules: a later local assignment also shadows earlier use.
        for child in region.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if child.name == name and not (region.__class__ is ast.Module and isinstance(child, ast.FunctionDef)):
                    return True
                continue
            for node in ast.walk(child):
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id == name:
                    return True
                if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                        a.name == '*' or (a.asname or a.name.split('.')[0]) == name for a in node.names):
                    return True
        return False

    @staticmethod
    def truth(value):
        if type(value) not in {int, bool, list, tuple, str, type(None)}:
            raise Unsupported('truth_value')
        return bool(value)

    @staticmethod
    def iter_values(value):
        # list iteration may mutate; CPython list iterators preserve that behavior.
        if type(value) not in {range, list, tuple} or len(value) > 128:
            raise Unsupported('iterator')
        return value

    def block(self, statements, env):
        for node in statements:
            self.tick(node)
            if isinstance(node, ast.Assign):
                value = self.expr(node.value, env)
                for target in node.targets:
                    self.bind(target, value, env)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self.bind(node.target, self.expr(node.value, env), env)
            elif isinstance(node, ast.Expr):
                self.expr(node.value, env)
            elif isinstance(node, ast.Return):
                raise Returned(self.expr(node.value, env) if node.value else None)
            elif isinstance(node, ast.If):
                taken = self.truth(self.expr(node.test, env))
                self.note(line=node.lineno, branch=taken)
                self.block(node.body if taken else node.orelse, env)
            elif isinstance(node, ast.For):
                broke = False
                for value in self.iter_values(self.expr(node.iter, env)):
                    self.tick(node)
                    self.bind(node.target, value, env)
                    self.note(line=node.lineno, iteration=value if type(value) is int else '<list>')
                    try:
                        self.block(node.body, env)
                    except Continued:
                        continue
                    except Broken:
                        broke = True
                        break
                if not broke:
                    self.block(node.orelse, env)
            elif isinstance(node, ast.Continue):
                raise Continued()
            elif isinstance(node, ast.Break):
                raise Broken()
            elif not isinstance(node, ast.Pass):
                raise Unsupported('statement')

    def call(self, function, values):
        args = [*function.args.posonlyargs, *function.args.args]
        if (function.decorator_list or function.args.vararg or function.args.kwarg or function.args.kwonlyargs
                or len(args) != len(values) or self.depth >= 4):
            raise Unsupported('function_signature_or_depth')
        # Global/nonlocal and nested definitions alter name resolution beyond this subset.
        if any(isinstance(n, (ast.Global, ast.Nonlocal, ast.NamedExpr, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.Yield, ast.YieldFrom, ast.Await))
               for statement in function.body for n in ast.walk(statement)):
            raise Unsupported('dynamic_scope')
        self.depth += 1
        self.scopes.append(function)
        try:
            self.block(function.body, dict(zip((a.arg for a in args), values)))
        except Returned as result:
            return result.value
        finally:
            self.scopes.pop()
            self.depth -= 1
        return None


def search(module, scope, target, allow_helpers=True):
    if not isinstance(scope, ast.FunctionDef) or scope.decorator_list:
        return {'status': 'unsupported', 'reason': 'counterexample_scope'}
    args = [*scope.args.posonlyargs, *scope.args.args]
    kinds = [annotation_kind(a.annotation) for a in args]
    if any(kind is None for kind in kinds) or len(args) > 3:
        return {'status': 'unsupported', 'reason': 'counterexample_input_types'}
    domains = [list(range(9)) if kind == 'list' else list(range(-2, 9)) for kind in kinds]
    budget, attempts = [100000], 0
    for choices in product(*domains):
        if attempts >= 256 or budget[0] <= 0:
            break
        values = [list(range(v)) if kind == 'list' else v for kind, v in zip(kinds, choices)]
        inputs = {a.arg: list(v) if type(v) is list else v for a, v in zip(args, values)}
        machine = Machine(module, target, inputs, budget, allow_helpers)
        attempts += 1
        try:
            machine.call(scope, values)
        except Counterexample as result:
            return {'status': 'supported', 'witness': result.witness,
                    'attempts': attempts, 'total_steps': 100000-budget[0],
                    'assumptions': ['入口参数采用报告给出的普通内置 list/int 值；标准 Python 内置未被替换',
                        '同文件辅助函数按当前源码绑定；无运行时替换、并发修改或资源异常'],
                    'derivation': {'rule': 'bounded_ast_counterexample_v1', 'list_lengths': [0, 8],
                        'integers': [-2, 8], 'max_inputs': 256, 'max_steps_per_input': 3000,
                        'max_total_steps': 100000, 'max_call_depth': 4,
                        'meaning': '有限白名单语义找到可达越界；不是执行目标源码，搜索失败不证明安全'}}
        except (Unsupported, IndexError, ZeroDivisionError, ValueError, TypeError, Continued, Broken):
            # Any preceding unknown/exception blocks this path. Never skip it.
            continue
    return {'status': 'unsupported', 'reason': 'no_bounded_counterexample',
            'attempts': attempts, 'total_steps': 100000-budget[0]}
