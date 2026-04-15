"""Leader-owned HA storage controller hosting for shared-authority storage resources."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ae.apishim.ha_store import MultiplexApishimStore
from ae.apishim.store import ObjectStore
from ae.controller.state import SQLiteStateStore
from ae.storage.controller import StorageController

LOGGER = logging.getLogger(__name__)


def build_storage_authority_store(state: SQLiteStateStore):
    """Build the HA apishim-store multiplexer used by the controller storage runner."""
    dsn = os.getenv("AE_APISHIM_DSN")
    db_path = Path(os.getenv("AE_APISHIM_DB", "state/apishim.db"))
    legacy = ObjectStore(db_path=db_path, dsn=dsn)
    return MultiplexApishimStore.from_state_and_legacy(state, legacy)


class StorageAuthorityRunner:
    """Run the storage reconciliation engine only while this controller is leader."""

    def __init__(
        self,
        store: Any,
        *,
        authority=None,
        poll_interval_s: float = 1.0,
        controller_factory: Callable[[Any], StorageController] | None = None,
        close_store: bool = False,
    ) -> None:
        self._store = store
        self._authority = authority
        self._poll_interval_s = max(0.1, float(poll_interval_s))
        self._controller_factory = controller_factory or self._default_controller_factory
        self._close_store = bool(close_store)
        self._thread = threading.Thread(target=self._run, name="storage-authority", daemon=True)
        self._started = False
        self._stop = False
        self._active: StorageController | None = None
        self._store_closed = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        elif self._active is not None:
            self._stop_active_controller()
        if self._close_store and not self._thread.is_alive() and not self._store_closed:
            self._close_backing_store()

    def _run(self) -> None:
        try:
            while not self._stop:
                try:
                    is_leader = self._is_leader()
                    if is_leader and self._active is None:
                        self._start_active_controller()
                    elif not is_leader and self._active is not None:
                        LOGGER.info(
                            "storage authority lost leadership; stopping storage reconcile"
                        )
                        self._stop_active_controller()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("storage authority loop failed: %s", exc)
                    self._stop_active_controller()
                self._sleep(self._poll_interval_s)
        finally:
            self._stop_active_controller()
            if self._close_store:
                self._close_backing_store()

    def _start_active_controller(self) -> None:
        controller = self._controller_factory(self._store)
        seeded = controller.sync()
        controller.start()
        self._active = controller
        if seeded:
            LOGGER.info("seeded %s StorageClass objects from config", seeded)
        LOGGER.info("started leader-owned storage authority controller")

    def _stop_active_controller(self) -> None:
        controller = self._active
        self._active = None
        if controller is None:
            return
        try:
            controller.stop()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("storage authority stop failed: %s", exc)

    def _close_backing_store(self) -> None:
        if self._store_closed:
            return
        try:
            self._store.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("storage authority store close failed: %s", exc)
        finally:
            self._store_closed = True

    def _is_leader(self) -> bool:
        if self._authority is None:
            return True
        try:
            snapshot = self._authority.snapshot()
        except Exception:
            return False
        return bool(snapshot is not None and getattr(snapshot, "is_leader", False))

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.1, float(seconds))
        while not self._stop and remaining > 0:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    @staticmethod
    def _default_controller_factory(store: Any) -> StorageController:
        return StorageController(store)
