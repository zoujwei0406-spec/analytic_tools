from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .languages import EXTENSIONS, FUNCTION_TYPES, calls_in, family, is_file_local, leaf_name, name_of, parser_for, walk

IGNORED = {'.git', '.venv', 'venv', 'node_modules', 'build', 'dist', 'reports', '.coverage-work',
           'models', 'runtime', '__pycache__', '.pytest_cache', '.npm-cache', '.ruff_cache'}


@dataclass
class Function:
    id: str
    name: str
    file: str
    start: int
    end: int
    calls: list[str]
    indirect_calls: int
    static: bool
    code: str
    language: str = 'c'

    def summary(self):
        return {key: value for key, value in asdict(self).items() if key != 'code'}


class Project:
    def __init__(self, root: Path, inputs: list[str] | None = None):
        requested = root.resolve()
        if requested.is_file():
            if inputs:
                raise ValueError('直接传入文件时无需 --inputs')
            self.root, inputs = requested.parent, [requested.name]
        else:
            self.root = requested
        if not self.root.is_dir():
            raise ValueError(f'项目目录不存在: {self.root}')
        self.sources: dict[str, bytes] = {}
        self.languages: dict[str, str] = {}
        self.functions: dict[str, Function] = {}
        self.warnings: list[str] = []
        selected: set[Path] = set()
        for item in inputs or ['.']:
            path = self.safe_path(item)
            if not path.exists():
                raise ValueError(f'输入不存在: {item}')
            if path.is_file():
                if path.suffix not in EXTENSIONS:
                    raise ValueError(f'不支持的文件类型: {path.name}；支持 C、C++、Python、JavaScript、TypeScript')
                candidates = [path]
            else:
                candidates = []
                for directory, dirs, files in os.walk(path, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in IGNORED and not (Path(directory) / d).is_symlink())
                    candidates.extend(Path(directory) / file for file in sorted(files) if Path(file).suffix in EXTENSIONS)
            for candidate in candidates:
                resolved = candidate.resolve()
                if resolved.is_relative_to(self.root) and resolved.is_file() and resolved.suffix in EXTENSIONS:
                    selected.add(resolved)
        if not selected:
            raise ValueError('没有找到支持的源码；支持 C、C++、Python、JavaScript、TypeScript。')
        has_cpp = any(EXTENSIONS[p.suffix] == 'cpp' for p in selected)
        for path in sorted(selected):
            name = path.relative_to(self.root).as_posix()
            if path.stat().st_size > 2_000_000:
                raise ValueError(f'文件超过 2 MB 限制: {name}')
            source = path.read_bytes()
            language = 'cpp' if path.suffix == '.h' and has_cpp else EXTENSIONS[path.suffix]
            self.sources[name], self.languages[name] = source, language
            tree = parser_for(language, path.suffix == '.tsx').parse(source)
            if tree.root_node.has_error:
                self.warnings.append(f'{name}: AST 含语法错误，宏、语法扩展或缺失上下文可能影响索引。')
            for node in walk(tree.root_node):
                if node.type not in FUNCTION_TYPES[language]:
                    continue
                func_name = name_of(node, language)
                if not func_name:
                    self.warnings.append(f'{name}:{node.start_point.row + 1}: 无法解析函数名。')
                    continue
                calls, indirect = calls_in(node, language)
                source_node = node.parent if language == 'python' and node.parent.type == 'decorated_definition' else node
                start, end = source_node.start_point.row + 1, source_node.end_point.row + 1
                fid = f'{name}:{start}:{func_name}'
                if fid in self.functions:
                    fid += f':col{node.start_point.column + 1}'
                self.functions[fid] = Function(
                    fid, func_name, name, start, end, calls, indirect,
                    is_file_local(node, language),
                    source[source_node.start_byte:source_node.end_byte].decode('utf-8', errors='replace'), language,
                )

    def safe_path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('路径必须位于项目目录内')
        return path

    def hashes(self):
        return {name: hashlib.sha256(data).hexdigest() for name, data in self.sources.items()}

    def check_unchanged(self):
        for name, data in self.sources.items():
            if self.safe_path(name).read_bytes() != data:
                raise ValueError(f'分析期间源码发生变化，请重新运行: {name}')

    def at(self, file: str, line: int) -> Function | None:
        matches = [f for f in self.functions.values() if f.file == file and f.start <= line <= f.end]
        return min(matches, key=lambda f: f.end - f.start) if matches else None

    @staticmethod
    def may_call(caller, target):
        if family(caller.language) != family(target.language):
            return False
        if target.static and caller.file != target.file:
            return False
        return any(leaf_name(call) == leaf_name(target.name) for call in caller.calls)

    def callers(self, fid: str):
        return [f for f in self.functions.values() if self.may_call(f, self.functions[fid])]

    def callees(self, fid: str):
        return [f for f in self.functions.values() if self.may_call(self.functions[fid], f)]

    def lines(self, file: str, start: int, end: int):
        if file not in self.sources:
            raise ValueError('只能读取本次导入的源码')
        lines = self.sources[file].decode('utf-8', errors='replace').splitlines()
        if start < 1 or end < start or end > len(lines):
            raise ValueError('无效的源码行范围')
        return '\n'.join(f'{i}: {lines[i - 1]}' for i in range(start, end + 1))
