def forward_three(values: list[int]):
    return [values[i + 3] for i in range(len(values) - 1)]


def negative_three(values: list[int]):
    for i in range(len(values)):
        item = values[-3]


def shrinking_loop(values: list[int]):
    for i in range(len(values)):
        values.pop()
        item = values[i]


def shrink(values):
    values.pop()


def helper_shrinks(values: list[int]):
    for i in range(len(values)):
        shrink(values)
        item = values[i]


def nested_offset(values: list[int]):
    for i in range(len(values)):
        for j in range(len(values)):
            item = values[j + 1]


def paired_access(values: list[int]):
    for i in range(len(values)):
        pair = values[i], values[i + 1]


def incomplete_lower_guard(values: list[int]):
    for i in range(len(values)):
        if i - 3 < len(values):
            item = values[i - 3]
