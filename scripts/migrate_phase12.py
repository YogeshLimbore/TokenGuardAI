import sqlite3
from pathlib import Path

DATABASE_PATH = Path("tokenguard.db")

TABLES_REQUIRING_PROJECT_ID = [
    "llm_request_logs",
    "exact_response_cache",
    "semantic_response_cache",
    "provider_credentials",
    "credential_audit_logs",
    "model_routing_logs",
    "provider_attempt_logs",
    "budget_decision_logs",
    "rate_limit_events",
]


def column_exists(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()

    return any(row[1] == column_name for row in rows)


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def main() -> None:
    if not DATABASE_PATH.exists():
        print("Database does not exist yet. Start FastAPI to create new tables.")
        return

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        for table_name in TABLES_REQUIRING_PROJECT_ID:
            if not table_exists(connection, table_name):
                print(f"Skipping missing table: {table_name}")
                continue

            if column_exists(
                connection,
                table_name,
                "project_id",
            ):
                print(f"{table_name}.project_id already exists")
                continue

            connection.execute(
                f"""
                ALTER TABLE {table_name}
                ADD COLUMN project_id INTEGER
                REFERENCES projects(id)
                ON DELETE CASCADE
                """
            )

            print(f"Added project_id to {table_name}")

        connection.commit()
        print("Phase 12 migration completed.")

    finally:
        connection.close()


if __name__ == "__main__":
    main()
