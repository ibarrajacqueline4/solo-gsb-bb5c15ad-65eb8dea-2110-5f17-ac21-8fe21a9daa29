"""Pydantic 请求 / 响应模型。"""
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.gate import is_valid_pointer


class AnalysisCreate(BaseModel):
    """POST /api/analyses 请求体。"""

    name: str | None = Field(default=None, max_length=255, description="可选的分析名称")
    old_schema: dict[str, Any] = Field(..., description="旧版 JSON Schema")
    new_schema: dict[str, Any] = Field(..., description="新版 JSON Schema")
    samples: list[Any] | None = Field(default=None, description="可选的样例文档列表")


class Issue(BaseModel):
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str
    counterexample: Any | None = None
    counterexample_path: str | None = None


class DirectionSummary(BaseModel):
    errors: int
    warnings: int
    total: int


class ReportSummary(BaseModel):
    backward: DirectionSummary
    forward: DirectionSummary
    total: int
    errors: int
    warnings: int


class SampleResult(BaseModel):
    index: int
    valid_against_old: bool
    valid_against_new: bool
    direction: str | None = None


class Report(BaseModel):
    backward_compatible: bool
    forward_compatible: bool
    directions: dict[str, str]
    summary: ReportSummary
    issues: list[Issue]
    sample_results: list[SampleResult]


class AnalysisResponse(BaseModel):
    id: int
    name: str | None
    created_at: str
    old_schema: dict[str, Any]
    new_schema: dict[str, Any]
    samples: list[Any]
    report: Report


class AnalysisListItem(BaseModel):
    id: int
    name: str | None
    created_at: str
    backward_compatible: bool
    forward_compatible: bool
    errors: int
    warnings: int


class AnalysisListResponse(BaseModel):
    total: int
    items: list[AnalysisListItem]


class CompareRequest(BaseModel):
    """POST /api/analyses/compare 请求体。"""

    baseline_id: int = Field(..., ge=1, description="基线分析记录 ID")
    current_id: int = Field(..., ge=1, description="当前分析记录 ID")


class ComparedIssue(BaseModel):
    """对比结果中的一条 issue（含稳定指纹，不含反例等大字段）。"""

    fingerprint: str
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str


class SeverityChangedIssue(ComparedIssue):
    baseline_severity: Literal["error", "warning"]
    current_severity: Literal["error", "warning"]


class IssueGroups(BaseModel):
    added: list[ComparedIssue]
    fixed: list[ComparedIssue]
    persisting: list[ComparedIssue]
    severity_changed: list[SeverityChangedIssue]


class IssueGroupCounts(BaseModel):
    added: int
    fixed: int
    persisting: int
    severity_changed: int


class CompatibilityChange(BaseModel):
    baseline: bool
    current: bool
    changed: bool


class CompatibilityDiff(BaseModel):
    backward: CompatibilityChange
    forward: CompatibilityChange


class MetricDiff(BaseModel):
    baseline: int
    current: int
    delta: int


class DirectionSummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff


class SummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff
    backward: DirectionSummaryDiff
    forward: DirectionSummaryDiff


class CompareResponse(BaseModel):
    baseline_id: int
    current_id: int
    compatibility: CompatibilityDiff
    summary_diff: SummaryDiff
    issues: IssueGroups
    counts: IssueGroupCounts


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate 发布门禁
# ---------------------------------------------------------------------------

class GateExemption(BaseModel):
    """一条 issue 豁免规则；三字段均可写 ``*`` 通配（path 支持单段 *）。"""

    direction: Literal["backward", "forward", "*"] = Field(
        ..., description="兼容方向，* 表示任意方向"
    )
    code: str = Field(..., min_length=1, description="issue 代码，* 表示任意代码")
    path: str = Field(
        ...,
        description="JSON Pointer 形式的 issue path（# 或 #/a/b），单段 * 通配",
    )

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code 不能为空")
        return value

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not is_valid_pointer(value):
            raise ValueError(
                "path 必须是 '#' 或 '#/段/段…' 形式的 JSON Pointer，段不能为空"
            )
        return value


class GateRequest(BaseModel):
    """POST /api/analyses/{id}/gate 请求体。"""

    directions: list[Literal["backward", "forward"]] = Field(
        ..., min_length=1, description="需要检查的兼容方向（去重后按声明顺序）"
    )
    warning_limits: dict[Literal["backward", "forward"], int] = Field(
        default_factory=dict, description="各方向允许的未豁免 warning 上限（默认 0）"
    )
    exemptions: list[GateExemption] = Field(
        default_factory=list, description="issue 豁免规则，序号从 1 开始"
    )

    @field_validator("directions")
    @classmethod
    def _dedupe_directions(cls, value: list[str]) -> list[str]:
        deduped: list[str] = []
        for direction in value:
            if direction not in deduped:
                deduped.append(direction)
        return deduped

    @field_validator("warning_limits")
    @classmethod
    def _non_negative_limits(cls, value: dict[str, int]) -> dict[str, int]:
        for direction, limit in value.items():
            if limit < 0:
                raise ValueError(f"{direction} 的 warning 上限不能为负数")
        return value


class GateIssue(BaseModel):
    """门禁结果中的轻量 issue（不含反例等大字段）。"""

    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str


class GateBlockingIssue(GateIssue):
    blocked_by: Literal["error", "warning_limit"]


class GateExemptedIssue(GateIssue):
    rule_index: int = Field(..., ge=1, description="命中的豁免规则序号（从 1 开始）")


class GateDirectionResult(BaseModel):
    errors: int
    warnings: int
    exempted: int
    total: int
    warning_limit: int
    warning_limit_exceeded: bool
    blocking: int


class GateResponse(BaseModel):
    pass_field: bool = Field(..., alias="pass", description="门禁是否通过")
    checked_directions: list[Literal["backward", "forward"]]
    directions: dict[str, GateDirectionResult]
    blocking: list[GateBlockingIssue]
    exempted: list[GateExemptedIssue]

    model_config = {"populate_by_name": True}
