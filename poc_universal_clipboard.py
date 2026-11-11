#!/usr/bin/env python3
"""
Standalone Proof-of-Concept: Universal Clipboard for Windows (iPhone -> Windows)
Built on Apple CoreDevice / RemoteXPC infrastructure via pymobiledevice3.

Architecture:
1. Connects to a paired iPhone over USB (CoreDeviceTunnelProxy) or Wi-Fi (_remotepairing._tcp.local.)
   using root-free in-process UserspaceRsdTunnel (pmd-pytcp).
2. Ensures the Personalized Developer Disk Image (Cryptex1 / DDI) is mounted so that
   com.apple.coredevice.pasteboardservice is available.
3. Subscribes to Darwin notification `com.apple.pasteboard.notify.changed` via
   `com.apple.mobile.notification_proxy.shim.remote` (NotificationProxyService).
4. On each notification, pulls the pasteboard snapshot from `com.apple.coredevice.pasteboardservice`
   using `POLICY_PROMISE_SECONDARY` (`{"promiseSecondary": {}}`) to avoid the 64-second Apple Notes
   `public.rtf` conversion stall.
5. Extracts UTF-8 plain text, URLs, rich HTML (`public.html` / `com.apple.webarchive`), and images
   (`public.png`, `public.jpeg`, `public.heic`, `public.tiff`, `com.compuserve.gif`, `org.webmproject.webp`),
   and resolves promised secondary representations via `RESOLVE` when necessary.
6. Writes the extracted payload to the native Windows system clipboard (`CF_UNICODETEXT`,
   `HTML Format`, `CF_DIBV5`, `CF_DIB`, and `PNG`) with retry/locking and echo-loop prevention.

Privacy Guarantee:
- Logs connection status, transport type, notification events, changeCount, UTI payload type,
  payload byte size, latency, and clipboard update status.
- NEVER logs actual clipboard contents (text, URLs, or image pixels).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Optional, cast
from urllib.parse import parse_qs, urlparse

import ifaddr
from PIL import Image, ImageGrab, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

from pymobiledevice3 import bonjour, usbmux
from pymobiledevice3.exceptions import (
    AlreadyMountedError,
    PyMobileDevice3Exception,
    RemotePairingCompletedError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.pair_records import iter_remote_paired_identifiers
from pymobiledevice3.remote import tunnel_service, userspace_tunnel
from pymobiledevice3.remote.core_device.pasteboard_service import (
    GENERAL_PASTEBOARD,
    IMAGE_UTIS,
    PASTEBOARD_CHANGED_NOTIFICATION,
    POLICY_ALL_PROMISED,
    POLICY_ALL_RESOLVED,
    POLICY_PROMISE_SECONDARY,
    RESOLVE_COMMAND,
    SET_COMMAND,
    UTI_PLAIN_TEXT,
    UTI_TEXT,
    UTI_URL,
    UTI_UTF8_PLAIN_TEXT,
    UTI_WEB_ARCHIVE,
    PasteboardService,
    data_item,
    text_item,
)
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
from pymobiledevice3.remote.xpc_message import XpcInt64Type
from pymobiledevice3.services.mobile_image_mounter import auto_mount
from pymobiledevice3.services.notification_proxy import NotificationProxyService

# ---------------------------------------------------------------------------
# Logging Configuration (Privacy-Safe: Never logs clipboard contents)
# ---------------------------------------------------------------------------

logger = logging.getLogger("universal_clipboard")

PASTEBOARD_CHANGED_NOTIFICATION = "com.apple.pasteboard.notify.changed"
SPRINGBOARD_LOCKSTATE_NOTIFICATION = "com.apple.springboard.lockstate"
KEYBAG_LOCK_STATUS_NOTIFICATION = "com.apple.mobile.keybagd.lock_status"

UTI_HTML = "public.html"
UTI_RTF = "public.rtf"
UTI_FLAT_RTFD = "com.apple.flat-rtfd"
TEXT_UTIS = (UTI_UTF8_PLAIN_TEXT, UTI_PLAIN_TEXT, UTI_TEXT)
URL_UTIS = (
    UTI_URL,
    "public.uri-list",
    "public.url-name",
    "com.apple.cocoa.pasteboard.url",
    "com.apple.pasteboard.promised-url",
)
URL_REGEX = re.compile(r"^(https?://|mailto:|ftp://)\S+$", re.IGNORECASE)

_BONJOUR_PATCHED = False
_LAST_KNOWN_WIFI_IPS: set[str] = set()


# ---------------------------------------------------------------------------
# Windows Multi-Adapter & Unicast-Safe mDNS Patch for Wi-Fi RemotePairing
# ---------------------------------------------------------------------------


def _get_local_ipv4_addresses() -> list[str]:
    """Return all active non-loopback, non-APIPA IPv4 addresses on Windows."""
    ips: list[str] = []
    try:
        for adapter in ifaddr.get_adapters():
            for ip_obj in adapter.ips:
                if isinstance(ip_obj.ip, str) and not ip_obj.ip.startswith(("127.", "169.254.")):
                    ips.append(ip_obj.ip)
    except Exception:
        pass
    return ips


def _get_arp_neighbor_ipv4s() -> list[str]:
    """Return reachable/stale unicast IPv4 neighbors from the Windows ARP table."""
    ips: list[str] = list(_LAST_KNOWN_WIFI_IPS)
    try:
        output = subprocess.check_output(
            ["arp", "-a"],
            text=True,
            timeout=2.5,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for match in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{17})\s+(dynamic|static)", output):
            ip = match.group(1)
            if not ip.startswith(("127.", "169.254.", "224.", "239.", "255.")) and not ip.endswith(".255"):
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips


def patch_windows_bonjour() -> None:
    """Fix three Windows mDNS issues in pymobiledevice3.bonjour:

    1. ``Address.full_ip`` appends ``%<adapter.nice_name>`` to IPv6 ``fe80::`` addresses.
       On Windows, ``ifaddr`` sets ``nice_name`` to a 65+ character human-readable description,
       causing ``socket.getaddrinfo`` to fail with ``UnicodeEncodeError: 'idna' codec can't encode
       characters: label too long``. We restrict ``%iface`` on Windows to numeric scope IDs and
       prefer IPv4 addresses first.
    2. ``AppleMobileDeviceLauncher`` binds specifically to ``<LAN_IP>:5353`` on Windows, intercepting
       unicast mDNS replies sent to port 5353 before ``0.0.0.0:5353`` can see them. We open an
       additional ephemeral-port UDP socket per local IPv4 interface and send ``CLASS_QU``
       (unicast-response) queries.
    3. Many Wi-Fi routers drop or isolate multicast ``224.0.0.251`` frames between wireless stations.
       We additionally send ``CLASS_QU`` mDNS queries directly to ARP-table neighbor IPv4 addresses.
    """
    global _BONJOUR_PATCHED
    if _BONJOUR_PATCHED:
        return

    @property  # type: ignore[misc]
    def _safe_full_ip(self: bonjour.Address) -> str:
        if self.iface and self.ip.lower().startswith("fe80:") and str(self.iface).isdigit():
            return f"{self.ip}%{self.iface}"
        return self.ip

    bonjour.Address.full_ip = _safe_full_ip  # type: ignore[assignment]

    orig_assemble = bonjour._assemble_instances

    def _assemble_ipv4_preferred(state: Any) -> list[bonjour.ServiceInstance]:
        instances = orig_assemble(state)
        for inst in instances:
            for addr in inst.addresses:
                if ":" not in addr.ip and not addr.ip.startswith(("127.", "169.254.")):
                    _LAST_KNOWN_WIFI_IPS.add(addr.ip)
            inst.addresses.sort(key=lambda a: (":" in a.ip, a.ip.lower().startswith("fe80:")))
        return instances

    bonjour._assemble_instances = _assemble_ipv4_preferred

    orig_open = bonjour._open_mdns_sockets
    orig_send = bonjour._send_query_all

    async def _open_mdns_sockets_enhanced():
        transports, queue = await orig_open()
        loop = asyncio.get_running_loop()
        for ip in _get_local_ipv4_addresses():
            # Bind ephemeral UDP socket on each LAN IPv4 so QU unicast replies bypass port 5353
            with contextlib.suppress(OSError):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((ip, 0))
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: bonjour._DatagramProtocol(queue), sock=sock
                )
                transports.append((transport, sock))
        return transports, queue

    async def _send_query_all_enhanced(transports, pkt: bytes, skip_interfaces: tuple[str, ...] = ()) -> int:
        sent = await orig_send(transports, pkt, skip_interfaces)
        if len(pkt) >= 16:
            qclass = struct.unpack("!H", pkt[-2:])[0] | bonjour.CLASS_QU
            qu_pkt = pkt[:-2] + struct.pack("!H", qclass)
        else:
            qu_pkt = pkt

        neighbor_ips = _get_arp_neighbor_ipv4s()
        for _, sock in transports:
            if sock.family != socket.AF_INET:
                continue
            local_ip = sock.getsockname()[0]
            if local_ip == "0.0.0.0":
                for ip in _get_local_ipv4_addresses():
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                        sock.sendto(pkt, (bonjour.MDNS_MCAST_V4, bonjour.MDNS_PORT))
                        sent += 1
            else:
                with contextlib.suppress(OSError):
                    sock.sendto(qu_pkt, (bonjour.MDNS_MCAST_V4, bonjour.MDNS_PORT))
                    sent += 1
                for peer_ip in neighbor_ips:
                    with contextlib.suppress(OSError):
                        sock.sendto(qu_pkt, (peer_ip, bonjour.MDNS_PORT))
                        sent += 1
        return sent

    bonjour._open_mdns_sockets = _open_mdns_sockets_enhanced
    bonjour._send_query_all = _send_query_all_enhanced
    _BONJOUR_PATCHED = True


# ---------------------------------------------------------------------------
# HTML Text Extractor & CF_HTML Formatter
# ---------------------------------------------------------------------------


class _AppleHTMLTextExtractor(HTMLParser):
    """Extract clean Unicode text from Apple `public.html` pasteboard representations."""

    _BLOCK_TAGS = frozenset({
        "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "section", "article"
    })
    _IGNORE_TAGS = frozenset({"head", "style", "script", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self._IGNORE_TAGS:
            self._ignore_depth += 1
        elif self._ignore_depth == 0:
            if t == "br":
                self._parts.append("\n")
            elif t in self._BLOCK_TAGS and self._parts and not self._parts[-1].endswith("\n"):
                self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._IGNORE_TAGS:
            self._ignore_depth = max(0, self._ignore_depth - 1)
        elif self._ignore_depth == 0 and t in self._BLOCK_TAGS:
            if self._parts and not self._parts[-1].endswith("\n"):
                self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Normalize single/multiple newlines cleanly while preserving intentional line breaks
        lines = [line.strip("\r") for line in raw.splitlines()]
        return "\n".join(lines).strip("\r\n\ufffc")


def extract_text_from_html(html_bytes: bytes) -> Optional[str]:
    """Decode an HTML byte buffer and extract its visible Unicode text."""
    try:
        html_str = html_bytes.decode("utf-8", errors="replace")
        parser = _AppleHTMLTextExtractor()
        parser.feed(html_str)
        text = parser.get_text()
        return text if text else None
    except Exception:
        return None


def build_cf_html_payload(html_fragment: str, source_url: Optional[str] = None) -> bytes:
    """Build a Windows ``HTML Format`` (CF_HTML v0.9) byte buffer with exact byte offsets."""
    start_marker = "<!--StartFragment-->"
    end_marker = "<!--EndFragment-->"
    if start_marker in html_fragment and end_marker in html_fragment:
        body_html = html_fragment
    else:
        body_html = f"<html>\r\n<body>\r\n{start_marker}{html_fragment}{end_marker}\r\n</body>\r\n</html>"

    url_line = f"SourceURL:{source_url}\r\n" if source_url else ""
    header_template = (
        "Version:0.9\r\n"
        "StartHTML:{start_html:010d}\r\n"
        "EndHTML:{end_html:010d}\r\n"
        "StartFragment:{start_frag:010d}\r\n"
        "EndFragment:{end_frag:010d}\r\n"
        + url_line
    )
    dummy_header = header_template.format(start_html=0, end_html=0, start_frag=0, end_frag=0).encode("utf-8")
    body_bytes = body_html.encode("utf-8")

    start_html = len(dummy_header)
    end_html = start_html + len(body_bytes)
    start_frag_idx = body_bytes.find(start_marker.encode("utf-8"))
    end_frag_idx = body_bytes.find(end_marker.encode("utf-8"))
    start_frag = start_html + (start_frag_idx + len(start_marker) if start_frag_idx >= 0 else 0)
    end_frag = start_html + (end_frag_idx if end_frag_idx >= 0 else len(body_bytes))

    final_header = header_template.format(
        start_html=start_html,
        end_html=end_html,
        start_frag=start_frag,
        end_frag=end_frag,
    ).encode("utf-8")
    return final_header + body_bytes + b"\x00"


# ---------------------------------------------------------------------------
# Phase 3: Native Win32 System Clipboard Writer (Text, URL, HTML, Images)
# ---------------------------------------------------------------------------

CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002

IMAGE_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".heic", ".heif", ".tif", ".tiff"
}
TEXT_FILE_EXTENSIONS: set[str] = {
    ".txt", ".md", ".json", ".py", ".cs", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm",
    ".css", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".log", ".sql",
    ".sh", ".bat", ".ps1", ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs",
}
FILE_EXT_TO_UTI: dict[str, str] = {
    ".pdf": "com.adobe.pdf",
    ".zip": "public.zip-archive",
    ".rtf": "public.rtf",
    ".html": "public.html",
    ".htm": "public.html",
    ".mp4": "public.mpeg-4",
    ".m4v": "public.mpeg-4",
    ".mov": "com.apple.quicktime-movie",
    ".mp3": "public.mp3",
    ".wav": "com.microsoft.waveform-audio",
    ".doc": "com.microsoft.word.doc",
    ".docx": "org.openxmlformats.wordprocessingml.document",
    ".xls": "com.microsoft.excel.xls",
    ".xlsx": "org.openxmlformats.spreadsheetml.sheet",
    ".ppt": "com.microsoft.powerpoint.ppt",
    ".pptx": "org.openxmlformats.presentationml.presentation",
    ".json": "public.json",
    ".xml": "public.xml",
    ".csv": "public.comma-separated-values-text",
}
UTI_TO_FILE_EXT: dict[str, str] = {
    "com.adobe.pdf": ".pdf",
    "public.zip-archive": ".zip",
    "com.pkware.zip-archive": ".zip",
    "public.mpeg-4": ".mp4",
    "com.apple.quicktime-movie": ".mov",
    "public.mp3": ".mp3",
    "com.microsoft.waveform-audio": ".wav",
    "com.microsoft.word.doc": ".doc",
    "org.openxmlformats.wordprocessingml.document": ".docx",
    "com.microsoft.excel.xls": ".xls",
    "org.openxmlformats.spreadsheetml.sheet": ".xlsx",
    "com.microsoft.powerpoint.ppt": ".ppt",
    "org.openxmlformats.presentationml.presentation": ".pptx",
    "public.json": ".json",
    "public.xml": ".xml",
    "public.comma-separated-values-text": ".csv",
}

BI_RGB = 0
BI_BITFIELDS = 3
LCS_sRGB = 0x73524742
LCS_GM_IMAGES = 0x00000004


class _BITMAPV5HEADER(ctypes.Structure):
    _fields_ = [
        ("bV5Size", wintypes.DWORD),
        ("bV5Width", wintypes.LONG),
        ("bV5Height", wintypes.LONG),
        ("bV5Planes", wintypes.WORD),
        ("bV5BitCount", wintypes.WORD),
        ("bV5Compression", wintypes.DWORD),
        ("bV5SizeImage", wintypes.DWORD),
        ("bV5XPelsPerMeter", wintypes.LONG),
        ("bV5YPelsPerMeter", wintypes.LONG),
        ("bV5ClrUsed", wintypes.DWORD),
        ("bV5ClrImportant", wintypes.DWORD),
        ("bV5RedMask", wintypes.DWORD),
        ("bV5GreenMask", wintypes.DWORD),
        ("bV5BlueMask", wintypes.DWORD),
        ("bV5AlphaMask", wintypes.DWORD),
        ("bV5CSType", wintypes.DWORD),
        ("bV5Endpoints", ctypes.c_byte * 36),
        ("bV5GammaRed", wintypes.DWORD),
        ("bV5GammaGreen", wintypes.DWORD),
        ("bV5GammaBlue", wintypes.DWORD),
        ("bV5Intent", wintypes.DWORD),
        ("bV5ProfileData", wintypes.DWORD),
        ("bV5ProfileSize", wintypes.DWORD),
        ("bV5Reserved", wintypes.DWORD),
    ]


class WindowsClipboardWriter:
    """Dedicated 64-bit safe Win32 Clipboard writer supporting Unicode text, HTML, URLs, and images."""

    def __init__(self, max_retries: int = 15, retry_delay_sec: float = 0.025) -> None:
        self.max_retries = max_retries
        self.retry_delay_sec = retry_delay_sec
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._configure_win32_signatures()
        self.cf_html = self._user32.RegisterClipboardFormatW("HTML Format")
        self.cf_png = self._user32.RegisterClipboardFormatW("PNG")
        self.cf_url_w = self._user32.RegisterClipboardFormatW("UniformResourceLocatorW")
        self.last_sequence_number: int = 0

    def _configure_win32_signatures(self) -> None:
        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.OpenClipboard.restype = wintypes.BOOL

        self._user32.CloseClipboard.argtypes = []
        self._user32.CloseClipboard.restype = wintypes.BOOL

        self._user32.EmptyClipboard.argtypes = []
        self._user32.EmptyClipboard.restype = wintypes.BOOL

        self._user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._user32.SetClipboardData.restype = wintypes.HANDLE

        self._user32.GetClipboardData.argtypes = [wintypes.UINT]
        self._user32.GetClipboardData.restype = wintypes.HANDLE

        self._user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        self._user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

        self._user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        self._user32.RegisterClipboardFormatW.restype = wintypes.UINT

        self._user32.GetClipboardSequenceNumber.argtypes = []
        self._user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HGLOBAL

        self._kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalSize.restype = ctypes.c_size_t

        self._kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalLock.restype = ctypes.c_void_p

        self._kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL

        self._kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalFree.restype = wintypes.HGLOBAL

        self._shell32.DragQueryFileW.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        self._shell32.DragQueryFileW.restype = wintypes.UINT

    def get_sequence_number(self) -> int:
        return int(self._user32.GetClipboardSequenceNumber())

    def _read_hdrop_files(self) -> list[str]:
        """Read copied file paths from Windows `CF_HDROP` clipboard format."""
        if not self._user32.IsClipboardFormatAvailable(CF_HDROP):
            return []
        opened = False
        for _ in range(5):
            if self._user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.015)
        if not opened:
            return []
        paths: list[str] = []
        try:
            handle = self._user32.GetClipboardData(CF_HDROP)
            if handle:
                count = int(self._shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0))
                for idx in range(count):
                    length = int(self._shell32.DragQueryFileW(handle, idx, None, 0))
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        self._shell32.DragQueryFileW(handle, idx, buf, length + 1)
                        if buf.value:
                            paths.append(buf.value)
        except Exception:
            pass
        finally:
            self._user32.CloseClipboard()
        return paths

    def read_payload(self) -> Optional[tuple["ExtractedPayload", Optional[tuple[int, int]]]]:
        """Read current Windows clipboard content (Copied Files via CF_HDROP, Unicode text/URL, or PNG/DIB image)."""
        # 0. Check CF_HDROP (Files copied in Windows File Explorer or Desktop)
        hdrop_paths = self._read_hdrop_files()
        for raw_path in hdrop_paths:
            with contextlib.suppress(Exception):
                p = Path(raw_path)
                if not p.is_file():
                    continue
                fsize = p.stat().st_size
                if fsize <= 0 or fsize > 40 * 1024 * 1024:
                    continue
                ext = p.suffix.lower()
                raw_bytes = p.read_bytes()

                if ext in IMAGE_EXTENSIONS:
                    with Image.open(io.BytesIO(raw_bytes)) as img:
                        img = ImageOps.exif_transpose(img)
                        rgba = img.convert("RGBA")
                        width, height = rgba.size
                        buf = io.BytesIO()
                        rgba.save(buf, format="PNG")
                        png_bytes = buf.getvalue()
                    return (
                        ExtractedPayload(
                            kind="image",
                            uti="public.png",
                            raw_size=len(raw_bytes),
                            image_bytes=png_bytes,
                            file_name=p.name,
                            file_bytes=raw_bytes,
                        ),
                        (width, height),
                    )

                uti = FILE_EXT_TO_UTI.get(
                    ext,
                    UTI_UTF8_PLAIN_TEXT if ext in TEXT_FILE_EXTENSIONS else "public.data",
                )
                preview_text = p.name
                if ext in TEXT_FILE_EXTENSIONS:
                    with contextlib.suppress(UnicodeDecodeError):
                        decoded = raw_bytes.decode("utf-8")
                        if decoded.strip():
                            preview_text = decoded
                return (
                    ExtractedPayload(
                        kind="file",
                        uti=uti,
                        raw_size=len(raw_bytes),
                        text=preview_text,
                        file_name=p.name,
                        file_bytes=raw_bytes,
                    ),
                    None,
                )

        # 1. Check Unicode Text
        if self._user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            opened = False
            for _ in range(5):
                if self._user32.OpenClipboard(None):
                    opened = True
                    break
                time.sleep(0.015)
            if opened:
                try:
                    handle = self._user32.GetClipboardData(CF_UNICODETEXT)
                    if handle:
                        ptr = self._kernel32.GlobalLock(handle)
                        if ptr:
                            try:
                                text = ctypes.wstring_at(ptr)
                            finally:
                                self._kernel32.GlobalUnlock(handle)
                            if text and text.strip():
                                is_url = bool(URL_REGEX.match(text.strip()))
                                uti = UTI_URL if is_url else UTI_UTF8_PLAIN_TEXT
                                raw_bytes = text.encode("utf-8")
                                return (
                                    ExtractedPayload(
                                        kind="url" if is_url else "text",
                                        uti=uti,
                                        raw_size=len(raw_bytes),
                                        text=text,
                                    ),
                                    None,
                                )
                except Exception:
                    pass
                finally:
                    self._user32.CloseClipboard()

        # 2. Check Image (PNG or DIB via ImageGrab)
        if (
            (self.cf_png and self._user32.IsClipboardFormatAvailable(self.cf_png))
            or self._user32.IsClipboardFormatAvailable(CF_DIB)
            or self._user32.IsClipboardFormatAvailable(CF_DIBV5)
        ):
            with contextlib.suppress(Exception):
                grabbed = ImageGrab.grabclipboard()
                if isinstance(grabbed, Image.Image):
                    rgba = grabbed.convert("RGBA")
                    width, height = rgba.size
                    buf = io.BytesIO()
                    rgba.save(buf, format="PNG")
                    png_bytes = buf.getvalue()
                    return (
                        ExtractedPayload(
                            kind="image",
                            uti="public.png",
                            raw_size=len(png_bytes),
                            image_bytes=png_bytes,
                        ),
                        (width, height),
                    )
        return None

    @contextlib.contextmanager
    def _open_clipboard_locked(self):
        """Acquire the Win32 clipboard lock with backoff retries if another app holds it."""
        opened = False
        last_err = 0
        for attempt in range(self.max_retries):
            if self._user32.OpenClipboard(None):
                opened = True
                break
            last_err = ctypes.get_last_error()
            time.sleep(self.retry_delay_sec * (1.0 + 0.25 * attempt))
        if not opened:
            raise RuntimeError(f"OpenClipboard failed after {self.max_retries} attempts (WinError={last_err})")
        try:
            yield
        finally:
            self._user32.CloseClipboard()
            self.last_sequence_number = int(self._user32.GetClipboardSequenceNumber())

    def _set_raw_format(self, fmt: int, payload: bytes) -> None:
        """Allocate moveable global memory, copy `payload`, and hand ownership to SetClipboardData."""
        hmem = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not hmem:
            raise MemoryError(f"GlobalAlloc({len(payload)}) failed (WinError={ctypes.get_last_error()})")
        locked_ptr = self._kernel32.GlobalLock(hmem)
        if not locked_ptr:
            self._kernel32.GlobalFree(hmem)
            raise MemoryError(f"GlobalLock failed (WinError={ctypes.get_last_error()})")
        try:
            ctypes.memmove(locked_ptr, payload, len(payload))
        finally:
            self._kernel32.GlobalUnlock(hmem)

        if not self._user32.SetClipboardData(fmt, hmem):
            err = ctypes.get_last_error()
            self._kernel32.GlobalFree(hmem)
            raise RuntimeError(f"SetClipboardData(format={fmt}) failed (WinError={err})")

    def write_text(
        self,
        text: str,
        *,
        html_bytes: Optional[bytes] = None,
        is_url: bool = False,
    ) -> list[str]:
        """Write UTF-16 text (`CF_UNICODETEXT`) plus optional `HTML Format` and `UniformResourceLocatorW`."""
        # Normalize line endings to CRLF for standard Windows notepad/editors
        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        utf16_bytes = normalized_text.encode("utf-16-le") + b"\x00\x00"

        cf_html_data: Optional[bytes] = None
        if html_bytes:
            try:
                html_str = html_bytes.decode("utf-8", errors="replace")
                cf_html_data = build_cf_html_payload(html_str, source_url=text if is_url else None)
            except Exception:
                cf_html_data = None
        elif is_url:
            escaped_url = text.replace("&", "&amp;").replace('"', "&quot;")
            cf_html_data = build_cf_html_payload(f'<a href="{escaped_url}">{escaped_url}</a>', source_url=text)

        formats_written: list[str] = []
        with self._open_clipboard_locked():
            if not self._user32.EmptyClipboard():
                raise RuntimeError(f"EmptyClipboard failed (WinError={ctypes.get_last_error()})")

            self._set_raw_format(CF_UNICODETEXT, utf16_bytes)
            formats_written.append("CF_UNICODETEXT")

            if cf_html_data and self.cf_html:
                with contextlib.suppress(Exception):
                    self._set_raw_format(self.cf_html, cf_html_data)
                    formats_written.append("HTML Format")

            if is_url and self.cf_url_w:
                with contextlib.suppress(Exception):
                    self._set_raw_format(self.cf_url_w, utf16_bytes)
                    formats_written.append("UniformResourceLocatorW")

        return formats_written

    @staticmethod
    def _encode_dibv5_and_dib_and_png(image_bytes: bytes) -> tuple[bytes, bytes, bytes, tuple[int, int]]:
        """Decode PNG/JPEG/HEIC/TIFF/GIF/WebP into `(dibv5_bytes, dib_bytes, png_bytes, (width, height))`."""
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            rgba = img.convert("RGBA")
            width, height = rgba.size

            # 1. Lossless PNG buffer for modern apps (browsers, Discord, Notion, Word)
            png_io = io.BytesIO()
            rgba.save(png_io, format="PNG")
            png_bytes = png_io.getvalue()

            # 2. Classic BMP / CF_DIB buffer (24-bit or 32-bit BMP without the 14-byte BITMAPFILEHEADER)
            bmp_io = io.BytesIO()
            rgba.convert("RGB").save(bmp_io, format="BMP")
            dib_bytes = bmp_io.getvalue()[14:]

            # 3. 32-bit BGRA bottom-up CF_DIBV5 buffer with BITMAPV5HEADER
            # Flip vertically for standard bottom-up Windows DIB orientation (positive height)
            flipped = rgba.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            bgra_bytes = flipped.tobytes("raw", "BGRA")

            hdr = _BITMAPV5HEADER()
            hdr.bV5Size = ctypes.sizeof(_BITMAPV5HEADER)
            hdr.bV5Width = width
            hdr.bV5Height = height
            hdr.bV5Planes = 1
            hdr.bV5BitCount = 32
            hdr.bV5Compression = BI_BITFIELDS
            hdr.bV5SizeImage = len(bgra_bytes)
            hdr.bV5XPelsPerMeter = 3780  # 96 DPI
            hdr.bV5YPelsPerMeter = 3780
            hdr.bV5ClrUsed = 0
            hdr.bV5ClrImportant = 0
            hdr.bV5RedMask = 0x00FF0000
            hdr.bV5GreenMask = 0x0000FF00
            hdr.bV5BlueMask = 0x000000FF
            hdr.bV5AlphaMask = 0xFF000000
            hdr.bV5CSType = LCS_sRGB
            hdr.bV5Intent = LCS_GM_IMAGES

            dibv5_bytes = ctypes.string_at(ctypes.byref(hdr), ctypes.sizeof(hdr)) + bgra_bytes
            return dibv5_bytes, dib_bytes, png_bytes, (width, height)

    @staticmethod
    def _build_hdrop_payload(file_paths: list[str]) -> bytes:
        """Build a Win32 `DROPFILES` struct (20-byte header + UTF-16LE null-separated paths + double null)."""
        header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
        body = b"".join(os.path.abspath(p).encode("utf-16-le") + b"\x00\x00" for p in file_paths) + b"\x00\x00"
        return header + body

    def write_file(self, file_path: str, text_fallback: Optional[str] = None) -> list[str]:
        """Write `CF_HDROP` to the Windows clipboard so the file can be pasted into File Explorer (Ctrl+V)."""
        hdrop_bytes = self._build_hdrop_payload([file_path])
        formats_written: list[str] = []
        with self._open_clipboard_locked():
            if not self._user32.EmptyClipboard():
                raise RuntimeError(f"EmptyClipboard failed (WinError={ctypes.get_last_error()})")
            self._set_raw_format(CF_HDROP, hdrop_bytes)
            formats_written.append("CF_HDROP")
            if text_fallback:
                utf16_bytes = text_fallback.encode("utf-16-le") + b"\x00\x00"
                with contextlib.suppress(Exception):
                    self._set_raw_format(CF_UNICODETEXT, utf16_bytes)
                    formats_written.append("CF_UNICODETEXT")
        return formats_written

    def write_image(
        self,
        image_bytes: bytes,
        uti: str,
        file_path: Optional[str] = None,
    ) -> tuple[list[str], tuple[int, int]]:
        """Decode image (PNG, JPEG, HEIC, TIFF, GIF, WebP) and write `CF_DIBV5`, `CF_DIB`, `PNG`, and optional `CF_HDROP`."""
        if uti == "public.heic" and not HEIF_AVAILABLE:
            raise RuntimeError("HEIC image received but pillow-heif is not installed")

        dibv5_bytes, dib_bytes, png_bytes, dimensions = self._encode_dibv5_and_dib_and_png(image_bytes)
        formats_written: list[str] = []

        with self._open_clipboard_locked():
            if not self._user32.EmptyClipboard():
                raise RuntimeError(f"EmptyClipboard failed (WinError={ctypes.get_last_error()})")

            self._set_raw_format(CF_DIBV5, dibv5_bytes)
            formats_written.append("CF_DIBV5")

            self._set_raw_format(CF_DIB, dib_bytes)
            formats_written.append("CF_DIB")

            if self.cf_png:
                self._set_raw_format(self.cf_png, png_bytes)
                formats_written.append("PNG")

            if file_path and os.path.isfile(file_path):
                with contextlib.suppress(Exception):
                    self._set_raw_format(CF_HDROP, self._build_hdrop_payload([file_path]))
                    formats_written.append("CF_HDROP")

        return formats_written, dimensions


# ---------------------------------------------------------------------------
# Extracted Pasteboard Payload Model & Smart Extraction / Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedPayload:
    kind: str  # "text", "url", "image", "file"
    uti: str
    raw_size: int
    text: Optional[str] = None
    html_bytes: Optional[bytes] = None
    image_bytes: Optional[bytes] = None
    file_name: Optional[str] = None
    file_bytes: Optional[bytes] = None

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(self.kind.encode("utf-8"))
        if self.image_bytes is not None:
            h.update(self.image_bytes)
        elif self.file_bytes is not None:
            h.update(self.file_bytes)
        elif self.text is not None:
            norm = self.text.replace("\r\n", "\n").replace("\r", "\n").strip()
            h.update(norm.encode("utf-8"))
        return h.hexdigest()


def _inline_bytes(data_map: dict[str, Any], uti: str) -> Optional[bytes]:
    datum = data_map.get(uti)
    if isinstance(datum, dict):
        raw = datum.get("data")
        if isinstance(raw, (bytes, bytearray)) and raw:
            return bytes(raw)
        if isinstance(raw, str) and raw:
            return raw.encode("utf-8")
    return None


def _find_url_in_plist_object(obj: Any) -> Optional[str]:
    """Recursively search a parsed plist object or Cocoa NSKeyedArchiver for a valid URL string."""
    if isinstance(obj, str):
        cleaned = obj.strip("\x00\ufffc\r\n\t ")
        if cleaned.startswith(("http://", "https://", "ftp://", "mailto:")) or URL_REGEX.match(cleaned):
            return cleaned
        return None
    if isinstance(obj, dict):
        # Specific known Cocoa NSURL archive keys
        for key in ("NS.relative", "relative", "URL", "url", "href", "urlString"):
            val = obj.get(key)
            if isinstance(val, str):
                cleaned = val.strip("\x00\ufffc\r\n\t ")
                if cleaned.startswith(("http://", "https://", "ftp://", "mailto:")) or URL_REGEX.match(cleaned):
                    return cleaned
        # Check NSKeyedArchiver $objects array
        objects = obj.get("$objects")
        if isinstance(objects, list):
            for item in objects:
                res = _find_url_in_plist_object(item)
                if res:
                    return res
        for val in obj.values():
            res = _find_url_in_plist_object(val)
            if res:
                return res
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            res = _find_url_in_plist_object(item)
            if res:
                return res
    return None


def _extract_url_from_raw(raw: bytes) -> Optional[str]:
    """Extract a valid URL string from raw bytes (binary plist, XML plist, UTF-8 text, or binary stream)."""
    if not raw:
        return None

    # Case 1: Binary property list (bplist00) or XML plist (iOS Share Sheet / UIPasteboard NSURL)
    if raw.startswith(b"bplist00") or raw.startswith(b"<?xml") or b"<!DOCTYPE plist" in raw[:120]:
        try:
            parsed = plistlib.loads(raw)
            url = _find_url_in_plist_object(parsed)
            if url:
                return url
        except Exception:
            pass

    # Case 2: Plain text UTF-8 / ASCII string
    try:
        decoded = raw.decode("utf-8").strip("\x00\ufffc\r\n\t ")
        if decoded and (decoded.startswith(("http://", "https://", "ftp://", "mailto:")) or URL_REGEX.match(decoded)):
            return decoded
    except UnicodeDecodeError:
        pass

    # Case 3: Binary regex fallback for URLs embedded inside binary serialized structures
    match = re.search(rb"https?://[a-zA-Z0-9\-\._~:/\?#\[\]@!$&'()*+,;=%]+", raw)
    if match:
        try:
            candidate = match.group(0).decode("utf-8", errors="replace").strip()
            if candidate and URL_REGEX.match(candidate):
                return candidate
        except Exception:
            pass

    return None


def _extract_from_webarchive(archive_bytes: bytes) -> tuple[Optional[tuple[str, bytes]], Optional[str], Optional[bytes]]:
    """Extract `(image_uti_and_bytes, text, html_bytes)` from a `com.apple.webarchive` plist."""
    try:
        plist = plistlib.loads(archive_bytes)
    except Exception:
        return None, None, None

    uti_by_mime = {mime: uti for uti, mime in IMAGE_UTIS.items()}
    for resource in plist.get("WebSubresources", []) or []:
        if not isinstance(resource, dict):
            continue
        mime = resource.get("WebResourceMIMEType")
        data = resource.get("WebResourceData")
        uti = uti_by_mime.get(mime)
        if uti and isinstance(data, (bytes, bytearray)) and data:
            return (uti, bytes(data)), None, None

    main_res = plist.get("WebMainResource")
    if isinstance(main_res, dict):
        mime = main_res.get("WebResourceMIMEType", "")
        data = main_res.get("WebResourceData")
        if isinstance(data, (bytes, bytearray)) and data:
            raw = bytes(data)
            if mime in uti_by_mime:
                return (uti_by_mime[mime], raw), None, None
            if "html" in mime.lower():
                text = extract_text_from_html(raw)
                return None, text, raw
    return None, None, None


def extract_inline_payload(snapshot: dict[str, Any]) -> Optional[ExtractedPayload]:
    """Extract text, URL, HTML, or image from the inline fields of a PasteboardSnapshot."""
    pb = snapshot.get("pasteboard")
    if isinstance(pb, dict):
        snapshot = pb

    items = cast(list[dict[str, Any]], snapshot.get("items", []) or [])
    if not items:
        return None

    # Pass 1: Look for inline image representations (Photos, screenshots, Safari Copy Image)
    for item in items:
        data_map = cast(dict[str, Any], item.get("data") or {})
        for img_uti in IMAGE_UTIS:
            img_data = _inline_bytes(data_map, img_uti)
            if img_data is not None:
                return ExtractedPayload(
                    kind="image",
                    uti=img_uti,
                    raw_size=len(img_data),
                    image_bytes=img_data,
                )

    # Pass 2: Look for inline plain text or URL representations
    for item in items:
        data_map = cast(dict[str, Any], item.get("data") or {})
        html_inline = _inline_bytes(data_map, UTI_HTML)

        # 2a. Priority check: Check URL UTIs (Safari Share Sheet 'Sao chép', Safari address bar, etc.)
        for u_uti in URL_UTIS:
            u_bytes = _inline_bytes(data_map, u_uti)
            if u_bytes is not None:
                extracted_url = _extract_url_from_raw(u_bytes)
                if extracted_url:
                    return ExtractedPayload(
                        kind="url",
                        uti=u_uti,
                        raw_size=len(u_bytes),
                        text=extracted_url,
                        html_bytes=html_inline,
                    )

        # 2b. Check plain text UTIs
        for txt_uti in TEXT_UTIS:
            txt_bytes = _inline_bytes(data_map, txt_uti)
            if txt_bytes is not None:
                try:
                    text = txt_bytes.decode("utf-8").strip("\ufffc")
                except UnicodeDecodeError:
                    continue
                if not text.strip():
                    continue
                is_url = bool(URL_REGEX.match(text.strip()))
                return ExtractedPayload(
                    kind="url" if is_url else "text",
                    uti=UTI_URL if is_url else txt_uti,
                    raw_size=len(txt_bytes),
                    text=text,
                    html_bytes=html_inline,
                )

    # Pass 3: Look for inline HTML (`public.html`) or `com.apple.webarchive`
    # Safari and Apple Notes often place `public.html` as the primary (first) representation and
    # promise `public.utf8-plain-text` behind `public.rtf`. Extracting directly from the inline
    # `public.html` avoids the 64-second Notes `public.rtf` daemon timeout while preserving full
    # Unicode and rich HTML formatting!
    for item in items:
        data_map = cast(dict[str, Any], item.get("data") or {})
        html_bytes = _inline_bytes(data_map, UTI_HTML)
        if html_bytes is not None:
            text = extract_text_from_html(html_bytes)
            if text:
                is_url = bool(URL_REGEX.match(text.strip()))
                return ExtractedPayload(
                    kind="url" if is_url else "text",
                    uti=UTI_HTML,
                    raw_size=len(html_bytes),
                    text=text,
                    html_bytes=html_bytes,
                )

        archive_bytes = _inline_bytes(data_map, UTI_WEB_ARCHIVE)
        if archive_bytes is not None:
            img_pair, arch_text, arch_html = _extract_from_webarchive(archive_bytes)
            if img_pair is not None:
                return ExtractedPayload(
                    kind="image",
                    uti=f"{UTI_WEB_ARCHIVE}:{img_pair[0]}",
                    raw_size=len(img_pair[1]),
                    image_bytes=img_pair[1],
                )
            if arch_text:
                return ExtractedPayload(
                    kind="text",
                    uti=UTI_WEB_ARCHIVE,
                    raw_size=len(archive_bytes),
                    text=arch_text,
                    html_bytes=arch_html,
                )

    # Pass 4: Look for inline file representations (PDF, ZIP, Office docs, audio/video from iOS Files)
    for item in items:
        data_map = cast(dict[str, Any], item.get("data") or {})
        for file_uti, ext in UTI_TO_FILE_EXT.items():
            f_bytes = _inline_bytes(data_map, file_uti)
            if f_bytes is not None:
                fname = f"iPhone_File_{int(time.time())}{ext}"
                return ExtractedPayload(
                    kind="file",
                    uti=file_uti,
                    raw_size=len(f_bytes),
                    text=fname,
                    file_name=fname,
                    file_bytes=f_bytes,
                )

    return None


def find_resolvable_Secondary_representation(
    snapshot: dict[str, Any],
    allow_behind_rtf: bool = False,
) -> Optional[tuple[int, str, bool]]:
    """Locate a promised secondary representation `(item_index, uti, behind_rtf)` to resolve via `RESOLVE`.

    Note: The iOS pasteboard daemon resolves representations in the order listed in `item["types"]`.
    If `public.rtf` precedes the target UTI (as in Apple Notes), `RESOLVE` can stall for ~64s while
    the daemon waits for `public.rtf` to fail. We detect whether `public.rtf` precedes the target UTI.
    """
    pb = snapshot.get("pasteboard")
    if isinstance(pb, dict):
        snapshot = pb

    items = cast(list[dict[str, Any]], snapshot.get("items", []) or [])
    candidate_utis = (
        *IMAGE_UTIS.keys(),
        *UTI_TO_FILE_EXT.keys(),
        UTI_UTF8_PLAIN_TEXT,
        UTI_PLAIN_TEXT,
        UTI_TEXT,
        *URL_UTIS,
        UTI_HTML,
        UTI_WEB_ARCHIVE,
    )
    for item_idx, item in enumerate(items):
        types = cast(list[str], item.get("types", []) or [])
        data_map = cast(dict[str, Any], item.get("data") or {})
        rtf_idx = types.index(UTI_RTF) if UTI_RTF in types else -1

        for target_uti in candidate_utis:
            if target_uti not in types:
                continue
            if _inline_bytes(data_map, target_uti) is not None:
                continue
            target_idx = types.index(target_uti)
            behind_rtf = rtf_idx != -1 and rtf_idx < target_idx
            if behind_rtf and not allow_behind_rtf:
                continue
            return item_idx, target_uti, behind_rtf

    return None


async def resolve_promised_representation(
    pb_service: PasteboardService,
    item_index: int,
    uti: str,
    timeout: float = 8.0,
) -> Optional[bytes]:
    """Send a `RESOLVE` command to `com.apple.coredevice.pasteboardservice` and return the `DATA` bytes."""
    reply = await asyncio.wait_for(
        pb_service.service.send_receive_request({
            "command": RESOLVE_COMMAND,
            "pasteboardName": GENERAL_PASTEBOARD,
            "itemIndex": XpcInt64Type(item_index),
            "type": uti,
        }),
        timeout=timeout,
    )
    if isinstance(reply, dict) and reply.get("command") == "DATA":
        raw = reply.get("data")
        if isinstance(raw, (bytes, bytearray)) and raw:
            return bytes(raw)
    return None


# ---------------------------------------------------------------------------
# Phase 2 & 4: Tunnel Management (USB & Wi-Fi) + Event-Driven Monitor
# ---------------------------------------------------------------------------


def list_usb_udids() -> list[str]:
    """Return UDIDs of iPhones currently attached over USB."""
    try:
        devices = asyncio.run(usbmux.list_devices())
        return [d.serial for d in devices if getattr(d, "connection_type", "USB") == "USB"]
    except RuntimeError:
        return []
    except Exception:
        return []


async def async_list_usb_udids() -> list[str]:
    """Async helper returning UDIDs of iPhones currently attached over USB."""
    try:
        devices = await usbmux.list_devices()
        return [d.serial for d in devices if getattr(d, "connection_type", "USB") == "USB"]
    except Exception:
        return []


def list_wifi_paired_udids() -> list[str]:
    """Return UDIDs that have a saved RemotePairing record on this Windows PC."""
    try:
        return sorted(set(iter_remote_paired_identifiers()))
    except Exception:
        return []


async def pair_device_for_wifi_over_usb(udid: Optional[str] = None) -> str:
    """Create a RemotePairing record over a trusted USB connection (`com.apple.dt.remotepairingdeviced.lockdown`)."""
    lockdown = await create_using_usbmux(serial=udid, autopair=True)
    resolved_udid = lockdown.udid
    try:
        # Ensure Wi-Fi connections are enabled in wireless_lockdown
        with contextlib.suppress(Exception):
            wifi_cfg = await lockdown.get_value("com.apple.mobile.wireless_lockdown") or {}
            if not wifi_cfg.get("EnableWifiConnections"):
                await lockdown.set_value(True, "com.apple.mobile.wireless_lockdown", "EnableWifiConnections")

        service = await tunnel_service.RemotePairingLockdownService.create(lockdown)
        try:
            await service.connect(autopair=True)
        except RemotePairingCompletedError:
            pass
        finally:
            await service.close()
    finally:
        await lockdown.close()
    return resolved_udid


class WifiRsdTunnel(userspace_tunnel.UserspaceRsdTunnel):
    """Root-free UserspaceRsdTunnel that connects over Wi-Fi (`_remotepairing._tcp.local.`) without USB."""

    def __init__(self, serial: Optional[str] = None, bonjour_timeout: float = 5.0) -> None:
        super().__init__(serial=serial, autopair=False, remotepairing_fallback=True)
        self.bonjour_timeout = bonjour_timeout

    async def _wifi_provider(self, serial: Optional[str], autopair: bool, remotepairing_fallback: bool = True):
        patch_windows_bonjour()
        services = await tunnel_service.get_remote_pairing_tunnel_services(
            udid=serial, bonjour_timeout=self.bonjour_timeout
        )
        if not services:
            raise ConnectionError(
                f"iPhone (UDID={serial or 'any'}) not found on Wi-Fi via _remotepairing._tcp.local."
            )
        for extra in services[1:]:
            await extra.close()
        return services[0], None

    async def _aopen_locked(self) -> RemoteServiceDiscoveryService:
        original = userspace_tunnel._create_no_root_tunnel_provider
        userspace_tunnel._create_no_root_tunnel_provider = self._wifi_provider
        try:
            return await super()._aopen_locked()
        finally:
            userspace_tunnel._create_no_root_tunnel_provider = original


def open_rsd_tunnel(udid: str, transport: str, bonjour_timeout: float = 5.0):
    """Return an async context manager yielding a connected `RemoteServiceDiscoveryService`."""
    if transport == "wifi":
        return WifiRsdTunnel(serial=udid, bonjour_timeout=bonjour_timeout)
    return PreferredRsdTunnel(serial=udid, autopair=True)


def extract_change_info(snapshot: dict[str, Any]) -> tuple[Optional[Any], Optional[int]]:
    """Extract `(nonce, changeCount)` from a pasteboard snapshot."""
    pb = snapshot.get("pasteboard")
    if isinstance(pb, dict):
        meta = pb.get("metadata")
        if isinstance(meta, dict):
            cc = meta.get("changeCount")
            return meta.get("nonce"), (int(cc) if cc is not None else None)
    return None, None


FRIENDLY_MODEL_NAMES: dict[str, str] = {
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,5": "iPhone 16e",
    "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max",
    "iPhone18,3": "iPhone 17",
    "iPhone18,4": "iPhone 17 Air",
}


def friendly_device_name(product_type: Optional[str]) -> str:
    if not product_type:
        return "iPhone"
    friendly = FRIENDLY_MODEL_NAMES.get(product_type)
    return f"{friendly} ({product_type})" if friendly else product_type


class ClipboardHistoryStore:
    """Thread-safe persistent history store for synced iPhone clipboard items (served locally to WinUI 3)."""

    def __init__(self, max_items: int = 80) -> None:
        self.max_items = max_items
        self._lock = threading.Lock()
        self.revision = 1
        app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        self.base_dir = app_data / "UniversalClipboard"
        self.images_dir = self.base_dir / "images"
        self.files_dir = self.base_dir / "files"
        self.history_file = self.base_dir / "history.json"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.history_file.exists():
            return
        with contextlib.suppress(Exception):
            raw = json.loads(self.history_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self._items = [item for item in raw if isinstance(item, dict)][: self.max_items]

    def _save_locked(self) -> None:
        with contextlib.suppress(Exception):
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text(
                json.dumps(self._items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def add_item(
        self,
        payload: ExtractedPayload,
        change_count: Optional[int],
        transport: str,
        product_type: Optional[str],
        ios_version: Optional[str],
        latency_ms: float,
        dimensions: Optional[tuple[int, int]] = None,
        source: str = "iphone",
    ) -> dict[str, Any]:
        now = time.time()
        iso_now = datetime.fromtimestamp(now, tz=timezone.utc).astimezone().strftime("%H:%M:%S")
        fp = payload.fingerprint
        item_id = f"clip-{int(now * 1000)}-{change_count or 0}"

        image_file: Optional[str] = None
        saved_file: Optional[str] = None
        width, height = dimensions or (0, 0)
        if payload.kind == "image" and payload.image_bytes is not None:
            image_file = f"{item_id}.png"
            img_path = self.images_dir / image_file
            with contextlib.suppress(Exception):
                with Image.open(io.BytesIO(payload.image_bytes)) as img:
                    img = ImageOps.exif_transpose(img)
                    width, height = img.size
                    img.save(img_path, format="PNG")
        elif payload.kind == "file" and payload.file_bytes is not None:
            raw_name = os.path.basename(payload.file_name or "") or f"{item_id}.bin"
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_name)
            saved_file = f"{item_id}_{safe_name}"
            file_path = self.files_dir / saved_file
            with contextlib.suppress(Exception):
                file_path.write_bytes(payload.file_bytes)

        html_b64: Optional[str] = None
        if payload.html_bytes:
            html_b64 = base64.b64encode(payload.html_bytes).decode("ascii")

        display_text = payload.text or ""
        if payload.kind == "image":
            if payload.file_name:
                display_text = payload.file_name
            else:
                display_text = f"Image ({width} × {height} px)" if width and height else "Image"
        elif payload.kind == "file":
            display_text = payload.file_name or payload.text or "File"

        device_label = "This device" if source == "pc" else friendly_device_name(product_type)

        entry: dict[str, Any] = {
            "id": item_id,
            "kind": payload.kind,
            "uti": payload.uti,
            "text": display_text,
            "file_name": payload.file_name or "",
            "saved_file": saved_file,
            "html_base64": html_b64,
            "image_file": image_file,
            "width": width,
            "height": height,
            "raw_size": payload.raw_size,
            "change_count": change_count,
            "transport": transport,
            "source": source,
            "device_model": device_label,
            "product_type": product_type or ("PC" if source == "pc" else "iPhone"),
            "ios_version": ios_version or "",
            "latency_ms": round(latency_ms, 1),
            "timestamp": now,
            "time_label": iso_now,
            "pinned": False,
            "fingerprint": fp,
        }

        with self._lock:
            # Deduplicate existing entry with identical fingerprint
            existing_pinned = False
            filtered: list[dict[str, Any]] = []
            for old in self._items:
                if old.get("fingerprint") == fp:
                    existing_pinned = bool(old.get("pinned"))
                    # If this exact payload was just recorded from PC within 15s, preserve PC source
                    if (
                        old.get("source") == "pc"
                        and source != "pc"
                        and (now - float(old.get("timestamp", 0.0))) < 15.0
                    ):
                        entry["source"] = "pc"
                        entry["device_model"] = "This device"
                        entry["product_type"] = "PC"
                    # If this exact payload was just recorded from iPhone within 30s, preserve iPhone source
                    elif (
                        old.get("source") == "iphone"
                        and source == "pc"
                        and (now - float(old.get("timestamp", 0.0))) < 30.0
                    ):
                        entry["source"] = "iphone"
                        entry["device_model"] = old.get("device_model", "iPhone 16e")
                        entry["product_type"] = old.get("product_type", "iPhone17,5")
                        entry["ios_version"] = old.get("ios_version", "")
                    old_img = old.get("image_file")
                    if old_img and old_img != image_file:
                        with contextlib.suppress(Exception):
                            (self.images_dir / str(old_img)).unlink(missing_ok=True)
                    old_file = old.get("saved_file")
                    if old_file and old_file != saved_file:
                        with contextlib.suppress(Exception):
                            (self.files_dir / str(old_file)).unlink(missing_ok=True)
                else:
                    filtered.append(old)
            entry["pinned"] = existing_pinned
            self._items = [entry, *filtered][: self.max_items]
            self.revision += 1
            self._save_locked()
            return dict(entry)

    def list_items(self) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            # Sort pinned items first, then newest first
            sorted_items = sorted(
                self._items,
                key=lambda x: (1 if x.get("pinned") else 0, float(x.get("timestamp", 0.0))),
                reverse=True,
            )
            sanitized = []
            for item in sorted_items:
                copy_item = dict(item)
                copy_item.pop("html_base64", None)
                sanitized.append(copy_item)
            return self.revision, sanitized

    def get_item(self, item_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id:
                    return dict(item)
        return None

    def get_image_bytes(self, item_id: str) -> Optional[bytes]:
        item = self.get_item(item_id)
        if not item or not item.get("image_file"):
            return None
        img_path = self.images_dir / str(item["image_file"])
        if img_path.exists():
            with contextlib.suppress(Exception):
                return img_path.read_bytes()
        return None

    def get_file_path(self, item_id: str) -> Optional[Path]:
        item = self.get_item(item_id)
        if not item:
            return None
        saved_file = item.get("saved_file")
        if saved_file:
            p = self.files_dir / str(saved_file)
            if p.exists():
                return p
        image_file = item.get("image_file")
        if image_file:
            p = self.images_dir / str(image_file)
            if p.exists():
                return p
        return None

    def toggle_pin(self, item_id: str) -> Optional[bool]:
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id:
                    item["pinned"] = not bool(item.get("pinned"))
                    self.revision += 1
                    self._save_locked()
                    return bool(item["pinned"])
        return None

    def delete_item(self, item_id: str) -> bool:
        with self._lock:
            for idx, item in enumerate(self._items):
                if item.get("id") == item_id:
                    old = self._items.pop(idx)
                    if old.get("image_file"):
                        with contextlib.suppress(Exception):
                            (self.images_dir / str(old["image_file"])).unlink(missing_ok=True)
                    if old.get("saved_file"):
                        with contextlib.suppress(Exception):
                            (self.files_dir / str(old["saved_file"])).unlink(missing_ok=True)
                    self.revision += 1
                    self._save_locked()
                    return True
        return False

    def clear(self, include_pinned: bool = False) -> int:
        with self._lock:
            kept: list[dict[str, Any]] = []
            removed = 0
            for item in self._items:
                if item.get("pinned") and not include_pinned:
                    kept.append(item)
                else:
                    removed += 1
                    if item.get("image_file"):
                        with contextlib.suppress(Exception):
                            (self.images_dir / str(item["image_file"])).unlink(missing_ok=True)
                    if item.get("saved_file"):
                        with contextlib.suppress(Exception):
                            (self.files_dir / str(item["saved_file"])).unlink(missing_ok=True)
            self._items = kept
            self.revision += 1
            self._save_locked()
            return removed


class UniversalClipboardCoordinator:
    """Manages the CoreDevice RSD tunnel, DDI mounting, NotificationProxy subscription,
    PasteboardService pulls, and Windows clipboard updates with automatic reconnection.
    """

    def __init__(
        self,
        udid: Optional[str] = None,
        transport_mode: str = "auto",
        allow_slow_resolve: bool = False,
        reconnect_delay: float = 2.0,
        sync_on_connect: bool = False,
    ) -> None:
        self.requested_udid = udid
        self.transport_mode = transport_mode.lower()
        self.allow_slow_resolve = allow_slow_resolve
        self.reconnect_delay = reconnect_delay
        self.sync_on_connect = sync_on_connect

        self.clipboard = WindowsClipboardWriter()
        self.history = ClipboardHistoryStore()
        self.last_change_id: Optional[tuple[Any, Optional[int]]] = None
        self.recent_fingerprints: dict[str, float] = {}
        self.echo_window_sec = 8.0
        self._pasteboard_io_lock = asyncio.Lock()

        # Live runtime state for WinUI 3 desktop client
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_rsd: Optional[RemoteServiceDiscoveryService] = None
        self._active_pull_trigger: Optional[asyncio.Event] = None
        self._session_cancel_event: Optional[asyncio.Event] = None
        self._own_change_ids: set[tuple[Any, Optional[int]]] = set()
        self.state: str = "starting"
        self.state_message: str = "Initializing Universal Clipboard service..."
        self.connected_udid: Optional[str] = None
        self.product_type: Optional[str] = None
        self.ios_version: Optional[str] = None
        self.active_transport: str = ""
        self.ddi_mounted: bool = False
        self.last_sync_ms: Optional[float] = None
        self.total_synced: int = 0
        self.last_usb_udids: list[str] = []
        self.last_wifi_udids: list[str] = list_wifi_paired_udids()

    def get_status_snapshot(self) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            self.last_wifi_udids = list_wifi_paired_udids()
        rev, items = self.history.list_items()
        return {
            "connected": self.state == "monitoring",
            "state": self.state,
            "state_message": self.state_message,
            "udid": self.connected_udid or self.requested_udid or (self.last_usb_udids[0] if self.last_usb_udids else (self.last_wifi_udids[0] if self.last_wifi_udids else "")),
            "device_name": friendly_device_name(self.product_type),
            "product_type": self.product_type or "",
            "ios_version": self.ios_version or "",
            "active_transport": self.active_transport,
            "transport_mode": self.transport_mode,
            "usb_connected": len(self.last_usb_udids) > 0 or self.active_transport == "USB",
            "usb_udids": self.last_usb_udids,
            "wifi_paired": len(self.last_wifi_udids) > 0,
            "wifi_udids": self.last_wifi_udids,
            "ddi_mounted": self.ddi_mounted,
            "last_change_count": self.last_change_id[1] if self.last_change_id else None,
            "last_sync_ms": round(self.last_sync_ms, 1) if self.last_sync_ms is not None else None,
            "total_synced": self.total_synced,
            "history_revision": rev,
            "history_count": len(items),
        }

    def trigger_manual_sync(self) -> bool:
        if self.loop and self._active_pull_trigger:
            self.loop.call_soon_threadsafe(self._active_pull_trigger.set)
            return True
        return False

    def set_transport_mode(self, mode: str) -> None:
        mode = mode.lower().strip()
        if mode not in ("auto", "usb", "wifi"):
            raise ValueError(f"Unsupported transport mode: {mode}")
        if self.transport_mode != mode:
            self.transport_mode = mode
            if self.loop and self._session_cancel_event:
                self.loop.call_soon_threadsafe(self._session_cancel_event.set)

    def copy_history_item_to_windows(self, item_id: str) -> list[str]:
        item = self.history.get_item(item_id)
        if not item:
            raise KeyError(f"History item {item_id} not found")
        fp = item.get("fingerprint")
        if isinstance(fp, str) and fp:
            self.recent_fingerprints[fp] = time.monotonic()

        kind = item.get("kind")
        if kind == "file":
            fpath = self.history.get_file_path(item_id)
            if not fpath or not fpath.is_file():
                raise RuntimeError("Saved file is missing on disk")
            fallback_name = str(item.get("file_name") or item.get("text") or fpath.name)
            return self.clipboard.write_file(str(fpath), text_fallback=fallback_name)

        if kind == "image":
            img_bytes = self.history.get_image_bytes(item_id)
            if not img_bytes:
                raise RuntimeError("Saved image file is missing")
            fpath = self.history.get_file_path(item_id)
            formats, _ = self.clipboard.write_image(
                img_bytes,
                "public.png",
                file_path=str(fpath) if fpath else None,
            )
            return formats

        text = str(item.get("text") or "")
        html_b64 = item.get("html_base64")
        html_bytes = base64.b64decode(html_b64) if isinstance(html_b64, str) and html_b64 else None
        return self.clipboard.write_text(text, html_bytes=html_bytes, is_url=(kind == "url"))

    def send_history_item_to_iphone(self, item_id: str) -> bool:
        """Push an existing history item from Windows directly to the connected iPhone pasteboard."""
        if not self.loop or not self._active_rsd:
            raise RuntimeError("iPhone is not currently connected")
        item = self.history.get_item(item_id)
        if not item:
            raise KeyError(f"History item {item_id} not found")

        kind = str(item.get("kind") or "text")
        if kind == "file":
            fpath = self.history.get_file_path(item_id)
            if not fpath or not fpath.is_file():
                raise RuntimeError("Saved file is missing on disk")
            file_bytes = fpath.read_bytes()
            fname = str(item.get("file_name") or item.get("text") or fpath.name)
            payload = ExtractedPayload(
                kind="file",
                uti=str(item.get("uti") or "public.data"),
                raw_size=len(file_bytes),
                text=fname,
                file_name=fname,
                file_bytes=file_bytes,
            )
            dims = None
        elif kind == "image":
            img_bytes = self.history.get_image_bytes(item_id)
            if not img_bytes:
                raise RuntimeError("Saved image file is missing")
            payload = ExtractedPayload(
                kind="image",
                uti="public.png",
                raw_size=len(img_bytes),
                image_bytes=img_bytes,
                file_name=str(item.get("file_name") or ""),
            )
            dims = (int(item.get("width") or 0), int(item.get("height") or 0))
        else:
            text = str(item.get("text") or "")
            raw = text.encode("utf-8")
            payload = ExtractedPayload(
                kind=kind,
                uti=UTI_URL if kind == "url" else UTI_UTF8_PLAIN_TEXT,
                raw_size=len(raw),
                text=text,
            )
            dims = None

        fut = asyncio.run_coroutine_threadsafe(
            self._push_payload_to_iphone(
                self._active_rsd,
                payload,
                self.active_transport or "USB",
                dimensions=dims,
                record_history=False,
            ),
            self.loop,
        )
        return bool(fut.result(timeout=12.0))

    async def _push_payload_to_iphone(
        self,
        rsd: RemoteServiceDiscoveryService,
        payload: ExtractedPayload,
        active_transport: str,
        dimensions: Optional[tuple[int, int]] = None,
        record_history: bool = True,
    ) -> bool:
        """Write `payload` to the iPhone general pasteboard via `com.apple.coredevice.pasteboardservice` `SET`."""
        t_start = time.perf_counter()
        fp = payload.fingerprint
        self.recent_fingerprints[fp] = time.monotonic()

        if payload.kind == "image" and payload.image_bytes is not None:
            items = [data_item("public.png", payload.image_bytes)]
        elif payload.kind == "file" and payload.file_bytes is not None:
            primary_uti = payload.uti or "public.data"
            fname = payload.file_name or payload.text or "file"
            name_bytes = fname.encode("utf-8")
            type_list: list[str] = []
            data_dict: dict[str, dict[str, Any]] = {}
            for u in (primary_uti, "public.data", "public.content", "public.item"):
                if u not in type_list:
                    type_list.append(u)
                    data_dict[u] = {"data": payload.file_bytes}
            for txt_u in (UTI_UTF8_PLAIN_TEXT, UTI_PLAIN_TEXT):
                if txt_u not in type_list:
                    type_list.append(txt_u)
                    data_dict[txt_u] = {"data": name_bytes}
            items = [{"types": type_list, "data": data_dict}]
        elif payload.text is not None:
            utis = [UTI_UTF8_PLAIN_TEXT, UTI_PLAIN_TEXT, UTI_TEXT]
            if payload.kind == "url":
                utis.append(UTI_URL)
            items = [text_item(payload.text, utis)]
        else:
            return False

        try:
            async with self._pasteboard_io_lock:
                async with PasteboardService(rsd) as pb_service:
                    reply = await asyncio.wait_for(
                        pb_service.set(items, GENERAL_PASTEBOARD),
                        timeout=10.0,
                    )
                    change_id = extract_change_info(reply) if isinstance(reply, dict) else (None, None)
                    if change_id[1] is None:
                        with contextlib.suppress(Exception):
                            meta_snap = await asyncio.wait_for(
                                pb_service.get(GENERAL_PASTEBOARD, POLICY_ALL_PROMISED),
                                timeout=4.0,
                            )
                            change_id = extract_change_info(meta_snap)

                total_ms = (time.perf_counter() - t_start) * 1000.0
                if change_id[1] is not None:
                    self._own_change_ids.add(change_id)
                    self.last_change_id = change_id
                self.recent_fingerprints[fp] = time.monotonic()
                self.last_sync_ms = total_ms
                self.total_synced += 1

                if record_history:
                    self.history.add_item(
                        payload=payload,
                        change_count=change_id[1],
                        transport=active_transport,
                        product_type="This device",
                        ios_version="",
                        latency_ms=total_ms,
                        dimensions=dimensions,
                        source="pc",
                    )

            logger.info(
                "[CLIPBOARD_PUSHED] status=SUCCESS direction=Windows->iPhone transport=%s changeCount=%s "
                "type=%s uti=%s size=%dB total_ms=%.1f",
                active_transport,
                change_id[1],
                payload.kind,
                payload.uti,
                payload.raw_size,
                total_ms,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[CLIPBOARD_PUSHED] status=ERROR direction=Windows->iPhone type=%s size=%dB error=%s: %s",
                payload.kind,
                payload.raw_size,
                type(exc).__name__,
                exc,
            )
            return False

    async def _resolve_target_and_transport(self) -> tuple[str, str]:
        """Determine `(udid, active_transport)` based on `--udid` and `--transport`."""
        usb_udids = await async_list_usb_udids()
        wifi_udids = list_wifi_paired_udids()
        self.last_usb_udids = usb_udids
        self.last_wifi_udids = wifi_udids

        # Opportunistically cache iPhone Wi-Fi IP while connected over USB
        if usb_udids and self.transport_mode in ("auto", "wifi"):
            for candidate in usb_udids:
                if self.requested_udid and candidate != self.requested_udid:
                    continue
                with contextlib.suppress(Exception):
                    async with await create_using_usbmux(serial=candidate, autopair=False) as ld:
                        wcfg = await ld.get_value("com.apple.mobile.wireless_lockdown") or {}
                        if not wcfg.get("EnableWifiConnections"):
                            await ld.set_value(True, "com.apple.mobile.wireless_lockdown", "EnableWifiConnections")

        udid = self.requested_udid
        if not udid:
            if self.transport_mode == "usb" and usb_udids:
                udid = usb_udids[0]
            elif self.transport_mode == "wifi" and wifi_udids:
                udid = wifi_udids[0]
            elif usb_udids:
                udid = usb_udids[0]
            elif wifi_udids:
                udid = wifi_udids[0]

        if not udid:
            raise ConnectionError("No paired iPhone found over USB or in RemotePairing records.")

        if self.transport_mode == "usb":
            if udid not in usb_udids:
                raise ConnectionError(f"iPhone {udid} is not connected via USB cable.")
            return udid, "USB"

        if self.transport_mode == "wifi":
            if udid not in wifi_udids:
                raise ConnectionError(
                    f"iPhone {udid} has no Wi-Fi RemotePairing record. Run with `--pair-wifi` over USB once."
                )
            return udid, "Wi-Fi"

        # auto mode: prefer USB when plugged in, seamlessly fall back to Wi-Fi when unplugged
        if udid in usb_udids:
            return udid, "USB"
        if udid in wifi_udids:
            return udid, "Wi-Fi"
        raise ConnectionError(f"iPhone {udid} is neither attached over USB nor paired for Wi-Fi.")

    async def _ensure_ddi_and_monitor(self, udid: str, active_transport: str) -> None:
        """Open RSD tunnel, mount Developer Disk Image if needed, and run the notification monitor."""
        transport_key = "wifi" if active_transport == "Wi-Fi" else "usb"
        self.state = "connecting"
        self.state_message = f"Connecting to iPhone over {active_transport}..."

        for attempt in range(2):
            async with open_rsd_tunnel(udid, transport_key) as rsd:
                self.connected_udid = rsd.udid
                self.product_type = rsd.product_type
                self.ios_version = rsd.product_version
                self.active_transport = active_transport

                services = (rsd.peer_info or {}).get("Services", {})
                has_pasteboard = PasteboardService.SERVICE_NAME in services
                if not has_pasteboard:
                    if attempt == 1:
                        raise RuntimeError(
                            f"{PasteboardService.SERVICE_NAME} still unavailable after mounting Developer Disk Image."
                        )
                    self.state = "mounting_ddi"
                    self.state_message = f"Mounting Developer Disk Image on {friendly_device_name(rsd.product_type)}..."
                    logger.info(
                        "[DDI] %s not yet advertised on %s (iOS %s); mounting Developer Disk Image...",
                        PasteboardService.SERVICE_NAME,
                        rsd.product_type,
                        rsd.product_version,
                    )
                    try:
                        await auto_mount(rsd)
                    except AlreadyMountedError:
                        pass
                    logger.info("[DDI] Developer Disk Image mounted; reconnecting RSD tunnel to refresh service table.")
                    continue

                self.ddi_mounted = True
                logger.info(
                    "[CONNECTED] device=%s ios=%s udid=%s transport=%s rsd_addr=%s:%s",
                    rsd.product_type,
                    rsd.product_version,
                    rsd.udid,
                    active_transport,
                    rsd.service.address[0],
                    rsd.service.address[1],
                )
                await self._monitor_session(rsd, active_transport)
                return

    async def _monitor_session(self, rsd: RemoteServiceDiscoveryService, active_transport: str) -> None:
        """Subscribe to `com.apple.pasteboard.notify.changed` and pull changes using isolated connections."""
        pull_trigger = asyncio.Event()
        session_cancel = asyncio.Event()
        self._active_rsd = rsd
        self._active_pull_trigger = pull_trigger
        self._session_cancel_event = session_cancel
        pull_reason = "notification"
        pending_locked_catchup = False

        async with NotificationProxyService(rsd) as notif_service:
            await notif_service.notify_register_dispatch(PASTEBOARD_CHANGED_NOTIFICATION)
            with contextlib.suppress(Exception):
                await notif_service.notify_register_dispatch(SPRINGBOARD_LOCKSTATE_NOTIFICATION)
                await notif_service.notify_register_dispatch(KEYBAG_LOCK_STATUS_NOTIFICATION)

            self.state = "monitoring"
            self.state_message = f"Connected to {friendly_device_name(rsd.product_type)} · iOS {rsd.product_version} ({active_transport})"
            logger.info(
                "[SUBSCRIBED] transport=%s notification=%s service=%s policy=POLICY_PROMISE_SECONDARY",
                active_transport,
                PASTEBOARD_CHANGED_NOTIFICATION,
                PasteboardService.SERVICE_NAME,
            )

            # Baseline check on connect/reconnect
            if self.last_change_id is None and not self.sync_on_connect:
                async with PasteboardService(rsd) as pb_service:
                    init_snap = await pb_service.get(GENERAL_PASTEBOARD, POLICY_ALL_PROMISED)
                self.last_change_id = extract_change_info(init_snap)
                logger.info(
                    "[BASELINE] transport=%s initial_changeCount=%s (waiting for new iPhone Copy events...)",
                    active_transport,
                    self.last_change_id[1] if self.last_change_id else None,
                )
            else:
                pull_reason = "reconnect_catchup" if self.last_change_id is not None else "initial_sync"
                pull_trigger.set()

            async def _notification_reader() -> None:
                nonlocal pull_reason, pending_locked_catchup
                async for event in notif_service.receive_notification():
                    if event.get("Command") == "ProxyDeath":
                        raise ConnectionError("NotificationProxyService reported ProxyDeath")
                    name = event.get("Name")
                    state = event.get("State")
                    if name == PASTEBOARD_CHANGED_NOTIFICATION:
                        logger.info(
                            "[NOTIFICATION] received=%s transport=%s state=%s",
                            PASTEBOARD_CHANGED_NOTIFICATION,
                            active_transport,
                            state,
                        )
                        pull_reason = "notification"
                        pull_trigger.set()
                    elif name in (SPRINGBOARD_LOCKSTATE_NOTIFICATION, KEYBAG_LOCK_STATUS_NOTIFICATION):
                        logger.info(
                            "[DEVICE_STATE] notification=%s transport=%s state=%s",
                            name,
                            active_transport,
                            state,
                        )
                        # If a previous pull occurred while the device pasteboard was locked, retry on unlock
                        if pending_locked_catchup or state == 0:
                            pull_reason = "unlock_catchup"
                            pull_trigger.set()

            async def _pull_worker() -> None:
                nonlocal pending_locked_catchup
                while True:
                    await pull_trigger.wait()
                    # Clear immediately BEFORE pulling so rapid consecutive Copy notifications
                    # arriving while a pull is in-flight re-set `pull_trigger` and immediately trigger
                    # a follow-up pull without dropping the newest Copy.
                    pull_trigger.clear()
                    reason = pull_reason
                    # Acquire `_pasteboard_io_lock` so notifications triggered by a Windows->iPhone
                    # `SET` wait until `_push_payload_to_iphone` records `change_id` and `source="pc"`.
                    async with self._pasteboard_io_lock:
                        async with PasteboardService(rsd) as pb_service:
                            needs_retry = await self._pull_and_update_clipboard(
                                rsd, pb_service, active_transport, reason
                            )
                            pending_locked_catchup = needs_retry

            async def _transport_switch_watcher() -> None:
                await session_cancel.wait()
                raise ConnectionError(f"Transport mode switched to {self.transport_mode}")

            async def _windows_clipboard_watcher() -> None:
                """Monitor Windows system clipboard sequence number and push PC copies to iPhone."""
                last_seq = self.clipboard.get_sequence_number()
                while True:
                    await asyncio.sleep(0.28)
                    cur_seq = self.clipboard.get_sequence_number()
                    if cur_seq == last_seq:
                        continue
                    last_seq = cur_seq
                    if cur_seq == self.clipboard.last_sequence_number:
                        continue

                    now = time.monotonic()
                    self.recent_fingerprints = {
                        fp: ts for fp, ts in self.recent_fingerprints.items() if now - ts < self.echo_window_sec
                    }

                    read_res = self.clipboard.read_payload()
                    if read_res is None:
                        continue
                    payload, dims = read_res
                    if payload.fingerprint in self.recent_fingerprints:
                        continue

                    await self._push_payload_to_iphone(
                        rsd,
                        payload,
                        active_transport,
                        dimensions=dims,
                        record_history=True,
                    )

            reader_task = asyncio.create_task(_notification_reader(), name="notif-reader")
            worker_task = asyncio.create_task(_pull_worker(), name="pull-worker")
            cancel_task = asyncio.create_task(_transport_switch_watcher(), name="transport-watcher")
            win_watch_task = asyncio.create_task(_windows_clipboard_watcher(), name="win-clip-watcher")
            try:
                done, pending = await asyncio.wait(
                    {reader_task, worker_task, cancel_task, win_watch_task},
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
            finally:
                self._active_rsd = None
                self._active_pull_trigger = None
                self._session_cancel_event = None
                for task in (reader_task, worker_task, cancel_task, win_watch_task):
                    if not task.done():
                        task.cancel()

    async def _pull_and_update_clipboard(
        self,
        rsd: RemoteServiceDiscoveryService,
        pb_service: PasteboardService,
        active_transport: str,
        reason: str,
    ) -> bool:
        """Pull the pasteboard with `POLICY_PROMISE_SECONDARY`, resolve secondary promises if needed,
        and write to the Windows clipboard. Returns True if pasteboard items were locked/unavailable
        and should be retried on unlock.
        """
        t_start = time.perf_counter()
        snapshot = await asyncio.wait_for(
            pb_service.get(GENERAL_PASTEBOARD, POLICY_PROMISE_SECONDARY),
            timeout=10.0,
        )
        pull_ms = (time.perf_counter() - t_start) * 1000.0

        change_id = extract_change_info(snapshot)
        change_count = change_id[1]

        if change_id in self._own_change_ids:
            self.last_change_id = change_id
            return False

        if change_id[1] is not None and change_id == self.last_change_id:
            if reason != "unlock_catchup":
                logger.info(
                    "[SKIP] reason=duplicate_changeCount changeCount=%s transport=%s pull_ms=%.1f",
                    change_count,
                    active_transport,
                    pull_ms,
                )
            return False

        payload = extract_inline_payload(snapshot)

        # Step 11: Resolve promised secondary representation when no inline representation was usable
        if payload is None:
            candidate = find_resolvable_Secondary_representation(
                snapshot, allow_behind_rtf=self.allow_slow_resolve
            )
            if candidate is not None:
                item_idx, target_uti, behind_rtf = candidate
                resolve_timeout = 70.0 if behind_rtf else 8.0
                logger.info(
                    "[RESOLVE] changeCount=%s itemIndex=%d uti=%s behind_rtf=%s timeout=%.0fs",
                    change_count,
                    item_idx,
                    target_uti,
                    behind_rtf,
                    resolve_timeout,
                )
                try:
                    resolved_bytes = await resolve_promised_representation(
                        pb_service, item_idx, target_uti, timeout=resolve_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[RESOLVE_TIMEOUT] changeCount=%s uti=%s timed out after %.0fs",
                        change_count,
                        target_uti,
                        resolve_timeout,
                    )
                    resolved_bytes = None

                if not resolved_bytes and not behind_rtf:
                    # Fallback: if no `public.rtf` is present to stall the daemon, pull `POLICY_ALL_RESOLVED`
                    with contextlib.suppress(Exception):
                        full_snap = await asyncio.wait_for(
                            pb_service.get(GENERAL_PASTEBOARD, POLICY_ALL_RESOLVED),
                            timeout=8.0,
                        )
                        payload = extract_inline_payload(full_snap)

                if resolved_bytes:
                    if target_uti in IMAGE_UTIS:
                        payload = ExtractedPayload(
                            kind="image",
                            uti=target_uti,
                            raw_size=len(resolved_bytes),
                            image_bytes=resolved_bytes,
                        )
                    elif target_uti in UTI_TO_FILE_EXT:
                        ext = UTI_TO_FILE_EXT[target_uti]
                        fname = f"iPhone_File_{int(time.time())}{ext}"
                        payload = ExtractedPayload(
                            kind="file",
                            uti=target_uti,
                            raw_size=len(resolved_bytes),
                            text=fname,
                            file_name=fname,
                            file_bytes=resolved_bytes,
                        )
                    elif target_uti == UTI_WEB_ARCHIVE:
                        img_pair, arch_text, arch_html = _extract_from_webarchive(resolved_bytes)
                        if img_pair is not None:
                            payload = ExtractedPayload(
                                kind="image",
                                uti=f"{UTI_WEB_ARCHIVE}:{img_pair[0]}",
                                raw_size=len(img_pair[1]),
                                image_bytes=img_pair[1],
                            )
                        elif arch_text:
                            payload = ExtractedPayload(
                                kind="text",
                                uti=UTI_WEB_ARCHIVE,
                                raw_size=len(resolved_bytes),
                                text=arch_text,
                                html_bytes=arch_html,
                            )
                    elif target_uti == UTI_HTML:
                        text = extract_text_from_html(resolved_bytes)
                        if text:
                            payload = ExtractedPayload(
                                kind="url" if URL_REGEX.match(text.strip()) else "text",
                                uti=UTI_HTML,
                                raw_size=len(resolved_bytes),
                                text=text,
                                html_bytes=resolved_bytes,
                            )
                    elif target_uti in URL_UTIS:
                        extracted_url = _extract_url_from_raw(resolved_bytes)
                        if extracted_url:
                            payload = ExtractedPayload(
                                kind="url",
                                uti=target_uti,
                                raw_size=len(resolved_bytes),
                                text=extracted_url,
                            )
                    else:
                        extracted_url = _extract_url_from_raw(resolved_bytes) if (target_uti in URL_UTIS or target_uti == UTI_URL) else None
                        if extracted_url:
                            payload = ExtractedPayload(
                                kind="url",
                                uti=target_uti,
                                raw_size=len(resolved_bytes),
                                text=extracted_url,
                            )
                        else:
                            try:
                                text = resolved_bytes.decode("utf-8").strip("\ufffc")
                                if text.strip():
                                    is_url = bool(URL_REGEX.match(text.strip()))
                                    payload = ExtractedPayload(
                                        kind="url" if is_url else "text",
                                        uti=target_uti,
                                        raw_size=len(resolved_bytes),
                                        text=text,
                                    )
                            except UnicodeDecodeError:
                                pass

        if payload is None:
            pb_dict = snapshot.get("pasteboard", {}) if isinstance(snapshot.get("pasteboard"), dict) else snapshot
            items = pb_dict.get("items", []) or []
            advertised = [i.get("types", []) for i in items if isinstance(i, dict)]
            # Do not mark `self.last_change_id = change_id` if items were advertised but data was
            # empty (e.g. iPhone screen was locked), so unlocking can retry this changeCount.
            if not advertised:
                self.last_change_id = change_id
            logger.info(
                "[SKIP] reason=no_supported_or_locked_payload changeCount=%s advertised_types=%s pull_ms=%.1f",
                change_count,
                advertised,
                pull_ms,
            )
            return bool(advertised)

        self.last_change_id = change_id

        # Feedback / echo-loop prevention
        now = time.monotonic()
        self.recent_fingerprints = {
            fp: ts for fp, ts in self.recent_fingerprints.items() if now - ts < self.echo_window_sec
        }
        fp = payload.fingerprint
        if fp in self.recent_fingerprints:
            logger.info(
                "[SKIP] reason=echo_or_identical_payload changeCount=%s type=%s uti=%s size=%dB",
                change_count,
                payload.kind,
                payload.uti,
                payload.raw_size,
            )
            return False

        # Pre-register fingerprint so local Windows clipboard watcher ignores this write
        self.recent_fingerprints[fp] = time.monotonic()

        # Write to Windows System Clipboard
        t_write = time.perf_counter()
        try:
            if payload.kind == "image" and payload.image_bytes is not None:
                base_uti = payload.uti.split(":")[-1]
                total_ms = (time.perf_counter() - t_start) * 1000.0
                entry = self.history.add_item(
                    payload=payload,
                    change_count=change_count,
                    transport=active_transport,
                    product_type=rsd.product_type,
                    ios_version=rsd.product_version,
                    latency_ms=total_ms,
                    source="iphone",
                )
                saved_img_path = self.history.get_file_path(str(entry["id"]))
                formats, (width, height) = self.clipboard.write_image(
                    payload.image_bytes,
                    base_uti,
                    file_path=str(saved_img_path) if saved_img_path else None,
                )
                write_ms = (time.perf_counter() - t_write) * 1000.0
                total_ms = (time.perf_counter() - t_start) * 1000.0
                self.recent_fingerprints[fp] = time.monotonic()
                self.last_sync_ms = total_ms
                self.total_synced += 1
                logger.info(
                    "[CLIPBOARD_UPDATED] status=SUCCESS trigger=%s transport=%s changeCount=%s "
                    "type=image uti=%s size=%dB dimensions=%dx%d formats=%s pull_ms=%.1f write_ms=%.1f total_ms=%.1f",
                    reason,
                    active_transport,
                    change_count,
                    payload.uti,
                    payload.raw_size,
                    width,
                    height,
                    "+".join(formats),
                    pull_ms,
                    write_ms,
                    total_ms,
                )
            elif payload.kind == "file" and payload.file_bytes is not None:
                total_ms = (time.perf_counter() - t_start) * 1000.0
                entry = self.history.add_item(
                    payload=payload,
                    change_count=change_count,
                    transport=active_transport,
                    product_type=rsd.product_type,
                    ios_version=rsd.product_version,
                    latency_ms=total_ms,
                    source="iphone",
                )
                saved_fpath = self.history.get_file_path(str(entry["id"]))
                formats = []
                if saved_fpath and saved_fpath.is_file():
                    formats = self.clipboard.write_file(
                        str(saved_fpath),
                        text_fallback=payload.file_name or payload.text,
                    )
                write_ms = (time.perf_counter() - t_write) * 1000.0
                total_ms = (time.perf_counter() - t_start) * 1000.0
                self.recent_fingerprints[fp] = time.monotonic()
                self.last_sync_ms = total_ms
                self.total_synced += 1
                logger.info(
                    "[CLIPBOARD_UPDATED] status=SUCCESS trigger=%s transport=%s changeCount=%s "
                    "type=file uti=%s size=%dB formats=%s pull_ms=%.1f write_ms=%.1f total_ms=%.1f",
                    reason,
                    active_transport,
                    change_count,
                    payload.uti,
                    payload.raw_size,
                    "+".join(formats),
                    pull_ms,
                    write_ms,
                    total_ms,
                )
            elif payload.text is not None:
                is_url = payload.kind == "url"
                formats = self.clipboard.write_text(
                    payload.text,
                    html_bytes=payload.html_bytes,
                    is_url=is_url,
                )
                write_ms = (time.perf_counter() - t_write) * 1000.0
                total_ms = (time.perf_counter() - t_start) * 1000.0
                utf8_len = len(payload.text.encode("utf-8"))
                char_len = len(payload.text)
                self.recent_fingerprints[fp] = time.monotonic()
                self.last_sync_ms = total_ms
                self.total_synced += 1
                self.history.add_item(
                    payload=payload,
                    change_count=change_count,
                    transport=active_transport,
                    product_type=rsd.product_type,
                    ios_version=rsd.product_version,
                    latency_ms=total_ms,
                    source="iphone",
                )
                logger.info(
                    "[CLIPBOARD_UPDATED] status=SUCCESS trigger=%s transport=%s changeCount=%s "
                    "type=%s uti=%s size=%dB utf8_bytes=%d chars=%d formats=%s pull_ms=%.1f write_ms=%.1f total_ms=%.1f",
                    reason,
                    active_transport,
                    change_count,
                    payload.kind,
                    payload.uti,
                    payload.raw_size,
                    utf8_len,
                    char_len,
                    "+".join(formats),
                    pull_ms,
                    write_ms,
                    total_ms,
                )
        except Exception as exc:
            logger.error(
                "[CLIPBOARD_UPDATED] status=ERROR changeCount=%s type=%s uti=%s size=%dB error=%s: %s",
                change_count,
                payload.kind,
                payload.uti,
                payload.raw_size,
                type(exc).__name__,
                exc,
            )
        return False

    async def run_forever(self) -> None:
        """Main resilient supervisor loop with automatic transport failover and reconnection."""
        self.loop = asyncio.get_running_loop()
        logger.info(
            "[STARTUP] Universal Clipboard PoC starting (mode=%s, requested_udid=%s, heif_support=%s)",
            self.transport_mode,
            self.requested_udid or "auto",
            HEIF_AVAILABLE,
        )
        while True:
            try:
                self.state = "connecting"
                udid, active_transport = await self._resolve_target_and_transport()
                self.state_message = f"Connecting to {udid} over {active_transport}..."
                logger.info("[CONNECTING] udid=%s transport=%s", udid, active_transport)
                await self._ensure_ddi_and_monitor(udid, active_transport)
            except asyncio.CancelledError:
                logger.info("[SHUTDOWN] Universal Clipboard PoC stopped cleanly.")
                raise
            except Exception as exc:
                self.state = "reconnecting"
                self.active_transport = ""
                self.state_message = str(exc)
                logger.warning(
                    "[CONNECTION_LOST] transport_mode=%s error=%s: %s — retrying in %.1fs...",
                    self.transport_mode,
                    type(exc).__name__,
                    exc,
                    self.reconnect_delay,
                )
            await asyncio.sleep(self.reconnect_delay)


# ---------------------------------------------------------------------------
# Local HTTP API Server for WinUI 3 Desktop Client (127.0.0.1:8767)
# ---------------------------------------------------------------------------


class CircularLogHandler(logging.Handler):
    """Keep last N formatted log lines in memory to serve live to the WinUI 3 Developer Mode console."""

    def __init__(self, capacity: int = 1500) -> None:
        super().__init__()
        self.capacity = capacity
        self.buffer: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
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
    coordinator: UniversalClipboardCoordinator,
    host: str = "127.0.0.1",
    port: int = 8767,
) -> Optional[ThreadingHTTPServer]:
    """Start a lightweight background HTTP JSON server on `127.0.0.1:8767` for the WinUI 3 client."""

    class ApiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress per-request access logs to keep stdout clean and privacy-safe
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


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Universal Clipboard for Windows PoC (CoreDevice / RemoteXPC Pasteboard Synchronizer)"
    )
    parser.add_argument(
        "--udid",
        type=str,
        default=None,
        help="Target iPhone UDID (default: auto-detect connected USB or paired Wi-Fi iPhone)",
    )
    parser.add_argument(
        "--transport",
        choices=["auto", "usb", "wifi"],
        default="auto",
        help="Transport mode: 'auto' (prefers USB, falls back to Wi-Fi), 'usb', or 'wifi' (default: auto)",
    )
    parser.add_argument(
        "--pair-wifi",
        action="store_true",
        help="Create a RemotePairing record over USB so the iPhone can connect over Wi-Fi without USB",
    )
    parser.add_argument(
        "--sync-on-connect",
        action="store_true",
        help="Immediately pull and write the iPhone's current clipboard to Windows upon starting",
    )
    parser.add_argument(
        "--allow-slow-resolve",
        action="store_true",
        help="Allow resolving promised representations even when behind Apple Notes public.rtf (~64s stall)",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=2.0,
        help="Delay in seconds before reconnecting after a disconnect (default: 2.0)",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=8767,
        help="Local HTTP API port for the WinUI 3 desktop client (default: 8767, 0 to disable)",
    )
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    if args.pair_wifi:
        logger.info("[PAIR_WIFI] Establishing RemotePairing record over USB...")
        paired_udid = await pair_device_for_wifi_over_usb(args.udid)
        logger.info("[PAIR_WIFI] Success! Paired UDID=%s for wireless RemotePairing.", paired_udid)
        return 0

    coordinator = UniversalClipboardCoordinator(
        udid=args.udid,
        transport_mode=args.transport,
        allow_slow_resolve=args.allow_slow_resolve,
        reconnect_delay=args.reconnect_delay,
        sync_on_connect=args.sync_on_connect,
    )
    if args.api_port > 0:
        start_local_api_server(coordinator, port=args.api_port)
    await coordinator.run_forever()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    GLOBAL_LOG_HANDLER.setFormatter(formatter)
    logging.getLogger().addHandler(GLOBAL_LOG_HANDLER)
    # Keep underlying libraries quiet at INFO level
    logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
    logging.getLogger("pmd_pytcp").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    args = parse_args()
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        logger.info("[SHUTDOWN] Interrupted by user (Ctrl+C).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
