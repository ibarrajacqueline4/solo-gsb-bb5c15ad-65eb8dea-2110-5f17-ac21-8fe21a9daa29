"""发布门禁引擎（app.gate）单元测试。"""
import pytest

from app import gate
from app.compat import HEURISTIC_SUFFIX


def make_issue(direction="backward", path="#", code="required_added",
               severity="error", message="m", reason="r"):
    return {
        "direction": direction, "path": path, "code": code,
        "severity": severity, "message": message, "reason": reason,
        "counterexample": None, "counterexample_path": None,
    }


def make_report(issues):
    def summarize(direction):
        subset = [i for i in issues if i["direction"] == direction]
        return {
            "errors": sum(1 for i in subset if i["severity"] == "error"),
            "warnings": sum(1 for i in subset if i["severity"] == "warning"),
            "total": len(subset),
        }

    return {"issues": issues, "summary": {
        "backward": summarize("backward"),
        "forward": summarize("forward"),
    }}


# ---------------------------------------------------------------------------
# JSON Pointer
# ---------------------------------------------------------------------------

class TestPointer:
    def test_root(self):
        assert gate.pointer_segments("#") == []

    def test_segments(self):
        assert gate.pointer_segments("#/properties/a/items") == [
            "properties", "a", "items"
        ]

    def test_unescape(self):
        assert gate.pointer_segments("#/properties/a~1b/~0c") == [
            "properties", "a/b", "~c"
        ]

    @pytest.mark.parametrize("path,ok", [
        ("#", True),
        ("#/properties/a", True),
        ("#/a~1b", True),
        ("#/properties//x", False),   # 空段
        ("/properties/a", False),     # 缺少 # 前缀
        ("properties/a", False),
        ("", False),
        (123, False),
    ])
    def test_is_valid_pointer(self, path, ok):
        assert gate.is_valid_pointer(path) is ok

    @pytest.mark.parametrize("pattern,actual,ok", [
        ("#", "#", True),
        ("#/properties/a", "#/properties/a", True),
        ("#/properties/a", "#/properties/b", False),
        ("#/properties/*", "#/properties/a", True),
        ("#/properties/*/items", "#/properties/a/items", True),
        ("#/properties/*/items", "#/properties/a/b/items", False),  # 段数不同
        ("#/a/items", "#/a/items/0", False),
        ("#/*", "#", False),            # 根无段
        # 解码后比较：a~1b 是单个段 "a/b"（~1 解码不引入段分隔）
        ("#/properties/a~1b", "#/properties/a/b", False),
        ("#/x~1y", "#/x/y", False),
        ("#/a~1b", "#/a~1b", True),
    ])
    def test_path_matches(self, pattern, actual, ok):
        assert gate.path_matches(pattern, actual) is ok


# ---------------------------------------------------------------------------
# 规则匹配优先级
# ---------------------------------------------------------------------------

class TestMatchRule:
    def test_no_match(self):
        rules = [{"direction": "forward", "code": "*", "path": "#"}]
        issue = make_issue(direction="backward")
        assert gate.match_rule(issue, rules) is None

    def test_exact_fields_outrank_wildcards(self):
        issue = make_issue(direction="backward", code="required_added",
                           path="#/properties/a")
        rules = [
            {"direction": "*", "code": "*", "path": "#/properties/*"},             # 0
            {"direction": "backward", "code": "*", "path": "#/properties/*"},      # 1
            {"direction": "backward", "code": "required_added",
             "path": "#/properties/*"},                                            # 2
        ]
        assert gate.match_rule(issue, rules) == 2

    def test_literal_path_segments_break_tie(self):
        issue = make_issue(path="#/properties/a/items")
        rules = [
            {"direction": "*", "code": "*", "path": "#/properties/*/items"},
            {"direction": "*", "code": "*", "path": "#/properties/a/items"},
        ]
        assert gate.match_rule(issue, rules) == 1

    def test_wildcard_path_is_one_exact_field_fewer(self):
        # 两条都精确到 direction+code；字面 path（3 精确字段）胜过通配 path（2）
        issue = make_issue(path="#/properties/a")
        rules = [
            {"direction": "backward", "code": "required_added",
             "path": "#/properties/*"},
            {"direction": "backward", "code": "required_added",
             "path": "#/properties/a"},
        ]
        assert gate.match_rule(issue, rules) == 1

    def test_declaration_order_wins_when_fully_tied(self):
        issue = make_issue()
        rules = [
            {"direction": "*", "code": "*", "path": "#"},
            {"direction": "*", "code": "*", "path": "#"},
        ]
        assert gate.match_rule(issue, rules) == 1

    def test_literal_segment_count_beats_earlier_more_generic(self):
        # 精确字段数相同（均仅 direction 精确、path 含通配 = 1），
        # 但 #/properties/*/items（3 字面段）比 #/properties/*（0 字面段）更具体
        issue = make_issue(direction="backward", code="x",
                           path="#/properties/a/items")
        rules = [
            {"direction": "backward", "code": "*", "path": "#/properties/*"},
            {"direction": "backward", "code": "*", "path": "#/properties/*/items"},
        ]
        assert gate.match_rule(issue, rules) == 1


# ---------------------------------------------------------------------------
# 门禁判定
# ---------------------------------------------------------------------------

class TestEvaluateGate:
    def test_pass_when_no_issues(self):
        result = gate.evaluate_gate(
            make_report([]), directions=["backward", "forward"],
        )
        assert result["pass"] is True
        assert result["directions"]["backward"] == {
            "errors": 0, "warnings": 0, "exempted": 0, "total": 0,
            "warning_limit": 0, "warning_limit_exceeded": False, "blocking": 0,
        }
        assert result["blocking"] == []
        assert result["exempted"] == []

    def test_error_blocks(self):
        report = make_report([make_issue(severity="error")])
        result = gate.evaluate_gate(report, directions=["backward"])
        assert result["pass"] is False
        assert len(result["blocking"]) == 1
        assert result["blocking"][0]["blocked_by"] == "error"

    def test_warning_blocks_by_default_limit_zero(self):
        report = make_report([make_issue(severity="warning")])
        result = gate.evaluate_gate(report, directions=["backward"])
        assert result["pass"] is False
        assert result["blocking"][0]["blocked_by"] == "warning_limit"
        assert result["directions"]["backward"]["warning_limit_exceeded"] is True

    def test_warning_within_limit_passes(self):
        report = make_report([
            make_issue(code="a", severity="warning"),
            make_issue(code="b", severity="warning"),
        ])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            warning_limits={"backward": 2},
        )
        assert result["pass"] is True
        assert result["blocking"] == []
        assert result["directions"]["backward"]["warnings"] == 2

    def test_warning_overflow_marks_only_excess(self):
        report = make_report([
            make_issue(path="#/properties/a", code="wa", severity="warning"),
            make_issue(path="#/properties/b", code="wb", severity="warning"),
            make_issue(path="#/properties/c", code="wc", severity="warning"),
        ])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            warning_limits={"backward": 1},
        )
        assert result["pass"] is False
        assert [i["code"] for i in result["blocking"]] == ["wb", "wc"]
        assert all(i["blocked_by"] == "warning_limit" for i in result["blocking"])
        assert result["directions"]["backward"]["blocking"] == 2

    def test_exempted_issue_not_counted_and_rule_index_is_one_based(self):
        report = make_report([
            make_issue(code="required_added", severity="error"),
            make_issue(code="other", severity="warning"),
        ])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            exemptions=[{"direction": "backward", "code": "required_added",
                         "path": "#"}],
        )
        # error 被豁免后只剩 1 个 warning，默认上限 0 仍阻断
        stats = result["directions"]["backward"]
        assert stats["errors"] == 0
        assert stats["warnings"] == 1
        assert stats["exempted"] == 1
        assert stats["total"] == 2
        assert len(result["exempted"]) == 1
        assert result["exempted"][0]["rule_index"] == 1
        assert result["exempted"][0]["code"] == "required_added"
        assert len(result["blocking"]) == 1
        assert result["blocking"][0]["code"] == "other"

    def test_all_issues_exempted_passes(self):
        report = make_report([
            make_issue(code="required_added", severity="error"),
            make_issue(code="pattern_changed", severity="warning"),
        ])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            exemptions=[{"direction": "*", "code": "*", "path": "#"}],
        )
        assert result["pass"] is True
        assert result["blocking"] == []
        assert len(result["exempted"]) == 2
        assert {i["rule_index"] for i in result["exempted"]} == {1}

    def test_unchecked_direction_ignored(self):
        report = make_report([
            make_issue(direction="backward", severity="error"),
            make_issue(direction="forward", code="f", severity="error"),
        ])
        result = gate.evaluate_gate(report, directions=["forward"])
        assert result["pass"] is False
        assert [i["direction"] for i in result["blocking"]] == ["forward"]
        assert set(result["directions"]) == {"forward"}
        assert result["checked_directions"] == ["forward"]

    def test_directions_deduped_preserving_order(self):
        result = gate.evaluate_gate(
            make_report([]), directions=["forward", "forward", "backward"],
        )
        assert result["checked_directions"] == ["forward", "backward"]

    def test_path_wildcard_exemption(self):
        report = make_report([
            make_issue(path="#/properties/a/items", severity="error"),
            make_issue(path="#/properties/b/items", severity="error"),
            make_issue(path="#/properties/a", code="kept", severity="error"),
        ])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            exemptions=[{"direction": "*", "code": "*",
                         "path": "#/properties/*/items"}],
        )
        assert [i["code"] for i in result["blocking"]] == ["kept"]
        assert len(result["exempted"]) == 2

    def test_report_not_mutated(self):
        report = make_report([make_issue()])
        snapshot = repr(report)
        gate.evaluate_gate(
            report, directions=["backward"],
            exemptions=[{"direction": "*", "code": "*", "path": "#"}],
        )
        assert repr(report) == snapshot

    def test_blocking_order_follows_report(self):
        report = make_report([
            make_issue(path="#/1", code="e1", severity="error"),
            make_issue(path="#/2", code="w1", severity="warning"),
            make_issue(path="#/3", code="e2", severity="error"),
            make_issue(path="#/4", code="w2", severity="warning"),
        ])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            warning_limits={"backward": 1},
        )
        assert [i["code"] for i in result["blocking"]] == ["e1", "e2", "w2"]
        assert [i["blocked_by"] for i in result["blocking"]] == [
            "error", "error", "warning_limit",
        ]

    def test_exemption_payload_uses_decoded_path(self):
        # issue path 带 RFC6901 转义，规则用解码后的字面段
        report = make_report([
            make_issue(path="#/properties/a~1b", severity="error"),
        ])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            exemptions=[{"direction": "backward", "code": "required_added",
                         "path": "#/properties/a~1b"}],
        )
        assert result["pass"] is True

    def test_heuristic_suffix_irrelevant_to_matching(self):
        # 匹配只看 direction/code/path，warning 降级后缀不影响
        issue = make_issue(severity="warning", reason="原因" + HEURISTIC_SUFFIX)
        report = make_report([issue])
        result = gate.evaluate_gate(
            report, directions=["backward"],
            exemptions=[{"direction": "backward", "code": "required_added",
                         "path": "#"}],
        )
        assert result["pass"] is True
