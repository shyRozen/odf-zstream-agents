# ODF Z-Stream Implementation Notes

> Implementation record for [[ODF-ZStream-Multi-Agent-Plan-v2]]
> Built: 2026-05-04

---

## What Was Built

### Architecture Delivered

**Hierarchical LangGraph pipeline** — no CrewAI, no NATS, no swarm. Single framework, single mental model.

```
Pipeline Orchestrator (top-level StateGraph)
├── Inspect Manager (sub-graph: fan-out/fan-in)
│   ├── Jira Inspector       → Sonnet, queries Jira Cloud API + fetches PR URLs from remote links
│   ├── Errata Parser        → (disabled — insufficient API access)
│   ├── PR Analyzer          → No LLM, fetches PR changed files from GitHub API
│   └── Merge & Cross-Ref    → Sonnet, deduplicates and reconciles
│
├── Map Tests Manager (sub-graph: sequential + retry loop)
│   ├── Code Analyzer        → Sonnet, component → squad → test functions (per-testcase)
│   ├── Mark Matcher         → No LLM, heuristic scoring (PR files, components, keywords)
│   └── Coverage Validator   → Sonnet, gap analysis
│
├── PR Builder (single node) → Sonnet, branch + marks + PR
├── Jenkins Agent (single node) → No LLM, API trigger + poll
│
├── Analyze Manager (sub-graph: DAG)
│   ├── Classifier           → No LLM, JUnit XML parsing
│   ├── Root Cause Analyzer  → Opus, failure classification
│   ├── Regression Detector  → Sonnet, historical comparison
│   └── Report Generator     → Sonnet, markdown + Slack output
│
└── Notifier (single node)   → No LLM, Slack + PR comment
```

### Project Stats

| Metric | Value |
|--------|-------|
| Total source files | 37 |
| Lines of code | ~6,200 |
| LangGraph sub-graphs | 3 (inspect, map, analyze) |
| Agent nodes | 14 |
| Tool functions | 23 across 9 modules |
| Test index | 532 files, 1058 test functions (from ocs-ci-codebase-map repo) |
| Nodes using Opus | 1 (root_cause) |
| Nodes using Sonnet | 7 |
| Nodes using no LLM | 6 (pr_analyzer, jenkins, classifier, notifier, mark_matcher, merge fallback) |

### Project Location

```
~/claude-sessions/multi_agent/odf-zstream-agents/
├── core/          # config, models, state, LLM client
├── graph/         # pipeline orchestrator + 3 sub-graphs
├── nodes/         # 14 agent node implementations
├── tools/         # 23 tool functions (Jira, errata, git, ocs-ci, GitHub, Jenkins, Slack, DB)
├── cli/           # Typer CLI: zstream run <version>
├── api/           # FastAPI (stub)
├── config.yaml    # Pipeline, test selection, Jenkins, LLM config
├── docker-compose.yml  # PostgreSQL
└── pyproject.toml # Dependencies
```

### Runtime Architecture

All AI nodes run through **Claude Code CLI** (`claude --print`) via `core/agent_runner.py`. No LangChain, no LiteLLM in the hot path. Each call spawns a Claude subprocess with the prompt via stdin.

Key settings per call:
- `start_new_session=True` — isolates from parent terminal
- `TERM=dumb`, `NO_COLOR=1` — prevents escape sequences
- Timeout: 180s default, 300s for Opus nodes
- Model routing: `sonnet` for most nodes, `opus` for mark_matcher and root_cause

Nodes call `run_node(prompt, node_name)` — model selection is automatic based on config. Deterministic nodes (classifier, notifier, merge fallback) use no AI at all.

### Key Technical Decisions

| Decision | Why |
|----------|-----|
| Claude Code CLI (not LangChain/LiteLLM) | Direct subprocess call, no framework overhead. CLI handles auth, tool access, model routing |
| Unified `run_node()` API | Nodes don't know the runtime details. Handles model routing, timeouts, and error recovery |
| Plain functions + separate `_tool` wrappers | `@tool` wraps in `StructuredTool` (not callable). Nodes call raw functions; `_tool` variants exist for potential future ReAct use |
| JSON string returns from all tools | Tools return serialized JSON. Nodes parse with `json.loads()` |
| Every AI node has a deterministic fallback | If Claude is unavailable, nodes use regex, heuristics, and templates |
| `Annotated[list, add]` reducers for error lists | Errors from parallel sub-graph nodes merge automatically |

### Tools Implemented

AI nodes delegate to Claude Code which can use Read, Bash, WebSearch. Tool modules below are used by deterministic nodes and provide the API wrappers.

| Module | Functions | External Service |
|--------|-----------|-----------------|
| `jira_tools.py` | `jira_search`, `jira_get_issue`, `jira_create_bug` | Jira Cloud REST API v3 |
| `errata_tools.py` | `errata_fetch`, `errata_parse` | Red Hat errata API |
| `git_tools.py` | `git_diff_files`, `git_log_between`, `git_show_commit` | Local git via subprocess |
| `ocs_ci_tools.py` | `list_tests`, `read_test_marks`, `squad_map_lookup`, `read_test_source` | Local ocs-ci repo (AST parsing) |
| `ocs_ci_scanner.py` | `scan_tests`, `build_test_index` | Builds test index (532 files, 1058 tests) for per-testcase selection |
| `github_tools.py` | `github_create_branch`, `github_add_mark_to_test`, `github_create_pr`, `github_comment_pr` | GitHub API via PyGithub |
| `jenkins_tools.py` | `jenkins_trigger_build`, `jenkins_get_build_status`, `jenkins_get_test_report`, `jenkins_get_console_log` | Jenkins REST API |
| `slack_tools.py` | `slack_post_message` | Slack webhook |
| `db_tools.py` | `save_pipeline_results`, `query_historical_results` | PostgreSQL via psycopg2 |

### Per-Testcase Selection

The pipeline selects individual test functions (e.g. `test_failover.py::TestFailover::test_failover`) instead of directories. Uses per-version test indexes from the `ocs-ci-codebase-map` repo — each `release-X.Y` branch has its own `test-index.json` + full Obsidian vault. When running `zstream run 4.20.5`, the pipeline does a shallow single-branch clone (`git clone --depth 1 --branch release-4.20`) to download only the needed data (~1.5MB). See [[ODF-ZStream-Next-Steps#Step 10]] for the update script and version details.

### PR-Driven Test Selection

Jira Inspector fetches remote links from each DFBUGS issue to extract GitHub PR URLs. The PR Analyzer node fetches actual changed files from each PR via GitHub API. Test scoring uses PR file paths as the strongest relevance signal (0.95). Component match alone scores 0.70; keyword overlap is needed for higher scores. A dynamic threshold cuts tests below 70% of the top score.

Force-include guarantees at least one test per changed component, using `component_test_mapping` directories (not substring matching) to determine coverage. This prevents false positives like `test_mcg_hpa.py` in `monitoring/` being counted as MCG coverage.

JIRA_COMPONENT_MAP normalizes DFBUGS component names (e.g. "Multi-Cloud Object Gateway" to "mcg", "ceph-monitoring" to "monitoring", "odf-cli" to "odf-cli").

### PR Builder

Creates a branch from `release-X.Y` (not master), registers z-stream markers following ocs-ci conventions, and opens a PR targeting the release branch. Each run uses a timestamped branch name to avoid collisions. All commits include `Signed-off-by` for DCO compliance (configured via `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` in `.env`). PRs are labeled "Automatic AI Generated".

**Per-component markers** (Pillar 2 optimization): each test gets a global marker (`zstream_4_18_1`) AND a component-specific marker (`zstream_4_18_1_ocs_operator`). This enables per-deployment test selection — each Jenkins deployment runs only the tests relevant to its fixes via `TEST_MARK_EXPRESSION`.

Marker registration in the PR:
1. `pytest.ini` — registers global + per-component markers for `--strict-markers`
2. `marks.py` — adds marker variables for import (e.g. `zstream_4_18_1_mcg = pytest.mark.zstream_4_18_1_mcg`)
3. Each test file — `github_add_marks_to_test()` applies all relevant marks in a single commit per file (avoids SHA conflicts)

The `component` field on `TestSelection` (populated by mark_matcher from `dir_to_component` mapping) drives which component marker each test gets. Helper `component_marker_name()` in `core/models.py` normalizes component names to valid marker suffixes.

### Topology Selector (Pillars 1+2 from [[Lane-C-ZStream-Optimization-Plan]])

Uses a 152-config Jenkins deployment catalog extracted from `ocs4-jenkins/jobs/qe_ci_production_job_triggers.groovy`. Covers 7 platforms: vSphere (68 configs), AWS (54), Baremetal (14), Azure (7), IBM Cloud (4), GCP (3), RHV (2).

**Platform priority** for bugs without a specific platform: `vsphere > ibmcloud > aws > baremetal > gcp > azure > rhv`. Platform-agnostic bugs join an existing deployment (saving clusters) rather than creating a new one. Only when no deployment exists does the priority create a new one.

**Install type default**: UPI when the bug doesn't specify (most configs support UPI).

Flow:
1. Fetches Jira bug descriptions, parses DFBUGS template fields for platform/deployment type
2. Skips CVE bugs (no platform info in their descriptions, saves ~20 API calls)
3. AI selects the best matching `CLUSTER_CONF` from the catalog per fix
4. Falls back to two-pass heuristic if AI fails: specific-platform bugs first, then agnostic bugs join existing deployments
5. Groups fixes by deployment config
6. **Composes per-deployment `TEST_MARK_EXPRESSION`** from component markers of each deployment's fixes (e.g. `zstream_4_18_1_mcg or zstream_4_18_1_csi`)
7. Shows test count per deployment
8. Adds deployment plan as formatted Markdown comment on the PR

All specs set `RUN_TEARDOWN=false` and `OCS_CI_REPOSITORY_BRANCH=pr/<number>|release-X.Y` so Jenkins uses the z-stream PR before it's merged.

### Jenkins Deployment API

`jenkins_tools.py` provides full Jenkins integration:
- `jenkins_trigger_build(job, params)` — trigger a parameterized build
- `jenkins_queue_to_build(job, queue_id)` — resolve queue item to build number
- `jenkins_deploy(spec)` — trigger one deployment from a topology spec
- `jenkins_deploy_all(specs)` — trigger all topologies from topology selector output
- `jenkins_get_build_status/test_report/console_log` — poll and collect results

### PR Analyzer Resilience

- Skips own z-stream PRs from previous runs (detected by title containing "z-stream"/"zstream"/"test enablement")
- Shows per-PR progress: `[1/29] noobaa/noobaa-core/pull/9676`
- If AI analysis fails on a PR, prints error and continues with basic summary
- External repos (NaturalIntelligence/fast-xml-parser, digitalbazaar/forge) are processed normally

### Process Stability

- Claude CLI subprocesses run with `start_new_session=True`, `TERM=dumb`, `NO_COLOR=1` to prevent terminal interference
- Default AI timeout: 180s (configurable in `config.yaml`)
- SIGTERM/SIGHUP/SIGPIPE signals are caught and ignored — something sends SIGTERM during long runs (~5 minutes in), but the pipeline continues
- Large z-streams (37+ bugs, 29+ PRs) take 8-12 minutes total
- `run_zstream.sh` wrapper script available for bash-level signal trapping + logging

### CLI Modes

- `--collect-only` — stops after test selection (inspect + map stages)
- `--stop-after-pr` — stops after PR is created (inspect + map + PR stages)
- `--plan-deploy` — classify fixes by topology, print Jenkins API calls (dry run)
- `--deploy` — classify fixes by topology AND trigger Jenkins deployments
- Full run — all 6 stages including Jenkins + analysis

### Jira Integration

Beyond the pipeline, the tools support:
- `jira_search(version)` — search DFBUGS by fixVersion
- `jira_get_issue(key)` — get full issue details including description
- Creating issues in any project (used for OCSQE-5130)
- Transitioning issues (used to mark ODFE-133 as Obsolete)

### Errata Disabled

The errata parser returns empty due to insufficient API access. The pipeline proceeds with Jira + PR data only.

### Bugs Fixed During Build

1. **`StructuredTool` not callable** — `@tool` decorator wraps functions, making them non-callable directly. Fixed by separating raw functions from tool-wrapped variants.
2. **Git diff missing `repo_path` argument** — Node called `git_diff_files(from, to)` but tool signature requires `git_diff_files(repo_path, from, to)`.
3. **Jenkins node parameter mismatch** — Node used `build_id=` kwarg but tool expects `build_number=`. Also `params=` vs `parameters=` mismatch.
4. **Jira results not parsed** — Tool returns JSON string, node treated it as list of dicts. Added `json.loads()` parsing.
5. **ChatLiteLLM deprecation** — Moved to try/except import pattern for `langchain-litellm` package.

### Verification

```bash
# Small z-stream (3 bugs, ~5 min)
zstream run 4.18.1 --stop-after-pr --plan-deploy --max-tests 30

# Large z-stream (37 bugs, ~10 min)
timeout 900 zstream run 4.20.14 --stop-after-pr --plan-deploy --max-tests 60

# Collect-only (no PR, no deploy plan)
zstream run 4.16.13 --collect-only --max-tests 30

# Dry run (show initial state only)
zstream run 4.16.2 --dry-run

# Dry run (shows initial state)
zstream run 4.16.2 --dry-run
```
