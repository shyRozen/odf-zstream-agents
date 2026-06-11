from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field


class ChangeSource(str, Enum):
    JIRA = "jira"
    ERRATA = "errata"
    GIT = "git"


class ChangeType(str, Enum):
    BUGFIX = "bugfix"
    SECURITY = "security"
    ENHANCEMENT = "enhancement"


class Severity(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    LOW = "low"


class FailureType(str, Enum):
    PRODUCT_BUG = "product_bug"
    TEST_BUG = "test_bug"
    INFRA_ISSUE = "infra_issue"


class Change(BaseModel):
    id: str
    source: ChangeSource
    component: str
    type: ChangeType
    severity: Severity
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    linked_errata: str | None = None
    linked_commits: list[str] = Field(default_factory=list)


class CoverageSummary(BaseModel):
    total_changes: int = 0
    by_component: dict[str, int] = Field(default_factory=dict)


class ChangeManifest(BaseModel):
    zstream_version: str
    previous_version: str
    changes: list[Change] = Field(default_factory=list)
    coverage_summary: CoverageSummary = Field(default_factory=CoverageSummary)


class TestSelection(BaseModel):
    test_node_id: str
    file_path: str
    relevance_score: float
    reason: str
    existing_marks: list[str] = Field(default_factory=list)
    component: str = ""


def component_marker_name(base_mark: str, component: str) -> str:
    """Build a per-component marker name from a base zstream marker and component."""
    comp_safe = component.replace("-", "_").replace(".", "_").lower()
    return f"{base_mark}_{comp_safe}"


class GapDetail(BaseModel):
    change_id: str
    component: str
    reason: str


class CoverageReport(BaseModel):
    total_changes: int = 0
    covered: int = 0
    gaps: int = 0
    coverage_ratio: float = 0.0
    gap_details: list[GapDetail] = Field(default_factory=list)


class TestResult(BaseModel):
    test_id: str
    name: str
    status: str
    duration_seconds: float = 0.0
    error_message: str | None = None


class JUnitResults(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0
    flaky: int = 0
    duration_seconds: float = 0.0
    test_details: list[TestResult] = Field(default_factory=list)


class FailureAnalysis(BaseModel):
    test_id: str
    test_name: str
    failure_type: FailureType
    root_cause: str
    confidence: float
    linked_bug: str | None = None
    error_snippet: str | None = None


class RegressionInfo(BaseModel):
    test_id: str
    test_name: str
    current_status: str
    previous_status: str
    first_failed_version: str | None = None


class AnalysisReport(BaseModel):
    pass_rate: float = 0.0
    classifications: list[FailureAnalysis] = Field(default_factory=list)
    regressions: list[RegressionInfo] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    markdown_report: str = ""
    slack_summary: str = ""


class StageError(BaseModel):
    stage: str
    error: str
    timestamp: str
    recoverable: bool = True
