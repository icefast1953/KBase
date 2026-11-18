import math
import os
import struct

import httpx


class ModelError(RuntimeError):
    pass


ANSWER_FORMAT = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "references": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answer", "references"],
}


def normalized(vector: list[float]) -> list[float]:
    magnitude = math.hypot(*vector)
    if not magnitude or not math.isfinite(magnitude):
        raise ModelError("向量模型返回了无效向量。")
    return [value / magnitude for value in vector]


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(vector: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(vector) // 4}f", vector)


class Ollama:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.embedding_model = os.getenv("KBASE_EMBED_MODEL", "bge-m3:latest")
        self.chat_model = os.getenv("KBASE_CHAT_MODEL", "qwen2.5:1.5b")
        self.client = httpx.Client(
            base_url=os.getenv("KBASE_OLLAMA_URL", "http://127.0.0.1:11434"),
            timeout=httpx.Timeout(180, connect=3),
            trust_env=False,
            transport=transport,
        )

    def request(self, endpoint: str, payload: dict | None = None, timeout: float = 180) -> dict:
        try:
            if payload is None:
                response = self.client.get(endpoint, timeout=timeout)
            else:
                response = self.client.post(endpoint, json=payload, timeout=httpx.Timeout(timeout, connect=3))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except httpx.HTTPStatusError as error:
            model = payload.get("model") if payload else None
            if error.response.status_code == 404 and model:
                raise ModelError(f"缺少模型 {model}。请运行 ollama pull {model}。") from error
            raise ModelError("Ollama 暂时无法处理请求。请检查本地模型服务。") from error
        except httpx.TimeoutException as error:
            raise ModelError("本地模型响应超时，请稍后重试。") from error
        except (httpx.RequestError, ValueError) as error:
            raise ModelError("无法连接本地模型服务，请确认 Ollama 正在运行。") from error

    def status(self) -> dict:
        try:
            available = {entry["name"] for entry in self.request("/api/tags", timeout=3).get("models", [])}
            connected = True
        except (ModelError, KeyError, TypeError):
            available = set()
            connected = False
        return {
            "connected": connected,
            "embedding": {"name": self.embedding_model, "ready": self.embedding_model in available},
            "chat": {"name": self.chat_model, "ready": self.chat_model in available, "provider": "ollama"},
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), 16):
            group = texts[start:start + 16]
            response = self.request("/api/embed", {
                "model": self.embedding_model, "input": group, "truncate": False,
            })
            try:
                batch = response["embeddings"]
                if len(batch) != len(group):
                    raise ValueError
                vectors.extend(normalized(vector) for vector in batch)
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise ModelError("向量模型返回的数据格式不正确。") from error
        if len({len(vector) for vector in vectors}) > 1:
            raise ModelError("向量模型返回了不同维度的向量，请重试。")
        return vectors

    def answer(self, messages: list[dict]) -> str:
        response = self.request("/api/chat", {
            "model": self.chat_model, "messages": messages, "stream": False,
            "format": ANSWER_FORMAT,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 600},
        })
        try:
            text = response["message"]["content"].strip()
            if not text:
                raise ValueError
            return text
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ModelError("问答模型未返回可用的文字，请重试。") from error


class DeepSeek:
    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None):
        key = (os.getenv("DEEPSEEK_API_KEY", "") if api_key is None else api_key).strip()
        self.chat_model = os.getenv("KBASE_DEEPSEEK_MODEL", "deepseek-flash")
        self.configured = bool(key)
        self.client = httpx.Client(
            base_url="https://api.deepseek.com",
            headers={"Authorization": f"Bearer {key}"} if key else None,
            timeout=httpx.Timeout(120, connect=8),
            trust_env=False,
            transport=transport,
        )

    def status(self) -> dict:
        return {"name": self.chat_model, "ready": self.configured, "provider": "deepseek"}

    def answer(self, messages: list[dict]) -> str:
        if not self.configured:
            raise ModelError("请在本机配置 DEEPSEEK_API_KEY 后重新启动 KBase。")
        try:
            response = self.client.post("/chat/completions", json={
                "model": self.chat_model, "messages": messages, "stream": False,
                "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": 1000,
                "response_format": {"type": "json_object"},
            })
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            return content
        except httpx.HTTPStatusError as error:
            detail = {
                401: "DeepSeek 鉴权失败，请检查本机的密钥配置。",
                402: "DeepSeek 账户余额不足。",
                429: "DeepSeek 请求过于频繁，请稍后重试。",
            }.get(error.response.status_code, "DeepSeek 暂时无法处理请求，请检查模型设置或稍后重试。")
            raise ModelError(detail) from error
        except httpx.RequestError as error:
            raise ModelError("暂时无法连接 DeepSeek，请检查网络后重试。") from error
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise ModelError("DeepSeek 未返回可用的回答，请重试。") from error
