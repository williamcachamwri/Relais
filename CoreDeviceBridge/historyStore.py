from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Optional

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from PIL import Image, ImageOps

from CoreDeviceBridge.payloadModels import ExtractedPayload

logger = logging.getLogger("universal_clipboard")

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
            existing_pinned = False
            filtered: list[dict[str, Any]] = []
            for old in self._items:
                if old.get("fingerprint") == fp:
                    existing_pinned = bool(old.get("pinned"))
                    if (
                        old.get("source") == "pc"
                        and source != "pc"
                        and (now - float(old.get("timestamp", 0.0))) < 15.0
                    ):
                        entry["source"] = "pc"
                        entry["device_model"] = "This device"
                        entry["product_type"] = "PC"
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
