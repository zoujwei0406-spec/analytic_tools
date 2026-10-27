# Analysis input only. Do not execute; labels are kept outside the model input.
def case_a(values: list[int]):
    return [values[i + 1] for i in range(len(values))]


def case_b(values: list[int]):
    return [values[i + 1] for i in range(len(values) - 1)]


def case_c(values: list[int]):
    return [values[i - 2] for i in range(len(values))]


def case_d(values: list[int]):
    return [values[-1] for i in range(len(values))]


def touch(values):
    pass


def case_e(values: list[int]):
    for i in range(len(values)):
        touch(values)
        item = values[i + 1]


def case_f(values: list[int]):
    for i in range(len(values) - 1):
        touch(values)
        item = values[i + 1]
