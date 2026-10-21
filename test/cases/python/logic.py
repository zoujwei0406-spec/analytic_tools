def mean(values):
    return sum(values) / len(values)


def neighbor_pairs(values):
    pairs = []
    for index in range(len(values)):
        pairs.append((values[index], values[index + 1]))
    return pairs


def shipping_fee(subtotal):
    """Orders with a subtotal of 100 or more ship for free."""
    return 0 if subtotal > 100 else 10


def maximum(values):
    result = 0
    for value in values:
        if value > result:
            result = value
    return result


def fresh_matrix(rows, columns):
    return [[0] * columns] * rows
