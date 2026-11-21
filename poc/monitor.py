"""Local page for the SQLite log. Binds to localhost unless LOG_HTTP_HOST says otherwise."""

from __future__ import annotations

import json
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from poc.config import Settings
from poc.store import Store

_FILTERS = (
    ("", "All"),
    ("info", "Info"),
    ("warn", "Warning"),
    ("error", "Error"),
)
_LABELS = {"info": "Info", "warn": "Warning", "error": "Error"}


class LogServer:
    def __init__(self, http: ThreadingHTTPServer, thread: threading.Thread, url: str) -> None:
        self.http = http
        self.thread = thread
        self.url = url

    def shutdown(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)


def start_monitor(settings: Settings, store: Store) -> LogServer | None:
    if not settings.log_http:
        return None
    try:
        server = ThreadingHTTPServer(
            (settings.log_http_host, settings.log_http_port),
            _handler(store),
        )
    except OSError as exc:
        print(f"log server failed: {exc}", flush=True)
        return None
    thread = threading.Thread(target=server.serve_forever, name="poc-log-http", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return LogServer(server, thread, f"http://{host}:{port}/")


def _handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            level = _query_level(parsed.query)
            if parsed.path == "/logs.json":
                body = json.dumps({"level": level or "all", "rows": store.recent_events(level)}).encode()
                self._send(200, "application/json; charset=utf-8", body)
                return
            if parsed.path not in {"", "/"}:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            self._send(200, "text/html; charset=utf-8", _page(store.recent_events(level), level).encode())

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _query_level(query: str) -> str:
    raw = parse_qs(query).get("level", [""])[0]
    text = raw.strip().lower()
    if text in {"warning", "warnings"}:
        return "warn"
    if text in {"info", "warn", "error"}:
        return text
    return ""


def _page(rows: list[dict], level: str) -> str:
    links = []
    for value, label in _FILTERS:
        href = "/" if not value else f"/?level={value}"
        mark = " on" if value == level else ""
        links.append(f'<a class="{mark.strip()}" href="{href}">{label}</a>')
    body = []
    if not rows:
        body.append('<p class="empty">No rows for this filter.</p>')
    else:
        body.append("<table><thead><tr><th>Time</th><th>Level</th><th>Event</th><th>Detail</th></tr></thead><tbody>")
        for row in rows:
            name = _LABELS.get(row["level"], escape(str(row["level"])))
            css = escape(str(row["level"]))
            body.append(
                "<tr>"
                f"<td>{escape(str(row['created_at']))}</td>"
                f'<td class="{css}">{name}</td>'
                f"<td>{escape(str(row['event']))}</td>"
                f"<td class=\"detail\">{escape(str(row['detail']))}</td>"
                "</tr>"
            )
        body.append("</tbody></table>")
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"5\">"
        "<title>POC logs</title><style>"
        "body{font:14px/1.45 ui-sans-serif,system-ui,sans-serif;margin:24px;color:#1c1917;background:#fafaf9}"
        "h1{font-size:18px;font-weight:600;margin:0 0 12px}"
        "nav a{display:inline-block;margin:0 8px 0 0;padding:4px 10px;border:1px solid #d6d3d1;border-radius:999px;"
        "color:inherit;text-decoration:none}"
        "nav a.on{background:#1c1917;color:#fafaf9;border-color:#1c1917}"
        "table{width:100%;border-collapse:collapse;margin-top:16px}"
        "th,td{text-align:left;vertical-align:top;padding:8px 10px;border-top:1px solid #e7e5e4}"
        "th{font-size:12px;color:#78716c;font-weight:600}"
        ".detail{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,monospace;font-size:12px}"
        ".warn{color:#9a3412}.error{color:#b91c1c}.empty{color:#78716c}"
        "</style></head><body><h1>POC logs</h1><nav>"
        + "".join(links)
        + "</nav>"
        + "".join(body)
        + "</body></html>"
    )
