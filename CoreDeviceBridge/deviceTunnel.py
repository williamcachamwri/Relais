from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import socket
import struct
import subprocess
from typing import Any, Optional

import ifaddr
from pymobiledevice3 import bonjour, usbmux
from pymobiledevice3.exceptions import (
    AlreadyMountedError,
    RemotePairingCompletedError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.pair_records import iter_remote_paired_identifiers
from pymobiledevice3.remote import tunnel_service, userspace_tunnel
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
from pymobiledevice3.services.mobile_image_mounter import auto_mount

logger = logging.getLogger("universal_clipboard")

_BONJOUR_PATCHED = False
_LAST_KNOWN_WIFI_IPS: set[str] = set()


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
    """Fix Windows mDNS issues in pymobiledevice3.bonjour:
    1. Scope ID safe handling on IPv6 fe80 addresses.
    2. Ephemeral UDP sockets per local IPv4 so AppleMobileDeviceLauncher doesn't swallow replies.
    3. Direct unicast query fallback to ARP table neighbors.
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
