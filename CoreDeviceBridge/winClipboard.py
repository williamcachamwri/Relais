from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
import io
import logging
import os
from pathlib import Path
import struct
import sys
import time
from typing import Optional

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from PIL import Image, ImageGrab, ImageOps

from CoreDeviceBridge.payloadModels import (
    ExtractedPayload,
    IMAGE_EXTENSIONS,
    URL_REGEX,
    UTI_URL,
    UTI_UTF8_PLAIN_TEXT,
    HEIF_AVAILABLE,
)

logger = logging.getLogger("universal_clipboard")

CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
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


def build_cf_html_payload(html_fragment: str, source_url: Optional[str] = None) -> bytes:
    """Pack an HTML snippet into standard Windows CF_HTML format with byte offsets."""
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

    def read_payload(self) -> Optional[tuple[ExtractedPayload, Optional[tuple[int, int]]]]:
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
                continue

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

    @contextlib.contextmanager
    def _open_and_empty_clipboard(self):
        """Acquire Win32 clipboard lock and empty it atomically with backoff retries to prevent WinError 1418."""
        last_err = 0
        for attempt in range(self.max_retries):
            if self._user32.OpenClipboard(None):
                try:
                    if self._user32.EmptyClipboard():
                        yield
                        return
                    last_err = ctypes.get_last_error()
                finally:
                    self._user32.CloseClipboard()
                    self.last_sequence_number = int(self._user32.GetClipboardSequenceNumber())
            else:
                last_err = ctypes.get_last_error()
            time.sleep(self.retry_delay_sec * (1.0 + 0.3 * attempt))
        raise RuntimeError(f"Open/Empty clipboard failed after {self.max_retries} attempts (WinError={last_err})")

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
        with self._open_and_empty_clipboard():
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

            # 1. Lossless PNG buffer
            png_io = io.BytesIO()
            rgba.save(png_io, format="PNG")
            png_bytes = png_io.getvalue()

            # 2. Classic BMP / CF_DIB buffer
            bmp_io = io.BytesIO()
            rgba.convert("RGB").save(bmp_io, format="BMP")
            dib_bytes = bmp_io.getvalue()[14:]

            # 3. 32-bit BGRA bottom-up CF_DIBV5 buffer
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

    def write_image(
        self,
        image_bytes: bytes,
        uti: str,
    ) -> tuple[list[str], tuple[int, int]]:
        """Decode image (PNG, JPEG, HEIC, TIFF, GIF, WebP) and write native `CF_DIBV5`, `CF_DIB`, and `PNG`."""
        if uti == "public.heic" and not HEIF_AVAILABLE:
            raise RuntimeError("HEIC image received but pillow-heif is not installed")

        dibv5_bytes, dib_bytes, png_bytes, dimensions = self._encode_dibv5_and_dib_and_png(image_bytes)
        formats_written: list[str] = []

        with self._open_and_empty_clipboard():
            self._set_raw_format(CF_DIBV5, dibv5_bytes)
            formats_written.append("CF_DIBV5")

            self._set_raw_format(CF_DIB, dib_bytes)
            formats_written.append("CF_DIB")

            if self.cf_png:
                self._set_raw_format(self.cf_png, png_bytes)
                formats_written.append("PNG")

        return formats_written, dimensions
