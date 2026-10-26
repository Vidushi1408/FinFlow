"""Tiny in-process request metrics in Prometheus text format. Per gunicorn worker (no shared state), so a
scraper sees one worker's counters per scrape; sum over `rate()` as usual. Deliberately dependency-free."""
import threading
from collections import defaultdict

_lock = threading.Lock()
_requests = defaultdict(int)       # (method, status_class) -> count
_duration_sum_ms = defaultdict(float)
_duration_count = defaultdict(int)


def record(method, status, duration_ms):
    key = (method, f"{int(status) // 100}xx")
    with _lock:
        _requests[key] += 1
        if duration_ms is not None:
            _duration_sum_ms[method] += duration_ms
            _duration_count[method] += 1


def render():
    lines = ["# HELP finflow_http_requests_total HTTP requests by method and status class",
             "# TYPE finflow_http_requests_total counter"]
    with _lock:
        for (method, klass), n in sorted(_requests.items()):
            lines.append(f'finflow_http_requests_total{{method="{method}",status="{klass}"}} {n}')
        lines += ["# HELP finflow_http_request_duration_ms_sum Total request time in milliseconds",
                  "# TYPE finflow_http_request_duration_ms_sum counter"]
        for method, total in sorted(_duration_sum_ms.items()):
            lines.append(f'finflow_http_request_duration_ms_sum{{method="{method}"}} {total:.1f}')
        lines += ["# TYPE finflow_http_request_duration_ms_count counter"]
        for method, n in sorted(_duration_count.items()):
            lines.append(f'finflow_http_request_duration_ms_count{{method="{method}"}} {n}')
    return "\n".join(lines) + "\n"


def reset():
    with _lock:
        _requests.clear()
        _duration_sum_ms.clear()
        _duration_count.clear()
