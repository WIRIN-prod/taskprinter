# Task Printer

A self-hosted print server for **SC03h / iPrint-family Bluetooth mini thermal printers**
(57 mm paper, 384 dots). Run it on a machine near the printer. It holds the Bluetooth link and
a print queue, and gives you three ways to print:

- **A web app (PWA)** with a formatting editor. It installs on your phone or desktop, and you can share text and pictures to it.
- **An MCP server**, so AI agents such as Claude can print notes and to-do lists for you.
- **A CLI and an HTTP API**, for scripts, cron jobs and automations.

## Features

- **Formatting editor.** Headings, **bold**, *italic*, underline, ~~strike~~, inline code, code blocks, quotes, bullet and numbered lists, tick-box checklists, dividers, and left, centre or right alignment.
- **Format panel.** Font (sans, serif or mono), size, line and paragraph spacing, side margins, borders (line, double, dashed, rounded), white-on-black, and a date/time footer. You can save any combination as your default.
- **Live preview.** Shows exactly what will print, updated as you type.
- **Images.** Atkinson, Floyd–Steinberg, ordered or threshold dithering, with brightness and contrast controls.
- **QR labels and sideways banners.**
- **Templates, copies (1–20), and a history** with reprint.
- **Paper modes.** A continuous roll with set spacing and an optional separator, or die-cut labels.
- **Back-to-back printing.** After each print the paper feeds out past the tear bar so you can read it, and the next print pulls it back first. The pull-back is compensated for gear slack.
- **Upright output.** Prints are rotated 180° so text reads the right way up as it leaves the printer.

## Quick start

```sh
uv sync
cp .env.example .env               # optional, then edit
uv run taskprinter serve --mock    # no hardware: prints land in data/mock_prints/
uv run taskprinter serve           # real printer over Bluetooth LE
```

Open <http://localhost:8080>. Turn the printer on and make sure it isn't connected to the phone
app, because it accepts only one connection at a time. The server finds it by name (`SC03h…`)
the first time you print. To pick one yourself, use **Printer → Scan**.

## Deploy with Docker (Linux server)

The host needs a Bluetooth adapter within range of the printer, plus BlueZ:

```sh
sudo apt install -y bluez && sudo systemctl enable --now bluetooth
# The SC03h also speaks classic Bluetooth. Without this, BlueZ connects over BR/EDR and fails.
sudo sed -i 's/^#\?\s*ControllerMode\s*=.*/ControllerMode = le/' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
git clone https://github.com/WIRIN-prod/taskprinter.git && cd taskprinter
cp .env.example .env && nano .env      # set TP_TOKEN
docker compose up -d --build
docker compose logs -f
```

The container uses host networking and the host's D-Bus socket to reach BlueZ. Settings,
history and templates live in `./data`.

Installing the PWA on a phone needs HTTPS. Put Caddy (`deploy/Caddyfile.example`) or
`tailscale serve` in front of the server, then set `TP_COOKIE_SECURE=1`. For running without
Docker there is a systemd unit in `deploy/taskprinter.service`.

## MCP: let agents print for you

The server exposes an MCP endpoint at **`/mcp`** (streamable HTTP). It uses the same token as
the API. Tools:

| Tool | What it prints |
|---|---|
| `print_tasks(title, tasks, note)` | A to-do list with a tick box per task (`"[x] …"` prints ticked, indent to nest) |
| `print_task(title, details, due, priority)` | One important task as a card with a **Done** box |
| `print_note(text, title, copies)` | Free text in Markdown |
| `print_qr(data, caption)` | A QR code |
| `print_banner(text)` | Huge sideways text |
| `printer_status()` / `recent_prints(limit)` | Printer state, battery, queue and history |

**Claude Code**, connecting to the server over HTTP:

```sh
claude mcp add --transport http taskprinter http://192.168.1.6:8080/mcp --header "Authorization: Bearer YOUR_TP_TOKEN"
```

**Claude Desktop and other stdio clients** run a local proxy that forwards to the server:

```json
{
  "mcpServers": {
    "taskprinter": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/WIRIN-prod/taskprinter", "taskprinter",
               "--server", "http://192.168.1.6:8080", "--token", "YOUR_TP_TOKEN", "mcp"]
    }
  }
}
```

Then ask something like *"print me a checklist of what I need to do before Friday's demo"*.

## CLI and HTTP API

The print commands go through the running server, which owns the Bluetooth link:

```sh
uv run taskprinter text "# Hello\nSome **bold** text"
echo "piped text works too" | uv run taskprinter text --title "Note"
uv run taskprinter image ~/photo.jpg --dither atkinson
uv run taskprinter qr https://example.com --caption "Scan me"
uv run taskprinter banner HAPPY BIRTHDAY
uv run taskprinter test
uv run taskprinter feed 30
uv run taskprinter status
```

Add `--server http://host:8080 --token …` to use another machine. Add `--direct` to `text`,
`image` or `test` to print without the server. The server must be stopped for that.

```sh
curl -X POST http://192.168.1.6:8080/api/print/note -H "Authorization: Bearer $TP_TOKEN" \
     -H "Content-Type: application/json" -d '{"text":"**Backup finished** at 03:00"}'
```

The interactive API docs are at `/docs`. Typed text in the API, CLI and share sheet is
Markdown. Pass `"markdown": false` or use `--plain` to print it literally.

## Printer tuning

Change these under **Printer → Print tuning**. The defaults are what the SC03h-14BF needs:

| Setting | Default | Notes |
|---|---|---|
| Darkness (energy, `0xAF`) | 17520 | Raise it for darker prints. |
| Row encoding | raw A2, **LSB-first** | The protocol doc says MSB-first, which prints every 8-pixel block mirrored on real hardware. |
| Feed after print | 90 dots via `0xA1` | About 11 mm, enough to clear the tear bar. The doc's `0xBD 23` feed does not move the paper. |
| Pull paper back | on, `0xA0` | Retracts the previous feed so prints join with no gap. |
| Pull-back slack | 24 dots | Absorbs gear slack after reversing. Raise it if the first line of a print looks squashed. |
| Rotate 180° | on | Makes text read upright as it comes out of the printer. |

## Protocol notes

The driver follows the community
[iPrint SC03h protocol documentation](https://github.com/shubhambelbase/mini-print-studio/blob/main/iPrint_SC03h_Protocol_Documentation.md),
with the hardware-verified differences listed above:

- Service `AE30`, write `AE01` (fallback `AE3B`), notify `AE02`. The server subscribes to every notify characteristic, which the printer treats as a "host alive" signal.
- Frames are `51 78 <cmd> 00 <len LE16> <payload> <crc8> FF`, with CRC-8/ATM (poly 0x07) over the payload.
- A job runs: wake `A3 00`, optional retract `A0`, quality `A4 33`, energy `AF`, mode `BE 00`, speed `BD 23`, one `A2` row of 48 bytes per pixel line, then feed `A1`.
- Writes go in chunks of up to 180 bytes, 10 ms apart. Jobs over 20 KB slow to 25 ms per chunk and pause 600 ms after every 4 KB. The server holds the link for 3 s after the last byte.
- The server never sends `0xA6` (lattice).

## Layout

```
src/taskprinter/
  protocol.py    packet framing, CRC, row encoding, job builder
  imaging.py     image -> 1-bit (EXIF, alpha, dithering)
  markdown.py    Markdown -> blocks (CLI, API, share sheet, QR captions)
  render.py      fonts, rich-text layout, formatting, QR labels, banners, test page
  printer.py     BLE connection manager (+ mock backend)
  service.py     queue worker, paper layout, settings
  db.py          SQLite: jobs, templates, settings (in TP_DATA_DIR)
  server.py      FastAPI API, SSE events, share target, PWA host, /mcp
  mcp_server.py  MCP tools for AI agents (HTTP + stdio)
  cli.py         the `taskprinter` command
  web/           PWA (vanilla JS, no build step)
legacy/          the original Tkinter diagnostic app
deploy/          systemd unit, Caddy example
```

Run the tests with `uv run pytest`.
