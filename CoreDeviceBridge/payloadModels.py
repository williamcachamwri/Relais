from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass
from html.parser import HTMLParser
import io
import logging
from pathlib import Path
import plistlib
import re
import sys
import time
from typing import Any, Optional, cast

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

from pymobiledevice3.remote.core_device.pasteboard_service import (
    GENERAL_PASTEBOARD,
    IMAGE_UTIS,
    POLICY_ALL_RESOLVED,
    RESOLVE_COMMAND,
    UTI_PLAIN_TEXT,
    UTI_TEXT,
    UTI_URL,
    UTI_UTF8_PLAIN_TEXT,
    UTI_WEB_ARCHIVE,
    PasteboardService,
)
from pymobiledevice3.remote.xpc_message import XpcInt64Type

logger = logging.getLogger("universal_clipboard")

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


@dataclass
class ExtractedPayload:
    kind: str  # "text", "url", "image", "file"
    uti: str
    raw_size: int
    text: Optional[str] = None
    html_bytes: Optional[bytes] = None
    image_bytes: Optional[bytes] = None
    file_bytes: Optional[bytes] = None
    file_name: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(self.kind.encode("ascii"))
        h.update(b":")
        h.update(self.uti.encode("ascii"))
        h.update(b":")
        if self.image_bytes is not None:
            h.update(self.image_bytes)
        elif self.file_bytes is not None:
            h.update(self.file_bytes)
            if self.file_name:
                h.update(self.file_name.encode("utf-8", errors="ignore"))
        elif self.text is not None:
            h.update(self.text.encode("utf-8"))
        elif self.html_bytes is not None:
            h.update(self.html_bytes)
        return h.hexdigest()


class HTMLTextExtractor(HTMLParser):
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
        lines = [line.strip("\r") for line in raw.splitlines()]
        return "\n".join(lines).strip("\r\n\ufffc")


def extract_text_from_html(html_bytes: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            html_str = html_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        html_str = html_bytes.decode("utf-8", errors="replace")
    parser = HTMLTextExtractor()
    with contextlib.suppress(Exception):
        parser.feed(html_str)
    return parser.get_text()


def _find_url_in_plist_object(obj: Any, root: Any = None) -> Optional[str]:
    """Recursively search for a valid URL string inside a decoded property list object (handles Cocoa NSKeyedArchiver)."""
    if obj is None:
        return None
    if root is None and isinstance(obj, dict):
        root = obj

    if isinstance(obj, str):
        cleaned = obj.strip("\x00\ufffc\r\n\t ")
        if cleaned.startswith(("http://", "https://", "ftp://", "mailto:")) or URL_REGEX.match(cleaned):
            return cleaned
        return None

    # Handle plistlib.UID dereferencing from NSKeyedArchiver $objects
    if hasattr(obj, "data") and isinstance(obj.data, int):
        idx = obj.data
        if root and isinstance(root, dict) and isinstance(root.get("$objects"), list):
            objs = root["$objects"]
            if 0 <= idx < len(objs):
                return _find_url_in_plist_object(objs[idx], root)

    if isinstance(obj, dict):
        # Specific known Cocoa NSURL archive keys
        for key in ("NS.relative", "relative", "URL", "url", "href", "urlString"):
            val = obj.get(key)
            if val is not None:
                res = _find_url_in_plist_object(val, root)
                if res:
                    return res
        # Check NSKeyedArchiver $objects array
        objects = obj.get("$objects")
        if isinstance(objects, list):
            for item in objects:
                res = _find_url_in_plist_object(item, root or obj)
                if res:
                    return res
        for val in obj.values():
            res = _find_url_in_plist_object(val, root)
            if res:
                return res
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            res = _find_url_in_plist_object(item, root)
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
            url = _find_url_in_plist_object(parsed, parsed)
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

    main_res = plist.get("WebMainResource")
    if not isinstance(main_res, dict):
        return None, None, None

    mime = (main_res.get("WebResourceMIMEType") or "").lower()
    raw_data = main_res.get("WebResourceData")
    if not isinstance(raw_data, bytes):
        return None, None, None

    mime_map = {
        "image/png": "public.png",
        "image/jpeg": "public.jpeg",
        "image/gif": "com.compuserve.gif",
        "image/tiff": "public.tiff",
        "image/webp": "org.webmproject.webp",
    }
    if mime in mime_map:
        return (mime_map[mime], raw_data), None, None

    if mime in ("text/html", "application/xhtml+xml"):
        txt = extract_text_from_html(raw_data)
        return None, txt, raw_data

    return None, None, None


def _inline_bytes(data_map: dict[str, Any], uti: str) -> Optional[bytes]:
    datum = data_map.get(uti)
    if isinstance(datum, (bytes, bytearray)) and datum:
        return bytes(datum)
    if isinstance(datum, str) and datum:
        return datum.encode("utf-8")
    if isinstance(datum, dict):
        raw = datum.get("data")
        if raw is None:
            raw = datum.get("inline")
        if isinstance(raw, (bytes, bytearray)) and raw:
            return bytes(raw)
        if isinstance(raw, str) and raw:
            return raw.encode("utf-8")
    return None


def extract_inline_payload(snapshot: dict[str, Any]) -> Optional[ExtractedPayload]:
    """Inspect snapshot items and return an `ExtractedPayload` if an inline representation is available."""
    pb_dict = snapshot.get("pasteboard", {}) if isinstance(snapshot.get("pasteboard"), dict) else snapshot
    items = pb_dict.get("items", [])
    if not items or not isinstance(items, list):
        return None

    # Pass 1: Look for inline image representations
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
                    uti=txt_uti,
                    raw_size=len(txt_bytes),
                    text=text,
                    html_bytes=html_inline,
                )

        # 2c. Check inline HTML if no plain text was found
        if html_inline is not None:
            text = extract_text_from_html(html_inline)
            if text:
                is_url = bool(URL_REGEX.match(text.strip()))
                return ExtractedPayload(
                    kind="url" if is_url else "text",
                    uti=UTI_HTML,
                    raw_size=len(html_inline),
                    text=text,
                    html_bytes=html_inline,
                )

        # 2d. Check inline webarchive
        web_inline = _inline_bytes(data_map, UTI_WEB_ARCHIVE)
        if web_inline is not None:
            img_pair, arch_text, arch_html = _extract_from_webarchive(web_inline)
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
                    raw_size=len(web_inline),
                    text=arch_text,
                    html_bytes=arch_html,
                )

        # 2e. Check file UTIs
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


def find_all_resolvable_representations(
    snapshot: dict[str, Any], allow_behind_rtf: bool = False
) -> list[tuple[int, str, bool]]:
    """Scan items for all promised representations that can be resolved via `RESOLVE` in priority order."""
    pb_dict = snapshot.get("pasteboard", {}) if isinstance(snapshot.get("pasteboard"), dict) else snapshot
    items = pb_dict.get("items", [])
    if not items or not isinstance(items, list):
        return []

    results: list[tuple[int, str, bool]] = []
    for idx, item in enumerate(items):
        types = item.get("types", [])
        if not types:
            continue
        data_map = item.get("data") or {}

        has_rtf_trap = UTI_RTF in types or UTI_FLAT_RTFD in types
        behind_rtf = has_rtf_trap and not allow_behind_rtf

        # If behind Apple Notes RTF trap, prioritize plain text to resolve fast without ~64s HTML stall
        if behind_rtf:
            for uti in TEXT_UTIS:
                if uti in types and _inline_bytes(data_map, uti) is None:
                    results.append((idx, uti, False))

        # Prioritize URLs
        for uti in URL_UTIS:
            if uti in types and _inline_bytes(data_map, uti) is None:
                results.append((idx, uti, behind_rtf))

        # Images
        for uti in IMAGE_UTIS:
            if uti in types and _inline_bytes(data_map, uti) is None:
                results.append((idx, uti, behind_rtf))

        # File documents
        for uti in UTI_TO_FILE_EXT:
            if uti in types and _inline_bytes(data_map, uti) is None:
                results.append((idx, uti, behind_rtf))

        # Rich text / web archive
        for uti in (UTI_HTML, UTI_WEB_ARCHIVE):
            if uti in types and _inline_bytes(data_map, uti) is None:
                results.append((idx, uti, behind_rtf))

        # Plain text
        for uti in TEXT_UTIS:
            if uti in types and _inline_bytes(data_map, uti) is None:
                if (idx, uti, False) not in results and (idx, uti, True) not in results:
                    results.append((idx, uti, behind_rtf))

    return results


def find_resolvable_Secondary_representation(
    snapshot: dict[str, Any], allow_behind_rtf: bool = False
) -> Optional[tuple[int, str, bool]]:
    """Scan items for promised representations that can be resolved via `RESOLVE`."""
    candidates = find_all_resolvable_representations(snapshot, allow_behind_rtf=allow_behind_rtf)
    return candidates[0] if candidates else None


async def resolve_promised_representation(
    pb_service: PasteboardService,
    item_index: int,
    representation_type: str,
    timeout: float = 8.0,
) -> Optional[bytes]:
    """Send a `RESOLVE` command to `com.apple.coredevice.pasteboardservice` and return the `DATA` bytes."""
    message = {
        "command": RESOLVE_COMMAND,
        "pasteboardName": GENERAL_PASTEBOARD,
        "itemIndex": XpcInt64Type(item_index),
        "type": representation_type,
    }
    try:
        response = await asyncio.wait_for(
            pb_service.service.send_receive_request(message),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.debug("[RESOLVE_TIMEOUT] itemIndex=%d type=%s after %.1fs", item_index, representation_type, timeout)
        return None
    except Exception as exc:
        logger.debug(
            "[RESOLVE_FAILED] itemIndex=%d type=%s error=%s",
            item_index,
            representation_type,
            exc,
        )
        return None

    if isinstance(response, dict) and response.get("command") == "DATA":
        raw = response.get("data")
        if isinstance(raw, (bytes, bytearray)) and raw:
            return bytes(raw)
        if isinstance(raw, str) and raw:
            return raw.encode("utf-8")
    return None


def extract_change_info(snapshot: dict[str, Any]) -> tuple[Optional[str], Optional[int]]:
    """Extract `(nonce_or_uuid, change_count)` identifying a unique clipboard revision."""
    pb_dict = snapshot.get("pasteboard", {}) if isinstance(snapshot.get("pasteboard"), dict) else snapshot
    meta = pb_dict.get("metadata", {})
    if not isinstance(meta, dict):
        meta = {}
    token = meta.get("nonce") or meta.get("persistenceUUID") or pb_dict.get("nonce") or pb_dict.get("persistenceUUID")
    count = meta.get("changeCount")
    if count is None:
        count = pb_dict.get("changeCount")
    return (str(token) if token is not None else None, int(count) if count is not None else None)
