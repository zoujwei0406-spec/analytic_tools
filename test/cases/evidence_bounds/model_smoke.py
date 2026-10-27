# Small real-model integration fixture; do not execute as a test runner.
def unsafe_neighbors(values: list[int]):
    return [values[i + 1] for i in range(len(values))]


def safe_neighbors(values: list[int]):
    return [values[i + 1] for i in range(len(values) - 1)]
