"""Database connection helpers — local SQLite or shared Turso (libSQL).

Local file mode is the default for offline demos. When TURSO_DATABASE_URL and
TURSO_AUTH_TOKEN are set, all reviewers share one remote SQLite-compatible DB.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.config_loader import DATA_DIR, DEFAULT_DB_PATH, PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

SCHEMA_PATH = PROJECT_ROOT / "data" / "schema.sql"
SEED_DB_PATH = DATA_DIR / "seed.db"


def turso_configured() -> bool:
    """True when shared Turso credentials are present in the environment."""
    url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        return False
    # Ignore .env.example placeholders so local demos stay on SQLite.
    if "your-" in url.lower() or "your-" in token.lower():
        return False
    return True


def db_label(db_path: Path | str | None = None) -> str:
    """Human-readable DB description for the UI."""
    if turso_configured():
        url = os.getenv("TURSO_DATABASE_URL", "").strip()
        # Show host only — never echo the auth token.
        host = url.split("@")[-1] if url else "turso"
        return f"Turso (shared): {host}"
    path = Path(db_path) if db_path else resolve_db_path()
    return f"Local SQLite: {path}"


def get_connection(db_path: Path | str | None = None) -> Any:
    """Open a DB connection.

    - Explicit ``db_path`` always uses local SQLite (tests / seed scripts).
    - No path + Turso env configured → shared remote libSQL.
    - Otherwise local default path.
    """
    if db_path is not None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    if turso_configured():
        return _turso_connection()

    path = DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _turso_connection() -> Any:
    """Connect to Turso via the official libsql Python package."""
    try:
        import libsql
    except ImportError as exc:
        raise RuntimeError(
            "Turso is configured but the 'libsql' package is missing. "
            "Run: pip install -r requirements.txt"
        ) from exc

    url = os.environ["TURSO_DATABASE_URL"].strip()
    token = os.environ["TURSO_AUTH_TOKEN"].strip()
    conn = libsql.connect(database=url, auth_token=token)
    # Best-effort Row factory (sqlite3-compatible consumers).
    try:
        conn.row_factory = sqlite3.Row
    except Exception:
        pass
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass
    return conn


def _apply_schema(conn: Any, schema_sql: str) -> None:
    """Apply schema. Prefer executescript; fall back to statement-splitting for remotes."""
    try:
        conn.executescript(schema_sql)
        conn.commit()
        return
    except Exception:
        pass

    # Turso / some libsql builds may not support executescript.
    statement = []
    for line in schema_sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        statement.append(line)
        if stripped.endswith(";"):
            sql = "\n".join(statement).strip()
            statement = []
            if sql:
                conn.execute(sql)
    conn.commit()


def init_db(db_path: Path | str | None = None, schema_path: Path | None = None) -> Path | str:
    """Create tables from schema.sql. Returns local path or 'turso' marker.

    Explicit ``db_path`` always initializes that local file (even if Turso env is set),
    so unit tests never touch the shared remote DB.
    """
    schema = schema_path or SCHEMA_PATH
    if not schema.exists():
        raise FileNotFoundError(f"Schema file not found: {schema}")
    sql = schema.read_text(encoding="utf-8")

    if db_path is not None:
        path = Path(db_path)
        with get_connection(path) as conn:
            _apply_schema(conn, sql)
        return path

    if turso_configured():
        conn = _turso_connection()
        try:
            _apply_schema(conn, sql)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return "turso"

    path = DEFAULT_DB_PATH
    with get_connection(path) as conn:
        _apply_schema(conn, sql)
    return path


def _news_count_on_conn(conn: Any) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()
        if row is None:
            return 0
        if isinstance(row, sqlite3.Row):
            return int(row["c"])
        if isinstance(row, (tuple, list)):
            return int(row[0])
        return int(row["c"])  # type: ignore[index]
    except Exception:
        return 0


def _news_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with get_connection(path) as conn:
            return _news_count_on_conn(conn)
    except sqlite3.Error:
        return 0


def resolve_db_path(explicit: Path | str | None = None) -> Path:
    """Pick the local runtime DB path (ignored when Turso is configured for data).

    Precedence for local mode:
      1. explicit argument
      2. CI_INTEL_DB env var
      3. data/ci_intel.db if it has news rows
      4. data/seed.db if present (offline demo)
      5. data/ci_intel.db (create on first write)
    """
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("CI_INTEL_DB")
    if env:
        return Path(env)
    if DEFAULT_DB_PATH.exists() and _news_count(DEFAULT_DB_PATH) > 0:
        return DEFAULT_DB_PATH
    if SEED_DB_PATH.exists() and _news_count(SEED_DB_PATH) > 0:
        return SEED_DB_PATH
    return DEFAULT_DB_PATH


def open_repo_connection(explicit: Path | str | None = None) -> tuple[Any, Path | None]:
    """Open the active DB and ensure schema exists.

    Returns (connection, local_path_or_None). local_path is None when on Turso.
    """
    if turso_configured():
        init_db()
        return get_connection(), None

    path = resolve_db_path(explicit)
    init_db(path)
    return get_connection(path), path


def table_names(db_path: Path | str | None = None) -> list[str]:
    conn = get_connection(db_path) if not turso_configured() else get_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names: list[str] = []
        for r in rows:
            if isinstance(r, sqlite3.Row):
                names.append(r["name"])
            elif isinstance(r, (tuple, list)):
                names.append(str(r[0]))
            else:
                names.append(str(r["name"]))
        return names
    finally:
        try:
            conn.close()
        except Exception:
            pass
