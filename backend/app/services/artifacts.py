from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import tempfile

from app.core.paths import safe_relative_path
from app.services.content import content_hash, normalize_content


@dataclass(frozen=True)
class StoredArtifact:
    relative_path: str
    content_hash: str
    byte_size: int


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve(self, relative_path: str) -> Path:
        safe_path = safe_relative_path(relative_path)
        if safe_path is None:
            raise ValueError("Artifact 相对路径无效")
        candidate = (self.root / safe_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Artifact 路径越界")
        return candidate

    def exists(self, relative_path: str) -> bool:
        try:
            return self._resolve(relative_path).is_file()
        except (OSError, ValueError):
            return False

    def verify(self, relative_path: str, expected_hash: str, expected_size: int) -> bool:
        try:
            content = self._resolve(relative_path).read_bytes()
        except (OSError, ValueError):
            return False
        return len(content) == expected_size and sha256(content).hexdigest() == expected_hash

    def put_text(self, content: str, suffix: str = ".md") -> StoredArtifact:
        normalized = normalize_content(content)
        return self.put_bytes(normalized.encode("utf-8"), suffix, digest=content_hash(normalized))

    def put_bytes(
        self, content: bytes, suffix: str = ".bin", *, digest: str | None = None
    ) -> StoredArtifact:
        content_digest = digest or sha256(content).hexdigest()
        relative = Path(content_digest[:2]) / f"{content_digest}{suffix}"
        target = self._resolve(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != content:
                raise OSError("Artifact 哈希冲突")
        else:
            target.write_bytes(content)
        return StoredArtifact(relative.as_posix(), content_digest, len(content))

    def put_file(
        self,
        source: Path,
        suffix: str = ".bin",
        *,
        max_bytes: int | None = None,
    ) -> StoredArtifact:
        """Store a provider-created file under a generated content-addressed name."""

        try:
            stat = source.stat()
        except OSError as error:
            raise OSError("Artifact 来源文件不可读") from error
        if not source.is_file() or stat.st_size < 0:
            raise OSError("Artifact 来源文件不可读")
        if max_bytes is not None and stat.st_size > max_bytes:
            raise ValueError("Artifact 文件超过大小限制")

        digest = sha256()
        byte_size = 0
        try:
            with source.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    byte_size += len(chunk)
                    if max_bytes is not None and byte_size > max_bytes:
                        raise ValueError("Artifact 文件超过大小限制")
                    digest.update(chunk)
        except OSError as error:
            raise OSError("Artifact 来源文件不可读") from error
        content_digest = digest.hexdigest()
        relative = Path(content_digest[:2]) / f"{content_digest}{suffix}"
        target = self._resolve(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or target.stat().st_size != byte_size:
                raise OSError("Artifact 哈希冲突")
            return StoredArtifact(relative.as_posix(), content_digest, byte_size)

        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{content_digest[:12]}.", suffix=".tmp", dir=target.parent
            )
            with os.fdopen(descriptor, "wb") as handle, source.open("rb") as source_handle:
                while chunk := source_handle.read(1024 * 1024):
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
            temporary_name = None
        except OSError as error:
            raise OSError("Artifact 写入失败") from error
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return StoredArtifact(relative.as_posix(), content_digest, byte_size)

    def delete(self, relative_path: str) -> None:
        """Delete one already-resolved artifact file, if it exists."""

        target = self._resolve(relative_path)
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            raise OSError("Artifact 清理失败") from error

    def read_text(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text(encoding="utf-8")

    def read_bytes(self, relative_path: str) -> bytes:
        return self._resolve(relative_path).read_bytes()
