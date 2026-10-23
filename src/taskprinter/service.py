"""The print service: settings, paper layout, event bus and the job queue worker."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from . import protocol, render
from .config import Config
from .db import Database
from .imaging import bitmap_rows
from .printer import BasePrinter, make_printer

log = logging.getLogger("taskprinter")

# Settings the web UI may change at runtime, with their validators.
TUNABLE = {
    "energy": lambda v: max(0, min(0xFFFF, int(v))),
    "quality": lambda v: max(0, min(0xFF, int(v))),
    "speed": lambda v: max(0, min(0xFF, int(v))),
    "draw_mode": lambda v: max(0, min(0xFF, int(v))),
    "feed": lambda v: max(0, min(200, int(v))),
    "feed_mode": lambda v: v if v in protocol.FEED_MODES else "a1",
    "encoding": lambda v: v if v in protocol.ENCODINGS else "raw-lsb",
    "mirror": bool,
    "rewind": bool,
    "rotate180": bool,
    "backlash": lambda v: max(0, min(64, int(v))),
    "printer_address": lambda v: str(v or "").strip(),
    # paper & layout
    "paper": lambda v: v if v in PAPERS else "continuous",
    "label_width": lambda v: _clamp(v, 10, 48),
    "label_height": lambda v: _clamp(v, 8, 200),
    "label_gap": lambda v: _clamp(v, 0, 10),
    "label_offset": lambda v: _clamp(v, -10, 10),
    "gap": lambda v: _clamp(v, 0, 30),
    "separator": lambda v: v if v in SEPARATORS else "none",
    "format": lambda v: render.Format.from_dict(v if isinstance(v, dict) else {}).as_dict(),
}

PAPERS = ("continuous", "label")
SEPARATORS = ("none", "dashed", "solid")
DOTS_PER_MM = 8
LAYOUT_DEFAULTS = {
    "paper": "continuous", "label_width": 40.0, "label_height": 30.0, "label_gap": 2.0,
    "label_offset": 0.0, "gap": 2.0, "separator": "none",
    "format": render.Format().as_dict(),
    "backlash": 24,  # dots (3 mm); measured on SC03h-14BF: ~2 mm of slack after a pull-back
}


def _clamp(value, low: float, high: float) -> float:
    return round(max(low, min(high, float(value))), 1)


KEEP_JOBS = 200
MAX_RETRACT = 200  # never pull back more than this many dots


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, data: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait({"type": kind, "data": data})
            except asyncio.QueueFull:
                pass  # slow client; it will resync on reconnect


class PrintService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = cfg.data_dir / "jobs"
        self.jobs_dir.mkdir(exist_ok=True)
        self.db = Database(cfg.data_dir / "taskprinter.db")
        self.events = EventBus()
        render.load_font_overrides(cfg.font, cfg.font_bold)
        overrides = self.db.get_settings()
        if overrides.get("printer_address") and not cfg.printer_address:
            cfg.printer_address = overrides["printer_address"]
        self.printer: BasePrinter = make_printer(cfg, self._printer_event)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._raw: dict[int, tuple[bytes, int]] = {}  # paper feed jobs: (data, dots)
        self.current: dict | None = None

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._worker(), name="print-worker"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.printer.close()
        self.db.close()

    def _printer_event(self, kind: str, data: dict) -> None:
        self.events.publish(kind, data)

    # ---- settings ----------------------------------------------------------

    def settings(self) -> dict:
        cfg = self.cfg
        base = {
            "energy": cfg.energy, "quality": cfg.quality, "speed": cfg.speed,
            "draw_mode": cfg.draw_mode, "feed": cfg.feed, "feed_mode": cfg.feed_mode,
            "encoding": cfg.encoding, "mirror": cfg.mirror,
            "printer_address": cfg.printer_address, "rewind": cfg.rewind, "rotate180": cfg.rotate180,
        }
        stored = {k: v for k, v in self.db.get_settings().items() if k in TUNABLE}
        merged = {**base, **LAYOUT_DEFAULTS, **stored}
        merged["format"] = render.Format.from_dict({**LAYOUT_DEFAULTS["format"], **merged["format"]}).as_dict()
        return merged

    def format(self, overrides: dict | None = None) -> render.Format:
        """The saved default format, with per-print overrides on top."""
        return render.Format.from_dict({**self.settings()["format"], **(overrides or {})})

    def update_settings(self, values: dict) -> dict:
        clean = {}
        for key, value in values.items():
            if key in TUNABLE:
                clean[key] = TUNABLE[key](value)
        self.db.set_settings(clean)
        if "printer_address" in clean:
            self.cfg.printer_address = clean["printer_address"]
            self.printer.address = clean["printer_address"] or self.printer.address
        return self.settings()

    def print_settings(self) -> protocol.PrintSettings:
        s = self.settings()
        return protocol.PrintSettings(
            width=self.cfg.width, energy=s["energy"], quality=s["quality"], speed=s["speed"],
            draw_mode=s["draw_mode"], feed=s["feed"], feed_mode=s["feed_mode"], encoding=s["encoding"],
        )

    # ---- jobs --------------------------------------------------------------

    def job_image_path(self, job_id: int) -> Path:
        return self.jobs_dir / f"{job_id}.png"

    # ---- paper layout -----------------------------------------------------

    def content_width(self) -> int:
        """Width templates should be drawn at: the full head, or the label width."""
        s = self.settings()
        if s["paper"] == "label":
            return min(self.cfg.width, int(s["label_width"] * DOTS_PER_MM) // 8 * 8)
        return self.cfg.width

    def finalize(self, bitmap: Image.Image) -> Image.Image:
        """Fit a rendered bitmap to the paper: label pitch, or roll spacing/separator."""
        s = self.settings()
        width = self.cfg.width
        bitmap = _crop_vertical(bitmap)
        if s["paper"] == "label":
            label_w = self.content_width()
            label_h = int(s["label_height"] * DOTS_PER_MM)
            pitch = label_h + int(s["label_gap"] * DOTS_PER_MM)
            if bitmap.width > label_w or bitmap.height > label_h:
                scale = min(label_w / bitmap.width, label_h / bitmap.height)
                size = (max(1, int(bitmap.width * scale)), max(1, int(bitmap.height * scale)))
                bitmap = bitmap.convert("L").resize(size, Image.Resampling.LANCZOS).point(
                    lambda v: 255 if v >= 128 else 0).convert("1")
            canvas = Image.new("1", (width, pitch), 1)
            top = (label_h - bitmap.height) // 2 + int(s["label_offset"] * DOTS_PER_MM)
            top = max(0, min(pitch - bitmap.height, top))
            canvas.paste(bitmap, ((width - bitmap.width) // 2, top))
            return canvas
        gap = int(s["gap"] * DOTS_PER_MM)
        if s["separator"] != "none":
            gap = max(gap, 24)
        canvas = Image.new("1", (width, bitmap.height + gap), 1)
        canvas.paste(bitmap, ((width - bitmap.width) // 2, 0))
        if s["separator"] != "none":
            d = ImageDraw.Draw(canvas)
            y = bitmap.height + gap // 2 - 1
            if s["separator"] == "solid":
                d.rectangle((0, y, width - 1, y + 1), fill=0)
            else:
                for x in range(0, width, 16):
                    d.rectangle((x, y, x + 7, y + 1), fill=0)
        return canvas

    def enqueue(self, kind: str, title: str, bitmap: Image.Image, *,
                copies: int = 1, finalize: bool = True) -> dict:
        if finalize:
            bitmap = self.finalize(bitmap)
        copies = max(1, min(20, int(copies)))
        if bitmap.height * copies > self.cfg.max_rows:
            raise ValueError(
                f"Print is {bitmap.height * copies} rows long; the limit is {self.cfg.max_rows} (TP_MAX_ROWS)."
            )
        first = None
        for n in range(copies):
            label = title if copies == 1 else f"{title} ({n + 1}/{copies})"
            job = self.db.create_job(kind, label, bitmap.height)
            bitmap.save(self.job_image_path(job["id"]))
            self._queue.put_nowait(job["id"])
            self.events.publish("job", job)
            first = first or job
        return {**first, "copies": copies}

    def reprint(self, job_id: int) -> dict:
        old = self.db.get_job(job_id)
        path = self.job_image_path(job_id)
        if not old or not path.is_file():
            raise KeyError(job_id)
        with Image.open(path) as img:  # stored bitmaps are already fitted to the paper
            return self.enqueue(old["kind"], old["title"], img.copy(), finalize=False)

    def cancel(self, job_id: int) -> dict | None:
        job = self.db.get_job(job_id)
        if job and job["status"] == "queued":
            job = self.db.update_job(job_id, status="cancelled", finished_at=time.time())
            self.events.publish("job", job)
        return job

    # Dots of paper fed past the head since the last print; with "rewind" on, the
    # next print pulls them back so prints follow on without a gap.
    def _fed(self) -> int:
        return int(self.db.get_settings().get("_fed", 0))

    def _set_fed(self, dots: int) -> None:
        self.db.set_settings({"_fed": max(0, min(MAX_RETRACT, dots))})

    def feed(self, amount: int) -> dict:
        """Feed paper without queueing a picture."""
        s = self.print_settings()
        amount = max(1, min(200, amount))
        data = protocol.feed_packets(amount, s.feed_mode, s.speed)
        job = self.db.create_job("feed", f"Feed {amount}", 0)
        self._queue.put_nowait(job["id"])
        self._raw[job["id"]] = (data, amount if s.feed_mode == "a1" else 0)
        self.events.publish("job", job)
        return job

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self.db.get_job(job_id)
            if not job or job["status"] != "queued":
                continue
            try:
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report every failure to the UI
                log.exception("Job %s failed", job_id)
                job = self.db.update_job(job_id, status="failed", error=str(exc), finished_at=time.time())
                self.events.publish("job", job)
            finally:
                self.current = None
                self._prune()

    async def _run(self, job: dict) -> None:
        job_id = job["id"]
        job = self.db.update_job(job_id, status="printing", started_at=time.time(), error=None)
        self.current = {**job, "progress": 0.0}
        self.events.publish("job", self.current)

        raw = self._raw.pop(job_id, None)
        if raw is not None:
            data, fed_after = raw
            fed_after += self._fed()
        else:
            settings = self.print_settings()
            if self.settings()["rewind"]:
                settings.retract = self._fed()
                settings.backlash = int(self.settings()["backlash"])
            with Image.open(self.job_image_path(job_id)) as img:
                if self.settings()["rotate180"]:  # so the print reads upright as it leaves the printer
                    img = img.transpose(Image.Transpose.ROTATE_180)
                rows = bitmap_rows(img, mirror=bool(self.settings()["mirror"]))
            data = protocol.build_job(rows, settings)
            fed_after = settings.feed if settings.feed_mode == "a1" else 0

        last = [0.0]

        def progress(fraction: float) -> None:
            if fraction - last[0] >= 0.05 or fraction >= 1.0:
                last[0] = fraction
                self.current = {**job, "progress": round(fraction, 3)}
                self.events.publish("job", self.current)

        try:
            await self.printer.send(data, progress)
        except Exception:
            self._set_fed(0)  # paper position unknown; don't risk over-retracting
            raise
        self._set_fed(fed_after)
        job = self.db.update_job(job_id, status="done", finished_at=time.time())
        self.events.publish("job", job)

    def _prune(self) -> None:
        old = self.db.old_job_ids(KEEP_JOBS)
        for job_id in old:
            self.job_image_path(job_id).unlink(missing_ok=True)
        if old:
            self.db.delete_jobs(old)

    def queue_info(self) -> dict:
        return {"pending": self._queue.qsize(), "current": self.current}


def _crop_vertical(bitmap: Image.Image) -> Image.Image:
    """Drop blank rows above and below the content (spacing is added by finalize)."""
    bbox = ImageOps.invert(bitmap.convert("L")).getbbox()
    if not bbox:
        return bitmap.crop((0, 0, bitmap.width, 1))
    return bitmap.crop((0, bbox[1], bitmap.width, bbox[3]))
