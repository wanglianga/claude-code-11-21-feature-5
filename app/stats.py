"""统计函数：两比例差正态近似区间、卡方拟合优度 p 值（正则化上不完全伽马函数）。"""
from __future__ import annotations

import math

Z_95 = 1.959963984540054  # 标准正态 97.5% 分位


def diff_ci(p1: float, n1: int, p2: float, n2: int) -> tuple[float, float, float]:
    """返回 (diff, low, high)：diff = p1 - p2，正态近似 95% 置信区间。"""
    diff = p1 - p2
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return diff, diff - Z_95 * se, diff + Z_95 * se


def _gammq(a: float, x: float) -> float:
    """正则化上不完全伽马函数 Q(a, x) = Γ(a,x)/Γ(a)。

    采用 Numerical Recipes 的级数展开（x < a+1）与连分式（x >= a+1）。
    """
    if x < 0 or a <= 0:
        raise ValueError("gammq 要求 a>0 且 x>=0")
    if x == 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        # 级数求 P(a,x)，再取补
        ap = a
        term = 1.0 / a
        total = term
        for _ in range(10000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        p = total * math.exp(-x + a * math.log(x) - gln)
        return max(0.0, min(1.0, 1.0 - p))
    # 连分式求 Q(a,x)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    q = h * math.exp(-x + a * math.log(x) - gln)
    return max(0.0, min(1.0, q))


def chi2_sf(stat: float, df: int) -> float:
    """卡方分布生存函数（p 值）。"""
    if df < 1:
        raise ValueError("df 必须 >= 1")
    if stat < 0:
        raise ValueError("stat 必须 >= 0")
    return _gammq(df / 2.0, stat / 2.0)
