"""发布门禁判定（纯函数，不修改原报告）。

门禁在一份已生成的分析报告之上回答"这次发布是否放行"：

* 请求体声明本次需要检查的兼容方向（``directions``）与各方向允许的
  warning 上限（``warning_limits``）；未检查方向的 issue 不参与结论；
* 豁免规则（``exemptions``）由 ``direction`` / ``code`` / ``path`` 三个
  字段组成，三者均可写 ``*`` 表示通配；其中 ``path`` 为 JSON Pointer，
  支持**单段** ``*`` 通配（如 ``#/properties/*/items``），路径段按
  RFC 6901 规则解码（``~1`` → ``/``、``~0`` → ``~``）后比较；
* 同一条 issue 可能命中多条规则，按以下优先级选出**唯一**规则：

  1. 精确字段数更多（direction、code 非 ``*`` 各计 1；path 不含通配段
     再计 1，上限 3）；
  2. path 字面段数更多（除 ``*`` 外的路径段数量）；
  3. 前两项仍相同，声明顺序靠后者获胜（规则序号从 1 开始）。

判定规则：被豁免的 issue 完全不计数；任一未豁免 error 直接阻断；某方向
未豁免 warning 数量超过该方向上限时，按报告顺序超出上限的部分作为阻断项；
未检查的方向不影响结论。
"""
from __future__ import annotations

from typing import Any

DIRECTIONS: tuple[str, str] = ("backward", "forward")


# ---------------------------------------------------------------------------
# JSON Pointer（带单段 * 通配）
# ---------------------------------------------------------------------------

def unescape_segment(segment: str) -> str:
    """按 RFC 6901 解码路径段（顺序不可颠倒：先 ~1 后 ~0）。"""
    return segment.replace("~1", "/").replace("~0", "~")


def pointer_segments(path: str) -> list[str]:
    """把 ``#/a/b`` 形式的 JSON Pointer 拆成解码后的段列表；根指针 ``#`` 为 []。"""
    body = path[1:]  # 去掉前缀 '#'
    return [unescape_segment(seg) for seg in body[1:].split("/")] if body else []


def is_valid_pointer(path: Any) -> bool:
    """校验门禁规则中的 path：必须是 ``#`` 或 ``#/段/段…``，段不能为空。"""
    if not isinstance(path, str) or not path.startswith("#"):
        return False
    return path == "#" or (path.startswith("#/") and all(path[2:].split("/")))


def path_has_wildcard(pattern: str) -> bool:
    return any(seg == "*" for seg in pointer_segments(pattern))


def path_matches(pattern: str, actual: str) -> bool:
    """规则 path 是否命中 issue path：段数相同且逐段相等或规则段为 ``*``。"""
    rule_segments = pointer_segments(pattern)
    issue_segments = pointer_segments(actual)
    if len(rule_segments) != len(issue_segments):
        return False
    return all(rule_seg == "*" or rule_seg == issue_seg
               for rule_seg, issue_seg in zip(rule_segments, issue_segments))


# ---------------------------------------------------------------------------
# 豁免规则匹配
# ---------------------------------------------------------------------------

def _field_matches(rule_value: Any, issue_value: Any) -> bool:
    return rule_value == "*" or rule_value == issue_value


def _exact_field_count(rule: dict) -> int:
    """direction、code 非 '*' 各计 1；path 不含通配段再计 1。"""
    return (
        int(rule.get("direction") != "*")
        + int(rule.get("code") != "*")
        + int(not path_has_wildcard(rule.get("path", "#")))
    )


def _literal_segment_count(rule: dict) -> int:
    """path 中字面段（非 '*'）的数量。"""
    return sum(1 for seg in pointer_segments(rule.get("path", "#")) if seg != "*")


def _rule_matches(rule: dict, issue: dict) -> bool:
    return (
        _field_matches(rule.get("direction"), issue.get("direction"))
        and _field_matches(rule.get("code"), issue.get("code"))
        and path_matches(rule.get("path", "#"), str(issue.get("path", "#")))
    )


def match_rule(issue: dict, rules: list[dict]) -> int | None:
    """返回命中的唯一规则序号（0 基下标）；无命中返回 None。

    依次按精确字段数、path 字面段数、声明顺序（靠后者胜）确定唯一规则。
    """
    best: tuple[int, int, int] | None = None
    best_index: int | None = None
    for index, rule in enumerate(rules):
        if not _rule_matches(rule, issue):
            continue
        rank = (_exact_field_count(rule), _literal_segment_count(rule), index)
        if best is None or rank > best:
            best, best_index = rank, index
    return best_index


# ---------------------------------------------------------------------------
# 门禁判定
# ---------------------------------------------------------------------------

def _issue_entry(issue: dict) -> dict:
    """从报告 issue 提取门禁条目（不含反例等大字段）。"""
    return {
        "direction": issue.get("direction"),
        "path": issue.get("path"),
        "code": issue.get("code"),
        "severity": issue.get("severity"),
        "message": issue.get("message"),
        "reason": issue.get("reason"),
    }


def evaluate_gate(
    report: dict,
    *,
    directions: list[str],
    warning_limits: dict[str, int] | None = None,
    exemptions: list[dict] | None = None,
) -> dict:
    """对报告执行发布门禁检查（纯函数，不修改入参报告）。

    返回 pass、各检查方向计数、阻断项与豁免项（含命中的规则序号，从 1 起）。
    """
    warning_limits = warning_limits or {}
    rules = exemptions or []

    # 请求方向去重并保持声明顺序，过滤未知方向（正常已被 Pydantic 拦截）
    checked: list[str] = []
    for direction in directions:
        if direction in DIRECTIONS and direction not in checked:
            checked.append(direction)

    issues_by_direction: dict[str, list[dict]] = {d: [] for d in checked}
    for issue in report.get("issues", []):
        direction = issue.get("direction")
        if direction in issues_by_direction:
            issues_by_direction[direction].append(issue)

    # 第一遍：为每条 issue 确定唯一豁免规则（或 None），并统计计数
    matched_rule: dict[int, int | None] = {}
    counts: dict[str, dict[str, int]] = {}
    for direction in checked:
        errors = warnings = exempted = 0
        for issue in issues_by_direction[direction]:
            rule_index = match_rule(issue, rules)
            matched_rule[id(issue)] = rule_index
            if rule_index is not None:
                exempted += 1
            elif issue.get("severity") == "error":
                errors += 1
            else:
                warnings += 1
        counts[direction] = {
            "errors": errors,
            "warnings": warnings,
            "exempted": exempted,
            "total": len(issues_by_direction[direction]),
        }

    # 第二遍：按报告顺序生成阻断项 / 豁免项；warning 按顺序超出上限的部分阻断
    blocking: list[dict] = []
    exempted_items: list[dict] = []
    direction_results: dict[str, dict] = {}
    passed = True

    for direction in checked:
        limit = int(warning_limits.get(direction, 0))
        warnings_seen = 0
        direction_blocking = 0
        for issue in issues_by_direction[direction]:
            rule_index = matched_rule[id(issue)]
            entry = _issue_entry(issue)
            if rule_index is not None:
                exempted_items.append({**entry, "rule_index": rule_index + 1})
            elif entry["severity"] == "error":
                blocking.append({**entry, "blocked_by": "error"})
                direction_blocking += 1
            else:
                warnings_seen += 1
                if warnings_seen > limit:
                    blocking.append({**entry, "blocked_by": "warning_limit"})
                    direction_blocking += 1

        warning_count = counts[direction]["warnings"]
        limit_exceeded = warning_count > limit
        if counts[direction]["errors"] > 0 or limit_exceeded:
            passed = False

        direction_results[direction] = {
            **counts[direction],
            "warning_limit": limit,
            "warning_limit_exceeded": limit_exceeded,
            "blocking": direction_blocking,
        }

    return {
        "pass": passed,
        "checked_directions": checked,
        "directions": direction_results,
        "blocking": blocking,
        "exempted": exempted_items,
    }
