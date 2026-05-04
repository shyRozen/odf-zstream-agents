"""Slack notification tools for z-stream pipeline status updates."""

from __future__ import annotations

import json

import httpx
from langchain_core.tools import tool

from core import config


def slack_post_message(message: str) -> str:
    """Post a message to the configured Slack webhook.

    Sends a text message to the Slack channel associated with the
    SLACK_WEBHOOK_URL environment variable. Supports Slack's mrkdwn
    formatting (bold, italic, code blocks, links).

    Args:
        message: The message text to post. Supports Slack mrkdwn formatting.

    Returns:
        JSON string confirming delivery, or an error message.
    """
    if not config.SLACK_WEBHOOK_URL:
        return json.dumps({
            "error": "SLACK_WEBHOOK_URL not configured",
            "note": "Message was not sent. Set SLACK_WEBHOOK_URL in .env to enable Slack notifications.",
            "unsent_message": message[:200],
        })

    payload = {
        "text": message,
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                config.SLACK_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()

        return json.dumps({
            "status": "sent",
            "message_preview": message[:100],
        })

    except httpx.HTTPStatusError as exc:
        return json.dumps({
            "error": f"Slack webhook returned {exc.response.status_code}: {exc.response.text[:200]}",
        })
    except Exception as exc:
        return json.dumps({"error": f"Failed to post Slack message: {str(exc)}"})


# Tool-wrapped version for LangGraph ReAct agents
slack_post_message_tool = tool(slack_post_message)
