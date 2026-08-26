import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from app.services.content import content_hash, normalize_content


@dataclass(frozen=True)
class ManagedNote:
    metadata: dict[str, object]
    body: str

    @property
    def zhiliu_id(self) -> str | None:
        value = self.metadata.get("zhiliu_id")
        return value if isinstance(value, str) else None


def _scalar(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_note(
    *,
    zhiliu_id: str,
    source_type: str,
    title: str,
    body: str,
    status: str,
    created_at: datetime,
    updated_at: datetime,
    tags: list[str] | None = None,
    source_url: str | None = None,
) -> str:
    lines = [
        "---",
        f"zhiliu_id: {_scalar(zhiliu_id)}",
        f"title: {_scalar(title)}",
        f"source_type: {_scalar(source_type)}",
    ]
    if source_url:
        lines.append(f"source_url: {_scalar(source_url)}")
    lines.extend(
        [
            f"status: {_scalar(status)}",
            f"created_at: {_scalar(created_at.isoformat())}",
            f"updated_at: {_scalar(updated_at.isoformat())}",
            "tags:",
        ]
    )
    lines.extend(f"  - {_scalar(tag)}" for tag in (tags or []))
    lines.extend(["---", "", normalize_content(body).rstrip(), ""])
    return "\n".join(lines)


def parse_note(raw: str) -> ManagedNote:
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("缺少 Frontmatter")
    closing = normalized.find("\n---\n", 4)
    if closing < 0:
        raise ValueError("Frontmatter 未闭合")
    header = normalized[4:closing]
    body = normalize_content(normalized[closing + 5 :])
    metadata: dict[str, object] = {}
    active_list: str | None = None
    for line in header.splitlines():
        if line.startswith("  - ") and active_list:
            raw_value = line[4:].strip()
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                value = raw_value
            target = metadata[active_list]
            if not isinstance(target, list):
                raise ValueError("Frontmatter 列表无效")
            target.append(value)
            continue
        if ":" not in line:
            raise ValueError("Frontmatter 行格式无效")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", key):
            raise ValueError("Frontmatter 键无效")
        if raw_value == "":
            metadata[key] = []
            active_list = key
            continue
        active_list = None
        try:
            metadata[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            metadata[key] = raw_value
    return ManagedNote(metadata=metadata, body=body)


def safe_note_name(title: str, item_id: str) -> str:
    slug = re.sub(r'[<>:"/\\|?*]', "-", title)
    slug = "".join("-" if ord(char) < 32 else char for char in slug).strip(" .")
    slug = re.sub(r"\s+", " ", slug)[:80] or "未命名知识"
    return f"{slug}-{item_id[:8]}.md"


class ObsidianVault:
    def __init__(self, vault_root: Path, managed_dir: str) -> None:
        self.vault_root = vault_root.resolve()
        self.managed_root = (self.vault_root / managed_dir).resolve()
        if not self.managed_root.is_relative_to(self.vault_root):
            raise ValueError("受管理 Vault 路径越界")

    def resolve(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Vault 相对路径无效")
        target = (self.managed_root / relative).resolve()
        if not target.is_relative_to(self.managed_root):
            raise ValueError("Vault 路径越界")
        return target

    def publish_path(self, title: str, item_id: str) -> str:
        return (Path("Notes") / safe_note_name(title, item_id)).as_posix()

    def atomic_write(self, relative_path: str, content: str) -> None:
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)

    def read(self, relative_path: str) -> ManagedNote:
        return parse_note(self.resolve(relative_path).read_text(encoding="utf-8"))

    def hash(self, relative_path: str) -> str:
        return content_hash(self.read(relative_path).body)

    def iter_markdown(self) -> list[Path]:
        if not self.managed_root.exists():
            return []
        return [
            path
            for path in self.managed_root.rglob("*.md")
            if path.is_file() and not path.name.startswith(".")
        ]

    def relative_path(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.managed_root):
            raise ValueError("Vault 路径越界")
        return resolved.relative_to(self.managed_root).as_posix()

    def uri(self, relative_path: str) -> str:
        target = self.resolve(relative_path)
        relative_to_vault = target.relative_to(self.vault_root).as_posix()
        return (
            "obsidian://open?vault="
            + quote(self.vault_root.name, safe="")
            + "&file="
            + quote(relative_to_vault, safe="/")
        )
