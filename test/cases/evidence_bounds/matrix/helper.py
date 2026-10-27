def observe(values):
    pass


def case_a(values: list[int]):
    for i in range(len(values)):
        observe(values)
        _item = values[i + 1]


def case_b(values: list[int]):
    for i in range(len(values) - 1):
        observe(values)
        _item = values[i + 1]
