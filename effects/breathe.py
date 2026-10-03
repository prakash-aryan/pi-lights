import math

NAME = "Breathe"
HINT = "A slow fade in and out"
ORDER = 4
PERIOD = 6


def frame(t, i, n, state):
    h, s = state.get("base", (30, 90))
    return h, s, 0.1 + 0.9 * (0.5 - 0.5 * math.cos(2 * math.pi * t / PERIOD))
