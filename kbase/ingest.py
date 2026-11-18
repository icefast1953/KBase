import csv
import io
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePath
from zipfile import ZipFile

import xlrd
from docx import Document
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from pypdf import PdfReader

EXTENSIONS = {".docx", ".xlsx", ".xls", ".pdf", ".md", ".markdown", ".txt", ".csv", ".tsv"}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CHUNKS = 2500
CHUNK_SIZE = 700
CHUNK_OVERLAP = 90


@dataclass(frozen=True)
class Chunk:
    location: str
    text: str


class DocumentError(ValueError):
    pass


def span(label: str, first: int, last: int) -> str:
    numbers = str(first) if first == last else f"{first}–{last}"
    return f"第 {numbers} {label}"


def text_windows(text: str, location: str) -> Iterator[Chunk]:
    text = text.replace("\x00", "").strip()
    while len(text) > CHUNK_SIZE:
        edge = max(text.rfind(mark, 400, CHUNK_SIZE) for mark in "\n。！？； ")
        end = CHUNK_SIZE if edge == -1 else edge + 1
        yield Chunk(location, text[:end].strip())
        text = text[end - CHUNK_OVERLAP:].lstrip()
    if text:
        yield Chunk(location, text)


def line_chunks(lines: Iterable[tuple[int, str]], unit: str, prefix: str = "") -> Iterator[Chunk]:
    group: list[str] = []
    size = first = last = 0
    for position, text in lines:
        if group and size + len(text) + 1 > CHUNK_SIZE:
            yield from text_windows("\n".join(group), prefix + span(unit, first, last))
            group.clear()
            size = 0
        if not text.strip() and not group:
            continue
        if not group:
            first = position
        last = position
        group.append(text)
        size += len(text) + 1
    if group:
        yield from text_windows("\n".join(group), prefix + span(unit, first, last))


def table_lines(rows: Iterable[Iterable[object]]) -> Iterator[tuple[int, str]]:
    headers: tuple[str, ...] = ()
    started = False
    for position, row in enumerate(rows, 1):
        values = tuple("" if value is None else str(value).strip() for value in row)
        if not any(values):
            continue
        if not started:
            started = True
            cells = [value for value in row if value is not None and str(value).strip()]
            if len(cells) > 1 and all(isinstance(value, str) and not value.isnumeric() for value in cells):
                headers = values
            yield position, " | ".join(value for value in values if value)
            continue
        columns = []
        for index, value in enumerate(values):
            if value:
                header = headers[index] if index < len(headers) else ""
                columns.append(f"{header}={value}" if header else value)
        yield position, " | ".join(columns)


def word_chunks(data: bytes) -> Iterator[Chunk]:
    document = Document(io.BytesIO(data))
    paragraph_number = table_number = 0
    paragraphs: list[tuple[int, str]] = []
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            paragraph_number += 1
            paragraphs.append((paragraph_number, item.text))
        else:
            yield from line_chunks(paragraphs, "段")
            paragraphs.clear()
            table_number += 1
            rows = (tuple(cell.text for cell in row.cells) for row in item.rows)
            yield from line_chunks(table_lines(rows), "行", f"表格 {table_number} · ")
    yield from line_chunks(paragraphs, "段")


def excel_chunks(data: bytes, suffix: str) -> Iterator[Chunk]:
    if suffix == ".xls":
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
        try:
            for sheet in book.sheets():
                rows = (sheet.row_values(index) for index in range(sheet.nrows))
                yield from line_chunks(table_lines(rows), "行", f"{sheet.name} · ")
        finally:
            book.release_resources()
        return

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    try:
        for sheet in book.worksheets:
            sheet.reset_dimensions()
            rows = sheet.iter_rows(values_only=True)
            yield from line_chunks(table_lines(rows), "行", f"{sheet.title} · ")
    finally:
        book.close()


def markdown_chunks(text: str) -> Iterator[Chunk]:
    heading = ""
    lines: list[tuple[int, str]] = []
    fence = ""
    for position, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            fence = "" if fence == marker[1][0] else fence or marker[1][0]
        match = None if fence else re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            yield from line_chunks(lines, "行", f"{heading} · " if heading else "")
            lines.clear()
            heading = match[1][:80]
        lines.append((position, line))
    yield from line_chunks(lines, "行", f"{heading} · " if heading else "")


def extract(data: bytes, suffix: str) -> Iterator[Chunk]:
    if suffix in {".docx", ".xlsx"}:
        with ZipFile(io.BytesIO(data)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 80 * 1024 * 1024:
                raise DocumentError("Office 文件展开后超过 80 MB，请拆分后导入。")
    if suffix == ".docx":
        yield from word_chunks(data)
    elif suffix in {".xls", ".xlsx"}:
        yield from excel_chunks(data, suffix)
    elif suffix == ".pdf":
        pdf = PdfReader(io.BytesIO(data))
        if pdf.is_encrypted:
            raise DocumentError("请先解除 PDF 加密再导入。")
        for number, page in enumerate(pdf.pages, 1):
            yield from text_windows(page.extract_text() or "", span("页", number, number))
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")
        if suffix in {".csv", ".tsv"}:
            rows = csv.reader(io.StringIO(text, newline=""), delimiter="\t" if suffix == ".tsv" else ",")
            yield from line_chunks(table_lines(rows), "行")
        elif suffix in {".md", ".markdown"}:
            yield from markdown_chunks(text)
        else:
            yield from line_chunks(enumerate(text.splitlines(), 1), "行")


def parse_document(filename: str, data: bytes) -> list[Chunk]:
    suffix = PurePath(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise DocumentError("此文件格式暂不支持。")
    chunks = []
    try:
        for chunk in extract(data, suffix):
            chunks.append(chunk)
            if len(chunks) > MAX_CHUNKS:
                raise DocumentError("文件可检索片段过多，请拆分后导入。")
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError("无法读取文件，请检查格式、文件完整性或文本编码。") from error
    if not chunks:
        raise DocumentError("文件中没有可提取的文本。扫描版 PDF 请先做文字识别。")
    return chunks
