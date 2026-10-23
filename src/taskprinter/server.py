"""HTTP API + PWA host."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from PIL import Image, ImageDraw, UnidentifiedImageError
from pydantic import BaseModel, Field

from . import __version__, markdown, render
from .config import Config
from .imaging import DITHER_MODES, to_bitmap
from .markdown import Block, Run, Style
from .mcp_server import build_mcp
from .service import PrintService

log = logging.getLogger("taskprinter.server")
WEB = Path(__file__).parent / "web"
COOKIE = "tp_session"

Copies = Field(1, ge=1, le=20)


# ---- request models ---------------------------------------------------------

class RunIn(BaseModel):
    text: str = Field("", max_length=4000)
    b: bool = False
    i: bool = False
    u: bool = False
    s: bool = False
    code: bool = False


class BlockIn(BaseModel):
    type: Literal["p", "h1", "h2", "h3", "quote", "code", "hr", "bullet", "number", "check"] = "p"
    runs: list[RunIn] = Field(default_factory=list, max_length=500)
    text: str = Field("", max_length=8000)  # code blocks
    align: Literal["", "left", "center", "right"] = ""
    level: int = Field(0, ge=0, le=3)  # list nesting
    checked: bool = False


class FormatIn(BaseModel):
    """Per-print overrides of the saved default format; unset fields use the default."""

    font: Literal["sans", "serif", "mono"] | None = None
    size: int | None = Field(None, ge=12, le=96)
    align: Literal["left", "center", "right"] | None = None
    line_spacing: float | None = Field(None, ge=0.7, le=2.5)
    paragraph_spacing: int | None = Field(None, ge=0, le=60)
    margin: int | None = Field(None, ge=0, le=60)
    border: Literal["none", "line", "double", "dashed", "rounded"] | None = None
    invert: bool | None = None
    timestamp: bool | None = None


class DocIn(BaseModel):
    blocks: list[BlockIn] = Field(min_length=1, max_length=400)
    format: FormatIn = Field(default_factory=FormatIn)
    title: str = Field("", max_length=200)  # shown in History only
    copies: int = Copies


class NoteIn(BaseModel):
    """Typed text for the CLI, scripts and the share sheet."""

    text: str = Field(min_length=1, max_length=8000)
    title: str = Field("", max_length=200)
    size: int | None = Field(None, ge=12, le=96)
    align: Literal["left", "center", "right"] | None = None
    bold: bool = False
    timestamp: bool | None = None
    markdown: bool = True
    copies: int = Copies


class QrIn(BaseModel):
    data: str = Field(min_length=1, max_length=1500)
    caption: str = Field("", max_length=500)
    scale: float = Field(1.0, ge=0.2, le=1.0)
    copies: int = Copies


class BannerIn(BaseModel):
    text: str = Field(min_length=1, max_length=120)
    font: Literal["sans", "serif", "mono"] = "sans"
    copies: int = Copies


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["doc", "note", "qr"]
    payload: dict


class FeedIn(BaseModel):
    amount: int = Field(20, ge=1, le=200)


class LoginIn(BaseModel):
    token: str


class ConnectIn(BaseModel):
    address: str | None = None


def doc_blocks(doc: DocIn) -> list[Block]:
    """Editor blocks -> the renderer's block model (numbered lists get their numbers here)."""
    out: list[Block] = []
    counters: dict[int, int] = {}
    for b in doc.blocks:
        runs = [Run(r.text, Style(bold=r.b, italic=r.i, code=r.code, strike=r.s, underline=r.u))
                for r in b.runs if r.text]
        if b.type == "number":
            counters = {lvl: n for lvl, n in counters.items() if lvl <= b.level}
            counters[b.level] = counters.get(b.level, 0) + 1
        elif b.type not in ("bullet", "check"):
            counters = {}
        if b.type in ("h1", "h2", "h3"):
            out.append(Block("heading", runs, level=int(b.type[1]), align=b.align))
        elif b.type == "p":
            out.append(Block("para", runs, align=b.align))
        elif b.type == "quote":
            out.append(Block("quote", runs, align=b.align))
        elif b.type == "code":
            out.append(Block("code", [], text=b.text or "".join(r.text for r in runs)))
        elif b.type == "hr":
            out.append(Block("rule", []))
        elif b.type == "bullet":
            out.append(Block("item", runs, level=b.level, marker="•"))
        elif b.type == "number":
            out.append(Block("item", runs, level=b.level, marker=f"{counters[b.level]}."))
        else:
            out.append(Block("item", runs, level=b.level, checked=b.checked))
    return out


def doc_title(doc: DocIn) -> str:
    if doc.title.strip():
        return doc.title.strip()
    for b in doc.blocks:
        text = b.text or "".join(r.text for r in b.runs)
        if text.strip():
            return " ".join(text.split())[:60]
    return "Document"


# ---- app factory ------------------------------------------------------------

def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    service = PrintService(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        if not cfg.token:
            log.warning("TP_TOKEN is not set - the web app and /mcp are open to anyone who can reach them.")
        async with mcp.session_manager.run():
            yield
        await service.stop()

    app = FastAPI(title="Task Printer", version=__version__, lifespan=lifespan)
    app.state.service = service
    session_value = hmac.new(cfg.token.encode(), b"tp-session-v1", hashlib.sha256).hexdigest()

    def authorized(request: Request) -> bool:
        if not cfg.token:
            return True
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and hmac.compare_digest(header[7:].strip(), cfg.token):
            return True
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_value)

    def require_auth(request: Request) -> None:
        if not authorized(request):
            raise HTTPException(401, "Login required")

    api = Depends(require_auth)

    # ---- MCP for AI agents: /mcp (streamable HTTP), tools call this API in-process ----
    auth_header = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    mcp = build_mcp(lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://taskprinter", headers=auth_header))
    # Bearer-token auth replaces the SDK's Host-header check, so it works on a LAN address.
    mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0",
                            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    mcp_asgi = StreamableHTTPASGIApp(mcp.session_manager)

    class MCPEndpoint:  # a class, so Starlette treats it as a raw ASGI app
        async def __call__(self, scope, receive, send):
            if not authorized(Request(scope)):
                await JSONResponse({"detail": "Login required"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
            await mcp_asgi(scope, receive, send)

    app.router.add_route("/mcp", MCPEndpoint(), include_in_schema=False)

    def png(img: Image.Image) -> Response:
        """Preview exactly what will print: the bitmap fitted to the current paper."""
        buf = io.BytesIO()
        service.finalize(img).save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

    def enqueue(kind: str, title: str, bitmap: Image.Image, copies: int = 1) -> dict:
        try:
            return service.enqueue(kind, title, bitmap, copies=copies)
        except ValueError as exc:
            raise HTTPException(413, str(exc)) from exc

    async def read_image(upload: UploadFile) -> Image.Image:
        limit = cfg.max_upload_mb * 1024 * 1024
        data = await upload.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"Image larger than {cfg.max_upload_mb} MB")
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise HTTPException(400, f"Not a supported image: {exc}") from exc

    async def image_bitmap(file: UploadFile, dither: str, threshold: int, brightness: float,
                           contrast: float, rotate: bool, autolevel: bool) -> Image.Image:
        if dither not in DITHER_MODES:
            raise HTTPException(400, f"dither must be one of {DITHER_MODES}")
        img = await read_image(file)
        return await asyncio.to_thread(
            to_bitmap, img, service.content_width(), dither=dither, threshold=threshold, brightness=brightness,
            contrast=contrast, rotate=rotate, autolevel=autolevel,
        )

    # ---- auth --------------------------------------------------------------

    @app.post("/api/login")
    async def login(body: LoginIn):
        if cfg.token and not hmac.compare_digest(body.token, cfg.token):
            raise HTTPException(401, "Wrong token")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, session_value, max_age=60 * 60 * 24 * 365, httponly=True,
                        samesite="lax", secure=cfg.cookie_secure)
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/me")
    async def me(request: Request):
        return {"authenticated": authorized(request), "auth_required": bool(cfg.token), "version": __version__}

    # ---- status & events ---------------------------------------------------

    @app.get("/api/status", dependencies=[api])
    async def status():
        return {"printer": service.printer.info(), "queue": service.queue_info(), "width": cfg.width}

    @app.get("/api/events", dependencies=[api])
    async def events(request: Request):
        queue = service.events.subscribe()

        async def stream():
            try:
                hello = {"printer": service.printer.info(), "queue": service.queue_info()}
                yield f"event: hello\ndata: {json.dumps(hello)}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                service.events.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- renderers ---------------------------------------------------------

    def doc_bitmap(doc: DocIn) -> Image.Image:
        fmt = service.format(doc.format.model_dump(exclude_none=True))
        return render.document(doc_blocks(doc), service.content_width(), fmt)

    def note_bitmap(body: NoteIn) -> Image.Image:
        overrides = {"size": body.size or 28, "align": body.align, "timestamp": body.timestamp}
        fmt = service.format({k: v for k, v in overrides.items() if v is not None})
        return render.note(body.text, service.content_width(), title=body.title, markdown=body.markdown,
                           bold=body.bold, fmt=fmt)

    def note_title(body: NoteIn) -> str:
        return body.title or (markdown.plain(body.text) if body.markdown else body.text)[:60] or "Note"

    def qr_bitmap(body: QrIn) -> Image.Image:
        return render.qr_label(body.data, service.content_width(), caption=body.caption, scale=body.scale)

    def banner_bitmap(body: BannerIn) -> Image.Image:
        return render.banner(body.text, cfg.width, family=body.font)

    def test_bitmap() -> Image.Image:
        s = service.settings()
        info = {"Energy": s["energy"], "Speed": hex(s["speed"]), "Feed": f"{s['feed']} ({s['feed_mode']})",
                "Encoding": s["encoding"], "Printer": service.printer.name or "-"}
        return render.test_page(service.content_width(), info)

    # ---- prints & previews -------------------------------------------------

    @app.post("/api/preview/doc", dependencies=[api])
    async def preview_doc(body: DocIn):
        return png(await asyncio.to_thread(doc_bitmap, body))

    @app.post("/api/print/doc", dependencies=[api])
    async def print_doc(body: DocIn):
        return enqueue("doc", doc_title(body), await asyncio.to_thread(doc_bitmap, body), body.copies)

    @app.post("/api/preview/note", dependencies=[api])
    async def preview_note(body: NoteIn):
        return png(await asyncio.to_thread(note_bitmap, body))

    @app.post("/api/print/note", dependencies=[api])
    async def print_note(body: NoteIn):
        return enqueue("note", note_title(body), await asyncio.to_thread(note_bitmap, body), body.copies)

    @app.post("/api/preview/qr", dependencies=[api])
    async def preview_qr(body: QrIn):
        return png(await asyncio.to_thread(qr_bitmap, body))

    @app.post("/api/print/qr", dependencies=[api])
    async def print_qr(body: QrIn):
        title = markdown.plain(body.caption)[:60] or body.data[:60]
        return enqueue("qr", title, await asyncio.to_thread(qr_bitmap, body), body.copies)

    @app.post("/api/preview/banner", dependencies=[api])
    async def preview_banner(body: BannerIn):
        return png(await asyncio.to_thread(banner_bitmap, body))

    @app.post("/api/print/banner", dependencies=[api])
    async def print_banner(body: BannerIn):
        return enqueue("banner", body.text[:60], await asyncio.to_thread(banner_bitmap, body), body.copies)

    image_form = dict(
        dither=Form("atkinson"), threshold=Form(150), brightness=Form(1.0),
        contrast=Form(1.0), rotate=Form(False), autolevel=Form(True),
    )

    @app.post("/api/preview/image", dependencies=[api])
    async def preview_image(file: UploadFile = File(...), dither: str = image_form["dither"],
                            threshold: int = image_form["threshold"], brightness: float = image_form["brightness"],
                            contrast: float = image_form["contrast"], rotate: bool = image_form["rotate"],
                            autolevel: bool = image_form["autolevel"]):
        return png(await image_bitmap(file, dither, threshold, brightness, contrast, rotate, autolevel))

    @app.post("/api/print/image", dependencies=[api])
    async def print_image(file: UploadFile = File(...), dither: str = image_form["dither"],
                          threshold: int = image_form["threshold"], brightness: float = image_form["brightness"],
                          contrast: float = image_form["contrast"], rotate: bool = image_form["rotate"],
                          autolevel: bool = image_form["autolevel"], copies: int = Form(1)):
        bitmap = await image_bitmap(file, dither, threshold, brightness, contrast, rotate, autolevel)
        return enqueue("image", file.filename or "Image", bitmap, max(1, min(20, copies)))

    @app.post("/api/print/test", dependencies=[api])
    async def print_test():
        return enqueue("test", "Test page", await asyncio.to_thread(test_bitmap))

    @app.get("/api/preview/test", dependencies=[api])
    async def preview_test():
        return png(await asyncio.to_thread(test_bitmap))

    @app.post("/api/print/feed", dependencies=[api])
    async def feed(body: FeedIn):
        return service.feed(body.amount)

    # ---- templates ---------------------------------------------------------

    TEMPLATE_MODELS = {"doc": DocIn, "note": NoteIn, "qr": QrIn}
    TEMPLATE_RENDER = {"doc": doc_bitmap, "note": note_bitmap, "qr": qr_bitmap}

    @app.get("/api/templates", dependencies=[api])
    async def list_templates():
        return [t for t in service.db.list_templates() if t["kind"] in TEMPLATE_MODELS]

    @app.post("/api/templates", dependencies=[api], status_code=201)
    async def create_template(body: TemplateIn):
        try:
            payload = TEMPLATE_MODELS[body.kind](**body.payload).model_dump(exclude={"copies"}, exclude_none=True)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid {body.kind} template: {exc}") from exc
        return service.db.create_template(body.name, body.kind, payload)

    @app.delete("/api/templates/{template_id}", dependencies=[api])
    async def delete_template(template_id: int):
        if not service.db.delete_template(template_id):
            raise HTTPException(404, "Template not found")
        return {"ok": True}

    @app.post("/api/templates/{template_id}/print", dependencies=[api])
    async def print_template(template_id: int, copies: int = 1):
        tpl = service.db.get_template(template_id)
        if not tpl or tpl["kind"] not in TEMPLATE_MODELS:
            raise HTTPException(404, "Template not found")
        body = TEMPLATE_MODELS[tpl["kind"]](**tpl["payload"])
        bitmap = await asyncio.to_thread(TEMPLATE_RENDER[tpl["kind"]], body)
        return enqueue(tpl["kind"], tpl["name"], bitmap, max(1, min(20, copies)))

    # ---- jobs --------------------------------------------------------------

    @app.get("/api/jobs", dependencies=[api])
    async def list_jobs(limit: int = 50):
        return service.db.list_jobs(max(1, min(200, limit)))

    @app.get("/api/jobs/{job_id}/preview.png", dependencies=[api])
    async def job_preview(job_id: int):
        path = service.job_image_path(job_id)
        if not path.is_file():
            raise HTTPException(404, "No preview")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/jobs/{job_id}/reprint", dependencies=[api])
    async def reprint(job_id: int):
        try:
            return service.reprint(job_id)
        except KeyError:
            raise HTTPException(404, "Job not found") from None

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[api])
    async def cancel(job_id: int):
        job = service.cancel(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    # ---- printer -----------------------------------------------------------

    @app.post("/api/printer/scan", dependencies=[api])
    async def scan(show_all: bool = False):
        try:
            return await service.printer.scan(8.0, show_all)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Bluetooth scan failed: {exc}") from exc

    @app.post("/api/printer/connect", dependencies=[api])
    async def connect(body: ConnectIn):
        if body.address:
            service.update_settings({"printer_address": body.address})
        try:
            async with service.printer.lock:
                await service.printer.connect(body.address)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Connect failed: {exc}") from exc
        return service.printer.info()

    @app.post("/api/printer/disconnect", dependencies=[api])
    async def disconnect():
        async with service.printer.lock:
            await service.printer.disconnect()
        return service.printer.info()

    @app.get("/api/printer/log", dependencies=[api])
    async def printer_log():
        return list(service.printer.log)

    @app.get("/api/settings", dependencies=[api])
    async def get_settings():
        return service.settings()

    @app.put("/api/settings", dependencies=[api])
    async def put_settings(values: dict):
        try:
            return service.update_settings(values)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"Invalid setting: {exc}") from exc

    # ---- share target fallback (when the service worker isn't active) -----

    @app.post("/share")
    async def share(request: Request, title: str = Form(""), text: str = Form(""), url: str = Form(""),
                    file: UploadFile | None = File(None)):
        if not authorized(request):
            return RedirectResponse("/?login=1", status_code=303)
        if file is not None and file.filename:
            bitmap = await image_bitmap(file, "atkinson", 150, 1.0, 1.0, False, True)
            enqueue("image", file.filename, bitmap)
        else:
            body = "\n".join(p for p in (text, url) if p).strip() or title
            if not body:
                return RedirectResponse("/", status_code=303)
            note = NoteIn(text=body[:8000], title=title[:200] if body != title else "")
            enqueue("note", title or body[:60], await asyncio.to_thread(note_bitmap, note))
        return RedirectResponse("/?shared=1", status_code=303)

    # ---- PWA shell ---------------------------------------------------------

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(WEB / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icons/icon-{size}.png")
    async def icon(size: int, maskable: bool = False):
        if size not in (48, 72, 96, 144, 180, 192, 256, 512):
            raise HTTPException(404)
        return Response(_icon(size, maskable), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


def _icon(size: int, maskable: bool) -> bytes:
    """Draw the app icon (a little receipt) so no binary assets are needed."""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (31, 41, 55, 255)
    if maskable:
        d.rectangle((0, 0, s, s), fill=bg)
        inset = s * 0.22
    else:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=bg)
        inset = s * 0.16
    left, right = inset + s * 0.08, s - inset - s * 0.08
    top, bottom = inset, s - inset
    teeth = 6
    tooth = (right - left) / teeth
    paper = [(left, top), (right, top), (right, bottom)]
    for i in range(teeth):
        x = right - i * tooth
        paper += [(x - tooth / 2, bottom - tooth * 0.5), (x - tooth, bottom)]
    paper += [(left, bottom)]
    d.polygon(paper, fill=(250, 250, 249, 255))
    ink = (31, 41, 55, 255)
    accent = (234, 88, 12, 255)
    line_h = (bottom - top) / 7
    box = line_h * 0.7
    for i in range(3):
        y = top + line_h * (1.2 + i * 1.5)
        d.rectangle((left + s * 0.05, y, left + s * 0.05 + box, y + box), outline=ink, width=max(2, s // 64))
        if i == 0:
            d.line((left + s * 0.06, y + box * 0.5, left + s * 0.05 + box * 0.45, y + box * 0.85,
                    left + s * 0.05 + box * 1.05, y - box * 0.1), fill=accent, width=max(3, s // 40))
        d.rectangle((left + s * 0.05 + box * 1.6, y + box * 0.3, right - s * 0.06 - i * s * 0.05,
                     y + box * 0.6), fill=ink)
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
