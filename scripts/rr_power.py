#!/usr/bin/env python3
"""Power of the registered study's primary test (exact two-sided McNemar).

Unconditional power averages over the number of discordant pairs
D ~ Binomial(n, q) and X | D ~ Binomial(D, p10 / q), where p10 - p01 = delta.
Inputs are planning assumptions, not pilot estimates.
"""

from __future__ import annotations

import json
from math import comb, sqrt
from pathlib import Path
from statistics import NormalDist

PROJECT = Path(__file__).resolve().parents[1]
ALPHA = 0.05


def binom_pmf(k: int, n: int, p: float) -> float:
    return comb(n, k) * p**k * (1 - p) ** (n - k)


def rejects(x: int, d: int) -> bool:
    """Exact two-sided McNemar: binomial test of x successes in d at p = 0.5."""
    if d == 0:
        return False
    tail = sum(comb(d, k) for k in range(0, min(x, d - x) + 1)) / 2**d
    return min(1.0, 2 * tail) <= ALPHA


def power(n: int, delta: float, q: float) -> float:
    share = (q + delta) / (2 * q)  # P(T wins | discordant)
    total = 0.0
    for d in range(n + 1):
        pd = binom_pmf(d, n, q)
        if pd < 1e-15:
            continue
        total += pd * sum(binom_pmf(x, d, share) for x in range(d + 1) if rejects(x, d))
    return total


def normal_n(delta: float, q: float, beta: float = 0.2) -> float:
    z = NormalDist().inv_cdf
    return (z(1 - ALPHA / 2) * sqrt(q) + z(1 - beta) * sqrt(q - delta**2)) ** 2 / delta**2


def main() -> None:
    delta = 0.10
    designs = [(120, 0.30), (240, 0.30), (252, 0.30), (252, 0.20), (252, 0.40), (252, 0.50)]
    result = {
        "schema": "pkgbridge-rr-power/1",
        "assumptions": {"alpha": ALPHA, "delta": delta, "test": "exact two-sided McNemar",
                        "note": "planning assumptions, not pilot estimates"},
        "normal_approximation_n_q030": round(normal_n(delta, 0.30), 1),
        "designs": [{"tasks": n, "discordance": q, "power": round(power(n, delta, q), 3)}
                    for n, q in designs],
    }
    (PROJECT / "evidence" / "rr-power.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
