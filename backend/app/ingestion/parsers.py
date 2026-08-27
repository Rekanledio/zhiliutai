from __future__ import annotations

import re
from html.parser import HTMLParser
from io import BytesIO
from typing import Any

from app.ingestion.types import ParsedSource, SourceBlock
from app.services.content import default_title, normalize_content


class SourceParseError(ValueError):
    """Raised when a supported source cannot produce textual content."""


def _clean_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.splitlines())
    return normalized.strip()


def _parsed(
    source_type: str,
    media_type: str,
    title: str | None,
    blocks: list[SourceBlock],
    metadata: dict[str, Any],
) -> ParsedSource:
    cleaned_blocks: list[SourceBlock] = []
    for block in blocks:
        text = _clean_text(block.text)
        if text:
            cleaned_blocks.append(SourceBlock(text=text, locator=block.locator))
    if not cleaned_blocks:
        raise SourceParseError("来源未提取到可用文本")
    body = normalize_content("\n\n".join(block.text for block in cleaned_blocks))
    resolved_title = _clean_text(title or "") or default_title(body)
    source_metadata = {
        **metadata,
        "source_type": source_type,
        "media_type": media_type,
        "title": resolved_title,
        "segments": [
            {"text": block.text, "locator": block.locator} for block in cleaned_blocks
        ],
    }
    return ParsedSource(
        source_type=source_type,
        media_type=media_type,
        title=resolved_title,
        body=body,
        blocks=tuple(cleaned_blocks),
        metadata=source_metadata,
    )


def parse_pdf(data: bytes) -> ParsedSource:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise SourceParseError("加密 PDF 无法解析")
        blocks: list[SourceBlock] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if _clean_text(text):
                blocks.append(
                    SourceBlock(
                        text=text,
                        locator={
                            "kind": "pdf",
                            "page": page_number,
                            "page_label": str(page_number),
                        },
                    )
                )
        metadata = {"page_count": len(reader.pages)}
        document_metadata = reader.metadata
        metadata_title = (
            getattr(document_metadata, "title", None) if document_metadata else None
        )
        return _parsed("pdf", "application/pdf", metadata_title, blocks, metadata)
    except SourceParseError:
        raise
    except Exception as error:
        raise SourceParseError(f"PDF 解析失败：{type(error).__name__}") from error


def _heading_level(style_name: str) -> int | None:
    match = re.search(r"(?:heading|标题)\s*([1-6])", style_name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_docx(data: bytes) -> ParsedSource:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = Document(BytesIO(data))
        blocks: list[SourceBlock] = []
        heading_path: list[str] = []
        paragraph_number = 0
        table_number = 0
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                paragraph = Paragraph(child, document)
                text = _clean_text(paragraph.text)
                if not text:
                    continue
                paragraph_number += 1
                level = _heading_level(paragraph.style.name if paragraph.style else "")
                if level is not None:
                    heading_path = heading_path[: level - 1]
                    heading_path.append(text)
                    rendered = f"{'#' * level} {text}"
                    locator = {
                        "kind": "docx",
                        "element": "heading",
                        "paragraph": paragraph_number,
                        "heading_level": level,
                        "heading_path": list(heading_path),
                    }
                else:
                    rendered = text
                    locator = {
                        "kind": "docx",
                        "element": "paragraph",
                        "paragraph": paragraph_number,
                        "heading_path": list(heading_path),
                    }
                blocks.append(SourceBlock(rendered, locator))
            elif child.tag == qn("w:tbl"):
                table = Table(child, document)
                table_number += 1
                for row_number, row in enumerate(table.rows, start=1):
                    cells = [_clean_text(cell.text) for cell in row.cells]
                    row_text = " | ".join(cell for cell in cells if cell)
                    if row_text:
                        blocks.append(
                            SourceBlock(
                                row_text,
                                {
                                    "kind": "docx",
                                    "element": "table_row",
                                    "table": table_number,
                                    "row": row_number,
                                    "heading_path": list(heading_path),
                                },
                            )
                        )
        core_title = _clean_text(document.core_properties.title or "")
        metadata = {
            "paragraph_count": paragraph_number,
            "table_count": table_number,
        }
        media_type = (
            "application/"
            "vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        return _parsed("docx", media_type, core_title, blocks, metadata)
    except SourceParseError:
        raise
    except Exception as error:
        raise SourceParseError(f"DOCX 解析失败：{type(error).__name__}") from error


class _HtmlTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "pre",
        "dt",
        "dd",
    }
    _SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "nav",
        "footer",
        "header",
        "aside",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._current_tag: str | None = None
        self._current_parts: list[str] = []
        self._current_level: int | None = None
        self._current_heading_path: list[str] = []
        self.heading_path: list[str] = []
        self.blocks: list[SourceBlock] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag in self._SKIP_TAGS:
                self._skip_depth += 1
            return
        if tag in self._SKIP_TAGS:
            self._skip_depth = 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in self._BLOCK_TAGS and self._current_tag is None:
            self._current_tag = tag
            self._current_parts = []
            self._current_level = int(tag[1]) if tag.startswith("h") else None
            self._current_heading_path = list(self.heading_path)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag in self._SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag != self._current_tag:
            return
        text = _clean_text("".join(self._current_parts))
        if text:
            if self._current_level is not None:
                self.heading_path = self.heading_path[: self._current_level - 1]
                self.heading_path.append(text)
                rendered = f"{'#' * self._current_level} {text}"
                locator = {
                    "kind": "webpage",
                    "element": "heading",
                    "heading_level": self._current_level,
                    "heading_path": list(self.heading_path),
                }
            else:
                rendered = text
                locator = {
                    "kind": "webpage",
                    "element": self._current_tag,
                    "heading_path": self._current_heading_path,
                }
            self.blocks.append(SourceBlock(rendered, locator))
        self._current_tag = None
        self._current_parts = []
        self._current_level = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif self._current_tag is not None:
            self._current_parts.append(data)

    def finish(self) -> None:
        if self._current_tag is not None:
            self.handle_endtag(self._current_tag)


def parse_html(data: bytes, *, url: str) -> ParsedSource:
    try:
        parser = _HtmlTextParser()
        parser.feed(data.decode("utf-8", errors="replace"))
        parser.close()
        parser.finish()
        html_title = _clean_text("".join(parser.title_parts))
        blocks = [
            SourceBlock(block.text, {**block.locator, "url": url})
            for block in parser.blocks
        ]
        metadata = {
            "url": url,
            "html_title": html_title or None,
            "heading_count": sum(
                1 for block in blocks if block.locator.get("element") == "heading"
            ),
        }
        return _parsed("webpage", "text/html", html_title, blocks, metadata)
    except SourceParseError:
        raise
    except Exception as error:
        raise SourceParseError(f"网页解析失败：{type(error).__name__}") from error


def parse_source(source_type: str, data: bytes, *, url: str | None = None) -> ParsedSource:
    if source_type == "pdf":
        return parse_pdf(data)
    if source_type == "docx":
        return parse_docx(data)
    if source_type == "webpage":
        if not url:
            raise SourceParseError("网页来源缺少 URL")
        return parse_html(data, url=url)
    raise SourceParseError(f"不支持的来源类型：{source_type}")
