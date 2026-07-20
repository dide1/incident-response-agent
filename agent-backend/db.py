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

                        CREATE EXTENSION IF NOT EXISTS vector;

                        CREATE TABLE IF NOT EXISTS runbooks (
                            id        SERIAL PRIMARY KEY,
                            filename  VARCHAR(200) NOT NULL UNIQUE,
                            title     VARCHAR(200) NOT NULL,
                            content   TEXT         NOT NULL,
                            embedding vector(384)
                        );

                        CREATE INDEX IF NOT EXISTS idx_runbooks_embedding
                            ON runbooks USING hnsw (embedding vector_cosine_ops);

                        CREATE TABLE IF NOT EXISTS users (
                            id           SERIAL PRIMARY KEY,
                            github_id    INTEGER      NOT NULL UNIQUE,
                            username     VARCHAR(100) NOT NULL,
                            access_token TEXT         NOT NULL,
                            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                        );

                        CREATE TABLE IF NOT EXISTS repos (
                            id             SERIAL PRIMARY KEY,
                            user_id        INTEGER      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            owner          VARCHAR(100) NOT NULL,
                            repo           VARCHAR(100) NOT NULL,
                            webhook_secret VARCHAR(100) NOT NULL DEFAULT '',
                            created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                            UNIQUE(user_id, owner, repo)
                        );

                        CREATE TABLE IF NOT EXISTS incidents (
                            id         SERIAL PRIMARY KEY,
                            alertname  VARCHAR(200) NOT NULL,
                            service    VARCHAR(100) NOT NULL,
                            severity   VARCHAR(50),
                            description TEXT,
                            result     JSONB,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        -- state machine: investigating -> brief_posted -> resolved (or failed)
                        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS
                            status VARCHAR(30) NOT NULL DEFAULT 'investigating';
                        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS
                            brief_posted_at TIMESTAMPTZ;
                        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS
                            resolved_at TIMESTAMPTZ;
                        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS
                            postmortem_id INTEGER;
                        ALTER TABLE incidents ADD COLUMN IF NOT EXISTS
                            user_id INTEGER REFERENCES users(id);

                        CREATE TABLE IF NOT EXISTS postmortems (
                            id            SERIAL PRIMARY KEY,
                            alertname     VARCHAR(200) NOT NULL,
                            service       VARCHAR(100) NOT NULL,
                            content       TEXT         NOT NULL,
                            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                            incident_data JSONB
                        );
                        ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS
                            user_id INTEGER REFERENCES users(id);
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
                VALUES (%s, %s, COALESCE(%s::timestamptz, NOW()), %s, %s, %s, %s)
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
    # NOTE: is_fault is deliberately excluded — it's a test-harness label and
    # returning it would leak the answer key into the agent's context.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sha, service, deployed_at, author, commit_message, branch
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


# ── Runbook store ─────────────────────────────────────────────────────────────

def _vec(embedding: list[float]) -> str:
    """Serialize a float list to pgvector literal, e.g. '[0.1,0.2,...]'."""
    return "[" + ",".join(map(str, embedding)) + "]"


def upsert_runbook(filename: str, title: str, content: str, embedding: list[float]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runbooks (filename, title, content, embedding)
                VALUES (%s, %s, %s, %s::vector)
                ON CONFLICT (filename) DO UPDATE
                    SET title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding
                """,
                (filename, title, content, _vec(embedding)),
            )
        conn.commit()


def search_runbooks_db(query_embedding: list[float], top_k: int = 3) -> list[dict]:
    vec = _vec(query_embedding)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT filename, title, content,
                       ROUND((1 - (embedding <=> %s::vector))::numeric, 3) AS similarity
                FROM runbooks
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vec, vec, top_k),
            )
            return [dict(r) for r in cur.fetchall()]


def create_incident(alert: dict, user_id: int | None = None) -> int:
    """Insert a new incident in 'investigating' state; returns its id."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (alertname, service, severity, description, status, user_id)
                VALUES (%s, %s, %s, %s, 'investigating', %s)
                RETURNING id
                """,
                (
                    alert.get("alertname", "unknown"),
                    alert.get("service", "unknown"),
                    alert.get("severity"),
                    alert.get("description", ""),
                    user_id,
                ),
            )
            incident_id = cur.fetchone()["id"]
        conn.commit()
    return incident_id


def complete_incident(incident_id: int, result: dict) -> None:
    """Attach the agent's analysis and transition to 'brief_posted'."""
    import json as _json
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE incidents
                SET result = %s, status = 'brief_posted', brief_posted_at = NOW()
                WHERE id = %s
                """,
                (_json.dumps(result, default=str), incident_id),
            )
        conn.commit()


def fail_incident(incident_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE incidents SET status = 'failed' WHERE id = %s", (incident_id,)
            )
        conn.commit()


def resolve_incident(
    alertname: str, service: str, resolved_at: str, postmortem_id: int | None
) -> int | None:
    """
    Transition the most recent unresolved incident for this alert+service to
    'resolved' and link its postmortem. Returns the incident id, or None if
    there was nothing to resolve.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE incidents
                SET status = 'resolved', resolved_at = %s::timestamptz, postmortem_id = %s
                WHERE id = (
                    SELECT id FROM incidents
                    WHERE alertname = %s AND service = %s AND status != 'resolved'
                    ORDER BY created_at DESC LIMIT 1
                )
                RETURNING id
                """,
                (resolved_at, postmortem_id, alertname, service),
            )
            row = cur.fetchone()
        conn.commit()
    return row["id"] if row else None


def list_incidents(user_id: int | None = None, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute(
                    "SELECT id, alertname, service, severity, description, result, "
                    "       status, created_at, brief_posted_at, resolved_at, postmortem_id "
                    "FROM incidents WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                )
            else:
                cur.execute(
                    "SELECT id, alertname, service, severity, description, result, "
                    "       status, created_at, brief_posted_at, resolved_at, postmortem_id "
                    "FROM incidents ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]


def list_postmortems(user_id: int | None = None, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute(
                    "SELECT id, alertname, service, content, created_at "
                    "FROM postmortems WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                )
            else:
                cur.execute(
                    "SELECT id, alertname, service, content, created_at "
                    "FROM postmortems ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]


def insert_postmortem(
    alertname: str, service: str, content: str, incident_data: dict,
    user_id: int | None = None,
) -> int:
    import json as _json
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO postmortems (alertname, service, content, incident_data, user_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (alertname, service, content, _json.dumps(incident_data, default=str), user_id),
            )
            pm_id = cur.fetchone()["id"]
        conn.commit()
    return pm_id


def get_latest_postmortem() -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, alertname, service, content, created_at FROM postmortems ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
    return dict(row) if row else None


def list_runbooks_db() -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, title FROM runbooks ORDER BY filename")
            return [dict(r) for r in cur.fetchall()]


# ── Users ─────────────────────────────────────────────────────────────────────

def get_or_create_user(github_id: int, username: str, access_token: str) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (github_id, username, access_token)
                VALUES (%s, %s, %s)
                ON CONFLICT (github_id) DO UPDATE
                    SET username = EXCLUDED.username,
                        access_token = EXCLUDED.access_token
                RETURNING id, github_id, username, created_at
                """,
                (github_id, username, access_token),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, github_id, username FROM users WHERE id = %s", (user_id,)
            )
            row = cur.fetchone()
    return dict(row) if row else None


# ── Repos ─────────────────────────────────────────────────────────────────────

def add_repo(user_id: int, owner: str, repo: str, webhook_secret: str) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO repos (user_id, owner, repo, webhook_secret)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, owner, repo) DO UPDATE
                    SET webhook_secret = EXCLUDED.webhook_secret
                RETURNING id, user_id, owner, repo, created_at
                """,
                (user_id, owner, repo, webhook_secret),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


def list_user_repos(user_id: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, owner, repo, webhook_secret, created_at "
                "FROM repos WHERE user_id = %s ORDER BY created_at",
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def remove_repo(user_id: int, repo_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM repos WHERE id = %s AND user_id = %s", (repo_id, user_id)
            )
        conn.commit()


def get_repo_by_full_name(owner: str, repo: str) -> dict | None:
    """Look up a repo + its owner's credentials. Used for webhook routing."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.user_id, r.owner, r.repo, r.webhook_secret,
                       u.username, u.access_token
                FROM repos r JOIN users u ON r.user_id = u.id
                WHERE r.owner = %s AND r.repo = %s
                """,
                (owner, repo),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def get_all_repos_with_token() -> list[dict]:
    """Returns all repos joined with their owner's token. Used by CI poller."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.user_id, r.owner, r.repo, u.access_token
                FROM repos r JOIN users u ON r.user_id = u.id
                ORDER BY r.created_at
                """
            )
            return [dict(r) for r in cur.fetchall()]
