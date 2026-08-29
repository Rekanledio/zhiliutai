from __future__ import annotations

import asyncio
from pathlib import Path

from app.services.backup import BackupRestoreService, BackupResult, RebuildResult
from app.services.stage2 import Stage2Service


class MaintenanceBusyError(RuntimeError):
    """Another controlled maintenance operation is already in progress."""


class MaintenanceCoordinator:
    """Serialize backup, rebuild, and rescan with Stage2 mutations."""

    def __init__(
        self,
        stage2: Stage2Service,
        backup_service: BackupRestoreService,
    ) -> None:
        self.stage2 = stage2
        self.backup_service = backup_service
        self.lock = asyncio.Lock()
        self._busy = False

    async def _acquire(self, *, wait_if_busy: bool = False) -> None:
        if self._busy and not wait_if_busy:
            raise MaintenanceBusyError
        if wait_if_busy:
            await self.lock.acquire()
            self._busy = True
            return
        self._busy = True
        try:
            await self.lock.acquire()
        except BaseException:
            self._busy = False
            raise

    def _release(self) -> None:
        if self.lock.locked():
            self.lock.release()
        self._busy = False

    async def rescan(
        self,
        *,
        minimum_file_age_seconds: float = 0,
        skip_if_busy: bool = False,
        wait_if_busy: bool = False,
    ) -> dict[str, int] | None:
        if skip_if_busy and self._busy:
            return None
        await self._acquire(wait_if_busy=wait_if_busy)
        try:
            # Stage2Service.rescan owns its mutation_lock; the maintenance
            # lock is acquired first so backup/rebuild cannot overtake it.
            return await self.stage2.rescan(
                minimum_file_age_seconds=minimum_file_age_seconds
            )
        finally:
            self._release()

    async def rebuild(self) -> RebuildResult:
        await self._acquire()
        try:
            async with self.stage2.mutation_lock:
                return await self.backup_service.rebuild_derived_state()
        finally:
            self._release()

    async def backup(self, destination: Path) -> BackupResult:
        await self._acquire()
        try:
            async with self.stage2.mutation_lock:
                return await self._backup_in_thread(destination)
        finally:
            self._release()

    async def _backup_in_thread(self, destination: Path) -> BackupResult:
        work: asyncio.Task[BackupResult] = asyncio.create_task(
            asyncio.to_thread(self.backup_service.create_backup, destination)
        )
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # A cancelled request must not release either lock while the
            # synchronous file operation is still copying.
            try:
                await asyncio.shield(work)
            except BaseException:
                pass
            raise
