import random

NAME = "Party"
HINT = "A new colour on every beat"
ORDER = 3
BEAT = 0.9
HUES = [0, 28, 52, 120, 170, 205, 245, 285, 320]


def frame(t, i, n, state):
    beat = int(t / BEAT)
    return random.Random(beat * 7919 + i * 104729).choice(HUES), 100, 1.0
