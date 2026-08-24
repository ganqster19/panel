"""Performans ölçümü (opsiyonel, varsayılan: sessiz no-op)."""


class PerfTimer:
    def __init__(self, location, message, hypothesis_id, extra=None):
        self.location = location
        self.message = message
        self.hypothesis_id = hypothesis_id
        self.extra = extra if extra is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def perf_log(location, message, data=None, hypothesis_id=None):
    pass
