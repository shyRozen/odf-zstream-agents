# ODF Z-Stream Multi-Agent Test Automation

AI-powered pipeline that automates the z-stream test lifecycle for **OpenShift Data Foundation (ODF)**. Inspects what went into a z-stream release (Jira bugs + GitHub PRs), selects individual test functions from the ocs-ci framework using per-version test indexes, creates a PR with temporary marks, triggers Jenkins runs, and analyzes results.

## Architecture

Hierarchical **LangGraph** pipeline — no CrewAI, no NATS, no swarm. Single framework, single mental model. AI nodes run via **Claude Code CLI** (`claude --print`). No LangChain, no LiteLLM in the hot path.

The pipeline is **PR-driven**: Jira Inspector fetches remote links from each bug to find GitHub PR URLs, then the PR Analyzer fetches changed files from each PR via GitHub API. Test selection scores individual test functions (not directories) against PR file paths as the strongest relevance signal.

```
Pipeline Orchestrator (top-level StateGraph)
├── Inspect Manager (sub-graph: fan-out/fan-in)
│   ├── Jira Inspector         Sonnet    Queries Jira + fetches PR URLs from remote links
│   ├── Errata Parser          (disabled — insufficient API access)
│   ├── PR Analyzer            No LLM    Fetches changed files from GitHub PRs
│   └── Merge & Cross-Ref      Sonnet    Deduplicates and reconciles
│
├── Map Tests Manager (sub-graph: sequential + retry)
│   ├── Code Analyzer          Sonnet    Component → squad → test functions
│   ├── Mark Matcher           No LLM    Heuristic scoring (PR files, components, keywords)
│   └── Coverage Validator     Sonnet    Gap analysis
│
├── PR Builder (single node)   Sonnet    Branch + marks + PR
├── Jenkins Agent (single)     No LLM    API trigger + poll
│
├── Analyze Manager (sub-graph: DAG)
│   ├── Classifier             No LLM    JUnit XML parsing
│   ├── Root Cause Analyzer    Opus      Failure classification
│   ├── Regression Detector    Sonnet    Historical comparison
│   └── Report Generator       Sonnet    Markdown + Slack output
│
└── Notifier (single node)     No LLM    Slack + PR comment
```

**14 nodes** | **3 sub-graphs** | **23 tool functions** | **2 use Opus, 7 use Sonnet, 5 need no LLM**

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your credentials:
#   ANTHROPIC_API_KEY, JIRA_API_TOKEN, JENKINS_API_TOKEN,
#   GITHUB_TOKEN, SLACK_WEBHOOK_URL
```

### 3. Run

```bash
# Full pipeline
zstream run 4.16.2

# Collect-only (inspect + map, show selected tests with scores)
zstream run 4.16.2 --collect-only

# Stop after PR is created (skip Jenkins + analysis)
zstream run 4.16.2 --stop-after-pr

# Plan deployments (classify fixes by topology, print Jenkins API calls)
zstream run 4.16.2 --collect-only --plan-deploy

# Actually trigger Jenkins deployments per topology
zstream run 4.16.2 --stop-after-pr --deploy

# Override max tests (default 50)
zstream run 4.16.2 --max-tests 30

# Dry run (show initial state, don't execute)
zstream run 4.16.2 --dry-run
```

### 4. (Optional) Start PostgreSQL for checkpointing

```bash
docker compose up -d
```

## Pipeline Stages

| Stage | Pattern | Nodes | What it Does |
|-------|---------|-------|-------------|
| **1. Inspect** | Fan-out → Fan-in | 4 | Query Jira (+ PR URLs from remote links), fetch PR changed files from GitHub, merge into Change Manifest. Errata disabled |
| **2. Map Tests** | Sequential + retry | 3 | Map changes to individual test functions (with component tagging) using per-version test index (downloaded from [ocs-ci-codebase-map](https://github.com/shyRozen/ocs-ci-codebase-map) `release-X.Y` branch), score relevance using PR file paths + components + keywords, validate coverage |
| **3. PR Builder** | Single node | 1 | Create branch, add global + per-component markers to tests, open PR |
| **4. Jenkins** | Single node + poll | 1 | Trigger `qe-deploy-ocs-cluster-prod`, poll until complete, fetch results |
| **5. Analyze** | DAG | 4 | Classify pass/fail, root cause failures, detect regressions, generate report |
| **6. Notify** | Single node | 1 | Post to Slack, comment on PR, file Jira bugs |

## Workflow Example

```
$ zstream run 4.16.2

[Stage 1/6] Inspect Manager
  ├── Jira Inspector       → 12 bugs, 8 with GitHub PRs (via remote links)
  ├── Errata Parser        → (disabled)
  ├── PR Analyzer          → 8 PRs, 47 changed files
  └── Merge & Cross-Ref    → 14 unique changes
  ⏱ 1m 48s

[Stage 2/6] Map Tests Manager
  ├── Code Analyzer        → ceph-csi (PV/SC), mcg, monitoring
  ├── Mark Matcher         → 1058 indexed, 34 selected (score > 0.70)
  │   Top: test_failover.py::TestFailover::test_failover (0.95)
  └── Coverage Validator   → 14/14 covered
  ⏱ 3m 12s

[Stage 3/6] PR Builder
  → @pytest.mark.zstream_4_16_2 on 34 tests → PR #1847
  ⏱ 42s

[Stage 4/6] Jenkins Agent
  → qe-deploy-ocs-cluster-prod #412 → Build complete
  ⏱ 3h 22m

[Stage 5/6] Analyze Manager
  ├── Classifier           → 31 PASS, 2 FAIL, 1 FLAKY
  ├── Root Cause           → product_bug (ceph-csi race), test_bug (stale fixture)
  ├── Regression Detector  → 1 new regression vs 4.16.1
  └── Report Generator     → Report generated
  ⏱ 4m 31s

[Stage 6/6] Notify
  → Slack + PR comment + Jira bug filed
  ⏱ 8s

Total: 3h 32m (agent work: 10m 21s)
```

## Project Structure

```
odf-zstream-agents/
├── core/                        # Config, models, state, agent runner
│   ├── config.py                # Loads env + config.yaml
│   ├── models.py                # Pydantic: ChangeManifest, TestSelection, AnalysisReport, ...
│   ├── state.py                 # TypedDict states with Annotated reducers
│   └── agent_runner.py          # run_node() → Claude Code CLI
│
├── graph/                       # LangGraph pipeline + sub-graphs
│   ├── pipeline.py              # Top-level orchestrator (6 stages)
│   ├── inspect.py               # Fan-out/fan-in sub-graph
│   ├── map_tests.py             # Sequential + retry loop sub-graph
│   └── analyze.py               # DAG sub-graph
│
├── nodes/                       # 14 agent node implementations
│   ├── jira_inspector.py        # Inspect: query Jira
│   ├── errata_parser.py         # Inspect: parse errata (disabled)
│   ├── git_diff.py              # Inspect: PR Analyzer — fetches PR changed files from GitHub
│   ├── merge_manifest.py        # Inspect: merge sources
│   ├── code_analyzer.py         # Map: component → test dirs
│   ├── mark_matcher.py          # Map: score test relevance (Opus)
│   ├── coverage_validator.py    # Map: validate coverage
│   ├── pr_builder.py            # PR: branch + marks + PR
│   ├── jenkins_agent.py         # Jenkins: trigger + poll (no LLM)
│   ├── classifier.py            # Analyze: pass/fail (no LLM)
│   ├── root_cause.py            # Analyze: failure classification (Opus)
│   ├── regression.py            # Analyze: historical comparison
│   ├── report_generator.py      # Analyze: markdown report
│   └── notifier.py              # Notify: Slack + PR (no LLM)
│
├── tools/                       # 23 tool functions
│   ├── jira_tools.py            # jira_search, jira_get_issue, jira_create_bug
│   ├── errata_tools.py          # errata_fetch, errata_parse
│   ├── git_tools.py             # git_diff_files, git_log_between, git_show_commit
│   ├── ocs_ci_tools.py          # list_tests, read_test_marks, squad_map_lookup, read_test_source
│   ├── ocs_ci_scanner.py        # Loads per-version test index from map repo for per-testcase selection
│   ├── github_tools.py          # github_create_branch, github_add_mark_to_test, github_create_pr, ...
│   ├── jenkins_tools.py         # jenkins_trigger_build, jenkins_get_build_status, ...
│   ├── slack_tools.py           # slack_post_message
│   └── db_tools.py              # save_pipeline_results, query_historical_results
│
├── cli/main.py                  # Typer CLI: zstream run / status
├── api/main.py                  # FastAPI (stub)
├── config.yaml                  # Pipeline, test selection, Jenkins, LLM config
├── docker-compose.yml           # PostgreSQL
└── pyproject.toml               # Dependencies
```

## Configuration

### config.yaml

```yaml
pipeline:
  max_retries_per_stage: 2

test_selection:
  min_relevance_score: 0.7    # Tests below this score are filtered out
  max_tests: 50               # Cap on selected tests (override with --max-tests)
  coverage_threshold: 0.8     # Retry if coverage below this

jenkins:
  job_name: qe-deploy-ocs-cluster-prod
  max_wait_hours: 6

llm:
  runtime: claude-code          # Claude Code CLI
  default_model: sonnet         # sonnet/opus/haiku
  opus_model: opus
  opus_nodes: [mark_matcher, root_cause]
  no_llm_nodes: [git_diff, jenkins_agent, classifier, notifier]
  temperature: 0.1
  max_tokens: 4096
  claude_code:
    max_turns: 10
    default_timeout: 120
    opus_timeout: 300
    allowed_tools_default: []
    allowed_tools_with_files: ["Read", "Bash(find*)", "Bash(grep*)", "Bash(cat*)"]
    allowed_tools_with_web: ["Read", "Bash(curl*)", "WebSearch", "WebFetch"]

squad_mapping:
  ceph-csi: {squad: green_squad, paths: ["tests/functional/pv/", "tests/functional/storageclass/"]}
  mcg: {squad: red_squad, paths: ["tests/functional/object/mcg/"]}
  # ... see config.yaml for full mapping
```

### Runtime Modes

The pipeline supports two LLM runtimes, controlled by `llm.runtime` in `config.yaml`:

All AI nodes use **Claude Code CLI** (`claude --print`). The CLI handles its own auth — no `ANTHROPIC_API_KEY` needed. Nodes call `run_node(prompt, node_name)` which spawns Claude with the right model and timeout.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | No | Not needed — Claude Code CLI handles its own auth |
| `JIRA_URL` | Yes | Jira Cloud instance URL |
| `JIRA_EMAIL` | Yes | Jira account email |
| `JIRA_API_TOKEN` | Yes | Jira API token |
| `JENKINS_URL` | Yes | Jenkins server URL |
| `JENKINS_USER` | Yes | Jenkins username |
| `JENKINS_API_TOKEN` | Yes | Jenkins API token |
| `GITHUB_TOKEN` | Yes | GitHub PAT with repo write access |
| `GIT_AUTHOR_NAME` | Yes | Name for Signed-off-by in PR commits (DCO requirement) |
| `GIT_AUTHOR_EMAIL` | Yes | Email for Signed-off-by in PR commits |
| `SLACK_WEBHOOK_URL` | No | Slack incoming webhook URL |
| `POSTGRES_URL` | No | PostgreSQL connection string |
| `OCS_CI_REPO_PATH` | No | Path to local ocs-ci repo (only for fallback index building) |

## Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Pipeline | LangGraph | Graph-based state machine with sub-graphs, fan-out/in, conditional edges |
| AI Runtime | Claude Code CLI (`claude --print`) | Direct subprocess, no framework overhead. CLI handles auth and model routing |
| API | FastAPI | Async, typed, auto-generated OpenAPI docs |
| State | PostgreSQL | Pipeline checkpoints + historical results for regression detection |
| CLI | Typer | Clean CLI with auto-help |
| Observability | LangSmith | Native LangGraph tracing (optional) |

## Docs

- [Architecture Plan (v2)](docs/PLAN-v2.md)
- [Jenkins Integration Reference](docs/JENKINS-REFERENCE.md)
- [Implementation Notes](docs/IMPLEMENTATION-NOTES.md)
- [Next Steps](docs/NEXT-STEPS.md)
