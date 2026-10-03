import random

NAME = "Storm"
HINT = "Dark blue with lightning flashes"
ORDER = 6


def frame(t, i, n, state):
    flashes = state.setdefault("flashes", [])
    if "next" not in state:
        state["next"] = t + random.uniform(0.8, 2.5)
    if t >= state["next"]:
        if random.random() < 0.3:
            who = set(range(n))
        else:
            who = set(random.sample(range(n), random.randint(1, max(1, n - 1))))
        flashes.append((t, t + 0.35, who))
        if random.random() < 0.5:
            flashes.append((t + 0.6, t + 0.95, who))
        state["next"] = t + random.uniform(1.2, 4.5)
    flashes[:] = [f for f in flashes if f[1] > t]
    for start, end, who in flashes:
        if start <= t < end and i in who:
            return 215, 6, 1.0
    return 228, 78, 0.07
