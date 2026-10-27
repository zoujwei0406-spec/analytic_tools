def alias_change(values: list[int]):
    alias = values
    for i in range(len(values)):
        alias.pop()
        item = values[i]


def callback_change(values: list[int], callback):
    for i in range(len(values)):
        callback(values)
        item = values[i]


def nonlinear_index(values: list[int]):
    for i in range(len(values)):
        item = values[i * i]


def custom_container(values):
    for i in range(len(values)):
        item = values[i + 1]


def stepped_range(values: list[int]):
    for i in range(0, len(values), 2):
        item = values[i + 1]


def conditional_access(values: list[int]):
    return [values[i + 1] if i + 1 < len(values) else None for i in range(len(values))]


def unreachable_expression(values: list[int]):
    return [values[i + 1] if False else None for i in range(len(values))]


def same_line_return(values: list[int]):
    for i in range(len(values)):
        return; item = values[i + 1]


def loop_rebinds_container(values: list[int]):
    for values in range(len(values)):
        item = values[values]


def comprehension_rebinds_container(values: list[int]):
    return [values[values] for values in range(len(values))]


def imported_container(values: list[int]):
    import math as values
    for i in range(len(values)):
        item = values[i]


def shadowed_range(values: list[int], range):
    return [values[i + 1] for i in range(len(values))]


def unrelated_condition(values: list[int]):
    for i in range(len(values)):
        if i >= 0:
            item = values[i + 1]


def compound_exit(values: list[int]):
    for i in range(len(values)):
        if i + 1 >= len(values):
            item = values[i + 1]
            continue
        item = values[i + 1]


def stale_outer_guard(values: list[int], i: int):
    if i + 1 < len(values):
        for i in range(len(values)):
            item = values[i + 1]


def stale_exit_before_loop(values: list[int], i: int):
    if i + 1 >= len(values):
        return None
    for i in range(len(values)):
        item = values[i + 1]
