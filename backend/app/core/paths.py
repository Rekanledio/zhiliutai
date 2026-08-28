from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath


def safe_relative_path(value: object) -> str | None:
    """Return a canonical repository/Vault-relative POSIX path or ``None``.

    Paths are stored as POSIX relative paths even on Windows.  Checking both
    PurePosixPath and PureWindowsPath is intentional: a Windows drive-relative
    path such as ``C:note.md`` is not absolute to pathlib, but it is not a
    valid application-relative target either.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if "\x00" in value or "\\" in value:
        return None

    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or value.startswith("//")
    ):
        return None
    if value in {".", ".."} or any(
        part in {"", ".", ".."} or ":" in part for part in posix.parts
    ):
        return None

    canonical = posix.as_posix()
    return canonical if canonical == value else None
