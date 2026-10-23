"""MCP server: lets AI agents print notes and to-dos on the user's printer.

Two ways to run it:
- built into the web server at ``/mcp`` (streamable HTTP, same bearer token as the API);
- ``taskprinter mcp`` over stdio, forwarding to a running server's HTTP API.

The tools only talk to the HTTP API, so both modes behave identically.
"""

from __future__ import annotations

from typing import Callable, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

ClientFactory = Callable[[], httpx.AsyncClient]

INSTRUCTIONS = """\
You can print on the user's small thermal printer (a 48 mm wide paper roll). Prints are
physical notes the user keeps on their desk, so use them for things worth having on paper:
tasks the user has to do, checklists, reminders, short summaries, QR codes for links.

Guidelines:
- Print only when the user asks, or when they have said you may print reminders for them.
- Keep it short and scannable: about 24-32 characters fit on a line at normal size.
- Use print_tasks for action items (each gets a tick box), print_task for one important to-do,
  print_note for free text (Markdown: # headings, **bold**, *italic*, - lists, - [ ] boxes).
- Write tasks as concrete actions ("Email Sam the Q3 numbers"), not vague topics.
- Check printer_status if a print fails; the printer may be off or out of paper.
"""

PRINT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _queued(job: dict) -> str:
    copies = job.get("copies", 1)
    extra = f" x{copies}" if copies > 1 else ""
    return f"Queued print job #{job['id']}{extra}: {job['title']!r}. It prints within a few seconds."


def build_mcp(client_factory: ClientFactory) -> MCPServer:
    mcp = MCPServer("taskprinter", title="Task Printer", instructions=INSTRUCTIONS)

    async def call(method: str, path: str, json: dict | None = None):
        async with client_factory() as client:
            resp = await client.request(method, path, json=json, timeout=60)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise RuntimeError(f"Printer server error {resp.status_code}: {detail}")
        return resp.json()

    @mcp.tool(annotations=PRINT)
    async def print_note(text: str, title: str = "", copies: int = 1) -> str:
        """Print a free-form note. `text` is Markdown: # headings, **bold**, *italic*, ~~strike~~,
        `code`, - bullets, 1. numbered items, - [ ] tick boxes, > quotes, --- dividers."""
        job = await call("POST", "/api/print/note",
                         {"text": text, "title": title, "copies": max(1, min(copies, 5))})
        return _queued(job)

    @mcp.tool(annotations=PRINT)
    async def print_tasks(title: str, tasks: list[str], note: str = "") -> str:
        """Print a to-do list where every task gets a tick box. Use this for action items the
        user has to do. Prefix a task with "[x] " to print it already ticked; indent with two
        spaces to nest it under the previous task. `note` is optional Markdown shown under the title."""
        lines = [f"# {title.strip() or 'To do'}"]
        if note.strip():
            lines += ["", note.strip()]
        lines.append("")
        for task in tasks:
            if not task.strip():
                continue
            indent = " " * (len(task) - len(task.lstrip()))
            body = task.strip()
            box = "[ ]"
            if body[:3].lower() == "[x]":
                box, body = "[x]", body[3:].strip()
            elif body[:3] == "[ ]":
                body = body[3:].strip()
            lines.append(f"{indent}- {box} {body}")
        job = await call("POST", "/api/print/note", {"text": "\n".join(lines), "title": ""})
        return _queued(job)

    @mcp.tool(annotations=PRINT)
    async def print_task(title: str, details: str = "", due: str = "",
                         priority: Literal["low", "normal", "high", "urgent"] = "normal") -> str:
        """Print one important task as a card the user can pin up: big title, optional
        priority, due date and details (Markdown), and a "Done" tick box."""
        lines = []
        if priority in ("high", "urgent"):
            lines.append(f"**!! {priority.upper()} !!**")
            lines.append("")
        lines.append(f"# {title.strip()}")
        if due.strip():
            lines += ["", f"**Due:** {due.strip()}"]
        if details.strip():
            lines += ["", details.strip()]
        lines += ["", "---", "- [ ] **Done**"]
        job = await call("POST", "/api/print/note", {"text": "\n".join(lines), "title": ""})
        return _queued(job)

    @mcp.tool(annotations=PRINT)
    async def print_qr(data: str, caption: str = "") -> str:
        """Print a QR code (for a URL, Wi-Fi string or any text) with an optional caption."""
        job = await call("POST", "/api/print/qr", {"data": data, "caption": caption})
        return _queued(job)

    @mcp.tool(annotations=PRINT)
    async def print_banner(text: str) -> str:
        """Print short text (max 120 chars) as a huge banner running along the paper roll."""
        job = await call("POST", "/api/print/banner", {"text": text[:120]})
        return _queued(job)

    @mcp.tool(annotations=READ)
    async def printer_status() -> str:
        """Report whether the printer is connected, its battery level and the print queue."""
        status = await call("GET", "/api/status")
        p, q = status["printer"], status["queue"]
        parts = [f"state: {p['state']}", f"printer: {p.get('name') or 'not found yet'}"]
        info = p.get("status") or {}
        if "battery" in info:
            parts.append(f"battery: {info['battery']}%")
        if info.get("paper_out"):
            parts.append("OUT OF PAPER")
        if p.get("error"):
            parts.append(f"last error: {p['error']}")
        parts.append(f"jobs waiting: {q['pending']}")
        return ", ".join(parts)

    @mcp.tool(annotations=READ)
    async def recent_prints(limit: int = 10) -> str:
        """List the most recent print jobs and whether they printed."""
        jobs = await call("GET", f"/api/jobs?limit={max(1, min(limit, 50))}")
        if not jobs:
            return "Nothing printed yet."
        return "\n".join(f"#{j['id']} {j['status']:<9} {j['kind']:<7} {j['title']}"
                         + (f"  (error: {j['error']})" if j.get("error") else "") for j in jobs)

    return mcp


def remote_client_factory(server: str, token: str) -> ClientFactory:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return lambda: httpx.AsyncClient(base_url=server.rstrip("/"), headers=headers)


def run_stdio(server: str, token: str) -> None:
    build_mcp(remote_client_factory(server, token)).run("stdio")
