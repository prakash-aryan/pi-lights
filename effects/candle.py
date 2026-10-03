import math
import random

NAME = "Candle"
HINT = "Warm, flickering flames"
ORDER = 5


def frame(t, i, n, state):
    p1, p2, p3 = state.setdefault(i, [random.uniform(0, 6.28) for _ in range(3)])
    flicker = 0.5 + 0.22 * math.sin(t * 2.1 + p1) + 0.16 * math.sin(t * 5.3 + p2) + 0.12 * math.sin(t * 9.7 + p3)
    return 28 + 6 * math.sin(t * 0.7 + p1), 88, 0.35 + 0.65 * max(0.0, min(1.0, flicker))
