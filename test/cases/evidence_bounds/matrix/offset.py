def case_a(values: list[int]):
    return [values[i + 2] for i in range(len(values) - 1)]


def case_b(values: list[int]):
    return [values[i + 2] for i in range(len(values) - 2)]
