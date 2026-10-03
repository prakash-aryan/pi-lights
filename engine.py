import asyncio
import importlib.util
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Effect:
    key: str
    name: str
    hint: str
    order: int
    frame: Callable


def load_effects(folder):
    effects = []
    for path in Path(folder).glob("*.py"):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"effect_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        effects.append(Effect(
            key=path.stem,
            name=getattr(module, "NAME", path.stem.title()),
            hint=getattr(module, "HINT", ""),
            order=getattr(module, "ORDER", 99),
            frame=module.frame,
        ))
    effects.sort(key=lambda e: (e.order, e.name))
    return {e.key: e for e in effects}


class EffectEngine:
    def __init__(self, effects, targets, restore):
        self.effects = effects
        self.targets = targets
        self.restore = restore
        self.active = None
        self.speed = 1.0
        self.levels = {}
        self.paused = set()
        self.snapshot = None
        self.last = {}
        self.tasks = []
        self.origin = (0.0, time.monotonic())

    def status(self):
        live = [v for k, v in self.levels.items() if k not in self.paused]
        return {"active": self.active, "speed": self.speed, "brightness": round(sum(live) / len(live)) if live else 0}

    def catalog(self):
        return [{"id": e.key, "name": e.name, "hint": e.hint} for e in self.effects.values()]

    def clock(self):
        base, since = self.origin
        return base + (time.monotonic() - since) * self.speed

    def set_speed(self, speed):
        self.origin = (self.clock(), time.monotonic())
        self.speed = max(0.25, min(4.0, speed))

    def set_level(self, lid, level):
        self.levels[lid] = max(1, min(100, int(level)))

    async def start(self, key, snapshot, base, speed=None, levels=None, paused=None):
        await self.cancel()
        if self.snapshot is None:
            self.snapshot = snapshot
        if levels is not None:
            self.levels = dict(levels)
        if paused is not None:
            self.paused = set(paused)
        if speed:
            self.set_speed(speed)
        self.active = key
        self.last.clear()
        self.origin = (0.0, time.monotonic())
        effect = self.effects[key]
        state = {"base": base}
        targets = self.targets()
        self.tasks = [asyncio.create_task(self.worker(effect, state, i, len(targets), lid, send, gap))
                      for i, (lid, send, gap) in enumerate(targets)]

    async def stop(self, restore=True):
        was = self.active
        await self.cancel()
        self.active = None
        self.last.clear()
        self.levels.clear()
        self.paused.clear()
        snapshot, self.snapshot = self.snapshot, None
        if restore and was and snapshot:
            await self.restore(snapshot)

    async def cancel(self):
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def frame(self, effect, state, i, n, lid):
        h, s, level = effect.frame(self.clock(), i, n, state)
        peak = self.levels.get(lid, 70)
        return round(h) % 360, max(0, min(100, round(s))), max(1, min(100, round(level * peak)))

    async def worker(self, effect, state, i, n, lid, send, gap):
        while True:
            if lid in self.paused:
                self.last.pop(lid, None)
                await asyncio.sleep(0.1)
                continue
            try:
                frame = self.frame(effect, state, i, n, lid)
            except Exception:
                await asyncio.sleep(0.2)
                continue
            if frame == self.last.get(lid):
                await asyncio.sleep(0.03)
                continue
            self.last[lid] = frame
            try:
                await asyncio.wait_for(send(*frame), 2)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last.pop(lid, None)
                await asyncio.sleep(0.3)
            await asyncio.sleep(gap)
