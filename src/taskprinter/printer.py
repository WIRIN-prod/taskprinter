"""Printer backends: a real BLE connection manager and a mock that saves PNGs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from PIL import Image

from . import protocol
from .config import Config

log = logging.getLogger("taskprinter.printer")

# On Linux, make BlueZ discover the printer as an LE device. Dual-mode printers seen by an
# unfiltered scan get cached as classic Bluetooth, and connecting then fails with
# "BREDR.ProfileUnavailable". Other platforms ignore this argument.
LE_ONLY = {"bluez": {"filters": {"Transport": "le"}}}

EventSink = Callable[[str, dict], Awaitable[None] | None]
Progress = Callable[[float], None]


def _matches(name: str, services: list[str], prefix: str) -> bool:
    lowered = name.lower()
    if prefix and lowered.startswith(prefix.lower()):
        return True
    if "sc03h" in lowered or "iprint" in lowered:
        return True
    return any("ae30" in s.lower() or "af30" in s.lower() for s in services)


class BasePrinter:
    """Shared state and event plumbing."""

    def __init__(self, cfg: Config, emit: EventSink | None = None):
        self.cfg = cfg
        self._emit = emit
        self.state = "disconnected"
        self.address: str | None = cfg.printer_address or None
        self.name: str | None = None
        self.last_error: str | None = None
        self.status: dict = {}
        self.log: deque[dict] = deque(maxlen=200)
        self.lock = asyncio.Lock()

    async def _event(self, kind: str, data: dict) -> None:
        if self._emit:
            result = self._emit(kind, data)
            if asyncio.iscoroutine(result):
                await result

    async def _set_state(self, state: str, error: str | None = None) -> None:
        self.state = state
        if error is not None:
            self.last_error = error
        await self._event("printer", self.info())

    def _log(self, message: str) -> None:
        log.info(message)
        self.log.append({"t": time.time(), "msg": message})

    def info(self) -> dict:
        return {
            "backend": self.cfg.backend,
            "state": self.state,
            "address": self.address,
            "name": self.name,
            "error": self.last_error,
            "status": self.status,
        }

    async def scan(self, timeout: float = 8.0, show_all: bool = False) -> list[dict]:
        return []

    async def connect(self, address: str | None = None) -> None: ...

    async def disconnect(self) -> None: ...

    async def send(self, data: bytes, progress: Progress | None = None) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        await self.disconnect()


class MockPrinter(BasePrinter):
    """Writes each job as a PNG into <data_dir>/mock_prints - handy for UI work."""

    def __init__(self, cfg: Config, emit: EventSink | None = None):
        super().__init__(cfg, emit)
        self.out = cfg.data_dir / "mock_prints"
        self.out.mkdir(parents=True, exist_ok=True)
        self.name = "Mock printer"
        self.address = "00:00:00:00:00:00"
        self.status = {"ready": True, "battery": 100}

    async def scan(self, timeout: float = 8.0, show_all: bool = False) -> list[dict]:
        return [{"name": self.name, "address": self.address, "rssi": -40, "services": []}]

    async def connect(self, address: str | None = None) -> None:
        await self._set_state("connected", None)

    async def disconnect(self) -> None:
        await self._set_state("disconnected")

    async def send(self, data: bytes, progress: Progress | None = None) -> None:
        async with self.lock:
            await self._set_state("printing")
            rows = []
            width = self.cfg.width // 8
            for cmd, payload in protocol.iter_packets(data):
                if cmd == protocol.Cmd.DRAW_RAW and len(payload) == width:
                    rows.append(payload)
            total = max(1, len(rows))
            for i in range(0, total, 40):
                if progress:
                    progress(min(1.0, i / total))
                await asyncio.sleep(0.05)
            if rows:
                raw = bytes(b ^ 0xFF for row in rows for b in row)
                img = Image.frombytes("1", (self.cfg.width, len(rows)), raw)
                path = self.out / f"print_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.png"
                img.save(path)
                self._log(f"Mock print saved: {path.name} ({len(rows)} rows)")
            if progress:
                progress(1.0)
            await self._set_state("connected")


class BlePrinter(BasePrinter):
    """Owns the BLE link; connects on demand and drops it after an idle period."""

    def __init__(self, cfg: Config, emit: EventSink | None = None):
        super().__init__(cfg, emit)
        self.client = None
        self.write_char = None
        self._resume = asyncio.Event()
        self._resume.set()
        self._idle_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cache = cfg.data_dir / "printer.json"
        if not self.address and self._cache.is_file():
            try:
                cached = json.loads(self._cache.read_text())
                self.address = cached.get("address")
                self.name = cached.get("name")
            except (OSError, ValueError):
                pass

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def scan(self, timeout: float = 8.0, show_all: bool = False) -> list[dict]:
        from bleak import BleakScanner

        await self._set_state("scanning" if not self.connected else self.state)
        try:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True, **LE_ONLY)
        finally:
            if self.state == "scanning":
                await self._set_state("connected" if self.connected else "disconnected")
        results = []
        for device, adv in found.values():
            name = device.name or adv.local_name or "(unnamed)"
            services = list(adv.service_uuids or [])
            if show_all or _matches(name, services, self.cfg.printer_name):
                results.append({"name": name, "address": device.address, "rssi": adv.rssi, "services": services})
        results.sort(key=lambda d: d["rssi"] or -999, reverse=True)
        self._log(f"Scan found {len(results)} device(s)")
        return results

    def _remember(self) -> None:
        try:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            self._cache.write_text(json.dumps({"address": self.address, "name": self.name}))
        except OSError:
            pass

    async def _find_device(self):
        from bleak import BleakScanner

        timeout = self.cfg.connect_timeout
        if self.address:
            device = await BleakScanner.find_device_by_address(self.address, timeout=timeout, **LE_ONLY)
            if device:
                return device
            self._log(f"{self.address} not seen, scanning by name")

        def match(device, adv) -> bool:
            name = device.name or adv.local_name or ""
            return _matches(name, list(adv.service_uuids or []), self.cfg.printer_name)

        return await BleakScanner.find_device_by_filter(match, timeout=timeout, **LE_ONLY)

    def _spawn(self, coro) -> None:
        """Schedule a coroutine from a bleak callback (may run off the loop thread)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(coro))

    def _on_disconnect(self, _client) -> None:
        self._log("Printer disconnected")
        self.client = None
        self.write_char = None
        self._resume.set()
        idle, self._idle_task = self._idle_task, None
        if idle and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(idle.cancel)
        if self.state != "disconnected":
            self._spawn(self._set_state("disconnected"))

    def _on_notify(self, sender, data: bytearray) -> None:
        raw = bytes(data)
        self._log(f"NOTIFY {sender}: {raw.hex(' ').upper()}")
        for cmd, payload in protocol.iter_packets(raw):
            pause = protocol.is_flow_pause(cmd, payload)
            if pause is True:
                self._resume.clear()
            elif pause is False:
                self._resume.set()
            elif cmd == protocol.Cmd.DEVICE_STATE:
                self.status = {**protocol.decode_status(payload), "raw": payload.hex()}
                self._spawn(self._event("printer", self.info()))

    async def connect(self, address: str | None = None) -> None:
        from bleak import BleakClient

        if address and address != self.address:
            await self.disconnect()
            self.address = address
        if self.connected:
            return

        self._loop = asyncio.get_running_loop()
        await self._set_state("connecting")
        try:
            device = await self._find_device()
            if device is None:
                raise RuntimeError("Printer not found. Is it on and disconnected from the phone app?")
            client = BleakClient(device, disconnected_callback=self._on_disconnect,
                                 timeout=self.cfg.connect_timeout)
            await client.connect()
            self.client = client
            self.address = device.address
            self.name = device.name or self.name
            self._remember()

            writable, notifiable = [], []
            for service in client.services:
                for char in service.characteristics:
                    props = char.properties
                    if "write" in props or "write-without-response" in props:
                        writable.append(char)
                    if "notify" in props or "indicate" in props:
                        notifiable.append(char)
            self.write_char = (
                next((c for c in writable if c.uuid.lower() == protocol.WRITE_UUID), None)
                or next((c for c in writable if c.uuid.lower().endswith(protocol.ALT_WRITE_SUFFIX)), None)
                or (writable[0] if writable else None)
            )
            if self.write_char is None:
                raise RuntimeError("No writable characteristic found on the printer")

            # The printer treats the notify subscription as a "host is alive" signal.
            for char in notifiable:
                try:
                    await client.start_notify(char, self._on_notify)
                except Exception as exc:  # noqa: BLE001 - keep going with the others
                    self._log(f"Notify subscribe failed on {char.uuid}: {exc}")

            self._log(f"Connected to {self.name} ({self.address}), write {self.write_char.uuid}")
            await self._set_state("connected", None)
            await self._write(protocol.packet(protocol.Cmd.DEVICE_STATE, b"\x00"))
        except Exception as exc:
            await self._drop()
            await self._set_state("error", str(exc))
            raise
        self._schedule_idle()

    async def _drop(self) -> None:
        client, self.client, self.write_char = self.client, None, None
        if client:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def disconnect(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        await self._drop()
        if self.state != "disconnected":
            await self._set_state("disconnected")

    def _schedule_idle(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        if self.cfg.idle_disconnect <= 0:
            return

        async def idle() -> None:
            await asyncio.sleep(self.cfg.idle_disconnect)
            if not self.lock.locked():
                self._log("Idle timeout, releasing printer")
                await self.disconnect()

        self._idle_task = asyncio.get_running_loop().create_task(idle())

    async def _write(self, chunk: bytes) -> None:
        await self.client.write_gatt_char(self.write_char, chunk, response=False)

    def _chunk_size(self) -> int:
        size = self.cfg.chunk_size or 180
        limit = getattr(self.write_char, "max_write_without_response_size", 0) or 0
        if limit >= 20:
            size = min(size, limit)
        return max(20, size)

    async def send(self, data: bytes, progress: Progress | None = None) -> None:
        async with self.lock:
            if self._idle_task:
                self._idle_task.cancel()
            for attempt in (1, 2):
                try:
                    if not self.connected:
                        await self.connect()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2)

            await self._set_state("printing")
            size = self._chunk_size()
            large = len(data) > 20_000
            delay = max(self.cfg.chunk_delay, 0.025) if large else self.cfg.chunk_delay
            burst = 0
            try:
                for offset in range(0, len(data), size):
                    if not self.connected:
                        raise RuntimeError("Printer disconnected mid-job")
                    try:
                        await asyncio.wait_for(self._resume.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        raise RuntimeError("Printer stayed paused for 30 s") from None
                    chunk = data[offset:offset + size]
                    await self._write(chunk)
                    burst += len(chunk)
                    if large and burst >= 4096:  # docs: breathe between 4 KB bursts
                        burst = 0
                        await asyncio.sleep(0.6)
                    else:
                        await asyncio.sleep(delay)
                    if progress:
                        progress(min(1.0, (offset + size) / len(data)))
                # Disconnecting right after the last chunk truncates the print.
                await asyncio.sleep(self.cfg.hold_after)
            except Exception as exc:
                self.last_error = str(exc)
                await self._set_state("connected" if self.connected else "error", str(exc))
                raise
            await self._set_state("connected", None)
        self._schedule_idle()

    async def gatt_dump(self) -> list[str]:
        if not self.connected:
            await self.connect()
        lines = []
        for service in self.client.services:
            lines.append(f"SERVICE {service.uuid}")
            for char in service.characteristics:
                lines.append(f"  CHAR {char.uuid} [{', '.join(char.properties)}]")
        return lines


def make_printer(cfg: Config, emit: EventSink | None = None) -> BasePrinter:
    if cfg.backend == "mock":
        return MockPrinter(cfg, emit)
    return BlePrinter(cfg, emit)
