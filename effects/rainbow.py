NAME = "Rainbow"
HINT = "All lights cycle through colours together"
ORDER = 1


def frame(t, i, n, state):
    return (t * 12) % 360, 100, 1.0
