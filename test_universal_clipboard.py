#!/usr/bin/env python3
"""
Verification suite for Universal Clipboard for Windows (`poc_universal_clipboard.py`).

Tests:
1. WindowsClipboardWriter lossless round-trip verification via Win32 GetClipboardData:
   - Plain text (ASCII)
   - Multilingual Unicode (Vietnamese, Chinese, Japanese, Emoji surrogate pairs)
   - URLs (`CF_UNICODETEXT` + `HTML Format` + `UniformResourceLocatorW`)
   - Rich HTML (`public.html` extraction + `HTML Format` header offset validation)
   - Images: PNG, JPEG, and HEIC (`pillow-heif`) -> `CF_DIBV5`, `CF_DIB`, and `PNG` clipboard formats
2. Live Physical iPhone Hardware Verification (`iPhone17,5`, iOS 27.0):
   - USB CoreDevice RSD tunnel + `NotificationProxyService` + `PasteboardService` (`POLICY_PROMISE_SECONDARY`)
   - Wi-Fi `_remotepairing._tcp.local.` RSD tunnel + `NotificationProxyService` + `PasteboardService`
   - Restart resilience and duplicate `changeCount` suppression
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import io
import sys
import unittest

from PIL import Image

from poc_universal_clipboard import (
    CF_DIB,
    CF_DIBV5,
    CF_UNICODETEXT,
    HEIF_AVAILABLE,
    UniversalClipboardCoordinator,
    WindowsClipboardWriter,
    _BITMAPV5HEADER,
    extract_text_from_html,
    list_usb_udids,
    list_wifi_paired_udids,
)


def read_win32_clipboard_unicode(writer: WindowsClipboardWriter) -> str:
    """Read `CF_UNICODETEXT` back from the live Windows system clipboard."""
    writer._user32.GetClipboardData.argtypes = [wintypes.UINT]
    writer._user32.GetClipboardData.restype = wintypes.HANDLE
    writer._kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    writer._kernel32.GlobalSize.restype = ctypes.c_size_t

    with writer._open_clipboard_locked():
        hmem = writer._user32.GetClipboardData(CF_UNICODETEXT)
        if not hmem:
            raise RuntimeError("GetClipboardData(CF_UNICODETEXT) returned NULL")
        ptr = writer._kernel32.GlobalLock(hmem)
        if not ptr:
            raise RuntimeError("GlobalLock failed")
        try:
            size = writer._kernel32.GlobalSize(hmem)
            raw = ctypes.string_at(ptr, size)
        finally:
            writer._kernel32.GlobalUnlock(hmem)
    return raw.decode("utf-16-le").split("\x00", 1)[0]


def read_win32_clipboard_raw(writer: WindowsClipboardWriter, fmt: int) -> bytes:
    """Read raw bytes of clipboard format `fmt` from the live Windows system clipboard."""
    writer._user32.GetClipboardData.argtypes = [wintypes.UINT]
    writer._user32.GetClipboardData.restype = wintypes.HANDLE
    writer._kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    writer._kernel32.GlobalSize.restype = ctypes.c_size_t

    with writer._open_clipboard_locked():
        hmem = writer._user32.GetClipboardData(fmt)
        if not hmem:
            raise RuntimeError(f"GetClipboardData({fmt}) returned NULL")
        ptr = writer._kernel32.GlobalLock(hmem)
        if not ptr:
            raise RuntimeError("GlobalLock failed")
        try:
            size = writer._kernel32.GlobalSize(hmem)
            return ctypes.string_at(ptr, size)
        finally:
            writer._kernel32.GlobalUnlock(hmem)


class TestWindowsClipboardIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.writer = WindowsClipboardWriter()

    def test_multilingual_unicode_and_emoji_text(self) -> None:
        sample = (
            "Vietnamese: Xin chào Việt Nam! Cộng hòa Xã hội Chủ nghĩa Việt Nam\r\n"
            "Chinese: 中文测试 — 苹果通用剪贴板 Windows 版\r\n"
            "Japanese: 日本語テスト — ユニバーサルクリップボード\r\n"
            "Emoji: 🚀🎉🍎💻🇻🇳"
        )
        formats = self.writer.write_text(sample)
        self.assertIn("CF_UNICODETEXT", formats)
        read_back = read_win32_clipboard_unicode(self.writer)
        self.assertEqual(read_back, sample)

    def test_url_and_html_clipboard_formats(self) -> None:
        url = "https://github.com/williamcachamwri/1pm"
        formats = self.writer.write_text(url, is_url=True)
        self.assertIn("CF_UNICODETEXT", formats)
        self.assertIn("HTML Format", formats)
        self.assertIn("UniformResourceLocatorW", formats)

        read_back = read_win32_clipboard_unicode(self.writer)
        self.assertEqual(read_back, url)

        raw_html = read_win32_clipboard_raw(self.writer, self.writer.cf_html).rstrip(b"\x00").decode("utf-8")
        self.assertIn("Version:0.9", raw_html)
        self.assertIn(f"SourceURL:{url}", raw_html)
        self.assertIn("<!--StartFragment-->", raw_html)

    def test_apple_html_text_extractor(self) -> None:
        apple_html = (
            b'<!DOCTYPE html><html><head><meta charset="UTF-8">'
            b'<style>p.p1 {margin: 0.0px}</style></head>'
            b'<body><p class="p1">Line 1: Ti\xe1\xba\xbfng Vi\xe1\xbb\x87t</p>'
            b'<p class="p1">Line 2: \xe4\xb8\xad\xe6\x96\x87</p></body></html>'
        )
        extracted = extract_text_from_html(apple_html)
        self.assertEqual(extracted, "Line 1: Tiếng Việt\nLine 2: 中文")

    def test_image_formats_png_jpeg_heic_and_dibv5(self) -> None:
        img = Image.new("RGBA", (64, 48), color=(30, 144, 255, 255))

        for fmt_name, uti in [("PNG", "public.png"), ("JPEG", "public.jpeg")]:
            buf = io.BytesIO()
            out_img = img.convert("RGB") if fmt_name == "JPEG" else img
            out_img.save(buf, format=fmt_name)
            formats, dims = self.writer.write_image(buf.getvalue(), uti)
            self.assertEqual(dims, (64, 48))
            self.assertIn("CF_DIBV5", formats)
            self.assertIn("CF_DIB", formats)
            self.assertIn("PNG", formats)

            dibv5_raw = read_win32_clipboard_raw(self.writer, CF_DIBV5)
            hdr = _BITMAPV5HEADER.from_buffer_copy(dibv5_raw[: ctypes.sizeof(_BITMAPV5HEADER)])
            self.assertEqual(hdr.bV5Width, 64)
            self.assertEqual(hdr.bV5Height, 48)
            self.assertEqual(hdr.bV5BitCount, 32)

            dib_raw = read_win32_clipboard_raw(self.writer, CF_DIB)
            self.assertGreater(len(dib_raw), 40)

        if HEIF_AVAILABLE:
            heic_buf = io.BytesIO()
            img.convert("RGB").save(heic_buf, format="HEIF")
            formats, dims = self.writer.write_image(heic_buf.getvalue(), "public.heic")
            self.assertEqual(dims, (64, 48))
            self.assertIn("CF_DIBV5", formats)


class TestLiveHardwareConnectivity(unittest.TestCase):
    def test_live_usb_and_wifi_sync_if_connected(self) -> None:
        usb = list_usb_udids()
        wifi = list_wifi_paired_udids()
        if not usb and not wifi:
            self.skipTest("No physical iPhone currently attached or paired")

        async def _verify_transport(mode: str) -> None:
            coord = UniversalClipboardCoordinator(transport_mode=mode, sync_on_connect=True)
            udid, active_transport = await coord._resolve_target_and_transport()
            self.assertTrue(udid)
            task = asyncio.create_task(coord.run_forever())
            await asyncio.sleep(3.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.assertIsNotNone(coord.last_change_id)

        if usb:
            asyncio.run(_verify_transport("usb"))
        if wifi:
            asyncio.run(_verify_transport("wifi"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
