from __future__ import annotations
from typing import Annotated, TypedDict
from operator import add

from core.models import (
    AnalysisReport,
    Change,
    ChangeManifest,
    CoverageReport,
    FailureAnalysis,
    JUnitResults,
    RegressionInfo,
    StageError,
    TestSelection,
)


def replace(_, new):
    return new


class PipelineState(TypedDict, total=False):
    zstream_version: str
    previous_version: str
    change_manifest: ChangeManifest
    selected_tests: list[TestSelection]
    coverage_report: CoverageReport
    pr_url: str
    pr_number: int
    jenkins_build_id: int
    jenkins_build_url: str
    junit_results: JUnitResults
    analysis_report: AnalysisReport
    errors: Annotated[list[StageError], add]
    current_stage: str


class InspectState(TypedDict, total=False):
    zstream_version: str
    previous_version: str
    jira_changes: list[Change]
    errata_changes: list[Change]
    git_changes: list[Change]
    change_manifest: ChangeManifest
    errors: Annotated[list[StageError], add]


class MapState(TypedDict, total=False):
    version: str
    change_manifest: ChangeManifest
    test_map_context: str
    search_areas: list[str]
    component_test_mapping: dict[str, list[str]]
    scored_tests: list[TestSelection]
    selected_tests: list[TestSelection]
    coverage_report: CoverageReport
    attempt_count: int
    errors: Annotated[list[StageError], add]


class AnalyzeState(TypedDict, total=False):
    junit_results: JUnitResults
    change_manifest: ChangeManifest
    classifications: Annotated[list[FailureAnalysis], add]
    regressions: Annotated[list[RegressionInfo], add]
    analysis_report: AnalysisReport
    errors: Annotated[list[StageError], add]
