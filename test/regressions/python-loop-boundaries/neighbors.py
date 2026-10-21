def neighbor_pairs(values: list[int]) -> list[tuple[int, int]]:
    pairs = []
    for index in range(len(values)):
        pairs.append((values[index], values[index + 1]))
    return pairs
