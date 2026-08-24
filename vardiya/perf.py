"""Performans ölçümü (opsiyonel, varsayılan: sessiz no-op)."""


class PerfTimer:
    def __init__(self, location, message, hypothesis_id, extra=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def perf_log(location, message, data=None, hypothesis_id=None):
    pass
