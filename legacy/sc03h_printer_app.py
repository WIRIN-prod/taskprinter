
import asyncio
import threading
import queue
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from bleak import BleakClient, BleakScanner
from PIL import Image, ImageOps, ImageDraw, ImageFont

SERVICE_UUID = "0000ae30-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"

NOTIFY_UUIDS = {
    "0000ae02-0000-1000-8000-00805f9b34fb",
    "0000ae04-0000-1000-8000-00805f9b34fb",
    "0000ae3c-0000-1000-8000-00805f9b34fb",
}

CHUNK_SIZE = 180


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def packet(opcode: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    return (
        bytes((
            0x51, 0x78, opcode, 0x00,
            length & 0xFF, (length >> 8) & 0xFF
        ))
        + payload
        + bytes((crc8(payload), 0xFF))
    )


def make_test_image():
    image = Image.new("L", (384, 240), 255)
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.truetype("arialbd.ttf", 29)
        small = ImageFont.truetype("arialbd.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
        small = font

    draw.rectangle((3, 3, 380, 236), outline=0, width=4)
    draw.text((16, 14), "SC03h TEST", font=font, fill=0)
    draw.text(
        (16, 54),
        datetime.now().strftime("%H:%M:%S"),
        font=small,
        fill=0,
    )
    draw.line((14, 83, 370, 83), fill=0, width=4)
    draw.text((16, 94), "0123456789", font=small, fill=0)

    for index in range(8):
        x = 18 + index * 43
        draw.rectangle(
            (x, 132, x + 25, 160),
            fill=0 if index % 2 == 0 else 255,
            outline=0,
            width=2,
        )

    draw.rectangle((18, 176, 365, 184), fill=0)
    draw.rectangle((18, 194, 300, 202), fill=0)
    draw.text((16, 210), "HIGH CONTRAST", font=small, fill=0)

    return image


def bitmap_rows(image):
    # Apply horizontal flip based on the earlier print test.
    image = image.convert("L")

    # Resize to the printer's assumed 384-dot width.
    if image.width > 384:
        height = max(1, round(image.height * 384 / image.width))
        image = image.resize(
            (384, height),
            Image.Resampling.LANCZOS,
        )

    # Center narrower images on a white canvas.
    if image.width < 384:
        canvas = Image.new("L", (384, image.height), 255)
        canvas.paste(image, ((384 - image.width) // 2, 0))
        image = canvas

    # Convert to black and white.
    monochrome = image.point(
        lambda p: 0 if p < 180 else 255,
        mode="L",
    )

    pixels = monochrome.load()
    rows = []

    for y in range(monochrome.height):
        row = [
            1 if pixels[x, y] == 0 else 0
            for x in range(384)
        ]
        rows.append(row)

    return rows


class PrinterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SC03h Mini Printer")
        self.root.geometry("900x720")

        self.events = queue.Queue()
        self.loop = None
        self.client = None
        self.devices = []
        self.write_uuid = WRITE_UUID
        self.busy = False

        self._build_ui()
        self._start_loop()
        self.root.after(100, self._poll_events)

        self.log("Ready. Close iPrint on your phone before connecting.")
        self.log("Printing uses an experimental protocol implementation.")

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="SC03h Mini Printer",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text="Bluetooth LE diagnostics and experimental printing",
        ).pack(anchor="w", pady=(0, 10))

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=5)

        self.scan_btn = ttk.Button(
            toolbar, text="Scan for printers", command=self.scan
        )
        self.scan_btn.pack(side="left")

        self.connect_btn = ttk.Button(
            toolbar, text="Connect selected", command=self.connect_selected
        )
        self.connect_btn.pack(side="left", padx=5)

        self.disconnect_btn = ttk.Button(
            toolbar, text="Disconnect", command=self.disconnect
        )
        self.disconnect_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(toolbar, textvariable=self.status_var).pack(side="right")

        panels = ttk.Frame(outer)
        panels.pack(fill="both", expand=True, pady=6)

        left = ttk.Frame(panels)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="BLE devices").pack(anchor="w")

        self.device_list = tk.Listbox(left, height=8)
        self.device_list.pack(fill="both", expand=True, pady=4)

        ttk.Label(left, text="Image file (optional)").pack(anchor="w")

        image_row = ttk.Frame(left)
        image_row.pack(fill="x")

        self.image_path = tk.StringVar()
        ttk.Entry(
            image_row, textvariable=self.image_path
        ).pack(side="left", fill="x", expand=True)

        ttk.Button(
            image_row, text="Browse", command=self.browse_image
        ).pack(side="left", padx=4)

        right = ttk.Frame(panels)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        ttk.Label(right, text="GATT services and characteristics").pack(
            anchor="w"
        )

        self.gatt_text = tk.Text(
            right, height=16, wrap="word", state="disabled",
            font=("Consolas", 9),
        )
        self.gatt_text.pack(fill="both", expand=True, pady=4)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=5)

        self.test_btn = ttk.Button(
            actions,
            text="Print test pattern",
            command=self.print_test,
        )
        self.test_btn.pack(side="left")

        self.image_btn = ttk.Button(
            actions,
            text="Print selected image",
            command=self.print_image,
        )
        self.image_btn.pack(side="left", padx=6)

        ttk.Button(
            actions, text="Save log", command=self.save_log
        ).pack(side="right")

        ttk.Label(outer, text="Activity log").pack(anchor="w")

        self.log_text = tk.Text(
            outer, height=10, wrap="word", state="disabled",
            font=("Consolas", 9),
        )
        self.log_text.pack(fill="both", expand=True)

    def _start_loop(self):
        def worker():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        threading.Thread(target=worker, daemon=True).start()

    def submit(self, coroutine, callback=None):
        if self.loop is None:
            coroutine.close()
            self.log("Async event loop is not ready.")
            return

        future = asyncio.run_coroutine_threadsafe(
            coroutine, self.loop
        )

        if callback:
            def finished(fut):
                try:
                    result = fut.result()
                    self.events.put(("callback", callback, result, None))
                except Exception as exc:
                    self.events.put(("callback", callback, None, exc))

            future.add_done_callback(finished)

    def scan(self):
        self.scan_btn.configure(state="disabled")
        self.status_var.set("Scanning…")
        self.submit(self._scan_async(), self._scan_done)

    async def _scan_async(self):
        found = await BleakScanner.discover(
            timeout=8.0, return_adv=True
        )

        results = []
        for device, advertisement in found.values():
            name = device.name or advertisement.local_name or "(unnamed)"
            services = advertisement.service_uuids or []

            if (
                "sc03h" in name.lower()
                or "iprint" in name.lower()
                or any("ae30" in s.lower() or "af30" in s.lower()
                       for s in services)
            ):
                results.append((
                    name, device.address, services,
                    getattr(advertisement, "rssi", None)
                ))

        return results

    def _scan_done(self, results, error):
        self.scan_btn.configure(state="normal")
        self.device_list.delete(0, "end")
        self.devices = results or []

        for name, address, services, rssi in self.devices:
            self.device_list.insert(
                "end",
                f"{name} | {address} | RSSI {rssi}"
            )

        if error:
            self.log(f"Scan failed: {error}")
            self.status_var.set("Scan failed")
            return

        self.status_var.set(f"{len(self.devices)} device(s) found")
        self.log(f"Scan complete: {len(self.devices)} matching device(s).")

        for index, device in enumerate(self.devices):
            if "sc03h" in device[0].lower():
                self.device_list.selection_set(index)
                break

    def connect_selected(self):
        selection = self.device_list.curselection()

        if not selection:
            messagebox.showinfo(
                "Select printer", "Scan and select SC03h-14BF first."
            )
            return

        address = self.devices[selection[0]][1]
        self.connect_btn.configure(state="disabled")
        self.status_var.set("Connecting…")
        self.submit(self._connect_async(address), self._connect_done)

    async def _connect_async(self, address):
        if self.client and self.client.is_connected:
            await self.client.disconnect()

        client = BleakClient(address, timeout=15)
        await client.connect()
        self.client = client

        lines = []
        writable = []
        notifiable = []

        for service in client.services:
            lines.append(f"SERVICE {service.uuid}")

            for characteristic in service.characteristics:
                properties = characteristic.properties
                lines.append(
                    f"  CHAR {characteristic.uuid} [{', '.join(properties)}]"
                )

                if (
                    "write" in properties
                    or "write-without-response" in properties
                ):
                    writable.append(characteristic.uuid)

                if "notify" in properties or "indicate" in properties:
                    notifiable.append(characteristic.uuid)

        preferred = next(
            (uuid for uuid in writable
             if uuid.lower() == WRITE_UUID),
            None,
        )

        if not preferred:
            preferred = next(
                (uuid for uuid in writable
                 if uuid.lower().endswith("ae3b")),
                None,
            )

        if not preferred and writable:
            preferred = writable[0]

        subscribed = []

        for uuid in notifiable:
            try:
                await client.start_notify(uuid, self._notify_callback)
                subscribed.append(uuid)
            except Exception as exc:
                lines.append(f"  Notify subscription failed: {exc}")

        return {
            "address": address,
            "gatt": "\n".join(lines),
            "write_uuid": preferred,
            "notifies": subscribed,
        }

    def _connect_done(self, result, error):
        self.connect_btn.configure(state="normal")

        if error:
            self.status_var.set("Connection failed")
            self.log(f"Connection failed: {error}")
            messagebox.showerror("Connection failed", str(error))
            return

        self.write_uuid = result["write_uuid"]
        self.status_var.set("Connected")

        self.gatt_text.configure(state="normal")
        self.gatt_text.delete("1.0", "end")
        self.gatt_text.insert("1.0", result["gatt"])
        self.gatt_text.configure(state="disabled")

        self.log(f"Connected to {result['address']}")
        self.log(f"Write characteristic: {self.write_uuid}")
        self.log(
            "Notifications: " + (
                ", ".join(result["notifies"]) or "none"
            )
        )

    def _notify_callback(self, sender, data):
        self.events.put(("notify", str(sender), bytes(data)))

    def disconnect(self):
        self.submit(self._disconnect_async(), self._disconnect_done)

    async def _disconnect_async(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        return True

    def _disconnect_done(self, result, error):
        self.status_var.set("Disconnected")
        self.log("Disconnected." if not error else f"Disconnect error: {error}")

    def print_test(self):
        self._send_image(make_test_image())

    def print_image(self):
        path = self.image_path.get()

        if not path:
            self.browse_image()
            path = self.image_path.get()

        if not path:
            return

        try:
            image = Image.open(path)
        except Exception as exc:
            messagebox.showerror("Image error", str(exc))
            return

        self._send_image(image)

    def _send_image(self, image):
        if not self.client or not self.client.is_connected:
            messagebox.showwarning("Not connected", "Connect first.")
            return

        if not self.write_uuid:
            messagebox.showerror(
                "No write characteristic", "No writable characteristic found."
            )
            return

        if self.busy:
            messagebox.showinfo("Busy", "A print job is already running.")
            return

        if not messagebox.askyesno(
            "Experimental print",
            "This sends an experimental SC03h 51 78 raw-row print job. "
            "Some firmware versions may require RLE encoding. Continue?"
        ):
            return

        rows = bitmap_rows(image)
        job = bytearray()

        # Experimental command sequence; firmware-specific behavior is unverified.
        job += packet(0xA3, b"\x00")
        job += packet(0xA4, b"\x33")
        job += packet(0xAF, b"\x70\x44")
        job += packet(0xBE, b"\x00")
        job += packet(0xBD, b"\x23")

        for row in rows:
            job += packet(0xBF, self.encode_rle_row(row))

        # Small feed only.
        for _ in range(4):
            job += packet(0xBD, b"\x23")

        self.busy = True
        self.test_btn.configure(state="disabled")
        self.image_btn.configure(state="disabled")
        self.status_var.set("Sending print job…")

        self.log(
            f"Sending {len(rows)} rows, {len(job)} bytes."
        )

        self.submit(self._send_job_async(bytes(job)), self._print_done)

    async def _send_job_async(self, job):
        if not self.client or not self.client.is_connected:
            raise RuntimeError("Printer disconnected.")

        # Respect BLE packet sizing and pace writes.
        delay = 0.025
        for offset in range(0, len(job), CHUNK_SIZE):
            chunk = job[offset:offset + CHUNK_SIZE]

            await self.client.write_gatt_char(
                self.write_uuid,
                chunk,
                response=False,
            )
            await asyncio.sleep(delay)

        await asyncio.sleep(2)
        return len(job)

    def _print_done(self, result, error):
        self.busy = False
        self.test_btn.configure(state="normal")
        self.image_btn.configure(state="normal")

        if error:
            self.status_var.set("Print failed")
            self.log(f"Print error: {error}")
            messagebox.showerror("Print failed", str(error))
        else:
            self.status_var.set("Connected")
            self.log(f"Sent {result} bytes. Inspect the printed output.")
            messagebox.showinfo(
                "Job sent",
                "The data was sent. This does not guarantee protocol compatibility."
            )

    def browse_image(self):
        path = filedialog.askopenfilename(
            title="Choose image",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.image_path.set(path)

    def save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as file:
                file.write(self.log_text.get("1.0", "end"))
            self.log(f"Log saved to {path}")

    def log(self, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()

                if event[0] == "callback":
                    _, callback, result, error = event
                    callback(result, error)

                elif event[0] == "notify":
                    _, sender, data = event
                    self.log(
                        f"NOTIFY {sender}: {data.hex(' ').upper()}"
                    )

        except queue.Empty:
            pass

        self.root.after(100, self._poll_events)

    def encode_rle_row(self, pixels):
        """Encode a 384-pixel monochrome row using experimental Tiny RLE."""
        if len(pixels) != 384:
            raise ValueError("Expected exactly 384 pixels")

        result = bytearray()
        current_color = pixels[0]
        run_length = 0

        for pixel in pixels:
            if pixel == current_color and run_length < 127:
                run_length += 1
            else:
                result.append(
                    (0x80 if current_color else 0) | run_length
                )
                current_color = pixel
                run_length = 1

        if run_length:
            result.append(
                (0x80 if current_color else 0) | run_length
            )

        return bytes(result)

def main():
    root = tk.Tk()
    PrinterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
