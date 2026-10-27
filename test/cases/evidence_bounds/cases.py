# Analysis inputs: do not execute. Contracts assume ordinary lists and builtins.
def unsafe_for(values: list[int]):
    for i in range(len(values)):
        item = values[i + 1]


def safe_trim(values: list[int]):
    for i in range(len(values) - 1):
        item = values[i + 1]


def guarded_safe(values: list[int]):
    for i in range(len(values)):
        if i + 1 < len(values):
            item = values[i + 1]


def negative_safe(values: list[int]):
    for i in range(len(values)):
        item = values[-1]


def negative_bad(values: list[int]):
    for i in range(len(values)):
        item = values[i - 2]


def comp_unsafe(values: list[int]):
    return [values[i + 1] for i in range(len(values))]


def comp_safe(values: list[int]):
    return [values[i + 1] for i in range(len(values) - 1)]


def empty_local():
    values = []
    for i in range(len(values)):
        item = values[i + 1]


def mutated_unknown(values: list[int]):
    for i in range(len(values)):
        values.pop()
        item = values[i]


def unknown_type(values):
    return [values[i + 1] for i in range(len(values))]


def touch(values):
    pass


def helper_unsafe(values: list[int]):
    for i in range(len(values)):
        touch(values)
        item = values[i + 1]


def unknown_helper(values: list[int]):
    for i in range(len(values)):
        external(values)
        item = values[i + 1]


def shadowed_len(values: list[int], len):
    return [values[i + 1] for i in range(len(values))]


def reassigned_index(values: list[int]):
    for i in range(len(values)):
        i = 0
        item = values[i + 1]


def early_break(values: list[int]):
    for i in range(len(values)):
        break
        item = values[i + 1]


def else_guard(values: list[int]):
    for i in range(len(values)):
        if i + 1 < len(values):
            pass
        else:
            item = values[i + 1]


def dict_unknown(values: dict[int, int]):
    return [values[i + 1] for i in range(len(values))]


def upper_only_negative(values: list[int]):
    for i in range(len(values)):
        if i - 2 < len(values):
            item = values[i - 2]


def unrelated_guard(values: list[int]):
    for i in range(len(values)):
        if i < len(values):
            item = values[i + 1]


def late_guard(values: list[int]):
    for i in range(len(values)):
        item = values[i + 1]
        if i + 1 < len(values):
            pass


def same_line(values: list[int]):
    for i in range(len(values)):
        pair = values[i], values[i + 1]


def sliced_unknown(values: list[int]):
    return [values[i + 1:] for i in range(len(values))]


def local_short():
    values = [1]
    for i in range(len(values)):
        item = values[-2]


def helper_safe(values: list[int]):
    for i in range(len(values) - 1):
        touch(values)
        item = values[i + 1]
