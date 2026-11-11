from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import sys
import threading
from typing import Any, Optional

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from CoreDeviceBridge.deviceTunnel import pair_device_for_wifi_over_usb
from CoreDeviceBridge.bridgeServer import GLOBAL_LOG_HANDLER, start_local_api_server
from CoreDeviceBridge.continuityCoordinator import UniversalClipboardCoordinator
from CoreDeviceBridge.airdropService import AIRDROP_SERVICE

logger = logging.getLogger("universal_clipboard")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Universal Clipboard for Windows (CoreDevice / RemoteXPC Pasteboard Synchronizer)"
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


def setup_global_exception_handlers(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Register top-level sys, threading, and asyncio unhandled exception handlers for robust error handling."""

    def _handle_sys_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error(
            "[UNHANDLED_EXCEPTION] Uncaught top-level exception: %s",
            exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        logger.error(
            "[THREAD_EXCEPTION] Thread '%s' uncaught exception: %s",
            args.thread.name,
            args.exc_value,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def _handle_asyncio_exception(active_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        msg = context.get("message", "Unhandled asyncio exception")
        exc = context.get("exception")
        logger.error("[ASYNCIO_EXCEPTION] %s: %s", msg, exc, exc_info=exc)

    sys.excepthook = _handle_sys_exception
    threading.excepthook = _handle_thread_exception
    if loop is not None:
        loop.set_exception_handler(_handle_asyncio_exception)


async def _async_main(args: argparse.Namespace) -> int:
    setup_global_exception_handlers(asyncio.get_running_loop())
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
    try:
        AIRDROP_SERVICE.start()
    except Exception as exc:
        logger.warning("[AirDrop] Could not auto-start AirDrop receiver: %s", exc)
    await coordinator.run()
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
    setup_global_exception_handlers()

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
