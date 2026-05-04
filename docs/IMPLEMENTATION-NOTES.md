# ODF Z-Stream Implementation Notes

> Implementation record for ODF-ZStream-Multi-Agent-Plan-v2
> Built: 2026-05-04

---

## What Was Built

### Architecture Delivered

**Hierarchical LangGraph pipeline** — no CrewAI, no NATS, no swarm. Single framework, single mental model.

```
Pipeline Orchestrator (top-level StateGraph)
├── Inspect Manager (sub-graph: fan-out/fan-in)
│   ├── Jira Inspector       → Sonnet, queries Jira Cloud API
│   ├── Errata Parser        → Sonnet, parses Red Hat advisories
│   ├── Git Diff Analyzer    → No LLM, deterministic git diff
│   └── Merge & Cross-Ref    → Sonnet, deduplicates and reconciles
│
├── Map Tests Manager (sub-graph: sequential + retry loop)
│   ├── Code Analyzer        → Sonnet, component → squad → test dirs
│   ├── Mark Matcher         → Opus, deep relevance scoring
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
| Total source files | 36 |
| Lines of code | ~5,955 |
| LangGraph sub-graphs | 3 (inspect, map, analyze) |
| Agent nodes | 14 |
| Tool functions | 23 across 8 modules |
| Nodes using Opus | 2 (mark_matcher, root_cause) |
| Nodes using Sonnet | 7 |
| Nodes using no LLM | 5 (git_diff, jenkins, classifier, notifier, merge fallback) |

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

### Runtime Architecture Change

LLM nodes now run through a **dual-runtime** architecture via `core/agent_runner.py`:

- **Default**: Claude Code CLI (`claude --print`). Each agent node gets configurable tool access via `--allowedTools` (Read, Bash, WebSearch, WebFetch). No `ANTHROPIC_API_KEY` needed — the CLI handles its own auth.
- **Fallback**: LiteLLM. Selected via `llm.runtime: litellm` in config, or auto-selected if the `claude` CLI is not installed. Supports GPT, Ollama, and any LiteLLM-compatible provider.

Nodes call `run_node(prompt, node_name)` — runtime and model selection is automatic based on config. The 5 deterministic nodes (git_diff, jenkins_agent, classifier, notifier, merge fallback) still use no LLM/agent at all.

### Key Technical Decisions Made During Implementation

| Decision | Why |
|----------|-----|
| Claude Code CLI as default runtime | Agents get tool access (Read, Bash, WebSearch) via `--allowedTools`. No API key management needed. Falls back to LiteLLM if CLI not found |
| Unified `run_node()` API | Nodes don't know which runtime they're using. `run_node(prompt, node_name)` handles model routing, timeouts, and tool access automatically |
| Plain functions + separate `_tool` wrappers | `@tool` decorator wraps functions in `StructuredTool` objects that aren't directly callable. Nodes call raw functions; `_tool` variants exist for future ReAct agent use |
| JSON string returns from all tools | Tools return serialized JSON strings so both LLM agents and direct callers can consume them. Nodes parse with `json.loads()` |
| Every LLM node has a deterministic fallback | If the LLM is unavailable (no API key, rate limit, error), nodes produce reasonable output using regex, heuristics, and templates |
| `Annotated[list, add]` reducers for error lists | Errors from parallel sub-graph nodes get merged automatically by LangGraph's reducer system, not overwritten |

### Tools Implemented

In claude-code mode, LLM nodes no longer call tool functions directly. Instead, they delegate to the Claude Code agent via `allowed_tools` — the agent reads files, runs shell commands, and searches the web as needed. The tool modules below are still used by deterministic nodes and as the LiteLLM-mode fallback.

| Module | Functions | External Service |
|--------|-----------|-----------------|
| `jira_tools.py` | `jira_search`, `jira_get_issue`, `jira_create_bug` | Jira Cloud REST API v3 |
| `errata_tools.py` | `errata_fetch`, `errata_parse` | Red Hat errata API |
| `git_tools.py` | `git_diff_files`, `git_log_between`, `git_show_commit` | Local git via subprocess |
| `ocs_ci_tools.py` | `list_tests`, `read_test_marks`, `squad_map_lookup`, `read_test_source` | Local ocs-ci repo (AST parsing) |
| `github_tools.py` | `github_create_branch`, `github_add_mark_to_test`, `github_create_pr`, `github_comment_pr` | GitHub API via PyGithub |
| `jenkins_tools.py` | `jenkins_trigger_build`, `jenkins_get_build_status`, `jenkins_get_test_report`, `jenkins_get_console_log` | Jenkins REST API |
| `slack_tools.py` | `slack_post_message` | Slack webhook |
| `db_tools.py` | `save_pipeline_results`, `query_historical_results` | PostgreSQL via psycopg2 |

### Bugs Fixed During Build

1. **`StructuredTool` not callable** — `@tool` decorator wraps functions, making them non-callable directly. Fixed by separating raw functions from tool-wrapped variants.
2. **Git diff missing `repo_path` argument** — Node called `git_diff_files(from, to)` but tool signature requires `git_diff_files(repo_path, from, to)`.
3. **Jenkins node parameter mismatch** — Node used `build_id=` kwarg but tool expects `build_number=`. Also `params=` vs `parameters=` mismatch.
4. **Jira results not parsed** — Tool returns JSON string, node treated it as list of dicts. Added `json.loads()` parsing.
5. **ChatLiteLLM deprecation** — Moved to try/except import pattern for `langchain-litellm` package.

### Verification

```bash
# Pipeline compiles
python -c "from graph.pipeline import build_pipeline; build_pipeline()"

# All modules import
python -c "from tools import jira_tools, errata_tools, git_tools, ocs_ci_tools, github_tools, jenkins_tools, slack_tools, db_tools"

# Agent runner imports and detects runtime
python -c "from core.agent_runner import RUNTIME; print(f'Runtime: {RUNTIME}')"

# Verify claude CLI is available (for claude-code runtime)
claude --version

# End-to-end run (degrades gracefully without API keys)
zstream run 4.16.2

# Dry run (shows initial state)
zstream run 4.16.2 --dry-run
```
