from __future__ import annotations

import collections
import contextlib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from CoreDeviceBridge.deviceTunnel import (
    list_wifi_paired_udids,
    pair_device_for_wifi_over_usb,
)

logger = logging.getLogger("universal_clipboard")


class CircularLogHandler(logging.Handler):
    """Keep last N formatted log lines in memory to serve live to the WinUI 3 Developer Mode console."""

    def __init__(self, capacity: int = 1500) -> None:
        super().__init__()
        self.capacity = capacity
        self.buffer: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    @staticmethod
    def _format_clean_log(record: logging.LogRecord) -> str:
        msg = record.getMessage()
        dt = datetime.fromtimestamp(record.created)
        time_str = dt.strftime("%H:%M:%S")

        if "[STARTUP]" in msg:
            return f"{time_str}  [STARTUP]  Universal Clipboard daemon started (Auto Wi-Fi / USB)"
        if "[API]" in msg and "listening on" in msg:
            return f"{time_str}  [API]      WinUI 3 local bridge listening on port 8767"
        if "[CONNECTING]" in msg:
            transport = "Wi-Fi" if "transport=Wi-Fi" in msg else "USB"
            return f"{time_str}  [CONNECT]  Connecting to iPhone via {transport}..."
        if "[CONNECTED]" in msg:
            device = "iPhone"
            m_dev = re.search(r"device=([^\s]+)", msg)
            if m_dev:
                device = m_dev.group(1)
            transport = "Wi-Fi" if "transport=Wi-Fi" in msg else "USB"
            return f"{time_str}  [ONLINE]   {device} paired and ready via {transport}"
        if "[SUBSCRIBED]" in msg:
            return f"{time_str}  [READY]    Listening for iPhone pasteboard copy events"
        if "[NOTIFICATION]" in msg:
            return f"{time_str}  [EVENT]    iPhone copied new content"
        if "[CLIPBOARD_UPDATED]" in msg:
            if "status=SUCCESS" in msg:
                m_type = re.search(r"type=([^\s]+)", msg)
                t_kind = m_type.group(1) if m_type else "text"
                m_lat = re.search(r"total_ms=([0-9\.]+)", msg)
                lat = f"{m_lat.group(1)}ms" if m_lat else ""
                transport = "Wi-Fi" if "transport=Wi-Fi" in msg else "USB"
                if t_kind == "image":
                    m_dim = re.search(r"dimensions=([0-9x]+)", msg)
                    dim = m_dim.group(1) if m_dim else "image"
                    return f"{time_str}  [SYNC]     iPhone → Windows · Image ({dim}) · {transport} · {lat}"
                elif t_kind == "url":
                    return f"{time_str}  [SYNC]     iPhone → Windows · URL Link · {transport} · {lat}"
                else:
                    m_chars = re.search(r"chars=([0-9]+)", msg)
                    chars = f"{m_chars.group(1)} chars" if m_chars else "text"
                    return f"{time_str}  [SYNC]     iPhone → Windows · Text ({chars}) · {transport} · {lat}"
            elif "status=ERROR" in msg:
                m_err = re.search(r"error=([^\n]+)", msg)
                err = m_err.group(1) if m_err else "Sync error"
                return f"{time_str}  [ERROR]    Sync failed: {err}"
        if "[CLIPBOARD_PUSHED]" in msg:
            if "status=SUCCESS" in msg:
                m_type = re.search(r"type=([^\s]+)", msg)
                t_kind = m_type.group(1) if m_type else "text"
                m_lat = re.search(r"total_ms=([0-9\.]+)", msg)
                lat = f"{m_lat.group(1)}ms" if m_lat else ""
                transport = "Wi-Fi" if "transport=Wi-Fi" in msg else "USB"
                return f"{time_str}  [PUSH]     Windows → iPhone · {t_kind.capitalize()} · {transport} · {lat}"
            else:
                return f"{time_str}  [ERROR]    Failed to push Windows clipboard to iPhone"
        if "[SKIP]" in msg:
            if "reason=duplicate_changeCount" in msg or "reason=no_supported" in msg:
                return ""
            return f"{time_str}  [SKIP]     Identical payload (echo loop prevented)"
        if "[DISCONNECTED]" in msg:
            return f"{time_str}  [OFFLINE]  iPhone disconnected, reconnecting in background..."

        level = record.levelname
        clean_msg = re.sub(r"^\[[^\]]+\]\s*", "", msg)
        return f"{time_str}  [{level}]  {clean_msg}"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self._format_clean_log(record)
            if not msg:
                return
            with self._lock:
                self.buffer.append(msg)
        except Exception:
            pass

    def get_lines(self, tail: int = 200) -> list[str]:
        with self._lock:
            items = list(self.buffer)
        return items[-tail:] if tail > 0 else items

    def clear(self) -> None:
        with self._lock:
            self.buffer.clear()


GLOBAL_LOG_HANDLER = CircularLogHandler(capacity=1500)


def start_local_api_server(
    coordinator: Any,
    host: str = "127.0.0.1",
    port: int = 8767,
) -> Optional[ThreadingHTTPServer]:
    """Start a lightweight background HTTP JSON server on `127.0.0.1:8767` for the WinUI 3 client."""

    class ApiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            with contextlib.suppress(Exception):
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, dict):
                    return parsed
            return {}

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            if path == "/api/status":
                self._send_json(200, coordinator.get_status_snapshot())
                return

            if path == "/api/history":
                rev, items = coordinator.history.list_items()
                self._send_json(200, {"revision": rev, "items": items})
                return

            if path == "/api/logs":
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                tail = int(query.get("tail", ["300"])[0])
                lines = GLOBAL_LOG_HANDLER.get_lines(tail)
                self._send_json(200, {"ok": True, "count": len(lines), "logs": lines})
                return

            m_img = re.match(r"^/api/history/([^/]+)/image$", path)
            if m_img:
                item_id = m_img.group(1)
                img_bytes = coordinator.history.get_image_bytes(item_id)
                if not img_bytes:
                    self._send_json(404, {"ok": False, "error": "Image not found"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(img_bytes)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(img_bytes)
                return

            self._send_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            body = self._read_json_body()

            if path == "/api/sync-now":
                triggered = coordinator.trigger_manual_sync()
                self._send_json(200, {"ok": triggered})
                return

            if path == "/api/transport":
                mode = str(body.get("mode", "auto"))
                try:
                    coordinator.set_transport_mode(mode)
                    self._send_json(200, {"ok": True, "transport_mode": coordinator.transport_mode})
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if path == "/api/pair-wifi":
                if not coordinator.loop:
                    self._send_json(503, {"ok": False, "error": "Event loop not ready"})
                    return
                try:
                    import asyncio
                    fut = asyncio.run_coroutine_threadsafe(
                        pair_device_for_wifi_over_usb(coordinator.requested_udid),
                        coordinator.loop,
                    )
                    paired_udid = fut.result(timeout=25.0)
                    coordinator.last_wifi_udids = list_wifi_paired_udids()
                    self._send_json(200, {"ok": True, "udid": paired_udid})
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if path == "/api/logs/clear":
                GLOBAL_LOG_HANDLER.clear()
                self._send_json(200, {"ok": True})
                return

            if path == "/api/history/clear":
                include_pinned = bool(body.get("include_pinned", False))
                removed = coordinator.history.clear(include_pinned=include_pinned)
                self._send_json(200, {"ok": True, "removed": removed, "revision": coordinator.history.revision})
                return

            m_action = re.match(r"^/api/history/([^/]+)/(copy|send|pin|delete)$", path)
            if m_action:
                item_id, action = m_action.group(1), m_action.group(2)
                if action == "copy":
                    try:
                        formats = coordinator.copy_history_item_to_windows(item_id)
                        self._send_json(200, {"ok": True, "formats": formats})
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                if action == "send":
                    try:
                        ok = coordinator.send_history_item_to_iphone(item_id)
                        self._send_json(200, {"ok": ok})
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                if action == "pin":
                    pinned = coordinator.history.toggle_pin(item_id)
                    if pinned is None:
                        self._send_json(404, {"ok": False, "error": "Item not found"})
                    else:
                        self._send_json(200, {"ok": True, "pinned": pinned, "revision": coordinator.history.revision})
                    return
                if action == "delete":
                    deleted = coordinator.history.delete_item(item_id)
                    self._send_json(200, {"ok": deleted, "revision": coordinator.history.revision})
                    return

            self._send_json(404, {"ok": False, "error": "Not found"})

    try:
        server = ThreadingHTTPServer((host, port), ApiHandler)
        thread = threading.Thread(target=server.serve_forever, name="clipboard-api", daemon=True)
        thread.start()
        logger.info("[API] Local WinUI 3 backend server listening on http://%s:%d", host, port)
        return server
    except OSError as exc:
        logger.warning(
            "[API] Another Universal Clipboard daemon is already listening on %s:%d (%s); exiting duplicate instance.",
            host,
            port,
            exc,
        )
        os._exit(0)
