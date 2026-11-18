"""SQLite 迁移。新表只补不改；已下线的 trace / baseline / 用例评审表会删除。

页面入口层（nav_page_*）和用例页面引用（nav_case_*）各一组新表。
迁移在严格 schema 校验之前执行；迁移失败必须直接拒绝启动，
不能带着半套表继续跑。
"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

SCHEMA_VERSION = 13

NAV_ENTRY_TABLES = (
    "nav_page_entries",
    "nav_page_entry_evidence",
    "nav_page_gaps",
)

NAV_CASE_TABLES = (
    "nav_case_page_refs",
)

CASE_SCRIPT_TABLES = (
    "case_scripts",
    "case_platform_scripts",
    "project_script_repos",
    "project_repo_apps",
)

OAUTH_TABLES = (
    "oauth_clients",
    "oauth_requests",
    "oauth_codes",
    "oauth_tokens",
)

ACCOUNT_DATA_TABLES = ("pool_account_data", "pool_account_data_requests")

# Historical IM inbox tables have no registered model or current consumer. Preserve
# them in existing databases, but do not require them when creating a current schema.
ADDITIVE_TABLES = NAV_ENTRY_TABLES + NAV_CASE_TABLES + CASE_SCRIPT_TABLES + OAUTH_TABLES + ("scout_record_index", "project_members", "agent_workflows") + ACCOUNT_DATA_TABLES

RETIRED_TABLES = (
    "m_case_run_trace",
    "m_case_baseline",
    "case_review_records",
)


def _user_version(conn) -> int:
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def _backup_before_account_scope(engine: Engine) -> None:
    """追加账号字段前生成 SQLite 一致备份，不覆盖历史备份。"""
    database = engine.url.database
    if not database or database == ":memory:":
        return
    from pathlib import Path
    import sqlite3
    import tempfile

    with engine.connect() as conn:
        if _user_version(conn) >= SCHEMA_VERSION or "pool_accounts" not in inspect(conn).get_table_names():
            return
        if "app_ids" in {column["name"] for column in inspect(conn).get_columns("pool_accounts")}:
            return
        path = Path(database)
        with tempfile.NamedTemporaryFile(prefix=f"{path.name}.pre-schema-{SCHEMA_VERSION}-", suffix=".backup", dir=path.parent, delete=False) as file:
            backup_path = Path(file.name)
        try:
            destination = sqlite3.connect(backup_path)
            try:
                conn.connection.driver_connection.backup(destination)
            finally:
                destination.close()
        except BaseException:
            backup_path.unlink(missing_ok=True)
            raise


def apply_additive_migrations(engine: Engine, metadata) -> None:
    """把旧库补到当前版本。新表只创建；已下线表直接删除。"""
    _backup_before_account_scope(engine)
    with engine.begin() as conn:
        version = _user_version(conn)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database_schema_newer; db_user_version={version}; "
                f"code_user_version={SCHEMA_VERSION}"
            )
        present = set(inspect(conn).get_table_names())
        for name in RETIRED_TABLES:
            if name in present:
                conn.exec_driver_sql(f'DROP TABLE "{name}"')
        if version < SCHEMA_VERSION:
            if "pool_accounts" in present:
                columns = {column["name"] for column in inspect(conn).get_columns("pool_accounts")}
                if "app_ids" not in columns:
                    conn.exec_driver_sql("ALTER TABLE pool_accounts ADD COLUMN app_ids JSON NOT NULL DEFAULT '[]'")
            for name in ADDITIVE_TABLES:
                if name in present:
                    continue
                table = metadata.tables.get(name)
                if table is None:
                    raise RuntimeError(f"schema_model_not_registered:{name}")
                table.create(bind=conn, checkfirst=True)
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
            version = SCHEMA_VERSION
        missing = [name for name in ADDITIVE_TABLES if name not in inspect(conn).get_table_names()]
        if missing:
            raise RuntimeError("schema_incomplete:" + ",".join(missing))
