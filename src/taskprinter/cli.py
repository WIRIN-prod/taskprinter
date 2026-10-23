"""Command line: run the server, or print from the server's shell.

Print commands talk to the running server (which owns the Bluetooth link) over HTTP.
Use --direct to drive the printer yourself when the server is not running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .config import Config


def _server_url(args) -> str:
    if args.server:
        return args.server.rstrip("/")
    cfg = Config.from_env()
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    return f"http://{host}:{cfg.port}"


def _request(args, method: str, path: str, body: dict | None = None,
             files: dict[str, Path] | None = None, fields: dict | None = None):
    url = _server_url(args) + path
    headers = {}
    token = args.token or os.environ.get("TP_TOKEN") or Config.from_env().token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if files:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in (fields or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        for key, path in files.items():
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{path.name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n".encode() + path.read_bytes() + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit(f"Server error {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach the server at {url} ({exc.reason}). Is `taskprinter serve` running? "
                 "Use --direct to print without it.")


async def _direct_print(bitmap) -> None:
    from PIL import Image

    from . import protocol
    from .imaging import bitmap_rows
    from .printer import make_printer

    cfg = Config.from_env()
    printer = make_printer(cfg)
    settings = protocol.PrintSettings(width=cfg.width, energy=cfg.energy, quality=cfg.quality, speed=cfg.speed,
                                      draw_mode=cfg.draw_mode, feed=cfg.feed, feed_mode=cfg.feed_mode,
                                      encoding=cfg.encoding)
    if cfg.rotate180:
        bitmap = bitmap.transpose(Image.Transpose.ROTATE_180)
    data = protocol.build_job(bitmap_rows(bitmap, mirror=cfg.mirror), settings)
    try:
        await printer.send(data, lambda f: print(f"\r{f * 100:5.1f}%", end="", flush=True))
        print("\nDone.")
    finally:
        await printer.close()


def _print_job(job) -> None:
    print(f"Queued job #{job['id']}: {job['title']} ({job['rows']} rows)")


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    cfg = Config.from_env()
    if args.mock:
        cfg.backend = "mock"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(cfg), host=args.host or cfg.host, port=args.port or cfg.port,
                proxy_headers=True, forwarded_allow_ips="*", log_level="info")


def cmd_scan(args) -> None:
    from .printer import BlePrinter

    async def run():
        printer = BlePrinter(Config.from_env())
        for d in await printer.scan(args.timeout, args.all):
            print(f"{d['name']:<24} {d['address']:<20} RSSI {d['rssi']}")

    asyncio.run(run())


def cmd_gatt(args) -> None:
    from .printer import BlePrinter

    async def run():
        cfg = Config.from_env()
        if args.address:
            cfg.printer_address = args.address
        printer = BlePrinter(cfg)
        try:
            print("\n".join(await printer.gatt_dump()))
            await asyncio.sleep(2)  # show any notifications that arrive
            for entry in printer.log:
                print(entry["msg"])
        finally:
            await printer.close()

    asyncio.run(run())


def cmd_text(args) -> None:
    from . import render

    text = " ".join(args.text) if args.text else sys.stdin.read()
    if args.direct:
        cfg = Config.from_env()
        asyncio.run(_direct_print(render.note(text, cfg.width, title=args.title, markdown=not args.plain,
                                              fmt=render.Format(size=args.size))))
        return
    _print_job(_request(args, "POST", "/api/print/note",
                        {"text": text, "title": args.title, "size": args.size, "markdown": not args.plain,
                         "copies": args.copies}))


def cmd_image(args) -> None:
    path = Path(args.path)
    if args.direct:
        from PIL import Image

        from .imaging import to_bitmap

        cfg = Config.from_env()
        with Image.open(path) as img:
            asyncio.run(_direct_print(to_bitmap(img, cfg.width, dither=args.dither, autolevel=True)))
        return
    _print_job(_request(args, "POST", "/api/print/image", files={"file": path},
                        fields={"dither": args.dither}))


def cmd_test(args) -> None:
    if args.direct:
        from . import render

        asyncio.run(_direct_print(render.test_page(Config.from_env().width)))
        return
    _print_job(_request(args, "POST", "/api/print/test"))


def cmd_qr(args) -> None:
    _print_job(_request(args, "POST", "/api/print/qr",
                        {"data": args.data, "caption": args.caption, "copies": args.copies}))


def cmd_banner(args) -> None:
    _print_job(_request(args, "POST", "/api/print/banner", {"text": " ".join(args.text), "copies": args.copies}))


def cmd_feed(args) -> None:
    _print_job(_request(args, "POST", "/api/print/feed", {"amount": args.amount}))


def cmd_mcp(args) -> None:
    """MCP over stdio for local agents; forwards to the server's HTTP API."""
    from .mcp_server import run_stdio

    token = args.token or os.environ.get("TP_TOKEN") or Config.from_env().token
    run_stdio(_server_url(args), token)


def cmd_status(args) -> None:
    print(json.dumps(_request(args, "GET", "/api/status"), indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="taskprinter", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", help="server URL (default: from TP_HOST/TP_PORT)")
    parser.add_argument("--token", help="API token (default: TP_TOKEN)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the web server")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--mock", action="store_true", help="use the mock printer (saves PNGs)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("scan", help="scan for BLE printers")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--all", action="store_true", help="show every BLE device")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("gatt", help="connect and dump GATT services (diagnostics)")
    p.add_argument("address", nargs="?")
    p.set_defaults(func=cmd_gatt)

    def direct(p):
        p.add_argument("--direct", action="store_true", help="talk to the printer directly, not via the server")

    p = sub.add_parser("text", help="print a Markdown note (reads stdin if no text given)")
    p.add_argument("text", nargs="*")
    p.add_argument("--title", default="")
    p.add_argument("--size", type=int, default=28)
    p.add_argument("--plain", action="store_true", help="print the text literally, no Markdown")
    p.add_argument("--copies", type=int, default=1)
    direct(p)
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("image", help="print an image file")
    p.add_argument("path")
    p.add_argument("--dither", default="atkinson", choices=["atkinson", "floyd", "ordered", "threshold"])
    direct(p)
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("test", help="print a test page")
    direct(p)
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("qr", help="print a QR code label")
    p.add_argument("data")
    p.add_argument("--caption", default="")
    p.add_argument("--copies", type=int, default=1)
    p.set_defaults(func=cmd_qr)

    p = sub.add_parser("banner", help="print big sideways banner text")
    p.add_argument("text", nargs="+")
    p.add_argument("--copies", type=int, default=1)
    p.set_defaults(func=cmd_banner)

    sub.add_parser("status", help="show printer and queue status").set_defaults(func=cmd_status)
    sub.add_parser("mcp", help="run the MCP server over stdio (for AI agents)").set_defaults(func=cmd_mcp)

    p = sub.add_parser("feed", help="feed paper")
    p.add_argument("amount", type=int, nargs="?", default=20)
    p.set_defaults(func=cmd_feed)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
