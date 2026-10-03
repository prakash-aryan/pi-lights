import asyncio
import json
import math
from pathlib import Path

from bleak import BleakClient, BleakScanner

WRITE = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY = "00010203-0405-0607-0809-0a0b0c0d2b10"
BALANCE = (1.0, 0.68, 0.26)


def frame(*payload):
    body = list(payload) + [0] * (19 - len(payload))
    check = 0
    for b in body:
        check ^= b
    return bytes(body + [check])


def kelvin_rgb(kelvin):
    t = kelvin / 100
    r = 255 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
    g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * (t - 60) ** -0.0755148492
    b = 255 if t >= 66 else 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple(max(0, min(255, round(v))) for v in (r, g, b))


class GoveeStrip:
    def __init__(self, address, name, state_file, balance=None):
        self.address = address
        self.balance = tuple(balance or BALANCE)
        self.name = name
        self.state_file = Path(state_file)
        saved = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.color = saved.get("color", "#ffffff")
        self.mode = saved.get("mode", "color")
        self.temp = saved.get("temp", 0)
        self.on = None
        self.brightness = None
        self.client = None
        self.lock = asyncio.Lock()
        self.replies = asyncio.Queue()

    @property
    def online(self):
        return self.client is not None and self.client.is_connected

    def state(self):
        return {
            "id": "strip",
            "name": self.name,
            "kind": "govee",
            "model": "H6125",
            "online": self.online,
            "on": self.on,
            "brightness": self.brightness,
            "color": self.color,
            "temp": self.temp if self.mode == "white" else 0,
            "temp_range": [2500, 6500],
            "mode": self.mode,
        }

    async def run(self):
        while True:
            try:
                if not self.online:
                    await self.connect()
                await self.refresh()
                await asyncio.sleep(5)
            except Exception:
                await self.drop()
                await asyncio.sleep(10)

    async def connect(self):
        device = await BleakScanner.find_device_by_address(self.address, timeout=15)
        if device is None:
            raise RuntimeError("strip not found")
        client = BleakClient(device, timeout=20)
        await client.connect()
        await client.start_notify(NOTIFY, lambda _h, data: self.replies.put_nowait(bytes(data)))
        self.client = client

    async def drop(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def request(self, *cmd):
        if not self.online:
            raise RuntimeError("LED strip is not connected")
        async with self.lock:
            while not self.replies.empty():
                self.replies.get_nowait()
            await self.client.write_gatt_char(WRITE, frame(*cmd), response=False)
            while True:
                reply = await asyncio.wait_for(self.replies.get(), 3)
                if reply[:2] == bytes(cmd[:2]):
                    return reply

    async def command(self, *cmd):
        reply = await self.request(*cmd)
        if reply[2] != 0:
            raise RuntimeError(f"LED strip rejected {bytes(cmd).hex(' ')}")

    async def refresh(self):
        self.on = (await self.request(0xAA, 0x01))[2] == 1
        self.brightness = (await self.request(0xAA, 0x04))[2]

    def save(self):
        self.state_file.write_text(json.dumps({"color": self.color, "mode": self.mode, "temp": self.temp}))

    async def set_power(self, on):
        await self.command(0x33, 0x01, 1 if on else 0)
        self.on = on

    async def set_brightness(self, percent):
        percent = max(1, min(100, int(percent)))
        await self.command(0x33, 0x04, percent)
        self.brightness = percent

    def balanced(self, r, g, b):
        scaled = [c * k for c, k in zip((r, g, b), self.balance)]
        peak = max(scaled)
        if peak == 0:
            return 0, 0, 0
        return tuple(round(c * max(r, g, b) / peak) for c in scaled)

    async def send_rgb(self, r, g, b):
        await self.command(0x33, 0x05, 0x15, 0x01, *self.balanced(r, g, b), 0, 0, 0, 0, 0, 0xFF, 0x7F)
        self.color = f"#{r:02x}{g:02x}{b:02x}"

    async def set_color(self, hex_color):
        r, g, b = (int(hex_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        await self.send_rgb(r, g, b)
        self.mode = "color"
        self.save()

    async def set_white(self, kelvin):
        kelvin = max(2000, min(9000, int(kelvin)))
        await self.send_rgb(*kelvin_rgb(kelvin))
        self.mode = "white"
        self.temp = int(kelvin)
        self.save()
