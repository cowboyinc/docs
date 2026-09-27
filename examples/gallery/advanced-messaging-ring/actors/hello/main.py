class Actor:
    """A Cowboy actor deployed on-chain."""

    def __init__(self):
        self.counter = 0

    def increment(self):
        self.counter += 1
        return self.counter

    def get_count(self):
        return self.counter
