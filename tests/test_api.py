import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from taskprinter.config import Config
from taskprinter.server import create_app


@pytest.fixture
def client(tmp_path):
    cfg = Config(backend="mock", data_dir=tmp_path, token="secret", hold_after=0)
    with TestClient(create_app(cfg)) as c:
        yield c


AUTH = {"Authorization": "Bearer secret"}


def doc(*texts, kind="p", **fmt):
    """A minimal editor document: one block per text."""
    return {"blocks": [{"type": kind, "runs": [{"text": t}]} for t in texts], "format": fmt}


def wait_done(client, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = next(j for j in client.get("/api/jobs", headers=AUTH).json() if j["id"] == job_id)
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_auth_required(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/login", json={"token": "nope"}).status_code == 401
    assert client.post("/api/login", json={"token": "secret"}).status_code == 200
    assert client.get("/api/jobs").status_code == 200  # cookie session


def test_previews_are_png(client):
    r = client.post("/api/preview/note", headers=AUTH, json={"text": "hello"})
    assert r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).width == 384
    assert client.get("/api/preview/test", headers=AUTH).status_code == 200


def test_image_print_and_reprint(client):
    buf = io.BytesIO()
    Image.new("RGB", (800, 400), "gray").save(buf, "JPEG")
    files = {"file": ("photo.jpg", buf.getvalue(), "image/jpeg")}
    job = client.post("/api/print/image", headers=AUTH, files=files, data={"dither": "floyd"}).json()
    assert job["rows"] == 192 + 16  # image + default 2 mm gap between prints
    assert wait_done(client, job["id"])["status"] == "done"
    again = client.post(f"/api/jobs/{job['id']}/reprint", headers=AUTH).json()
    assert wait_done(client, again["id"])["status"] == "done"


def test_rewind_retracts_previous_feed(client):
    service = client.app.state.service
    client.put("/api/settings", headers=AUTH, json={"rewind": True, "feed": 90, "feed_mode": "a1"})
    sent = []
    original = service.printer.send

    async def capture(data, progress=None):
        sent.append(data)
        await original(data, progress)

    service.printer.send = capture
    for _ in range(2):
        job = client.post("/api/print/note", headers=AUTH, json={"text": "hi"}).json()
        wait_done(client, job["id"])
    from taskprinter import protocol as p
    first, second = ([c for c, _ in p.iter_packets(d)] for d in sent)
    assert 0xA0 not in first  # nothing fed yet
    assert second[1] == 0xA0 and second[-1] == 0xA1


def test_bad_image_rejected(client):
    files = {"file": ("x.png", b"not an image", "image/png")}
    assert client.post("/api/print/image", headers=AUTH, files=files).status_code == 400


def test_settings_validation(client):
    s = client.put("/api/settings", headers=AUTH, json={"energy": 99999, "encoding": "bogus", "feed": 20}).json()
    assert s["energy"] == 0xFFFF and s["encoding"] == "raw-lsb" and s["feed"] == 20
    assert client.put("/api/settings", headers=AUTH, json={"label_width": "wide"}).status_code == 400


def test_share_fallback(client):
    client.post("/api/login", json={"token": "secret"})
    r = client.post("/share", data={"title": "Link", "text": "Check this", "url": "https://example.com"},
                    follow_redirects=False)
    assert r.status_code == 303 and "shared" in r.headers["location"]


def test_pwa_assets(client):
    assert client.get("/").status_code == 200
    assert client.get("/sw.js").status_code == 200
    assert client.get("/manifest.webmanifest").json()["share_target"]["action"] == "/share"
    assert client.get("/icons/icon-192.png").headers["content-type"] == "image/png"


def test_label_mode_pads_to_pitch(client):
    client.put("/api/settings", headers=AUTH, json={"paper": "label", "label_width": 40,
                                                    "label_height": 30, "label_gap": 2})
    job = client.post("/api/print/note", headers=AUTH, json={"text": "# Label\nhello"}).json()
    assert job["rows"] == (30 + 2) * 8
    big = client.post("/api/print/note", headers=AUTH, json={"text": "line\n" * 60}).json()
    assert big["rows"] == (30 + 2) * 8  # long content is scaled down to fit the label
    img = Image.open(io.BytesIO(client.post("/api/preview/note", headers=AUTH, json={"text": "x"}).content))
    assert img.size == (384, 256)


def test_roll_separator_and_no_timestamp(client):
    client.put("/api/settings", headers=AUTH, json={"gap": 0, "separator": "dashed"})
    with_ts = client.post("/api/preview/doc", headers=AUTH, json=doc("a", timestamp=True))
    without = client.post("/api/preview/doc", headers=AUTH, json=doc("a", timestamp=False))
    assert Image.open(io.BytesIO(without.content)).height < Image.open(io.BytesIO(with_ts.content)).height


def test_copies(client):
    job = client.post("/api/print/note", headers=AUTH, json={"text": "hi", "copies": 3}).json()
    assert job["copies"] == 3
    titles = [j["title"] for j in client.get("/api/jobs", headers=AUTH).json()]
    assert {"hi (1/3)", "hi (2/3)", "hi (3/3)"} <= set(titles)


def test_qr_and_banner(client):
    qr = client.post("/api/print/qr", headers=AUTH, json={"data": "https://example.com", "caption": "**Site**"})
    assert qr.status_code == 200 and qr.json()["kind"] == "qr"
    banner = client.post("/api/preview/banner", headers=AUTH, json={"text": "HELLO"})
    img = Image.open(io.BytesIO(banner.content))
    assert img.width == 384 and img.height > 384  # runs along the roll


def test_templates(client):
    tpl = client.post("/api/templates", headers=AUTH, json={
        "name": "Morning", "kind": "doc", "payload": doc("Water", "Stretch", kind="check")}).json()
    assert [t["name"] for t in client.get("/api/templates", headers=AUTH).json()] == ["Morning"]
    job = client.post(f"/api/templates/{tpl['id']}/print?copies=2", headers=AUTH).json()
    assert job["kind"] == "doc" and job["copies"] == 2
    bad = client.post("/api/templates", headers=AUTH, json={"name": "x", "kind": "note", "payload": {}})
    assert bad.status_code == 422
    assert client.delete(f"/api/templates/{tpl['id']}", headers=AUTH).status_code == 200




def test_document_print_with_formatting(client, tmp_path):
    body = {
        "blocks": [
            {"type": "h1", "runs": [{"text": "Plan"}], "align": "center"},
            {"type": "p", "runs": [{"text": "Ship "}, {"text": "today", "b": True, "u": True}]},
            {"type": "number", "runs": [{"text": "one"}]},
            {"type": "number", "runs": [{"text": "two"}]},
            {"type": "check", "runs": [{"text": "done"}], "checked": True},
            {"type": "hr"},
            {"type": "code", "text": "uv run x"},
        ],
        "format": {"font": "serif", "size": 22, "border": "rounded", "invert": True, "line_spacing": 1.3},
        "copies": 1,
    }
    job = client.post("/api/print/doc", headers=AUTH, json=body).json()
    assert job["title"] == "Plan" and wait_done(client, job["id"])["status"] == "done"
    img = Image.open(io.BytesIO(client.post("/api/preview/doc", headers=AUTH, json=body).content)).convert("L")
    assert img.getpixel((4, 4)) == 0  # inverted: the corner is black


def test_numbered_lists_restart_after_other_blocks():
    from taskprinter.server import DocIn, doc_blocks

    blocks = doc_blocks(DocIn(**{"blocks": [
        {"type": "number", "runs": [{"text": "a"}]}, {"type": "number", "runs": [{"text": "b"}]},
        {"type": "number", "level": 1, "runs": [{"text": "b.1"}]},
        {"type": "number", "runs": [{"text": "c"}]},
        {"type": "p", "runs": [{"text": "break"}]},
        {"type": "number", "runs": [{"text": "again"}]},
    ]}))
    assert [b.marker for b in blocks if b.kind == "item"] == ["1.", "2.", "1.", "3.", "1."]


def test_default_format_setting(client):
    s = client.put("/api/settings", headers=AUTH, json={"format": {"font": "mono", "size": 300, "border": "x"}}).json()
    assert s["format"]["font"] == "mono" and s["format"]["size"] == 96 and s["format"]["border"] == "none"
    wide = client.post("/api/preview/doc", headers=AUTH, json=doc("x", margin=0))
    assert wide.status_code == 200


MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def mcp_call(client, method, params=None, headers=MCP_HEADERS):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", headers=headers, json=body)


def test_mcp_requires_token(client):
    assert mcp_call(client, "tools/list", headers={**MCP_HEADERS, "Authorization": "Bearer nope"}).status_code == 401


def test_mcp_tools_print(client):
    init = mcp_call(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}})
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "taskprinter"
    tools = {t["name"] for t in mcp_call(client, "tools/list").json()["result"]["tools"]}
    assert {"print_note", "print_tasks", "print_task", "printer_status"} <= tools
    res = mcp_call(client, "tools/call", {"name": "print_tasks", "arguments": {
        "title": "Today", "tasks": ["Call the bank", "[x] Water plants", "  buy soil"]}}).json()
    text = res["result"]["content"][0]["text"]
    assert "Queued print job" in text, res
    job_id = int(text.split("#")[1].split(":")[0].split()[0])
    assert wait_done(client, job_id)["status"] == "done"
    status = mcp_call(client, "tools/call", {"name": "printer_status", "arguments": {}}).json()
    assert "state:" in status["result"]["content"][0]["text"]


def test_rotate180_flips_the_printed_rows(client):
    service = client.app.state.service
    sent = []
    original = service.printer.send

    async def capture(data, progress=None):
        sent.append(data)
        await original(data, progress)

    service.printer.send = capture
    from taskprinter import protocol as p

    def rows_of(data):
        return [pl for c, pl in p.iter_packets(data) if c == 0xA2]

    for flag in (False, True):
        client.put("/api/settings", headers=AUTH, json={"rotate180": flag, "rewind": False, "encoding": "raw"})
        job = client.post("/api/print/doc", headers=AUTH, json=doc("Top line", timestamp=False)).json()
        wait_done(client, job["id"])
    upright, rotated = rows_of(sent[0]), rows_of(sent[1])
    assert len(upright) == len(rotated)
    assert rotated == [bytes(int(f"{b:08b}"[::-1], 2) for b in reversed(r)) for r in reversed(upright)]
