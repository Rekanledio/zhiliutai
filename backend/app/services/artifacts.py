from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

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
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Artifact 路径越界")
        return candidate

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

    def read_text(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text(encoding="utf-8")

    def read_bytes(self, relative_path: str) -> bytes:
        return self._resolve(relative_path).read_bytes()
