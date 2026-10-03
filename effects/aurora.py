import math

NAME = "Aurora"
HINT = "Drifting greens, blues and purples"
ORDER = 7


def frame(t, i, n, state):
    level = 0.5 - 0.5 * math.cos(2 * math.pi * (t + i * 2.3) / 9)
    return (200 + 100 * math.sin(t * 0.11 + i * 1.3)) % 360, 85, 0.45 + 0.55 * level
