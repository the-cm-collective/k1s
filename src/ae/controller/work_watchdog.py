"""Work ledger watchdogs for rescheduling stuck dispatches."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ae.ha.fencing import resolve_controller_identity
from ae.controller.state import SQLiteStateStore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkWatchdogConfig:
    interval_s: float = 5.0
    dispatched_max_s: float = 300.0
    running_max_s: float = 1800.0


class WorkWatchdog:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        config: WorkWatchdogConfig | None = None,
        authority=None,
    ) -> None:
        self._store = store
        self._config = config or WorkWatchdogConfig()
        self._authority = authority
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = False
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        while not self._stop:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("work watchdog error: %s", exc)
            time.sleep(self._config.interval_s)

    def run_once(self) -> None:
        now = datetime.now(timezone.utc)
        if self._config.dispatched_max_s > 0:
            cutoff = now - timedelta(seconds=self._config.dispatched_max_s)
            for entry in self._store.list_work_state_before("Dispatched", cutoff):
                self._reschedule(entry.work_id, entry.attempt, "dispatched_timeout")
        if self._config.running_max_s > 0:
            cutoff = now - timedelta(seconds=self._config.running_max_s)
            for entry in self._store.list_work_state_before("Running", cutoff):
                self._reschedule(entry.work_id, entry.attempt, "running_timeout")

    def _reschedule(self, work_id: str, attempt: int, reason: str) -> None:
        identity = resolve_controller_identity(self._authority)
        new_attempt = self._store.reschedule_work(
            work_id=work_id,
            attempt=attempt,
            controller_id=identity.controller_id,
            controller_epoch=identity.controller_epoch,
        )
        if new_attempt is None:
            return
        LOGGER.warning(
            "work rescheduled work_id=%s old_attempt=%s new_attempt=%s reason=%s",
            work_id,
            attempt,
            new_attempt,
            reason,
        )


__all__ = ["WorkWatchdog", "WorkWatchdogConfig"]
