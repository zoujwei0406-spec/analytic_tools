def checked_mean(values):
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)


def remember(value, history=None):
    if history is None:
        history = []
    history.append(value)
    return history


def fresh_matrix(rows, columns):
    return [[0 for _ in range(columns)] for _ in range(rows)]
