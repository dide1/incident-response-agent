import logging
import os
import time

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@postgres:5432/incidents",
)


def get_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Create tables, retrying until Postgres is ready."""
    for attempt in range(15):
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS deploy_tracker (
                            id           SERIAL PRIMARY KEY,
                            sha          CHAR(40)      NOT NULL,
                            service      VARCHAR(100)  NOT NULL,
                            deployed_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                            author       VARCHAR(100)  NOT NULL,
                            commit_message TEXT         NOT NULL,
                            branch       VARCHAR(100)  NOT NULL DEFAULT 'main',
                            is_fault     BOOLEAN       NOT NULL DEFAULT FALSE
                        );
                        CREATE INDEX IF NOT EXISTS idx_deploy_service_time
                            ON deploy_tracker(service, deployed_at DESC);

                        CREATE TABLE IF NOT EXISTS commit_diffs (
                            sha      CHAR(40)     PRIMARY KEY,
                            service  VARCHAR(100) NOT NULL,
                            diff     TEXT         NOT NULL
                        );
                    """)
                conn.commit()
            logger.info("Database initialized")
            return
        except Exception as exc:
            if attempt < 14:
                logger.warning("DB not ready (attempt %d/15): %s", attempt + 1, exc)
                time.sleep(2)
            else:
                raise


def insert_deploy(data: dict) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            if diff := data.get("diff"):
                cur.execute(
                    """
                    INSERT INTO commit_diffs (sha, service, diff)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (sha) DO UPDATE SET diff = EXCLUDED.diff
                    """,
                    (data["sha"], data["service"], diff),
                )
            cur.execute(
                """
                INSERT INTO deploy_tracker
                    (sha, service, deployed_at, author, commit_message, branch, is_fault)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    data["sha"],
                    data["service"],
                    data.get("deployed_at"),
                    data["author"],
                    data["commit_message"],
                    data.get("branch", "main"),
                    data.get("is_fault", False),
                ),
            )
        conn.commit()


def fetch_recent_deploys(service: str, window_minutes: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sha, service, deployed_at, author, commit_message, branch, is_fault
                FROM deploy_tracker
                WHERE service = %s
                  AND deployed_at >= NOW() - INTERVAL '%s minutes'
                ORDER BY deployed_at DESC
                """,
                (service, window_minutes),
            )
            return [dict(r) for r in cur.fetchall()]


def fetch_commit_diff(sha: str) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sha, service, diff FROM commit_diffs WHERE sha = %s",
                (sha,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def clear_deploys() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE deploy_tracker, commit_diffs")
        conn.commit()


def list_deploys(service: str | None, limit: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            if service:
                cur.execute(
                    "SELECT * FROM deploy_tracker WHERE service = %s ORDER BY deployed_at DESC LIMIT %s",
                    (service, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM deploy_tracker ORDER BY deployed_at DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]
