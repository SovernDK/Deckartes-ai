import math


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
