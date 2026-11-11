from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from pathlib import Path
import sys
import time
from typing import Any, Optional

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from pymobiledevice3.exceptions import (
    AlreadyMountedError,
    PyMobileDevice3Exception,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.core_device.pasteboard_service import (
    GENERAL_PASTEBOARD,
    IMAGE_UTIS,
    PASTEBOARD_CHANGED_NOTIFICATION,
    POLICY_ALL_PROMISED,
    POLICY_ALL_RESOLVED,
    POLICY_PROMISE_SECONDARY,
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
from pymobiledevice3.remote.xpc_message import XpcInt64Type
from pymobiledevice3.services.mobile_image_mounter import auto_mount
from pymobiledevice3.services.notification_proxy import NotificationProxyService

from CoreDeviceBridge.payloadModels import (
    ExtractedPayload,
    URL_REGEX,
    UTI_HTML,
    UTI_RTF,
    TEXT_UTIS,
    URL_UTIS,
    IMAGE_UTIS,
    UTI_TO_FILE_EXT,
    extract_change_info,
    extract_inline_payload,
    extract_text_from_html,
    find_all_resolvable_representations,
    find_resolvable_Secondary_representation,
    resolve_promised_representation,
    _extract_from_webarchive,
    _extract_url_from_raw,
)
from CoreDeviceBridge.winClipboard import WindowsClipboardWriter
from CoreDeviceBridge.historyStore import (
    ClipboardHistoryStore,
    friendly_device_name,
)
from CoreDeviceBridge.deviceTunnel import (
    async_list_usb_udids,
    list_usb_udids,
    list_wifi_paired_udids,
    open_rsd_tunnel,
)

logger = logging.getLogger("universal_clipboard")

PASTEBOARD_NOTIFICATIONS = (
    "com.apple.pasteboard.notify.changed",
    "com.apple.UIKit.pasteboard.changed",
    "com.apple.pasteboard.changed",
    "com.apple.pasteboard.pboard-changed",
)
SPRINGBOARD_LOCKSTATE_NOTIFICATION = "com.apple.springboard.lockstate"
KEYBAG_LOCK_STATUS_NOTIFICATION = "com.apple.mobile.keybagd.lock_status"


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
            return []

        if kind == "image":
            img_bytes = self.history.get_image_bytes(item_id)
            if not img_bytes:
                raise RuntimeError("Saved image file is missing")
            formats, _ = self.clipboard.write_image(img_bytes, "public.png")
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
            return False
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
        elif payload.text is not None:
            utis = [UTI_UTF8_PLAIN_TEXT, UTI_PLAIN_TEXT, UTI_TEXT]
            items = [text_item(payload.text, utis=utis)]
        else:
            return False

        async with self._pasteboard_io_lock:
            try:
                async with PasteboardService(rsd) as pb_service:
                    response = await asyncio.wait_for(
                        pb_service.set(items, GENERAL_PASTEBOARD),
                        timeout=10.0,
                    )
                ch_id = extract_change_info(response) if isinstance(response, dict) else (None, None)
                if ch_id[1] is not None:
                    self._own_change_ids.add(ch_id)
                    self.last_change_id = ch_id

                total_ms = (time.perf_counter() - t_start) * 1000.0
                self.last_sync_ms = total_ms
                self.total_synced += 1

                if record_history:
                    self.history.add_item(
                        payload=payload,
                        change_count=ch_id[1],
                        transport=active_transport,
                        product_type=rsd.product_type,
                        ios_version=rsd.product_version,
                        latency_ms=total_ms,
                        dimensions=dimensions,
                        source="pc",
                    )

                logger.info(
                    "[CLIPBOARD_PUSHED] status=SUCCESS direction=Windows->iPhone transport=%s changeCount=%s "
                    "type=%s uti=%s size=%dB total_ms=%.1f",
                    active_transport,
                    ch_id[1],
                    payload.kind,
                    payload.uti,
                    payload.raw_size,
                    total_ms,
                )
                return True
            except Exception as exc:
                logger.error(
                    "[CLIPBOARD_PUSHED] status=ERROR direction=Windows->iPhone type=%s size=%dB error=%s: %s",
                    payload.kind,
                    payload.raw_size,
                    type(exc).__name__,
                    exc,
                )
                return False

    async def _resolve_device_and_transport(self) -> tuple[str, str]:
        """Detect attached USB iPhones and saved Wi-Fi RemotePairing records, resolving the active target."""
        usb_udids = await async_list_usb_udids()
        wifi_udids = list_wifi_paired_udids()
        self.last_usb_udids = usb_udids
        self.last_wifi_udids = wifi_udids

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
        """Subscribe to pasteboard notifications + start fallback poller and Windows clipboard watcher."""
        pull_trigger = asyncio.Event()
        session_cancel = asyncio.Event()
        self._active_rsd = rsd
        self._active_pull_trigger = pull_trigger
        self._session_cancel_event = session_cancel
        pull_reason = "notification"
        pending_locked_catchup = False

        async with NotificationProxyService(rsd) as notif_service:
            for notif_name in PASTEBOARD_NOTIFICATIONS:
                with contextlib.suppress(Exception):
                    await notif_service.notify_register_dispatch(notif_name)

            with contextlib.suppress(Exception):
                await notif_service.notify_register_dispatch(SPRINGBOARD_LOCKSTATE_NOTIFICATION)
                await notif_service.notify_register_dispatch(KEYBAG_LOCK_STATUS_NOTIFICATION)

            self.state = "monitoring"
            self.state_message = f"Connected to {friendly_device_name(rsd.product_type)} · iOS {rsd.product_version} ({active_transport})"
            logger.info(
                "[SUBSCRIBED] transport=%s notification=%s service=%s policy=POLICY_PROMISE_SECONDARY",
                active_transport,
                PASTEBOARD_NOTIFICATIONS[0],
                PasteboardService.SERVICE_NAME,
            )

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
                    if name in PASTEBOARD_NOTIFICATIONS:
                        logger.info(
                            "[NOTIFICATION] received=%s transport=%s state=%s",
                            name,
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
                        if pending_locked_catchup or state == 0:
                            pull_reason = "unlock_catchup"
                            pull_trigger.set()

            async def _pasteboard_fallback_poller() -> None:
                """Safety-net periodic poller (every 1.5s) to catch iOS Share Sheet 'Sao chép' events."""
                nonlocal pull_reason
                last_polled_count: Optional[int] = None
                while True:
                    await asyncio.sleep(1.5)
                    if session_cancel.is_set():
                        break
                    try:
                        async with self._pasteboard_io_lock:
                            async with PasteboardService(rsd) as pb_service:
                                snap = await asyncio.wait_for(
                                    pb_service.get(GENERAL_PASTEBOARD, POLICY_ALL_PROMISED),
                                    timeout=3.0,
                                )
                        ch_id = extract_change_info(snap)
                        cur_count = ch_id[1]
                        if cur_count is None:
                            continue
                        last_processed_count = self.last_change_id[1] if self.last_change_id else None
                        if cur_count == last_processed_count or cur_count == last_polled_count:
                            continue
                        if ch_id in self._own_change_ids:
                            continue
                        last_polled_count = cur_count
                        pull_reason = "fallback_poll"
                        pull_trigger.set()
                    except Exception:
                        pass

            async def _pull_worker() -> None:
                nonlocal pending_locked_catchup
                while True:
                    await pull_trigger.wait()
                    pull_trigger.clear()
                    reason = pull_reason
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
                    await asyncio.sleep(0.25)
                    try:
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
                    except asyncio.CancelledError:
                        break
                    except Exception as watcher_err:
                        logger.debug("[WIN_WATCH_ERROR] %s", watcher_err)

            reader_task = asyncio.create_task(_notification_reader(), name="notif-reader")
            poller_task = asyncio.create_task(_pasteboard_fallback_poller(), name="fallback-poller")
            worker_task = asyncio.create_task(_pull_worker(), name="pull-worker")
            cancel_task = asyncio.create_task(_transport_switch_watcher(), name="transport-watcher")
            win_watch_task = asyncio.create_task(_windows_clipboard_watcher(), name="win-clip-watcher")
            try:
                done, pending = await asyncio.wait(
                    {reader_task, poller_task, worker_task, cancel_task, win_watch_task},
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
                for task in (reader_task, poller_task, worker_task, cancel_task, win_watch_task):
                    if not task.done():
                        task.cancel()

    async def _pull_and_update_clipboard(
        self,
        rsd: RemoteServiceDiscoveryService,
        pb_service: PasteboardService,
        active_transport: str,
        reason: str,
    ) -> bool:
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

        # If primary representations were promised, resolve candidate representations
        if payload is None:
            candidates = find_all_resolvable_representations(
                snapshot, allow_behind_rtf=self.allow_slow_resolve
            )
            for item_idx, target_uti, behind_rtf in candidates:
                resolve_timeout = 10.0 if not behind_rtf else 15.0
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
                except Exception as r_err:
                    logger.debug("[RESOLVE_FAIL] itemIndex=%d uti=%s err=%s", item_idx, target_uti, r_err)
                    resolved_bytes = None

                if resolved_bytes:
                    if target_uti in IMAGE_UTIS:
                        payload = ExtractedPayload(
                            kind="image",
                            uti=target_uti,
                            raw_size=len(resolved_bytes),
                            image_bytes=resolved_bytes,
                        )
                        break
                    elif target_uti == UTI_WEB_ARCHIVE:
                        img_pair, arch_text, arch_html = _extract_from_webarchive(resolved_bytes)
                        if img_pair is not None:
                            payload = ExtractedPayload(
                                kind="image",
                                uti=f"{UTI_WEB_ARCHIVE}:{img_pair[0]}",
                                raw_size=len(img_pair[1]),
                                image_bytes=img_pair[1],
                            )
                            break
                        elif arch_text:
                            is_url = bool(URL_REGEX.match(arch_text.strip()))
                            payload = ExtractedPayload(
                                kind="url" if is_url else "text",
                                uti=UTI_URL if is_url else UTI_UTF8_PLAIN_TEXT,
                                raw_size=len(resolved_bytes),
                                text=arch_text,
                                html_bytes=arch_html,
                            )
                            break
                    elif target_uti in (UTI_HTML, UTI_RTF):
                        txt = extract_text_from_html(resolved_bytes)
                        if txt and txt.strip():
                            is_url = bool(URL_REGEX.match(txt.strip()))
                            payload = ExtractedPayload(
                                kind="url" if is_url else "text",
                                uti=UTI_URL if is_url else target_uti,
                                raw_size=len(resolved_bytes),
                                text=txt,
                                html_bytes=resolved_bytes if target_uti == UTI_HTML else None,
                            )
                            break
                    elif target_uti in URL_UTIS:
                        extracted_url = _extract_url_from_raw(resolved_bytes)
                        if extracted_url:
                            payload = ExtractedPayload(
                                kind="url",
                                uti=target_uti,
                                raw_size=len(resolved_bytes),
                                text=extracted_url,
                            )
                            break
                    elif target_uti in TEXT_UTIS:
                        try:
                            txt = resolved_bytes.decode("utf-8").strip("\ufffc")
                        except UnicodeDecodeError:
                            txt = None
                        if txt and txt.strip():
                            is_url = bool(URL_REGEX.match(txt.strip()))
                            payload = ExtractedPayload(
                                kind="url" if is_url else "text",
                                uti=UTI_URL if is_url else target_uti,
                                raw_size=len(resolved_bytes),
                                text=txt,
                            )
                            break

        # Fallback: if primary representations were promised and no RTF stall trap is present, pull fully resolved snapshot
        if payload is None:
            pb_dict = snapshot.get("pasteboard", {}) if isinstance(snapshot.get("pasteboard"), dict) else snapshot
            items = pb_dict.get("items", []) or []
            has_rtf = any(
                "public.rtf" in i.get("types", []) or "com.apple.flat-rtfd" in i.get("types", [])
                for i in items if isinstance(i, dict)
            )
            if not has_rtf or self.allow_slow_resolve:
                with contextlib.suppress(Exception):
                    full_snap = await asyncio.wait_for(
                        pb_service.get(GENERAL_PASTEBOARD, POLICY_ALL_RESOLVED),
                        timeout=8.0,
                    )
                    payload = extract_inline_payload(full_snap)

        if payload is None:
            self.last_change_id = change_id
            pb_dict = snapshot.get("pasteboard", {}) if isinstance(snapshot.get("pasteboard"), dict) else snapshot
            items = pb_dict.get("items", []) or []
            advertised = [i.get("types", []) for i in items if isinstance(i, dict)]
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

        self.recent_fingerprints[fp] = time.monotonic()

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
                formats, (width, height) = self.clipboard.write_image(
                    payload.image_bytes,
                    base_uti,
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
            return False
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

    async def run(self) -> None:
        """Outer supervisor loop with automatic reconnection over USB or Wi-Fi."""
        self.loop = asyncio.get_running_loop()
        while True:
            try:
                target_udid, active_transport = await self._resolve_device_and_transport()
                logger.info(
                    "[CONNECTING] udid=%s transport=%s requested_udid=%s transport_mode=%s",
                    target_udid,
                    active_transport,
                    self.requested_udid,
                    self.transport_mode,
                )
                await self._ensure_ddi_and_monitor(target_udid, active_transport)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state = "disconnected"
                self.state_message = f"Disconnected: {exc}. Retrying in {self.reconnect_delay:.0f}s..."
                logger.warning(
                    "[DISCONNECTED] error=%s: %s (reconnecting in %.1fs...)",
                    type(exc).__name__,
                    exc,
                    self.reconnect_delay,
                )
            await asyncio.sleep(self.reconnect_delay)
