import heapq
import math
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from pathlib import Path

from .ingest import Chunk
from .models import ModelError, pack_vector, unpack_vector

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES libraries(id),
    filename TEXT NOT NULL,
    digest TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    original BLOB NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(library_id, digest)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    location TEXT NOT NULL,
    text TEXT NOT NULL,
    vector BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(tokens);
CREATE TRIGGER IF NOT EXISTS delete_chunk AFTER DELETE ON chunks
BEGIN
    DELETE FROM chunk_fts WHERE rowid = old.id;
END;
"""

HAN = r"\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ffff"
HAN_RUN = re.compile(f"[{HAN}]+")
WORDS = re.compile(r"[^\W_]+")
CANDIDATES = 30
MIN_SIMILARITY = 0.35


class DuplicateError(ValueError):
    pass


def tokens(text: str, for_query: bool = False) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens_found: list[str] = []
    start = 0
    for match in HAN_RUN.finditer(text):
        tokens_found.extend(WORDS.findall(text[start:match.start()]))
        run = match[0]
        if len(run) == 1 or not for_query:
            tokens_found.extend(run)
        tokens_found.extend(run[index:index + 2] for index in range(len(run) - 1))
        start = match.end()
    tokens_found.extend(WORDS.findall(text[start:]))
    return tokens_found


class Index:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO libraries(id, name) VALUES (1, '默认知识库')")

    def libraries(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT l.id, l.name, COUNT(d.id) AS documents
                FROM libraries l LEFT JOIN documents d ON d.library_id = l.id
                GROUP BY l.id ORDER BY l.id
            """)]

    def create_library(self, name: str):
        try:
            with self.connect() as conn:
                library_id = conn.execute("INSERT INTO libraries(name) VALUES (?)", (name,)).lastrowid
        except sqlite3.IntegrityError as error:
            raise DuplicateError("知识库名称已存在。") from error
        return {"id": library_id, "name": name, "documents": 0}

    def has_library(self, library: int) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM libraries WHERE id = ?", (library,)).fetchone() is not None

    def documents(self, library: int):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT d.id, d.filename, d.imported_at, d.embedding_model,
                       length(d.original) AS bytes, COUNT(c.id) AS chunks
                FROM documents d JOIN chunks c ON c.document_id = d.id
                WHERE d.library_id = ? GROUP BY d.id ORDER BY d.id DESC
            """, (library,))]

    def has_document(self, library: int, digest: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM documents WHERE library_id = ? AND digest = ?",
                                (library, digest)).fetchone() is not None

    def add_document(self, library: int, filename: str, digest: str, original: bytes,
                     embedding_model: str, chunks: list[Chunk], vectors: list[list[float]]):
        with self.connect() as conn:
            document = conn.execute("""
                INSERT INTO documents(library_id, filename, digest, original, embedding_model)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(library_id, digest) DO NOTHING
                RETURNING id, filename, imported_at, embedding_model
            """, (library, filename, digest, original, embedding_model)).fetchone()
            if document is None:
                raise DuplicateError("相同内容已导入当前知识库。")
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk_id = conn.execute("""
                    INSERT INTO chunks(document_id, location, text, vector) VALUES (?, ?, ?, ?)
                """, (document["id"], chunk.location, chunk.text, pack_vector(vector))).lastrowid
                conn.execute("INSERT INTO chunk_fts(rowid, tokens) VALUES (?, ?)",
                             (chunk_id, " ".join(tokens(f"{filename}\n{chunk.location}\n{chunk.text}"))))
            return {**dict(document), "chunks": len(chunks), "bytes": len(original)}

    def original(self, library: int, document: int):
        with self.connect() as conn:
            return conn.execute("SELECT filename, original FROM documents WHERE library_id = ? AND id = ?",
                                (library, document)).fetchone()

    def delete_document(self, library: int, document: int) -> bool:
        with self.connect() as conn:
            return bool(conn.execute("DELETE FROM documents WHERE library_id = ? AND id = ?",
                                     (library, document)).rowcount)

    def search(self, library: int, question: str, mode: str,
               vector: list[float] | None, embedding_model: str, limit: int):
        with self.connect() as conn:
            conn.execute("BEGIN")
            keyword = self.keyword(conn, library, question) if mode != "semantic" else []
            semantic = self.semantic(conn, library, vector, embedding_model) if vector is not None else []
            keyword_ranks = {chunk_id: rank for rank, chunk_id in enumerate(keyword, 1)}
            semantic_ranks = {chunk_id: rank for rank, chunk_id in enumerate(semantic, 1)}
            identifiers = keyword_ranks.keys() | semantic_ranks.keys()
            scores = {chunk_id: sum(1 / (60 + ranks[chunk_id]) for ranks in (keyword_ranks, semantic_ranks)
                                   if chunk_id in ranks) for chunk_id in identifiers}
            best = sorted(identifiers, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]
            sources = []
            for reference, chunk_id in enumerate(best, 1):
                row = conn.execute("""
                    SELECT d.id AS document_id, d.filename, c.location, c.text
                    FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?
                """, (chunk_id,)).fetchone()
                sources.append({
                    **dict(row), "reference": reference, "keyword_rank": keyword_ranks.get(chunk_id),
                    "semantic_rank": semantic_ranks.get(chunk_id), "score": round(scores[chunk_id], 6),
                })
            return sources

    @staticmethod
    def keyword(conn, library: int, question: str):
        terms = list(dict.fromkeys(tokens(question, for_query=True)))
        if not terms:
            return []
        pattern = " OR ".join(f'"{term}"' for term in terms[:120])
        return [row[0] for row in conn.execute("""
            SELECT c.id
            FROM chunk_fts JOIN chunks c ON c.id = chunk_fts.rowid
            JOIN documents d ON d.id = c.document_id
            WHERE chunk_fts MATCH ? AND d.library_id = ?
            ORDER BY bm25(chunk_fts), c.id LIMIT ?
        """, (pattern, library, CANDIDATES))]

    @staticmethod
    def semantic(conn, library: int, vector: list[float], embedding_model: str):
        rows = conn.execute("""
            SELECT c.id, c.vector, d.embedding_model FROM chunks c
            JOIN documents d ON d.id = c.document_id WHERE d.library_id = ?
        """, (library,))

        def hits():
            for chunk_id, stored, model in rows:
                if model != embedding_model or len(stored) != 4 * len(vector):
                    raise ModelError("当前知识库的向量配置已变更。请重新导入文档，或先用关键词检索。")
                similarity = math.fsum(a * b for a, b in zip(unpack_vector(stored), vector))
                if similarity >= MIN_SIMILARITY:
                    yield similarity, -chunk_id

        return [-negative_id for _, negative_id in heapq.nlargest(CANDIDATES, hits())]
