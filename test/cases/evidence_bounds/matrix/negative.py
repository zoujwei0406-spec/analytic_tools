def case_a(values: list[int]):
    return [values[i - 2] for i in range(len(values))]


def case_b(values: list[int]):
    return [values[-1] for i in range(len(values))]
