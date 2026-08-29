import hashlib
import re


DEFAULT_CHUNK_MAX_CHARS = 800


def normalize_content(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("内容不能为空")
    if chr(0) in normalized:
        raise ValueError("内容不能包含 NUL 字符")
    return normalized + "\n"


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def default_title(content: str) -> str:
    first = normalize_content(content).splitlines()[0]
    first = re.sub(r"^#{1,6}\s*", "", first).strip()
    return (first[:80] or "未命名知识").strip()


def chunk_content(content: str, max_chars: int = DEFAULT_CHUNK_MAX_CHARS) -> list[str]:
    text = normalize_content(content)
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                paragraph[offset : offset + max_chars]
                for offset in range(0, len(paragraph), max_chars)
            )
        elif not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= max_chars:
            current += "\n\n" + paragraph
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks or [text.strip()]
