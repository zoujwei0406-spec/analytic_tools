"""Executable defect knowledge. Rules return scoped results, never model assertions."""

LIST_BOUNDS = {
    "id": "python.list.index_bounds", "version": 2, "language": "python",
    "property": "IndexError at an integer list subscript",
    "obligations": ["bindings", "container", "domain", "guards", "effects", "reachability"],
    "supported": "unit-step range(len(list)-c), affine index, direct guards, one comprehension generator; bounded AST counterexamples for list mutations, helpers, nested/stepped loops and integer arithmetic",
}
