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
import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
import io
import logging
import plistlib
import re
import socket
import struct
import subprocess
import sys
import time
from typing import Any, Optional, cast

import ifaddr
from PIL import Image, ImageOps

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
    UTI_PLAIN_TEXT,
    UTI_TEXT,
    UTI_URL,
    UTI_UTF8_PLAIN_TEXT,
    UTI_WEB_ARCHIVE,
    PasteboardService,
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

UTI_HTML = "public.html"
UTI_RTF = "public.rtf"
UTI_FLAT_RTFD = "com.apple.flat-rtfd"
TEXT_UTIS = (UTI_UTF8_PLAIN_TEXT, UTI_PLAIN_TEXT, UTI_TEXT)
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
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002

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

        self._user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        self._user32.RegisterClipboardFormatW.restype = wintypes.UINT

        self._user32.GetClipboardSequenceNumber.argtypes = []
        self._user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HGLOBAL

        self._kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalLock.restype = ctypes.c_void_p

        self._kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL

        self._kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalFree.restype = wintypes.HGLOBAL

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

    def write_image(self, image_bytes: bytes, uti: str) -> tuple[list[str], tuple[int, int]]:
        """Decode image (PNG, JPEG, HEIC, TIFF, GIF, WebP) and write `CF_DIBV5`, `CF_DIB`, and `PNG`."""
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

        return formats_written, dimensions


# ---------------------------------------------------------------------------
# Extracted Pasteboard Payload Model & Smart Extraction / Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedPayload:
    kind: str  # "text", "url", "image"
    uti: str
    raw_size: int
    text: Optional[str] = None
    html_bytes: Optional[bytes] = None
    image_bytes: Optional[bytes] = None

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(self.kind.encode("utf-8"))
        if self.image_bytes is not None:
            h.update(self.image_bytes)
        elif self.text is not None:
            h.update(self.text.encode("utf-8"))
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

        url_bytes = _inline_bytes(data_map, UTI_URL)
        for txt_uti in TEXT_UTIS:
            txt_bytes = _inline_bytes(data_map, txt_uti)
            if txt_bytes is not None:
                try:
                    text = txt_bytes.decode("utf-8").strip("\ufffc")
                except UnicodeDecodeError:
                    continue
                if not text.strip():
                    continue
                is_url = bool(url_bytes or URL_REGEX.match(text.strip()))
                return ExtractedPayload(
                    kind="url" if is_url else "text",
                    uti=UTI_URL if is_url and url_bytes else txt_uti,
                    raw_size=len(txt_bytes),
                    text=text,
                    html_bytes=html_inline,
                )

        if url_bytes is not None:
            try:
                url_text = url_bytes.decode("utf-8").strip()
            except UnicodeDecodeError:
                url_text = ""
            if url_text:
                return ExtractedPayload(
                    kind="url",
                    uti=UTI_URL,
                    raw_size=len(url_bytes),
                    text=url_text,
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
        UTI_UTF8_PLAIN_TEXT,
        UTI_PLAIN_TEXT,
        UTI_TEXT,
        UTI_URL,
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
        self.last_change_id: Optional[tuple[Any, Optional[int]]] = None
        self.recent_fingerprints: dict[str, float] = {}
        self.echo_window_sec = 5.0

    async def _resolve_target_and_transport(self) -> tuple[str, str]:
        """Determine `(udid, active_transport)` based on `--udid` and `--transport`."""
        usb_udids = await async_list_usb_udids()
        wifi_udids = list_wifi_paired_udids()

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

        for attempt in range(2):
            async with open_rsd_tunnel(udid, transport_key) as rsd:
                services = (rsd.peer_info or {}).get("Services", {})
                has_pasteboard = PasteboardService.SERVICE_NAME in services
                if not has_pasteboard:
                    if attempt == 1:
                        raise RuntimeError(
                            f"{PasteboardService.SERVICE_NAME} still unavailable after mounting Developer Disk Image."
                        )
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
        """Subscribe to `com.apple.pasteboard.notify.changed` and pull changes over a persistent session."""
        pull_trigger = asyncio.Event()
        pull_reason = "notification"

        async with NotificationProxyService(rsd) as notif_service, PasteboardService(rsd) as pb_service:
            await notif_service.notify_register_dispatch(PASTEBOARD_CHANGED_NOTIFICATION)
            logger.info(
                "[SUBSCRIBED] transport=%s notification=%s service=%s policy=POLICY_PROMISE_SECONDARY",
                active_transport,
                PASTEBOARD_CHANGED_NOTIFICATION,
                PasteboardService.SERVICE_NAME,
            )

            # Baseline check on connect/reconnect
            if self.last_change_id is None and not self.sync_on_connect:
                init_snap = await pb_service.get(GENERAL_PASTEBOARD, POLICY_ALL_PROMISED)
                self.last_change_id = extract_change_info(init_snap)
                logger.info(
                    "[BASELINE] transport=%s initial_changeCount=%s (waiting for new iPhone Copy events...)",
                    active_transport,
                    self.last_change_id[1] if self.last_change_id else None,
                )
            else:
                # Either sync_on_connect=True or we are reconnecting after a dropped link and
                # want to catch any Copy made while disconnected.
                pull_reason = "reconnect_catchup" if self.last_change_id is not None else "initial_sync"
                pull_trigger.set()

            async def _notification_reader() -> None:
                nonlocal pull_reason
                async for event in notif_service.receive_notification():
                    if event.get("Command") == "ProxyDeath":
                        raise ConnectionError("NotificationProxyService reported ProxyDeath")
                    if event.get("Name") == PASTEBOARD_CHANGED_NOTIFICATION:
                        logger.info(
                            "[NOTIFICATION] received=%s transport=%s state=%s",
                            PASTEBOARD_CHANGED_NOTIFICATION,
                            active_transport,
                            event.get("State"),
                        )
                        pull_reason = "notification"
                        pull_trigger.set()

            async def _pull_worker() -> None:
                while True:
                    await pull_trigger.wait()
                    # Clear immediately BEFORE pulling so that rapid consecutive Copy notifications
                    # arriving while a pull is in-flight re-set `pull_trigger` and immediately trigger
                    # a follow-up pull without dropping the newest Copy!
                    pull_trigger.clear()
                    reason = pull_reason
                    await self._pull_and_update_clipboard(rsd, pb_service, active_transport, reason)

            reader_task = asyncio.create_task(_notification_reader(), name="notif-reader")
            worker_task = asyncio.create_task(_pull_worker(), name="pull-worker")
            try:
                done, pending = await asyncio.wait(
                    {reader_task, worker_task},
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
                for task in (reader_task, worker_task):
                    if not task.done():
                        task.cancel()

    async def _pull_and_update_clipboard(
        self,
        rsd: RemoteServiceDiscoveryService,
        pb_service: PasteboardService,
        active_transport: str,
        reason: str,
    ) -> None:
        """Pull the pasteboard with `POLICY_PROMISE_SECONDARY`, resolve secondary promises if needed,
        and write to the Windows clipboard.
        """
        t_start = time.perf_counter()
        snapshot = await asyncio.wait_for(
            pb_service.get(GENERAL_PASTEBOARD, POLICY_PROMISE_SECONDARY),
            timeout=10.0,
        )
        pull_ms = (time.perf_counter() - t_start) * 1000.0

        change_id = extract_change_info(snapshot)
        change_count = change_id[1]

        if change_id[1] is not None and change_id == self.last_change_id:
            logger.info(
                "[SKIP] reason=duplicate_changeCount changeCount=%s transport=%s pull_ms=%.1f",
                change_count,
                active_transport,
                pull_ms,
            )
            return

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

                if resolved_bytes:
                    if target_uti in IMAGE_UTIS:
                        payload = ExtractedPayload(
                            kind="image",
                            uti=target_uti,
                            raw_size=len(resolved_bytes),
                            image_bytes=resolved_bytes,
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
                    else:
                        try:
                            text = resolved_bytes.decode("utf-8").strip("\ufffc")
                            if text.strip():
                                is_url = target_uti == UTI_URL or bool(URL_REGEX.match(text.strip()))
                                payload = ExtractedPayload(
                                    kind="url" if is_url else "text",
                                    uti=target_uti,
                                    raw_size=len(resolved_bytes),
                                    text=text,
                                )
                        except UnicodeDecodeError:
                            pass

        self.last_change_id = change_id

        if payload is None:
            pb_dict = snapshot.get("pasteboard", {}) if isinstance(snapshot.get("pasteboard"), dict) else snapshot
            items = pb_dict.get("items", []) or []
            advertised = [i.get("types", []) for i in items if isinstance(i, dict)]
            logger.info(
                "[SKIP] reason=no_supported_payload changeCount=%s advertised_types=%s pull_ms=%.1f",
                change_count,
                advertised,
                pull_ms,
            )
            return

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
            return

        # Write to Windows System Clipboard
        t_write = time.perf_counter()
        try:
            if payload.kind == "image" and payload.image_bytes is not None:
                base_uti = payload.uti.split(":")[-1]
                formats, (width, height) = self.clipboard.write_image(payload.image_bytes, base_uti)
                write_ms = (time.perf_counter() - t_write) * 1000.0
                total_ms = (time.perf_counter() - t_start) * 1000.0
                self.recent_fingerprints[fp] = time.monotonic()
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

    async def run_forever(self) -> None:
        """Main resilient supervisor loop with automatic transport failover and reconnection."""
        logger.info(
            "[STARTUP] Universal Clipboard PoC starting (mode=%s, requested_udid=%s, heif_support=%s)",
            self.transport_mode,
            self.requested_udid or "auto",
            HEIF_AVAILABLE,
        )
        while True:
            try:
                udid, active_transport = await self._resolve_target_and_transport()
                logger.info("[CONNECTING] udid=%s transport=%s", udid, active_transport)
                await self._ensure_ddi_and_monitor(udid, active_transport)
            except asyncio.CancelledError:
                logger.info("[SHUTDOWN] Universal Clipboard PoC stopped cleanly.")
                raise
            except Exception as exc:
                logger.warning(
                    "[CONNECTION_LOST] transport_mode=%s error=%s: %s — retrying in %.1fs...",
                    self.transport_mode,
                    type(exc).__name__,
                    exc,
                    self.reconnect_delay,
                )
            await asyncio.sleep(self.reconnect_delay)


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
    await coordinator.run_forever()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
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
