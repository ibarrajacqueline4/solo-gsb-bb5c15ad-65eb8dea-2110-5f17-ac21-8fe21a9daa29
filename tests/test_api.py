"""FastAPI 接口测试（TestClient + 临时 SQLite）。"""
import os
import tempfile

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


OLD_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
    },
}
NEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_get_analysis(client):
    resp = client.post("/api/analyses", json={
        "name": "user-schema-v1-v2",
        "old_schema": OLD_SCHEMA,
        "new_schema": NEW_SCHEMA,
        "samples": [{"status": "pending"}],
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    analysis_id = data["id"]
    assert data["report"]["backward_compatible"] is False
    assert data["report"]["forward_compatible"] is True
    assert data["report"]["summary"]["errors"] >= 1

    # 反例字段存在
    issues = data["report"]["issues"]
    backward = [i for i in issues if i["direction"] == "backward"]
    assert backward
    assert any(i["counterexample"] is not None for i in backward)

    # 样例结果
    assert data["report"]["sample_results"][0]["valid_against_new"] is False

    # 查询报告
    resp2 = client.get(f"/api/analyses/{analysis_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == analysis_id

    # 列表
    resp3 = client.get("/api/analyses")
    assert resp3.status_code == 200
    assert resp3.json()["total"] >= 1


def test_get_missing_returns_404(client):
    resp = client.get("/api/analyses/999999")
    assert resp.status_code == 404


def test_invalid_schema_returns_400(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "not-a-real-type"},
        "new_schema": {"type": "string"},
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["field"] == "old_schema"


def test_compatible_schemas(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        "new_schema": {"type": "object",
                       "properties": {"a": {"type": "string"},
                                      "b": {"type": "integer"}}},
    })
    assert resp.status_code == 201
    report = resp.json()["report"]
    assert report["backward_compatible"]
    assert report["forward_compatible"]
    assert report["summary"]["errors"] == 0


# ---------------------------------------------------------------------------
# POST /api/analyses/compare
# ---------------------------------------------------------------------------

COMPARE_OLD = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
}
# v2：a 收紧 minLength、新增必填 b
COMPARE_V2 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "b": {"type": "integer"}},
    "required": ["a", "b"],
}
# v3：b 不再必填（修复）、新增必填 c（新引入）、a 的 minLength 保持（仍存在）
COMPARE_V3 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "c": {"type": "string"}},
    "required": ["a", "c"],
}


def _create(client, old_schema, new_schema, name=None):
    resp = client.post("/api/analyses", json={
        "name": name, "old_schema": old_schema, "new_schema": new_schema,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_compare_added_fixed_persisting(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2, "baseline")
    current = _create(client, COMPARE_OLD, COMPARE_V3, "current")

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["baseline_id"] == baseline["id"]
    assert data["current_id"] == current["id"]
    assert data["counts"] == {"added": 1, "fixed": 1, "persisting": 1,
                              "severity_changed": 0}

    added = data["issues"]["added"]
    fixed = data["issues"]["fixed"]
    persisting = data["issues"]["persisting"]
    assert [i["code"] for i in added] == ["required_added"]
    assert "'c'" in added[0]["reason"]
    assert [i["code"] for i in fixed] == ["required_added"]
    assert "'b'" in fixed[0]["reason"]
    assert [i["code"] for i in persisting] == ["min_length_increased"]

    # 每条对比 issue 都带稳定指纹
    for group in (added, fixed, persisting):
        for entry in group:
            assert len(entry["fingerprint"]) == 16

    # 双向兼容性：两个方向都保持不兼容 -> changed 为 False
    assert data["compatibility"]["backward"] == {
        "baseline": False, "current": False, "changed": False,
    }
    assert data["compatibility"]["forward"] == {
        "baseline": True, "current": True, "changed": False,
    }

    # 错误 / 警告汇总差异
    diff = data["summary_diff"]
    assert diff["errors"]["baseline"] == baseline["report"]["summary"]["errors"]
    assert diff["errors"]["current"] == current["report"]["summary"]["errors"]
    assert diff["errors"]["delta"] == (
        current["report"]["summary"]["errors"]
        - baseline["report"]["summary"]["errors"]
    )
    assert diff["warnings"]["delta"] == 0
    assert diff["backward"]["errors"]["baseline"] == 2


def test_compare_severity_only_change(client):
    # 同一 integer_required 问题：baseline 可构造验证反例（error），
    # current 中 maximum 限制使反例不再成立（降级 warning）
    baseline = _create(client, {"minimum": 0}, {"type": "integer"})
    current = _create(client, {"minimum": 0, "maximum": 1}, {"type": "integer"})

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    changed = data["issues"]["severity_changed"]
    assert data["counts"]["severity_changed"] == 1
    assert changed[0]["code"] == "integer_required"
    assert changed[0]["baseline_severity"] == "error"
    assert changed[0]["current_severity"] == "warning"
    # 仍存在：type_restricted 两份报告都有
    assert any(i["code"] == "type_restricted" for i in data["issues"]["persisting"])


def test_compare_does_not_modify_reports(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2)
    current = _create(client, COMPARE_OLD, COMPARE_V3)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200

    for original in (baseline, current):
        after = client.get(f"/api/analyses/{original['id']}").json()
        assert after["report"] == original["report"]
        assert after["old_schema"] == original["old_schema"]
        assert after["new_schema"] == original["new_schema"]


def test_compare_self_all_persisting(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)
    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": analysis["id"],
    })
    assert resp.status_code == 200
    data = resp.json()
    total = analysis["report"]["summary"]["total"]
    assert data["counts"]["persisting"] == total
    assert data["counts"]["added"] == 0
    assert data["counts"]["fixed"] == 0


def test_compare_missing_analysis_returns_404(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": 999999, "current_id": analysis["id"],
    })
    assert resp.status_code == 404

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": 999999,
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate
# ---------------------------------------------------------------------------

GATE_ERROR_OLD = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
}
GATE_ERROR_NEW = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    "required": ["b"],
}

GATE_WARN_OLD = {
    "type": "object",
    "properties": {"a": {"type": "string", "pattern": "^x+$"}},
}
GATE_WARN_NEW = {
    "type": "object",
    "properties": {"a": {"type": "string", "pattern": "^xy$"}},
}


def _gate(client, analysis_id, **body):
    return client.post(f"/api/analyses/{analysis_id}/gate", json=body)


def test_gate_missing_analysis_returns_404(client):
    resp = _gate(client, 999999, directions=["backward"])
    assert resp.status_code == 404


def test_gate_error_blocks_and_counts(client):
    analysis = _create(client, GATE_ERROR_OLD, GATE_ERROR_NEW)
    resp = _gate(client, analysis["id"], directions=["backward"])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["pass"] is False
    assert data["checked_directions"] == ["backward"]
    backward = data["directions"]["backward"]
    assert backward["errors"] >= 1
    assert backward["warning_limit"] == 0
    assert backward["blocking"] == len(data["blocking"])
    assert data["blocking"]
    assert all(item["blocked_by"] == "error" for item in data["blocking"])
    # 只检查 backward 时，forward 方向不参与
    assert "forward" not in data["directions"]
    assert all(item["direction"] == "backward" for item in data["blocking"])


def test_gate_clean_direction_passes_regardless_of_unchecked(client):
    analysis = _create(client, GATE_ERROR_OLD, GATE_ERROR_NEW)
    # forward 方向无 error（新增可选属性不破坏向前兼容）
    resp = _gate(client, analysis["id"], directions=["forward"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["pass"] is True
    assert data["directions"]["forward"]["errors"] == 0
    assert data["blocking"] == []


def test_gate_exemption_excludes_issue_and_reports_rule_index(client):
    analysis = _create(client, GATE_ERROR_OLD, GATE_ERROR_NEW)
    target = next(
        i for i in analysis["report"]["issues"]
        if i["direction"] == "backward" and i["code"] == "required_added"
    )
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[{
                     "direction": "backward",
                     "code": "required_added",
                     "path": target["path"],
                 }])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    stats = data["directions"]["backward"]
    assert stats["exempted"] == 1
    assert stats["errors"] == 0
    assert len(data["exempted"]) == 1
    assert data["exempted"][0]["rule_index"] == 1
    assert data["exempted"][0]["path"] == target["path"]
    assert data["pass"] is True
    assert data["blocking"] == []


def test_gate_path_wildcard_single_segment(client):
    analysis = _create(client,
                       {"type": "object",
                        "properties": {"a": {"type": "string"},
                                       "b": {"type": "string"}}},
                       {"type": "object",
                        "properties": {"a": {"type": "integer"},
                                       "b": {"type": "integer"}}})
    # 报告含 #/properties/a、#/properties/b 两处 type_removed，用单段 * 一并豁免
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[{"direction": "backward", "code": "type_removed",
                              "path": "#/properties/*"}])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass"] is True
    assert len(data["exempted"]) == 2
    assert {item["path"] for item in data["exempted"]} == {
        "#/properties/a", "#/properties/b"
    }


def test_gate_wildcard_does_not_cross_segments(client):
    analysis = _create(client,
                       {"type": "object",
                        "properties": {"a": {"type": "string"},
                                       "b": {"type": "string"}}},
                       {"type": "object",
                        "properties": {"a": {"type": "integer"},
                                       "b": {"type": "integer"}}})
    # #/* 只有一段，命不中 #/properties/a
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[{"direction": "backward", "code": "type_removed",
                              "path": "#/*"}])
    data = resp.json()
    assert data["pass"] is False
    assert len(data["blocking"]) == 2
    assert data["exempted"] == []


def test_gate_unique_rule_prefers_exact_then_literal_then_order(client):
    analysis = _create(client,
                       {"type": "object",
                        "properties": {"a": {"type": "string"}}},
                       {"type": "object",
                        "properties": {"a": {"type": "integer"}}})
    # 三条规则都命中该 issue：最精确的规则 3（direction+code+字面 path）必须唯一获胜
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[
                     {"direction": "*", "code": "*", "path": "#/properties/*"},
                     {"direction": "backward", "code": "type_removed",
                      "path": "#/properties/*"},
                     {"direction": "backward", "code": "type_removed",
                      "path": "#/properties/a"},
                 ])
    data = resp.json()
    assert data["pass"] is True
    assert len(data["exempted"]) == 1
    assert data["exempted"][0]["rule_index"] == 3

    # 前两者同精确字段数与字面段数，声明顺序靠后者（规则 2）获胜
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[
                     {"direction": "backward", "code": "type_removed",
                      "path": "#/properties/*"},
                     {"direction": "backward", "code": "type_removed",
                      "path": "#/properties/*"},
                 ])
    data = resp.json()
    assert data["exempted"][0]["rule_index"] == 2


def test_gate_warning_limit(client):
    analysis = _create(client, GATE_WARN_OLD, GATE_WARN_NEW)
    # backward 恰有 1 条 warning：上限 1 通过
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 warning_limits={"backward": 1})
    data = resp.json()
    assert resp.status_code == 200
    assert data["pass"] is True
    assert data["directions"]["backward"]["warnings"] == 1
    assert data["directions"]["backward"]["warning_limit_exceeded"] is False

    # 上限 0：warning 超限阻断
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 warning_limits={"backward": 0})
    data = resp.json()
    assert data["pass"] is False
    assert data["directions"]["backward"]["warning_limit_exceeded"] is True
    assert len(data["blocking"]) == 1
    assert data["blocking"][0]["blocked_by"] == "warning_limit"
    assert data["blocking"][0]["code"] == "pattern_changed"


def test_gate_exempted_warning_not_counted_toward_limit(client):
    analysis = _create(client, GATE_WARN_OLD, GATE_WARN_NEW)
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 warning_limits={"backward": 0},
                 exemptions=[{"direction": "backward", "code": "pattern_changed",
                              "path": "#/properties/a"}])
    data = resp.json()
    assert data["pass"] is True
    stats = data["directions"]["backward"]
    assert stats["warnings"] == 0
    assert stats["exempted"] == 1


def test_gate_both_directions_separate_limits(client):
    analysis = _create(client, GATE_WARN_OLD, GATE_WARN_NEW)
    # 两个方向各 1 条 warning；backward 限额 1 放行，forward 限额 0 阻断
    resp = _gate(client, analysis["id"],
                 directions=["backward", "forward"],
                 warning_limits={"backward": 1, "forward": 0})
    data = resp.json()
    assert data["pass"] is False
    assert data["directions"]["backward"]["warning_limit_exceeded"] is False
    assert data["directions"]["forward"]["warning_limit_exceeded"] is True
    assert [item["direction"] for item in data["blocking"]] == ["forward"]


def test_gate_directions_deduped(client):
    analysis = _create(client, GATE_WARN_OLD, GATE_WARN_NEW)
    resp = _gate(client, analysis["id"],
                 directions=["forward", "forward"],
                 warning_limits={"forward": 1})
    data = resp.json()
    assert resp.status_code == 200
    assert data["checked_directions"] == ["forward"]


def test_gate_does_not_modify_report(client):
    analysis = _create(client, GATE_ERROR_OLD, GATE_ERROR_NEW)
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[{"direction": "*", "code": "*", "path": "#/properties/*"}])
    assert resp.status_code == 200
    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["old_schema"] == analysis["old_schema"]
    assert after["new_schema"] == analysis["new_schema"]


def test_gate_validation_errors(client):
    analysis = _create(client, GATE_ERROR_OLD, GATE_ERROR_NEW)

    # directions 不能为空
    resp = _gate(client, analysis["id"], directions=[])
    assert resp.status_code == 422

    # 非法方向
    resp = _gate(client, analysis["id"], directions=["sideways"])
    assert resp.status_code == 422

    # warning 上限不能为负
    resp = _gate(client, analysis["id"],
                 directions=["backward"], warning_limits={"backward": -1})
    assert resp.status_code == 422

    # path 必须是 # 开头的 JSON Pointer
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[{"direction": "backward", "code": "x",
                              "path": "/properties/a"}])
    assert resp.status_code == 422

    # path 不允许空段
    resp = _gate(client, analysis["id"],
                 directions=["backward"],
                 exemptions=[{"direction": "backward", "code": "x",
                              "path": "#/properties//a"}])
    assert resp.status_code == 422


def test_gate_response_uses_pass_key(client):
    analysis = _create(client,
                       {"type": "object", "properties": {"a": {"type": "string"}}},
                       {"type": "object",
                        "properties": {"a": {"type": "string"},
                                       "b": {"type": "integer"}}})
    resp = _gate(client, analysis["id"], directions=["backward", "forward"])
    assert resp.status_code == 200
    assert "pass" in resp.json()
    assert resp.json()["pass"] is True
