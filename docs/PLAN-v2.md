# ODF Z-Stream Multi-Agent Test Automation — v2

> Revised architecture. Drops CrewAI and swarm patterns entirely.
> Single framework (LangGraph) with hierarchical sub-graphs.
> 
> Replaces: ODF-ZStream-Multi-Agent-Plan
> Jenkins reference: ODF-ZStream-Jenkins-Reference
> Implementation record: ODF-ZStream-Implementation-Notes
> Next steps: ODF-ZStream-Next-Steps

---

## What Changed from v1

| Aspect              | v1                                    | v2                                                 |
| ------------------- | ------------------------------------- | -------------------------------------------------- |
| Frameworks          | LangGraph + CrewAI + NATS             | **LangGraph only**                                 |
| Coordination        | Orchestrator + Swarm hybrid           | **Hierarchical: Orchestrator → Managers → Agents** |
| Agent communication | NATS pub/sub messaging                | **LangGraph state passing** (in-process)           |
| Dependencies        | 3 frameworks to learn/maintain        | **1 framework**                                    |
| Complexity          | High (swarm negotiation, message bus) | **Low (directed graphs, function calls)**          |

### Why the change

The v1 "swarm" stages were never true swarms — agents didn't discover tasks, negotiate, or delegate autonomously. Every stage had a fixed, known set of agents with predetermined data flow:

- **Inspect**: 3 agents query independent APIs → merge. That's parallel execution, not a swarm.
- **Map Tests**: Sequential chain with a retry loop. No peer-to-peer collaboration.
- **Analyze**: DAG (classify → parallel analysis → report). Fixed input/output contracts.

LangGraph handles all of these patterns natively: fan-out/fan-in, conditional edges, sub-graphs. Adding CrewAI and NATS was unnecessary complexity.

---

## Architecture: Hierarchical Sub-Graphs

```
┌─────────────────────────────────────────────────────────────────────┐
│                    PIPELINE ORCHESTRATOR                             │
│                    (Top-Level LangGraph)                             │
│                                                                     │
│  Manages stage sequencing, error handling, state persistence        │
│  Knows: current stage, pipeline state, retry policy                 │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
    ┌─────────────────┼─────────────────────────────────────┐
    │                 │                                     │
    ▼                 ▼                                     ▼
┌────────┐    ┌──────────────┐                      ┌────────────┐
│INSPECT │    │  MAP TESTS   │    PR    JENKINS     │  ANALYZE   │   NOTIFY
│MANAGER │    │  MANAGER     │  BUILDER  AGENT      │  MANAGER   │   (node)
│(sub-   │    │  (sub-graph) │  (node)  (node)      │  (sub-     │
│ graph) │    │              │                      │   graph)   │
│        │    │              │                      │            │
│ ┌────┐ │    │ ┌─────────┐ │                      │ ┌────────┐ │
│ │Jira│ │    │ │Code     │ │                      │ │Classif.│ │
│ │Node│ │    │ │Analyzer │ │                      │ │Node    │ │
│ └────┘ │    │ └────┬────┘ │                      │ └────┬───┘ │
│ ┌────┐ │    │      ▼      │                      │      │     │
│ │Err.│ │    │ ┌─────────┐ │                      │  ┌───┴───┐ │
│ │Node│ │    │ │Mark     │ │                      │  │       │ │
│ └────┘ │    │ │Matcher  │ │                      │  ▼       ▼ │
│ ┌────┐ │    │ └────┬────┘ │                      │┌─────┐┌───┐│
│ │Git │ │    │      ▼      │                      ││Root ││Reg││
│ │Node│ │    │ ┌─────────┐ │                      ││Cause││Det││
│ └────┘ │    │ │Coverage │ │                      │└──┬──┘└─┬─┘│
│   │    │    │ │Validator│ │                      │   └──┬──┘  │
│   ▼    │    │ └────┬────┘ │                      │      ▼     │
│┌─────┐ │    │      │      │                      │ ┌────────┐ │
││Merge│ │    │  (loop if   │                      │ │Report  │ │
││Node │ │    │   gaps)     │                      │ │Gen.    │ │
│└─────┘ │    │             │                      │ └────────┘ │
└────────┘    └──────────────┘                      └────────────┘

   FAN-OUT        SEQUENTIAL         SINGLE           DAG
   FAN-IN         + RETRY            NODES         FAN-OUT/IN
```

### Hierarchy

| Level | Role | What it does |
|-------|------|-------------|
| **Pipeline Orchestrator** | Top-level manager | Sequences stages, manages global state, handles errors, decides retries |
| **Stage Managers** (Inspect, Map, Analyze) | Team leads | Sub-graphs that coordinate their agents. Own their stage's state and logic |
| **Agent Nodes** | Workers | Individual LangGraph nodes. Each calls an LLM with specific tools to do one job |

Simple stages (PR Builder, Jenkins, Notify) don't need a manager — they're single nodes reporting directly to the orchestrator.

---

## Pipeline Orchestrator (Top-Level Graph)

```python
# Simplified LangGraph definition

pipeline = StateGraph(PipelineState)

# Stages
pipeline.add_node("inspect", inspect_manager)       # sub-graph
pipeline.add_node("map_tests", map_tests_manager)    # sub-graph
pipeline.add_node("pr_builder", pr_builder_node)     # single node
pipeline.add_node("jenkins", jenkins_node)            # single node
pipeline.add_node("analyze", analyze_manager)         # sub-graph
pipeline.add_node("notify", notify_node)              # single node

# Sequential flow
pipeline.add_edge(START, "inspect")
pipeline.add_edge("inspect", "map_tests")
pipeline.add_edge("map_tests", "pr_builder")
pipeline.add_edge("pr_builder", "jenkins")
pipeline.add_edge("jenkins", "analyze")
pipeline.add_edge("analyze", "notify")
pipeline.add_edge("notify", END)
```

### Pipeline State

```python
class PipelineState(TypedDict):
    zstream_version: str           # e.g. "4.16.2"
    previous_version: str          # e.g. "4.16.1"
    change_manifest: ChangeManifest
    selected_tests: list[TestSelection]
    coverage_report: CoverageReport
    pr_url: str
    pr_number: int
    jenkins_build_id: int
    jenkins_build_url: str
    junit_results: JUnitResults
    analysis_report: AnalysisReport
    errors: list[StageError]
    current_stage: str
```

---

## Stage 1: Inspect Manager (Sub-Graph)

**Pattern**: Fan-out → Fan-in

Three agent nodes query independent data sources in parallel, then a merge node combines their outputs into a unified Change Manifest.

```
         ┌──────────────┐
         │ Inspect      │
         │ Manager      │
         │ (entry)      │
         └──────┬───────┘
       ┌────────┼────────┐
       ▼        ▼        ▼
  ┌─────────┐┌────────┐┌─────────┐
  │Jira     ││Errata  ││Git Diff │    ← All 3 run in parallel
  │Inspector││Parser  ││Analyzer │
  └────┬────┘└───┬────┘└────┬────┘
       └─────────┼──────────┘
                 ▼
         ┌──────────────┐
         │ Merge &      │
         │ Cross-Ref    │    ← Deterministic merge, no LLM needed
         └──────────────┘
```

| Node | LLM? | Tools | Input | Output |
|------|------|-------|-------|--------|
| Jira Inspector | Yes (extraction) | `jira_search`, `jira_get_issue` | z-stream version | Ticket list with metadata |
| Errata Parser | Yes (parsing) | `errata_fetch`, `errata_parse` | z-stream version or errata ID | Advisory-to-component mapping |
| Git Diff Analyzer | No (deterministic) | `git_diff`, `git_log` | Version tags | Changed files and components |
| Merge & Cross-Ref | Yes (reconciliation) | None | All 3 outputs | Unified Change Manifest |

**Why a manager sub-graph?** The fan-out/fan-in pattern has error handling logic — if Jira is down, proceed with errata + git only (graceful degradation). The manager handles this conditional logic.

---

## Stage 2: Map Tests Manager (Sub-Graph)

**Pattern**: Sequential chain with conditional retry loop

```
  ┌──────────────┐
  │ Code         │
  │ Analyzer     │
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ Mark         │
  │ Matcher      │
  └──────┬───────┘
         ▼
  ┌──────────────┐     gaps found?
  │ Coverage     │────────────────┐
  │ Validator    │                │
  └──────┬───────┘                │
         │ no gaps                ▼
         ▼              ┌──────────────┐
      [done]            │ Widen Search │──→ back to Code Analyzer
                        └──────────────┘   (max 2 retries)
```

| Node | LLM? | Tools | Input | Output |
|------|------|-------|-------|--------|
| Code Analyzer | Yes | `ocs_ci_list_tests`, `ocs_ci_read_marks`, `squad_map_lookup` | Change Manifest | Component → test directory mapping |
| Mark Matcher | Yes (scoring) | `read_test_file`, `parse_marks` | Mapped test dirs | Scored test list with relevance rationale |
| Coverage Validator | Yes (gap analysis) | None | Scored tests + manifest | Final test list + coverage gaps |
| Widen Search | No (deterministic) | None | Gap list | Expanded search areas for Code Analyzer |

**Key LLM decision**: Mark Matcher uses the LLM to understand *why* a test is relevant to a specific bug fix — not just keyword matching. This is where AI adds real value vs. a regex.

**Retry policy**: Max 2 retry loops. If coverage is still < 80% after retries, flag for human review and proceed.

---

## Stage 3: PR Builder (Single Node)

**Pattern**: Single node, no manager needed

```python
def pr_builder_node(state: PipelineState) -> PipelineState:
    # 1. Create branch zstream/ODF-{version}-{date}
    # 2. Add @pytest.mark.zstream_{version} to selected tests
    # 3. Register mark in pytest.ini
    # 4. Generate PR description from state
    # 5. Create PR via GitHub API
    return {**state, pr_url: url, pr_number: number}
```

| Tools | LLM? |
|-------|------|
| `git_create_branch`, `git_add_mark`, `github_create_pr` | Yes (PR description generation) |

---

## Stage 4: Jenkins Agent (Single Node)

**Pattern**: Single node with internal poll loop

```python
def jenkins_node(state: PipelineState) -> PipelineState:
    # 1. POST /buildWithParameters
    #    TEST_MARK_EXPRESSION = zstream_{version}
    #    OCS_VERSION, RUN_TEST=true, RUN_TEARDOWN=false
    # 2. Poll queue → get build number
    # 3. Poll build status (30s → 5m backoff, 6h timeout)
    # 4. Download JUnit XML + console log
    return {**state, jenkins_build_id, junit_results}
```

| Tools | LLM? |
|-------|------|
| `jenkins_trigger`, `jenkins_poll`, `jenkins_get_results`, `jenkins_get_log` | No (pure API calls) |

**Target job**: `qe-deploy-ocs-cluster-prod` — see ODF-ZStream-Jenkins-Reference for parameter details.

---

## Stage 5: Analyze Manager (Sub-Graph)

**Pattern**: DAG — classify first, then parallel analysis, then aggregate

```
         ┌──────────────┐
         │ Pass/Fail    │
         │ Classifier   │
         └──────┬───────┘
         ┌──────┴───────┐
         ▼              ▼
  ┌──────────────┐ ┌──────────────┐
  │ Root Cause   │ │ Regression   │    ← These 2 run in parallel
  │ Analyzer     │ │ Detector     │
  └──────┬───────┘ └──────┬───────┘
         └──────┬─────────┘
                ▼
         ┌──────────────┐
         │ Report       │
         │ Generator    │
         └──────────────┘
```

| Node | LLM? | Tools | Input | Output |
|------|------|-------|-------|--------|
| Pass/Fail Classifier | No | `parse_junit_xml` | JUnit XML | Classification: PASS/FAIL/ERROR/SKIP/FLAKY with counts |
| Root Cause Analyzer | Yes (deep) | `read_test_log`, `read_test_source`, `jira_search_bug` | Failed tests + logs | Failure type (product_bug / test_bug / infra_issue) with confidence |
| Regression Detector | Yes | `query_historical_results`, `compare_pass_rates` | Current results + historical DB | New regressions vs last 5 z-streams |
| Report Generator | Yes | None | All analysis outputs | Markdown report + Slack summary |

**Root Cause Analyzer is the highest-value LLM use in the pipeline.** It reads the test log, the test source code, the component that changed, and determines whether the failure is caused by the z-stream change or is pre-existing. This is where Opus should be used (most capable model).

---

## Stage 6: Notify (Single Node)

**Pattern**: Single node, fan-out to multiple channels

```python
def notify_node(state: PipelineState) -> PipelineState:
    # 1. Post Slack summary to #odf-zstream-results
    # 2. Comment full analysis on the z-stream PR
    # 3. Create Jira bugs for product_bug failures (if auto_file_bugs=true)
    # 4. Update web dashboard
    return state
```

| Tools | LLM? |
|-------|------|
| `slack_post`, `github_comment_pr`, `jira_create_bug` | No (template-based formatting) |

---

## LLM Allocation Per Node

Not every node needs an LLM. Not every LLM call needs the most expensive model.

| Node | Model | Why |
|------|-------|-----|
| Jira Inspector | Sonnet | Extraction from structured API data — fast, cheap |
| Errata Parser | Sonnet | Same — parsing structured advisories |
| Git Diff Analyzer | **None** | Deterministic git commands, no LLM needed |
| Merge & Cross-Ref | Sonnet | Reconcile 3 data sources, flag conflicts |
| Code Analyzer | Sonnet | Map components to test dirs using known mapping table |
| Mark Matcher | **Opus** | Needs deep understanding of test code vs. bug description to score relevance |
| Coverage Validator | Sonnet | Gap analysis against manifest — structured comparison |
| PR Builder | Sonnet | Generate PR description — templated |
| Jenkins Agent | **None** | Pure API calls — no LLM |
| Pass/Fail Classifier | **None** | Parse JUnit XML — deterministic |
| Root Cause Analyzer | **Opus** | Deep reasoning: read log + source + change → classify failure cause |
| Regression Detector | Sonnet | Compare current vs. historical results — structured |
| Report Generator | Sonnet | Aggregate and format — templated with some narrative |
| Notify | **None** | Template-based channel posting |

**Summary**: 14 nodes. 5 need no LLM. 7 use Sonnet. 2 use Opus (the two highest-value reasoning tasks).

---

## Tech Stack (Simplified)

| Layer | Technology | Why |
|-------|-----------|-----|
| **Everything agent** | **LangGraph** | Orchestration, sub-graphs, fan-out/in, state, retry — one framework for all patterns |
| LLM abstraction | **LiteLLM** | Unified API for Claude + GPT + Ollama. Route Sonnet vs. Opus per node |
| State | **PostgreSQL** | Pipeline runs, historical results, regression baselines |
| Cache | **Redis** | LLM response cache, rate limiting |
| API | **FastAPI** | Trigger pipeline, check status, view results |
| CLI | **Typer** | `zstream run 4.16.2`, `zstream status`, `zstream report` |
| Observability | **LangSmith** | LangGraph-native tracing, evaluation, debugging (replaces OpenTelemetry + Grafana) |
| Deploy | **Docker Compose** → **Kubernetes** | Start simple, scale later |

### Removed from v1

| Removed | Why |
|---------|-----|
| **CrewAI** | No swarm patterns needed. LangGraph sub-graphs replace all "crew" coordination |
| **NATS** | No inter-agent messaging needed. LangGraph passes state between nodes in-process |
| **Slack Bolt** | Notify node calls Slack webhook directly — no need for a full bot framework |
| **Streamlit** | Dashboard can be a simple FastAPI + HTML page (like the one we already built) |

**Result**: 4 fewer dependencies. One framework to learn. Simpler debugging (LangSmith traces the entire pipeline as one graph).

---

## Project Structure (Simplified)

```
odf-zstream-agents/
├── docker-compose.yml
├── pyproject.toml
├── .env.example
│
├── core/
│   ├── config.py                # Pipeline & LLM config
│   ├── models.py                # Pydantic: ChangeManifest, TestSelection, AnalysisReport
│   ├── state.py                 # PipelineState TypedDict
│   └── llm.py                   # LiteLLM client (model routing per node)
│
├── graph/
│   ├── pipeline.py              # Top-level orchestrator graph
│   ├── inspect.py               # Inspect Manager sub-graph
│   ├── map_tests.py             # Map Tests Manager sub-graph
│   └── analyze.py               # Analyze Manager sub-graph
│
├── nodes/
│   ├── jira_inspector.py        # Inspect: query Jira Cloud
│   ├── errata_parser.py         # Inspect: parse errata
│   ├── git_diff.py              # Inspect: git diff between tags
│   ├── merge_manifest.py        # Inspect: merge & cross-ref
│   ├── code_analyzer.py         # Map: component → test dirs
│   ├── mark_matcher.py          # Map: score test relevance
│   ├── coverage_validator.py    # Map: validate coverage
│   ├── pr_builder.py            # PR: branch + mark + PR
│   ├── jenkins_agent.py         # Jenkins: trigger + poll + fetch
│   ├── classifier.py            # Analyze: pass/fail from JUnit
│   ├── root_cause.py            # Analyze: failure classification
│   ├── regression.py            # Analyze: compare historical
│   ├── report_generator.py      # Analyze: produce report
│   └── notifier.py              # Notify: Slack, PR, Jira
│
├── tools/                       # LangGraph tool definitions
│   ├── jira_tools.py            # @tool: jira_search, jira_get_issue, jira_create_bug
│   ├── errata_tools.py          # @tool: errata_fetch, errata_parse
│   ├── git_tools.py             # @tool: git_diff, git_log, git_create_branch
│   ├── github_tools.py          # @tool: github_create_pr, github_comment_pr
│   ├── jenkins_tools.py         # @tool: jenkins_trigger, jenkins_poll, jenkins_get_results
│   ├── ocs_ci_tools.py          # @tool: list_tests, read_marks, parse_conftest
│   ├── slack_tools.py           # @tool: slack_post
│   └── db_tools.py              # @tool: query_historical_results, save_results
│
├── api/
│   └── main.py                  # FastAPI: POST /run, GET /status/{id}, GET /report/{id}
│
├── cli/
│   └── main.py                  # Typer: zstream run, status, report
│
└── tests/
    ├── test_graph/              # Graph integration tests
    ├── test_nodes/              # Unit tests per node
    └── test_tools/              # Tool unit tests
```

**v1 had 28 files. v2 has 25 files. But more importantly: one framework, one mental model.**

---

## Workflow Example: ODF 4.16.2

```
$ zstream run 4.16.2

[Pipeline] Starting z-stream pipeline for ODF 4.16.2
[Pipeline] Previous version: 4.16.1

[Stage 1/6] Inspect Manager
  ├── Jira Inspector (Sonnet)     → 12 bugs (3 critical, 5 major, 4 minor)
  ├── Errata Parser (Sonnet)      → 2 CVEs + 10 bugfixes
  ├── Git Diff Analyzer (no LLM)  → 47 commits, 128 files
  └── Merge & Cross-Ref (Sonnet)  → 14 unique changes in manifest
  ⏱ 1m 48s

[Stage 2/6] Map Tests Manager
  ├── Code Analyzer (Sonnet)      → green_squad (PV/SC), red_squad (MCG)
  ├── Mark Matcher (Opus)         → 83 scanned, 34 selected (score > 0.7)
  ├── Coverage Validator (Sonnet) → 13/14 covered, 1 gap
  └── Retry: widened search       → 14/14 covered
  ⏱ 3m 12s

[Stage 3/6] PR Builder
  → Branch: zstream/ODF-4.16.2-20260504
  → @pytest.mark.zstream_4_16_2 on 34 tests
  → PR #1847 created
  ⏱ 42s

[Stage 4/6] Jenkins Agent
  → Triggered qe-deploy-ocs-cluster-prod #412
  → TEST_MARK_EXPRESSION=zstream_4_16_2
  → Polling... (backoff 30s → 5m)
  → Build complete
  ⏱ 3h 22m

[Stage 5/6] Analyze Manager
  ├── Classifier (no LLM)         → 31 PASS, 2 FAIL, 1 FLAKY
  ├── Root Cause (Opus)           → FAIL#1: product_bug (ceph-csi race, BZ#2234)
  │                                  FAIL#2: test_bug (stale fixture)
  ├── Regression Detector (Sonnet)→ 1 new regression vs 4.16.1
  └── Report Generator (Sonnet)   → Report generated
  ⏱ 4m 31s

[Stage 6/6] Notify
  → Slack: posted to #odf-zstream-results
  → PR #1847: analysis comment added
  → Jira: ODF-5678 created for ceph-csi regression
  ⏱ 8s

[Pipeline] Complete
  Total: 3h 32m (agent work: 10m 21s)
  Result: 31/34 PASS (91.2%), 1 regression, 1 test bug
```

---

## Implementation Phases

### Phase 1: Skeleton (Week 1-2)
- [ ] Project scaffold with pyproject.toml, Docker Compose
- [ ] `core/`: config, models, state, LiteLLM client
- [ ] `graph/pipeline.py`: top-level graph with stub nodes
- [ ] `cli/main.py`: `zstream run {version}` entry point
- [ ] `api/main.py`: POST /run endpoint
- [ ] Verify: pipeline runs end-to-end with stub data

### Phase 2: Inspect + Map (Week 3-4)
- [ ] `tools/jira_tools.py`: Jira Cloud API
- [ ] `tools/errata_tools.py`: errata parsing
- [ ] `tools/git_tools.py`: git diff between version tags
- [ ] `nodes/jira_inspector.py`, `errata_parser.py`, `git_diff.py`, `merge_manifest.py`
- [ ] `graph/inspect.py`: fan-out/fan-in sub-graph
- [ ] `tools/ocs_ci_tools.py`: read tests, marks, squad mappings
- [ ] `nodes/code_analyzer.py`, `mark_matcher.py`, `coverage_validator.py`
- [ ] `graph/map_tests.py`: sequential + retry sub-graph
- [ ] Verify: Change Manifest + test selection from real data

### Phase 3: PR + Jenkins (Week 5-6)
- [ ] `tools/github_tools.py`: branch, mark injection, PR
- [ ] `nodes/pr_builder.py`: end-to-end PR creation
- [ ] `tools/jenkins_tools.py`: trigger, poll, results, log download
- [ ] `nodes/jenkins_agent.py`: trigger + poll loop
- [ ] Verify: full pipeline from inspect → Jenkins build start

### Phase 4: Analysis + Notify (Week 7-8)
- [ ] `nodes/classifier.py`: JUnit XML parsing (no LLM)
- [ ] `nodes/root_cause.py`: failure classification with Opus
- [ ] `tools/db_tools.py`: historical results storage + query
- [ ] `nodes/regression.py`: compare against previous runs
- [ ] `nodes/report_generator.py`: Markdown + Slack formatting
- [ ] `graph/analyze.py`: DAG sub-graph
- [ ] `nodes/notifier.py`: Slack webhook, GitHub PR comment, Jira bug
- [ ] Verify: full pipeline end-to-end with real Jenkins results

### Phase 5: Production (Week 9-10)
- [ ] LangSmith integration for tracing
- [ ] Error handling: per-stage retry, graceful degradation
- [ ] Auth & secrets management (Vault or env-based)
- [ ] Historical results database schema + migrations
- [ ] API: GET /status/{id}, GET /report/{id}
- [ ] Web dashboard (FastAPI + HTML, reuse existing design)
- [ ] Rate limiting for external API calls

### Phase 6: Hardening (Week 11-12)
- [ ] Kubernetes deployment manifests
- [ ] CI/CD for the agent system itself
- [ ] Load test: concurrent z-stream pipelines
- [ ] Runbook and team onboarding
- [ ] Tune LLM prompts based on first 5 real runs
- [ ] Set `auto_file_bugs: true` after validation

---

## Configuration

### Environment Variables

```bash
# LLM Providers
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
LITELLM_DEFAULT_MODEL=claude-sonnet-4-6
LITELLM_OPUS_MODEL=claude-opus-4-7

# Jira Cloud
JIRA_URL=https://your-org.atlassian.net
JIRA_EMAIL=team@company.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=ODF

# GitHub
GITHUB_TOKEN=ghp_...
GITHUB_REPO=red-hat-storage/ocs-ci

# Jenkins
JENKINS_URL=https://jenkins-csb-odf-qe-ocs4.dno.corp.redhat.com
JENKINS_USER=...
JENKINS_API_TOKEN=...
JENKINS_JOB_NAME=qe-deploy-ocs-cluster-prod

# Infrastructure
POSTGRES_URL=postgresql://user:pass@localhost:5432/zstream
REDIS_URL=redis://localhost:6379

# Slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_CHANNEL=#odf-zstream-results

# LangSmith (observability)
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=odf-zstream
```

### Pipeline Config

```yaml
pipeline:
  max_retries_per_stage: 2
  timeout_hours: 6

test_selection:
  min_relevance_score: 0.7
  max_tests: 100
  include_tiers: [tier1, tier2]
  always_include_marks: [acceptance]
  coverage_threshold: 0.8        # retry if below this

jenkins:
  job_name: qe-deploy-ocs-cluster-prod
  poll_backoff:
    initial_seconds: 30
    max_seconds: 300
    multiplier: 2
  max_wait_hours: 6
  params:
    RUN_INSTALL_OCP: false
    RUN_INSTALL_OCS: false
    RUN_TEST: true
    RUN_TEARDOWN: false
    PRODUCTION_RUN: true
    REPORT_PORTAL: true

analysis:
  regression_lookback: 5
  auto_file_bugs: false
  root_cause_confidence_threshold: 0.7

llm:
  default_model: claude-sonnet-4-6
  opus_nodes:
    - mark_matcher
    - root_cause
  temperature: 0.1
  max_tokens: 4096
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Single framework | LangGraph only | No stage actually needed swarm behavior. All patterns (fan-out, retry, DAG) are native to LangGraph |
| Hierarchical sub-graphs | Manager → Agent nodes | Clean separation of concerns. Each manager owns its stage logic. Easy to test in isolation |
| No message bus | LangGraph state passing | Agents are in the same process. State is a TypedDict passed between nodes. No serialization overhead |
| LiteLLM for routing | Per-node model selection | Mark Matcher and Root Cause need Opus. Everything else uses Sonnet. 5 nodes need no LLM at all |
| LangSmith over OTel | Native LangGraph tracing | LangSmith understands graph structure, shows node-by-node traces. OTel would need custom instrumentation |
| No Slack bot framework | Direct webhook POST | Notify node sends one message. Doesn't need event handling, slash commands, or interactive messages |
| PostgreSQL only | Drop Redis for MVP | LLM caching is nice-to-have. Start with Postgres for pipeline state + historical results. Add Redis later if needed |

---

## Comparison: v1 vs v2

| Aspect | v1 | v2 |
|--------|----|----|
| Frameworks | 3 (LangGraph + CrewAI + NATS) | **1 (LangGraph)** |
| Dependencies | ~15 packages | **~8 packages** |
| Mental models | Orchestrator + Swarm + Pub/Sub | **Graphs + Nodes** |
| Infrastructure | PostgreSQL + Redis + NATS | **PostgreSQL (Redis optional)** |
| Observability | OpenTelemetry + Grafana | **LangSmith (native)** |
| Files | 28 | **25** |
| Learning curve | High (3 paradigms) | **Low (1 paradigm)** |
| Debugging | Traces across 3 systems | **Single trace in LangSmith** |
| Node count | 13 agents | **14 nodes (same work, clearer roles)** |
| LLM cost | All agents use LLM | **5 nodes skip LLM entirely** |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| LLM hallucination in test selection | High | Coverage Validator node catches gaps. Human reviews PR. Score threshold at 0.7 |
| Jira/Errata API down | Medium | Inspect Manager proceeds with available sources (fan-out handles partial failure) |
| Jenkins timeout (>6h) | Medium | Configurable timeout. Slack alert. Can kill and re-trigger from same state |
| Wrong root cause classification | Medium | `auto_file_bugs: false` until validated. Confidence threshold at 0.7 |
| LangGraph state too large | Low | Only pass references (file paths, URLs) not full content. Keep state under 1MB |
| Rate limiting on LLM APIs | Low | LiteLLM built-in retry + backoff. Only 9 nodes call LLMs |

---

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Z-stream to results | < 4 hours | Pipeline run duration (LangSmith trace) |
| Test selection accuracy | > 85% | Human review of first 10 runs |
| Coverage completeness | > 90% | Coverage Validator gap count |
| Root cause accuracy | > 75% | Human review of analysis reports |
| LLM cost per run | < $5 | LiteLLM token tracking |
| Team adoption | > 80% of z-streams | Usage count within 3 months |