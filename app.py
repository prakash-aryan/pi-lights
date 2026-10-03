import asyncio
import colorsys
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from kasa import Credentials, Discover, Module
from pydantic import BaseModel

from engine import EffectEngine, load_effects
from govee import GoveeStrip

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text())
CREDS = Credentials(os.environ["TAPO_USERNAME"], os.environ["TAPO_PASSWORD"])
NAMES = CONFIG.get("names", {})
ROOMS = CONFIG.get("rooms", {})
ORDER = list(NAMES)

bulbs = {}
offline = set()
locks = {}
strip = GoveeStrip(CONFIG["govee"]["address"], CONFIG["govee"]["name"], BASE / "strip_state.json")


class Change(BaseModel):
    on: bool | None = None
    brightness: int | None = None
    color: str | None = None
    temp: int | None = None
    only_lit: bool = False


class Calibration(BaseModel):
    gamma: float | None = None
    gain: list[float] | None = None
    level: float | None = None


class EffectSettings(BaseModel):
    speed: float | None = None
    brightness: int | None = None
    color: str | None = None


def lock_for(mac):
    return locks.setdefault(mac, asyncio.Lock())


def bulb_state(mac, dev):
    light = dev.modules[Module.Light]
    h, s, _ = light.hsv
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(h / 360, s / 100, 1))
    temp_range = light.valid_temperature_range
    return {
        "id": mac,
        "name": NAMES.get(mac, dev.alias),
        "kind": "tapo",
        "model": dev.model,
        "online": mac not in offline,
        "on": dev.is_on,
        "brightness": light.brightness,
        "color": f"#{r:02x}{g:02x}{b:02x}",
        "temp": light.color_temp,
        "temp_range": [temp_range.min, temp_range.max],
        "mode": "white" if light.color_temp else "color",
    }


async def discover_bulbs():
    found = await Discover.discover(target=CONFIG["broadcast"], credentials=CREDS, discovery_timeout=4)
    known = {d.host for m, d in bulbs.items() if m not in offline}
    for dev in found.values():
        if dev.host in known:
            await dev.disconnect()
            continue
        try:
            await asyncio.wait_for(dev.update(), 8)
        except Exception:
            await dev.disconnect()
            continue
        if Module.Light not in dev.modules:
            await dev.disconnect()
            continue
        old = bulbs.get(dev.mac)
        if old is not None:
            await old.disconnect()
        bulbs[dev.mac] = dev
        offline.discard(dev.mac)


async def update_bulb(mac, dev):
    try:
        async with lock_for(mac):
            await asyncio.wait_for(dev.update(), 8)
        offline.discard(mac)
    except Exception:
        offline.add(mac)


async def poll_bulbs():
    rounds = 0
    while True:
        if engine.active:
            await asyncio.sleep(2)
            continue
        if rounds % 60 == 0 or offline or set(NAMES) - set(bulbs):
            try:
                await discover_bulbs()
            except Exception:
                pass
        await asyncio.gather(*(update_bulb(mac, dev) for mac, dev in list(bulbs.items())))
        rounds += 1
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(poll_bulbs()), asyncio.create_task(strip.run())]
    yield
    await engine.cancel()
    for task in tasks:
        task.cancel()
    await strip.drop()


app = FastAPI(lifespan=lifespan)


def hsv_hex(h, s, v=100):
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(h / 360, s / 100, v / 100))
    return f"#{r:02x}{g:02x}{b:02x}"


def with_effect(state):
    lid = state["id"]
    default_room = "Govee H6125" if lid == "strip" else f"Tapo {state['model']}"
    state = {**state, "room": ROOMS.get(lid, default_room), "level": state["brightness"]}
    if not engine.active:
        return state
    state["level"] = engine.levels.get(lid, state["brightness"])
    if lid in engine.paused:
        return {**state, "on": False}
    frame = engine.last.get(lid)
    if frame is None:
        return state
    h, s, v = frame
    return {**state, "on": True, "brightness": v, "color": hsv_hex(h, s), "mode": "color", "temp": 0}


def ordered_macs():
    return sorted(bulbs, key=lambda m: (ORDER.index(m) if m in ORDER else len(ORDER), NAMES.get(m, bulbs[m].alias).lower()))


def all_states():
    states = [bulb_state(mac, bulbs[mac]) for mac in ordered_macs()] + [strip.state()]
    return [with_effect(s) for s in states]


async def bulb_frame(mac, h, s, v):
    light = bulbs[mac].modules[Module.Light]
    async with lock_for(mac):
        await light.set_hsv(h, s, v)


async def strip_frame(h, s, v):
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(h / 360, s / 100, 1))
    if strip.brightness != v:
        await strip.set_brightness(v)
    await strip.send_rgb(r, g, b)


def effect_targets():
    targets = [(mac, lambda h, s, v, mac=mac: bulb_frame(mac, h, s, v), 0.0) for mac in ordered_macs() if mac not in offline]
    if strip.online:
        targets.append(("strip", strip_frame, 0.04))
    return targets


async def restore_states(snapshot):
    async def one(state):
        look = {"temp": state["temp"]} if state["mode"] == "white" and state["temp"] else {"color": state["color"]}
        apply = apply_strip if state["id"] == "strip" else (lambda c: apply_bulb(state["id"], c))
        if state["on"]:
            await apply(Change(on=True, brightness=state["brightness"], **look))
        else:
            await apply(Change(brightness=state["brightness"], **look))
            await apply(Change(on=False))
    jobs = [one(s) for s in snapshot if s["online"] and (s["id"] == "strip" or s["id"] in bulbs)]
    await asyncio.gather(*jobs, return_exceptions=True)


engine = EffectEngine(load_effects(BASE / "effects"), effect_targets, restore_states)


@app.get("/api/lights")
async def list_lights():
    return all_states()


async def apply_bulb(mac, change):
    dev = bulbs[mac]
    light = dev.modules[Module.Light]
    async with lock_for(mac):
        if change.on is False and dev.is_on:
            await dev.turn_off()
        elif change.on is True and not dev.is_on:
            await dev.turn_on()
        if change.color:
            r, g, b = (int(change.color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
            h, s, _ = colorsys.rgb_to_hsv(r, g, b)
            await light.set_hsv(round(h * 360), round(s * 100), change.brightness or light.brightness or 100)
        elif change.temp:
            await light.set_color_temp(change.temp)
            if change.brightness:
                await light.set_brightness(change.brightness)
        elif change.brightness:
            await light.set_brightness(change.brightness)
        await dev.update()
    return bulb_state(mac, dev)


async def apply_strip(change):
    if change.on is not None and change.on != strip.on:
        await strip.set_power(change.on)
    if change.color:
        await strip.set_color(change.color)
    elif change.temp:
        await strip.set_white(change.temp)
    if change.brightness:
        await strip.set_brightness(change.brightness)
    return strip.state()


def keeps_effect(change):
    return engine.active and change.color is None and change.temp is None


async def switch(lid, on):
    if lid == "strip":
        await apply_strip(Change(on=on))
    else:
        await apply_bulb(lid, Change(on=on))


async def effect_light(lid, change):
    if change.brightness:
        engine.set_level(lid, change.brightness)
    if change.on is False:
        engine.paused.add(lid)
        await switch(lid, False)
    elif change.on is True or (change.brightness and lid in engine.paused):
        await switch(lid, True)
        engine.paused.discard(lid)


def state_of(lid):
    return next(s for s in all_states() if s["id"] == lid)


@app.post("/api/lights/{light_id}")
async def change_light(light_id: str, change: Change):
    if light_id != "strip" and light_id not in bulbs:
        raise HTTPException(404, "unknown light")
    try:
        if keeps_effect(change):
            await effect_light(light_id, change)
            return state_of(light_id)
        if engine.active:
            level = engine.levels.get(light_id)
            await engine.stop(restore=False)
            change = change.model_copy(update={"brightness": change.brightness or level})
        if light_id == "strip":
            await apply_strip(change)
        else:
            await apply_bulb(light_id, change)
        return state_of(light_id)
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/all")
async def change_all(change: Change):
    targets = [mac for mac in bulbs if mac not in offline]
    use_strip = strip.online
    if keeps_effect(change):
        ids = targets + (["strip"] if use_strip else [])
        await asyncio.gather(*(effect_light(i, change) for i in ids), return_exceptions=True)
        return all_states()
    levels = {}
    if engine.active:
        levels = dict(engine.levels)
        await engine.stop(restore=False)
    dim_only = change.brightness and change.on is None and not change.color and not change.temp
    lit = [mac for mac in targets if bulbs[mac].is_on]
    any_lit = bool(lit) or bool(use_strip and strip.on)
    if (dim_only or change.only_lit) and any_lit:
        targets, use_strip = lit, use_strip and bool(strip.on)
    if change.color or change.temp:
        change.on = True
    def for_light(lid):
        return change.model_copy(update={"brightness": change.brightness or levels.get(lid)})
    jobs = [apply_bulb(mac, for_light(mac)) for mac in targets]
    if use_strip:
        jobs.append(apply_strip(for_light("strip")))
    await asyncio.gather(*jobs, return_exceptions=True)
    return all_states()


def base_colour(snapshot):
    lit = [s for s in snapshot if s["online"] and s["on"] and s["mode"] == "color"]
    if not lit:
        return 32, 80
    r, g, b = (int(lit[0]["color"].lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 360), max(40, round(s * 100))


def hex_base(hex_color):
    r, g, b = (int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 360), max(40, round(s * 100))


async def all_on():
    jobs = [switch(mac, True) for mac in bulbs if mac not in offline]
    if strip.online:
        jobs.append(switch("strip", True))
    await asyncio.gather(*jobs, return_exceptions=True)


@app.get("/api/strip/calibration")
async def get_calibration():
    return strip.cal


@app.post("/api/strip/calibration")
async def set_calibration(cal: Calibration):
    try:
        await strip.set_calibration(cal.gamma, cal.gain, cal.level)
    except Exception as e:
        raise HTTPException(503, str(e))
    return strip.cal


@app.get("/api/effects")
async def list_effects():
    return {"effects": engine.catalog(), **engine.status()}


@app.post("/api/effects/stop")
async def stop_effect():
    await engine.stop(restore=True)
    return engine.status()


@app.post("/api/effects/settings")
async def effect_settings(settings: EffectSettings):
    if settings.speed:
        engine.set_speed(settings.speed)
    return engine.status()


@app.post("/api/effects/{key}")
async def start_effect(key: str, settings: EffectSettings):
    if key not in engine.effects:
        raise HTTPException(404, "unknown effect")
    snapshot = levels = paused = None
    if not engine.active:
        snapshot = all_states()
        online = [s for s in snapshot if s["online"]]
        if any(s["on"] for s in online):
            levels = {s["id"]: s["brightness"] or 70 for s in online}
            paused = {s["id"] for s in online if not s["on"]}
        else:
            levels = {s["id"]: settings.brightness or 70 for s in online}
            paused = set()
            await all_on()
    base = hex_base(settings.color) if settings.color else base_colour(snapshot or engine.snapshot or [])
    await engine.start(key, snapshot, base, settings.speed, levels, paused)
    return engine.status()


@app.get("/api/stream")
async def stream(request: Request):
    async def events():
        while not await request.is_disconnected():
            payload = {"lights": all_states(), "effects": engine.status()}
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.12 if engine.active else 1.0)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
