"""Language-specific syntax adapters. Names are candidates, not resolved symbols."""
from __future__ import annotations

from functools import lru_cache

from tree_sitter import Language, Parser
import tree_sitter_c
import tree_sitter_cpp
import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_typescript

EXTENSIONS = {
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".C": "cpp", ".H": "cpp",
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
}
LABELS = {"c": "C", "cpp": "C++", "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript"}
FUNCTION_TYPES = {
    "c": {"function_definition"}, "cpp": {"function_definition"},
    "python": {"function_definition"},
    "javascript": {"function_declaration", "generator_function_declaration", "function_expression", "generator_function", "arrow_function", "method_definition"},
    "typescript": {"function_declaration", "generator_function_declaration", "function_expression", "generator_function", "arrow_function", "method_definition"},
}


def family(language):
    if language in {"c", "cpp"}:
        return "c-family"
    if language in {"javascript", "typescript"}:
        return "js-family"
    return language


@lru_cache(maxsize=8)
def parser_for(language: str, tsx=False):
    factories = {"c": tree_sitter_c.language, "cpp": tree_sitter_cpp.language,
                 "python": tree_sitter_python.language, "javascript": tree_sitter_javascript.language,
                 "typescript": tree_sitter_typescript.language_tsx if tsx else tree_sitter_typescript.language_typescript}
    return Parser(Language(factories[language]()))


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def text(node):
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def name_of(node, language):
    if language in {"c", "cpp"}:
        declarator = node.child_by_field_name("declarator")
        while declarator is not None:
            if declarator.type in {"identifier", "field_identifier", "qualified_identifier", "operator_name", "destructor_name"}:
                return text(declarator)
            declarator = declarator.child_by_field_name("declarator")
        return None
    name = node.child_by_field_name("name")
    if name:
        return text(name)
    parent = node.parent
    if parent and parent.type in {"variable_declarator", "pair", "public_field_definition", "field_definition"}:
        name = parent.child_by_field_name("name") or parent.child_by_field_name("key") or parent.child_by_field_name("property")
    elif parent and parent.type == "assignment_expression":
        name = parent.child_by_field_name("left")
    if name:
        return text(name)
    return f"<anonymous@{node.start_point.row + 1}:{node.start_point.column + 1}>"


def calls_in(node, language):
    """Do not attribute an inner function's body calls to its outer function."""
    calls, indirect = set(), 0
    stack = list(node.named_children)
    while stack:
        child = stack.pop()
        if child.type in FUNCTION_TYPES[language]:
            continue
        if child.type in {"call_expression", "call"}:
            callee = child.child_by_field_name("function")
            if callee and callee.type in {"identifier", "qualified_identifier"}:
                calls.add(text(callee))
            elif callee and callee.type in {"attribute", "member_expression", "field_expression"}:
                leaf = (callee.child_by_field_name("attribute") or callee.child_by_field_name("property")
                        or callee.child_by_field_name("field"))
                if leaf:
                    calls.add(text(leaf))
                indirect += 1
            else:
                indirect += 1
        stack.extend(child.named_children)
    return sorted(calls), indirect


def leaf_name(name):
    return name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def is_file_local(node, language):
    if language not in {"c", "cpp"}:
        return False
    parent = node.parent
    while parent:
        if parent.type in {"class_specifier", "struct_specifier"}:
            return False  # A C++ static member is not a file-local free function.
        parent = parent.parent
    return any(c.type == "storage_class_specifier" and c.text == b"static" for c in node.named_children)
