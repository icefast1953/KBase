import io
import json

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def word_bytes():
    doc = Document()
    doc.add_paragraph("报销凭证须在当月提交。")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "审批人"
    table.cell(0, 1).text = "财务主管"
    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def excel_bytes():
    book = Workbook()
    book.active.title = "库存"
    book.active.append(["物料", "数量"])
    book.active.append(["测试夹具", 37])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 150 Td (Pump inspection: 14 days.) Tj ET")
    page.replace_contents(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize(("name", "body", "fragment", "locator"), [
    ("制度.docx", word_bytes(), "财务主管", "表格 1"),
    ("备件.xlsx", excel_bytes(), "测试夹具", "库存"),
    ("手册.pdf", pdf_bytes(), "14 days", "第 2 页"),
    ("指南.md", "# 维修指南\n\n断电后检查。".encode(), "断电后检查", "维修指南"),
    ("清单.csv", "编号,数量\nA-17,27".encode(), "数量=27", "第 1–2 行"),
    ("通知.txt", "当日截止".encode("gb18030"), "当日截止", "第 1 行"),
])
def test_parser_preserves_text_and_locator(name, body, fragment, locator):
    from kbase.ingest import parse_document

    assert any(fragment in chunk.text and locator in chunk.location
               for chunk in parse_document(name, body))


@pytest.fixture
def model_transport():
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"},
                                                       {"name": "qwen2.5:1.5b"}]})
        payload = json.loads(request.content)
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [
                [0.8, 0.6] if "PX-481" in value and "权限" not in value else
                [1.0, 0.0] if "权限" in value or "授权" in value else [-1.0, 0.0]
                for value in payload["input"]
            ]})
        if request.url.path == "/api/chat":
            evidence = json.loads(payload["messages"][-1]["content"])
            assert any("文档负责人授权" in source["text"] for source in evidence["sources"])
            return httpx.Response(200, json={"message": {
                "content": json.dumps({"answer": "由文档负责人授权。", "references": [1]})
            }})
        raise AssertionError(request.url)

    return httpx.MockTransport(respond)


@pytest.fixture
def client(tmp_path, model_transport):
    from kbase.app import create_app
    from kbase.models import Ollama

    model = Ollama(transport=model_transport)
    with TestClient(create_app(tmp_path / "test.db", model), base_url="http://localhost") as test:
        yield test


def upload(client, library, filename, body):
    return client.post(f"/api/libraries/{library}/documents", files={"file": (filename, body)})


def query(client, library, question, mode="hybrid", answer=False):
    return client.post(f"/api/libraries/{library}/query", json={
        "question": question, "mode": mode, "answer": answer,
    })


def test_hybrid_fuses_both_paths_and_isolates_libraries(client):
    first = client.get("/api/libraries").json()[0]["id"]
    other = client.post("/api/libraries", json={"name": "另一知识库"}).json()["id"]
    assert upload(client, first, "编号.txt", "设备编号 PX-481".encode()).status_code == 201
    assert upload(client, first, "授权.txt", "操作须经文档负责人授权。".encode()).status_code == 201
    assert upload(client, other, "保密.txt", "PX-481 权限保密".encode()).status_code == 201

    keyword = query(client, first, "PX-481 权限", "keyword").json()["sources"]
    semantic = query(client, first, "PX-481 权限", "semantic").json()["sources"]
    hybrid = query(client, first, "PX-481 权限").json()["sources"]
    assert [item["filename"] for item in keyword] == ["编号.txt"]
    assert semantic[0]["filename"] == "授权.txt"
    assert {item["filename"] for item in hybrid} == {"编号.txt", "授权.txt"}
    assert hybrid[0]["keyword_rank"] == 1 and hybrid[0]["semantic_rank"] == 2


def test_answer_cites_real_uploaded_content(client):
    library = client.get("/api/libraries").json()[0]["id"]
    upload(client, library, "授权指南.md", "# 作业\n须经文档负责人授权。".encode())
    response = query(client, library, "权限由谁发放？", answer=True)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer"] == "由文档负责人授权。 [1]"
    assert result["sources"][0]["reference"] == 1
    assert result["sources"][0]["filename"] == "授权指南.md"


def test_duplicate_and_delete_keep_indexes_consistent(client):
    library = client.get("/api/libraries").json()[0]["id"]
    body = "设备编号 PX-481".encode()
    document = upload(client, library, "装置.txt", body).json()
    assert upload(client, library, "副本.txt", body).status_code == 409
    endpoint = f"/api/libraries/{library}/documents/{document['id']}"
    assert client.get(f"{endpoint}/download").content == body
    assert client.delete(endpoint).status_code == 204
    assert query(client, library, "PX-481", "keyword").json()["sources"] == []
    assert query(client, library, "PX-481", "semantic").json()["sources"] == []
    assert client.get(f"{endpoint}/download").status_code == 404


def test_invalid_upload_leaves_no_document(client):
    library = client.get("/api/libraries").json()[0]["id"]
    assert upload(client, library, "坏文件.docx", b"not a word file").status_code == 422
    assert upload(client, library, "未知.exe", b"unsupported").status_code == 415
    assert client.get(f"/api/libraries/{library}/documents").json() == []


def test_model_failure_does_not_leave_partial_index(tmp_path):
    from kbase.app import create_app
    from kbase.models import Ollama

    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "model not found"}))
    with TestClient(create_app(tmp_path / "failed.db", Ollama(transport=transport)),
                    base_url="http://localhost") as client:
        library = client.get("/api/libraries").json()[0]["id"]
        assert upload(client, library, "索引.txt", b"test content").status_code == 503
        assert client.get(f"/api/libraries/{library}/documents").json() == []
        result = query(client, library, "test content", answer=True).json()
        assert result["sources"] == []
        assert "没有找到" in result["answer"]


def test_web_workspace_and_model_status(client):
    assert "KBase".encode() in client.get("/").content
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/models").json()["embedding"]["ready"] is True


@pytest.mark.parametrize("reference", [1, 99])
def test_deepseek_chat_uses_selected_excerpts_and_checks_citations(tmp_path, model_transport, reference):
    from kbase.app import create_app
    from kbase.models import DeepSeek, Ollama

    def deepseek_response(request):
        assert request.url.host == "api.deepseek.com"
        assert request.url.path == "/chat/completions"
        assert request.headers.get("Authorization") == "Bearer test-placeholder"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["response_format"] == {"type": "json_object"}
        evidence = json.loads(payload["messages"][-1]["content"])
        assert evidence["sources"] == [{"reference": 1, "text": "须经文档负责人授权。"}]
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps({"answer": "由文档负责人授权。", "references": [reference]})
        }}]})

    chat = DeepSeek("test-placeholder", transport=httpx.MockTransport(deepseek_response))
    app = create_app(tmp_path / "deepseek.db", Ollama(transport=model_transport), chat)
    with TestClient(app, base_url="http://localhost") as client:
        library = client.get("/api/libraries").json()[0]["id"]
        assert upload(client, library, "授权指南.txt", "须经文档负责人授权。".encode()).status_code == 201
        assert "test-placeholder" not in client.get("/api/models").text
        result = query(client, library, "权限由谁发放？", answer=True).json()
        assert result["sources"][0]["filename"] == "授权指南.txt"
        if reference == 1:
            assert result["answer"] == "由文档负责人授权。 [1]"
        else:
            assert result["answer"] is None and "引用" in result["notice"]
