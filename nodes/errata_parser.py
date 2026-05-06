"""Errata Parser node -- disabled.

Errata parsing is disabled until we have reliable access to the
Red Hat errata API. Returns empty changes.
"""

from __future__ import annotations

import logging

from core.state import InspectState

logger = logging.getLogger(__name__)


def errata_parser(state: InspectState) -> dict:
    logger.info("Errata parsing disabled — skipping")
    return {"errata_changes": []}
