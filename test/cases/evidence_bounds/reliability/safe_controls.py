def chained_protection(values: list[int]):
    for i in range(len(values)):
        if 0 <= i + 1 < len(values):
            item = values[i + 1]


def conjunction_protection(values: list[int]):
    for i in range(len(values)):
        if i - 3 >= -len(values) and i - 3 < len(values):
            item = values[i - 3]


def skip_last(values: list[int]):
    for i in range(len(values)):
        if i + 1 >= len(values):
            continue
        item = values[i + 1]


def stop_at_last(values: list[int]):
    for i in range(len(values)):
        if i + 1 >= len(values):
            return None
        item = values[i + 1]


def negated_exit(values: list[int]):
    for i in range(len(values)):
        if not i + 1 < len(values):
            return None
        item = values[i + 1]


def nested_protection(values: list[int]):
    for i in range(len(values)):
        if i - 3 >= -len(values):
            if i - 3 < len(values):
                item = values[i - 3]


def filtered_negative(values: list[int]):
    return [values[i - 3] for i in range(len(values)) if -len(values) <= i - 3 < len(values)]


def empty_iteration():
    values = []
    for i in range(len(values)):
        item = values[i + 20]


def last_element(values: list[int]):
    for i in range(len(values)):
        item = values[-1]


def standalone_return(values: list[int], i: int):
    if not 0 <= i < len(values) - 1:
        return None
    return values[i + 1]
