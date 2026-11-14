"""受众规则评估：在首次申请分配时按当时资料判定，结果随分配事实一并快照保存。

规则结构：
{
  "conditions": [
    {"field": "age", "op": "gte", "value": 18},
    {"field": "region", "op": "in", "value": ["cn", "sg"]},
    {"field": "tags", "op": "contains", "value": "new_user"}
  ]
}
所有条件为 AND 关系；空规则表示全部合格。字段缺失视为不满足。
"""
from __future__ import annotations

from typing import Any

_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "exists")


def _check(op: str, actual: Any, expected: Any, present: bool) -> bool:
    if op == "exists":
        return present == bool(expected)
    if not present:
        return False
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
        if op == "in":
            return actual in expected
        if op == "contains":
            return expected in actual
    except TypeError:
        return False
    raise ValueError(f"不支持的运算符: {op}")


def validate_rule(rule: dict) -> None:
    if not isinstance(rule, dict):
        raise ValueError("audience_rule 必须是对象")
    conditions = rule.get("conditions", [])
    if not isinstance(conditions, list):
        raise ValueError("audience_rule.conditions 必须是数组")
    for c in conditions:
        if not isinstance(c, dict) or "field" not in c or "op" not in c:
            raise ValueError("每个条件必须包含 field 与 op")
        if c["op"] not in _OPS:
            raise ValueError(f"不支持的运算符: {c['op']}（可选：{', '.join(_OPS)}）")
        if c["op"] != "exists" and "value" not in c:
            raise ValueError(f"条件 {c.get('field')} 缺少 value")


def evaluate(rule: dict, profile: dict) -> dict:
    """返回 {"eligible": bool, "conditions": [...], "profile": profile}，整体作为资格快照保存。"""
    conditions = (rule or {}).get("conditions", [])
    results = []
    eligible = True
    for c in conditions:
        field, op = c["field"], c["op"]
        expected = c.get("value")
        present = field in profile
        actual = profile.get(field)
        ok = _check(op, actual, expected, present)
        eligible = eligible and ok
        results.append(
            {
                "field": field,
                "op": op,
                "expected": expected,
                "actual": actual,
                "present": present,
                "passed": ok,
            }
        )
    return {"eligible": eligible, "conditions": results, "profile": profile}


def failed_conditions(evaluation: dict) -> list[str]:
    out = []
    for c in evaluation.get("conditions", []):
        if not c["passed"]:
            out.append(
                f"{c['field']} {c['op']} {c['expected']!r} 不满足（实际值 {c['actual']!r}）"
            )
    return out
