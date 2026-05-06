from __future__ import annotations
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

_config_cache: dict | None = None


def _load_yaml() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            _config_cache = yaml.safe_load(f)
    else:
        _config_cache = {}
    return _config_cache


def get(key: str, default=None):
    parts = key.split(".")
    cfg = _load_yaml()
    for part in parts:
        if isinstance(cfg, dict):
            cfg = cfg.get(part)
        else:
            return default
        if cfg is None:
            return default
    return cfg


JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "DFBUGS")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "red-hat-storage/ocs-ci")

JENKINS_URL = os.getenv("JENKINS_URL", "")
JENKINS_USER = os.getenv("JENKINS_USER", "")
JENKINS_API_TOKEN = os.getenv("JENKINS_API_TOKEN", "")
JENKINS_JOB_NAME = os.getenv("JENKINS_JOB_NAME", "qe-deploy-ocs-cluster-prod")

POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://zstream:zstream@localhost:5432/zstream")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

OCS_CI_REPO_PATH = os.getenv("OCS_CI_REPO_PATH", str(Path.home() / "codcod/new-ocs-ci/ocs-ci"))

LLM_RUNTIME = get("llm.runtime", "claude-code")
DEFAULT_MODEL = get("llm.default_model", "sonnet")
OPUS_MODEL = get("llm.opus_model", "opus")
OPUS_NODES = get("llm.opus_nodes", ["mark_matcher", "root_cause"])
NO_LLM_NODES = get("llm.no_llm_nodes", ["git_diff", "jenkins_agent", "classifier", "notifier"])
LLM_TEMPERATURE = get("llm.temperature", 0.1)
LLM_MAX_TOKENS = get("llm.max_tokens", 4096)

SQUAD_MAPPING: dict = get("squad_mapping", {})

MIN_RELEVANCE_SCORE = get("test_selection.min_relevance_score", 0.7)
MAX_TESTS = get("test_selection.max_tests", 100)
COVERAGE_THRESHOLD = get("test_selection.coverage_threshold", 0.8)

JENKINS_POLL_INITIAL = get("jenkins.poll_backoff.initial_seconds", 30)
JENKINS_POLL_MAX = get("jenkins.poll_backoff.max_seconds", 300)
JENKINS_POLL_MULTIPLIER = get("jenkins.poll_backoff.multiplier", 2)
JENKINS_MAX_WAIT_HOURS = get("jenkins.max_wait_hours", 6)
JENKINS_DEFAULT_PARAMS: dict = get("jenkins.params", {})

REGRESSION_LOOKBACK = get("analysis.regression_lookback", 5)
AUTO_FILE_BUGS = get("analysis.auto_file_bugs", False)
ROOT_CAUSE_CONFIDENCE_THRESHOLD = get("analysis.root_cause_confidence_threshold", 0.7)
