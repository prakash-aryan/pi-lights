NAME = "Wave"
HINT = "Colours roll from light to light"
ORDER = 2


def frame(t, i, n, state):
    return (t * 12 + i * 360 / n) % 360, 100, 1.0
