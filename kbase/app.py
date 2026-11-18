import hashlib
import os
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path, PurePath
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .conversation import MAX_SOURCES, converse, grounded_answer
from .index import DuplicateError, Index
from .ingest import EXTENSIONS, MAX_FILE_BYTES, DocumentError, parse_document
from .models import DeepSeek, ModelError, Ollama

STATIC = Path(__file__).parent / "static"


class LibraryInput(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class QueryInput(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid"
    answer: bool = False


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)]


class ChatInput(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]
    history: list[ChatMessage] = Field(default_factory=list, max_length=8)
    library_id: int | None = None
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid"


def create_app(db_path: Path | None = None, model: Ollama | None = None,
               cloud_chat: DeepSeek | None = None) -> FastAPI:
    index = Index(db_path or Path(os.getenv("KBASE_DB", ".data/kbase.sqlite3")))
    ollama = model or Ollama()
    provider = os.getenv("KBASE_CHAT_PROVIDER", "ollama")
    if provider not in {"ollama", "deepseek"}:
        raise ValueError("KBASE_CHAT_PROVIDER 必须为 ollama 或 deepseek。")
    if cloud_chat is None and provider == "deepseek":
        cloud_chat = DeepSeek()
    chat = cloud_chat or ollama

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        with ExitStack() as clients:
            clients.enter_context(ollama.client)
            if cloud_chat:
                clients.enter_context(cloud_chat.client)
            index.initialize()
            yield

    app = FastAPI(title="KBase", version="1.0.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def validate_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "DELETE"} and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "请从 KBase 工作台发起请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ModelError)
    async def model_failure(request: Request, error: ModelError):
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.exception_handler(DuplicateError)
    async def duplicate(request: Request, error: DuplicateError):
        return JSONResponse({"detail": str(error)}, status_code=409)

    def require_library(library: int):
        if not index.has_library(library):
            raise HTTPException(404, "知识库不存在。")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/models")
    def model_status():
        status = ollama.status()
        if cloud_chat:
            status["chat"] = cloud_chat.status()
        return status

    @app.get("/api/libraries")
    def libraries():
        return index.libraries()

    @app.post("/api/libraries", status_code=201)
    def create_library(payload: LibraryInput):
        return index.create_library(payload.name)

    @app.get("/api/libraries/{library}/documents")
    def documents(library: int):
        require_library(library)
        return index.documents(library)

    @app.post("/api/libraries/{library}/documents", status_code=201)
    def upload(library: int, file: Annotated[UploadFile, File()]):
        require_library(library)
        filename = PurePath((file.filename or "").replace("\\", "/")).name
        if len(filename) > 240 or any(ord(char) < 32 for char in filename):
            raise HTTPException(422, "文件名过长或包含无效字符。")
        if PurePath(filename).suffix.lower() not in EXTENSIONS:
            raise HTTPException(415, "此文件格式暂不支持。")
        data = file.file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "单个文件最大支持 20 MB。")
        digest = hashlib.sha256(data).hexdigest()
        if index.has_document(library, digest):
            raise DuplicateError("相同内容已导入当前知识库。")
        try:
            chunks = parse_document(filename, data)
        except DocumentError as error:
            raise HTTPException(422, str(error)) from error
        vectors = ollama.embed([chunk.text for chunk in chunks])
        return index.add_document(library, filename, digest, data, ollama.embedding_model, chunks, vectors)

    @app.delete("/api/libraries/{library}/documents/{document}", status_code=204)
    def delete_document(library: int, document: int):
        if not index.delete_document(library, document):
            raise HTTPException(404, "文档不存在。")

    @app.get("/api/libraries/{library}/documents/{document}/download")
    def download(library: int, document: int):
        row = index.original(library, document)
        if not row:
            raise HTTPException(404, "文档不存在。")
        return Response(row["original"], media_type="application/octet-stream", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["filename"], safe=""),
            "Cache-Control": "no-store",
        })

    def search_documents(library: int, question: str, mode: str, limit: int):
        vector = None
        if mode != "keyword" and index.documents(library):
            vector = ollama.embed([question])[0]
        return index.search(library, question, mode, vector, ollama.embedding_model, limit=limit)

    @app.post("/api/chat")
    def chat_turn(payload: ChatInput):
        library = payload.library_id
        if library is not None:
            require_library(library)

        def search(question: str):
            return search_documents(library, question, payload.mode, MAX_SOURCES)

        return converse(chat, [message.model_dump() for message in payload.history],
                        payload.question, search if library is not None else None)

    @app.post("/api/libraries/{library}/query")
    def query(library: int, payload: QueryInput):
        require_library(library)
        sources = search_documents(library, payload.question, payload.mode, 6 if payload.answer else 10)
        result = {"mode": payload.mode, "sources": sources, "answer": None, "notice": None}
        return {**result, **grounded_answer(chat, payload.question, sources)} if payload.answer else result

    return app
