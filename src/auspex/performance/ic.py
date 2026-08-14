"""Correlation statistics — Spearman IC and Pearson correlation (arc42 §5.8).

Pure ``Decimal`` implementations (no numpy/scipy dependency) so results stay
exactly reproducible from stored state, consistent with the rest of the
deterministic engine.
"""

from __future__ import annotations

from decimal import Decimal


def rank(values: list[Decimal]) -> list[Decimal]:
    """Average (fractional) rank, 1-based, ties share the mean rank."""

    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [Decimal(0)] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = Decimal(i + j + 2) / Decimal(2)  # 1-based average of positions i..j
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def pearson(x: list[Decimal], y: list[Decimal]) -> Decimal | None:
    n = len(x)
    if n < 2 or n != len(y):
        return None
    mean_x = sum(x, Decimal(0)) / Decimal(n)
    mean_y = sum(y, Decimal(0)) / Decimal(n)
    cov = sum(((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y, strict=True)), Decimal(0))
    var_x = sum(((xi - mean_x) ** 2 for xi in x), Decimal(0))
    var_y = sum(((yi - mean_y) ** 2 for yi in y), Decimal(0))
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x.sqrt() * var_y.sqrt())


def spearman_ic(x: list[Decimal], y: list[Decimal]) -> Decimal | None:
    """Spearman rank correlation — Pearson correlation of the two rank vectors."""

    if len(x) < 2 or len(x) != len(y):
        return None
    return pearson(rank(x), rank(y))
