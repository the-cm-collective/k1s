from datetime import UTC, datetime
from pathlib import Path

from ae.controller.state import SQLiteStateStore


def test_probe_history_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)
    # Seed a history row by calling the internal insert used during reconcile
    with store._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            INSERT INTO probe_history(app_name, replica_id, check_time, ready, live, readiness_message, liveness_message)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                "echo",
                "echo-0",
                datetime.now(UTC).isoformat(),
                1,
                1,
                "ok",
                "ok",
            ),
        )
        conn.commit()
    out = store.get_probe_history("echo", 10)
    assert out and out[0].replica_id == "echo-0" and out[0].ready and out[0].live
# ruff: noqa: E501
