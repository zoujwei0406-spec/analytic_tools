def undefined_after_read(values: list[int]):
    for i in range(len(values)):
        item = values[i]; result = missing_name


def attribute_after_read(values: list[int]):
    for i in range(len(values)):
        item = values[i].missing_attribute


def divide_after_read(values: list[int]):
    for i in range(len(values)):
        item = values[i]; result = 1 / 0


def distinct_same_line(values: list[int]):
    for i in range(len(values)):
        pair = values[i], values[i + 1]; result = missing_name
