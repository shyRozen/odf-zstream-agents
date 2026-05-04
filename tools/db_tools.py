"""Database tools for persisting and querying z-stream pipeline results."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langchain_core.tools import tool

from core import config


def _get_connection():
    """Create a psycopg2 connection, or return an error string."""
    try:
        import psycopg2

        conn = psycopg2.connect(config.POSTGRES_URL)
        return conn, None
    except ImportError:
        return None, json.dumps({"error": "psycopg2 not installed"})
    except Exception as exc:
        return None, json.dumps({"error": f"Database connection failed: {str(exc)}"})


def _ensure_table(conn) -> None:
    """Create the pipeline_results table if it does not exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_results (
                id SERIAL PRIMARY KEY,
                pipeline_id VARCHAR(255) NOT NULL,
                version VARCHAR(50) NOT NULL,
                results JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pipeline_results_version
            ON pipeline_results (version)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pipeline_results_created
            ON pipeline_results (created_at DESC)
        """)
    conn.commit()


def save_pipeline_results(pipeline_id: str, version: str, results_json: str) -> str:
    """Save z-stream pipeline results to the Postgres database.

    Stores the full results JSON for a pipeline run, indexed by pipeline ID
    and version for later retrieval and comparison.

    Args:
        pipeline_id: Unique identifier for the pipeline run.
        version: The ODF z-stream version (e.g. "4.16.1").
        results_json: JSON string containing the pipeline results to store.

    Returns:
        JSON string confirming the save, or an error message.
    """
    conn, error = _get_connection()
    if error:
        return error

    try:
        # Validate the results JSON
        try:
            results_data = json.loads(results_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid results JSON: {str(exc)}"})

        _ensure_table(conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_results (pipeline_id, version, results, created_at)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (pipeline_id, version, json.dumps(results_data), datetime.now(timezone.utc)),
            )
            row_id = cur.fetchone()[0]
        conn.commit()

        return json.dumps(
            {
                "status": "saved",
                "id": row_id,
                "pipeline_id": pipeline_id,
                "version": version,
                "message": f"Results saved (row {row_id})",
            },
            indent=2,
        )

    except Exception as exc:
        return json.dumps({"error": f"Failed to save results: {str(exc)}"})
    finally:
        conn.close()


def query_historical_results(version: str, lookback: int = 5) -> str:
    """Query the last N z-stream pipeline results for a version for comparison.

    Retrieves recent pipeline results for the given version to enable
    regression detection and trend analysis.

    Args:
        version: The ODF version to query results for (e.g. "4.16.1").
                 Pass "*" to query across all versions.
        lookback: Number of recent results to retrieve. Defaults to 5.

    Returns:
        JSON string with historical results, or an error message.
    """
    conn, error = _get_connection()
    if error:
        return error

    try:
        _ensure_table(conn)

        with conn.cursor() as cur:
            if version == "*":
                cur.execute(
                    """
                    SELECT pipeline_id, version, results, created_at
                    FROM pipeline_results
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (lookback,),
                )
            else:
                cur.execute(
                    """
                    SELECT pipeline_id, version, results, created_at
                    FROM pipeline_results
                    WHERE version = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (version, lookback),
                )

            rows = cur.fetchall()

        results = []
        for row in rows:
            results.append(
                {
                    "pipeline_id": row[0],
                    "version": row[1],
                    "results": row[2],
                    "created_at": row[3].isoformat() if row[3] else None,
                }
            )

        return json.dumps(
            {
                "version": version,
                "count": len(results),
                "results": results,
            },
            indent=2,
        )

    except Exception as exc:
        return json.dumps({"error": f"Failed to query results: {str(exc)}"})
    finally:
        conn.close()


# Tool-wrapped versions for LangGraph ReAct agents
save_pipeline_results_tool = tool(save_pipeline_results)
query_historical_results_tool = tool(query_historical_results)
