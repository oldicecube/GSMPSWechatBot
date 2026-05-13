class Dedup:
    def __init__(self):
        self.cache = set()

    def exists(self, key):
        return key in self.cache

    def add(self, key):
        self.cache.add(key)