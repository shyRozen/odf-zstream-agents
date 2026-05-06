"""CLI entry point for the ODF z-stream multi-agent pipeline.

Usage::

    zstream run 4.16.2
    zstream run 4.16.2 --collect-only
    zstream status
"""

from __future__ import annotations

import json

import typer

from core.state import PipelineState

app = typer.Typer(
    name="zstream",
    help="ODF z-stream test automation pipeline",
    add_completion=False,
)


def _parse_version(version: str) -> tuple[str, str]:
    """Parse a z-stream version into (current, previous).

    ``"4.16.2"`` -> ``("4.16.2", "4.16.1")``
    """
    parts = version.split(".")
    if len(parts) != 3:
        raise typer.BadParameter(f"Version must be in X.Y.Z format, got '{version}'")

    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        raise typer.BadParameter(f"Version components must be integers, got '{version}'")

    if patch < 1:
        raise typer.BadParameter(
            f"Patch version must be >= 1 for z-stream (need a previous version), got '{version}'"
        )

    previous = f"{major}.{minor}.{patch - 1}"
    return version, previous


@app.command()
def run(
    version: str = typer.Argument(
        ...,
        help="ODF z-stream version to process (e.g. 4.16.2)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the initial state and exit without executing",
    ),
    collect_only: bool = typer.Option(
        False,
        "--collect-only",
        help="Run inspect + map stages only — show selected tests without "
        "creating a PR or triggering Jenkins",
    ),
) -> None:
    """Run the z-stream pipeline for a given ODF version."""
    zstream, previous = _parse_version(version)

    mode = "collect-only" if collect_only else "full"
    typer.echo(f"ODF z-stream pipeline ({mode}): {previous} -> {zstream}")
    typer.echo("=" * 50)

    # Ensure the codebase map is available
    from core.test_map import ensure_map

    typer.echo("Downloading codebase map...")
    map_path = ensure_map(force_pull=True)
    typer.echo(f"  Map loaded: {map_path}")

    initial_state: PipelineState = {
        "zstream_version": zstream,
        "previous_version": previous,
        "errors": [],
        "current_stage": "init",
    }

    if dry_run:
        typer.echo("\n[dry-run] Initial state:")
        typer.echo(json.dumps(initial_state, indent=2, default=str))
        raise typer.Exit()

    from graph.pipeline import build_pipeline

    typer.echo("Building pipeline graph...")
    pipeline = build_pipeline(collect_only=collect_only)

    typer.echo("Invoking pipeline...\n")
    final_state = pipeline.invoke(initial_state)

    # ── Summary ──────────────────────────────────────────────────────
    typer.echo("\n" + "=" * 50)
    typer.echo("Pipeline complete!" if not collect_only else "Collection complete!")
    typer.echo("=" * 50)

    manifest = final_state.get("change_manifest")
    if manifest:
        typer.echo(f"\n  Changes found: {len(manifest.changes)}")
        for change in manifest.changes:
            typer.echo(
                f"    {change.id:16s} [{change.severity.value:8s}] "
                f"[{change.component}] {change.summary[:60]}"
            )

    selected = final_state.get("selected_tests") or []
    typer.echo(f"\n  Tests selected: {len(selected)}")
    if selected:
        typer.echo(f"  {'Test':<60s} {'Score':>5s}  Squad")
        typer.echo(f"  {'-'*60} {'-'*5}  {'-'*15}")
        for test in sorted(selected, key=lambda t: -t.relevance_score):
            marks = ", ".join(m for m in test.existing_marks if "squad" in m) or "?"
            typer.echo(f"  {test.test_node_id:<60s} {test.relevance_score:5.2f}  {marks}")

    coverage = final_state.get("coverage_report")
    if coverage:
        typer.echo(
            f"\n  Coverage: {coverage.coverage_ratio:.0%} "
            f"({coverage.covered}/{coverage.total_changes})"
        )
        if coverage.gap_details:
            typer.echo("  Gaps:")
            for gap in coverage.gap_details:
                typer.echo(f"    - [{gap.component}] {gap.reason}")

    if collect_only:
        errors = final_state.get("errors") or []
        if errors:
            typer.echo(f"\n  Errors ({len(errors)}):")
            for err in errors:
                typer.echo(f"    [{err.stage}] {err.error}")
        raise typer.Exit()

    # Full run — show remaining stages
    pr_url = final_state.get("pr_url")
    if pr_url:
        typer.echo(f"\n  PR: {pr_url}")

    junit = final_state.get("junit_results")
    if junit:
        typer.echo(
            f"  Test results: {junit.passed} passed, {junit.failed} failed, "
            f"{junit.errored} errored, {junit.skipped} skipped"
        )

    report = final_state.get("analysis_report")
    if report:
        typer.echo(f"  Pass rate: {report.pass_rate:.0%}")
        if report.regressions:
            typer.echo(f"  Regressions: {len(report.regressions)}")

    errors = final_state.get("errors") or []
    if errors:
        typer.echo(f"\n  Errors ({len(errors)}):")
        for err in errors:
            typer.echo(f"    [{err.stage}] {err.error}")


@app.command()
def status() -> None:
    """Show pipeline status."""
    typer.echo("Status: not implemented yet (needs DB)")


if __name__ == "__main__":
    app()
