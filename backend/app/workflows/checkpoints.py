"""Explicit lifecycle management for the workflow checkpoint database."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import TracebackType
from typing import AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import PROJECT_ROOT, Settings, get_settings


DEFAULT_WORKFLOW_CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "workflows.db"


def workflow_checkpoint_path(settings: Settings | None = None) -> Path:
    """Return the configured checkpoint file without exposing it in graph state."""

    return (settings or get_settings()).workflow_checkpoint_path


class WorkflowCheckpoint:
    """Own one AsyncSqliteSaver connection and close it deterministically."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        selected_path = workflow_checkpoint_path() if path is None else Path(path)
        self._path = selected_path.expanduser().resolve()
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self._busy_timeout_ms = busy_timeout_ms
        self._context = None
        self._saver: AsyncSqliteSaver | None = None

    @property
    def path(self) -> Path:
        """The internal file path, available to lifecycle tests only."""

        return self._path

    @property
    def saver(self) -> AsyncSqliteSaver:
        if self._saver is None:
            raise RuntimeError("workflow checkpoint is not open")
        return self._saver

    async def __aenter__(self) -> WorkflowCheckpoint:
        if self._context is not None:
            raise RuntimeError("workflow checkpoint is already open")
        if self._path.exists() and not self._path.is_file():
            raise ValueError("workflow checkpoint path must be a file")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._context = AsyncSqliteSaver.from_conn_string(self._path.as_posix())
        try:
            self._saver = await self._context.__aenter__()
            await self._saver.setup()
            await self._saver.conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            await self._saver.conn.commit()
        except BaseException:
            context = self._context
            self._context = None
            self._saver = None
            await context.__aexit__(*sys.exc_info())
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        context = self._context
        self._context = None
        self._saver = None
        if context is None:
            return None
        return await context.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        """Close the owned connection when a context manager is inconvenient."""

        await self.__aexit__(None, None, None)


@asynccontextmanager
async def open_workflow_checkpoint(
    path: Path | str | None = None,
    *,
    settings: Settings | None = None,
    busy_timeout_ms: int | None = None,
) -> AsyncIterator[WorkflowCheckpoint]:
    """Open, set up, and close an independent workflow checkpoint database."""

    selected_path = workflow_checkpoint_path(settings) if path is None else path
    timeout = (
        settings.sqlite_busy_timeout_ms
        if busy_timeout_ms is None and settings is not None
        else 5_000 if busy_timeout_ms is None else busy_timeout_ms
    )
    async with WorkflowCheckpoint(selected_path, busy_timeout_ms=timeout) as checkpoint:
        yield checkpoint
