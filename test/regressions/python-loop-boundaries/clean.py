def adjacent_pairs(values: list[int]) -> list[tuple[int, int]]:
    pairs = []
    for index in range(len(values) - 1):
        pairs.append((values[index], values[index + 1]))
    return pairs


def guarded_neighbors(values: list[int]) -> list[int]:
    following = []
    for index in range(len(values)):
        if index + 1 < len(values):
            following.append(values[index + 1])
    return following


def repeated_last(values: list[int]) -> list[int]:
    result = []
    for _ in range(len(values)):
        result.append(values[-1])
    return result


def suffixes(values: list[int]) -> list[list[int]]:
    result = []
    for index in range(len(values)):
        result.append(values[index + 1:])
    return result
