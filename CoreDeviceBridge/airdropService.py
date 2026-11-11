from __future__ import annotations

import collections
import contextlib
from dataclasses import asdict, dataclass
import datetime
import io
import json
import logging
import os
from pathlib import Path
import plistlib
import queue
import re
import shutil
import socket
import ssl
import stat
import struct
import threading
import time
from typing import Any, Optional
import unicodedata
import uuid
import zlib

logger = logging.getLogger("universal_clipboard")

MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GiB limit
MAX_ARCHIVE_BYTES = MAX_FILE_BYTES + 32 * 1024 * 1024
MAX_FILES = 120

MEDIA_CAPABILITIES: dict[str, Any] = {
    "SupportsAdjustmentBaseResources": True,
    "Version": 3,
    "Codecs": {
        "hvc1": {
            "Profiles": {
                "VTIsHDRAllowedOnDevice": True,
                "VTSupportedProfiles": [1, 2, 3, 4],
                "VTDoViIsHardwareAccelerated": True,
                "VTDoViSupportedLevels": ["01", "02", "03", "04", "05", "06", "07", "08", "09"],
                "VTDoViSupportedProfiles": ["05", "08"],
            }
        }
    },
    "ContainerFormats": {
        "public.heif-standard": {
            "HeifSubtypes": ["public.avci", "public.avif", "public.heic", "public.heics", "public.heif"]
        }
    },
    "Vendor": {
        "com.apple": {
            "LivePhotoFormatVersion": "1",
            "AssetBundleFormatVersion": "1",
        }
    },
}

MEDIA_CAPABILITIES_BYTES = json.dumps(MEDIA_CAPABILITIES).encode("utf-8")


def safe_name(name: str) -> str:
    """Sanitize incoming peer-supplied filename to prevent directory traversal and invalid Windows characters."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = "".join(
        "_" if c in '<>:"/\\|?*' or unicodedata.category(c).startswith("C") else c
        for c in base
    )
    base = base[:180].strip(" .")
    if not base:
        base = "unnamed_transfer"
    stem = base.split(".")[0]
    if re.fullmatch(r"(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])", stem, re.IGNORECASE):
        base = "_" + base
    return base


def mark_of_the_web(path: Path) -> None:
    """Attach NTFS Zone.Identifier stream so Windows SmartScreen and Defender inspect downloaded files."""
    try:
        zone_file = f"{path}:Zone.Identifier"
        with open(zone_file, "w", encoding="ascii") as f:
            f.write("[ZoneTransfer]\r\nZoneId=3\r\nReferrerUrl=airdrop:\r\n")
    except OSError as err:
        logger.debug(f"[AirDrop] Could not set Mark of the Web on {path.name}: {err}")


def decode_dvzip(body: bytes) -> bytes:
    """Inflate Apple DVZip or standard gzip stream."""
    if body.startswith(b"\x1f\x8b"):
        decoder = zlib.decompressobj(31)
        result = decoder.decompress(body, MAX_ARCHIVE_BYTES + 1)
        if len(result) > MAX_ARCHIVE_BYTES or decoder.unconsumed_tail:
            raise ValueError("Oversized gzip payload")
        return result

    out = bytearray()
    offset = 0
    body_len = len(body)
    while offset < body_len:
        if offset + 4 > body_len:
            raise ValueError("Truncated DVZip header")
        (value,) = struct.unpack_from(">I", body, offset)
        offset += 4
        size = value & 0x7FFFFFFF
        if not size or size > body_len - offset:
            raise ValueError("Invalid DVZip block length")
        block = body[offset : offset + size]
        offset += size
        left = MAX_ARCHIVE_BYTES - len(out)
        if value & 0x80000000:
            decoded = block
        else:
            decoder = zlib.decompressobj(15)
            decoded = decoder.decompress(block, left + 1)
        if len(decoded) > left:
            raise ValueError("Archive exceeds maximum permitted size")
        out.extend(decoded)
    return bytes(out)


def parse_cpio(data: bytes) -> list[tuple[str, bytes]]:
    """Parse standard POSIX odc or newc archives received from Apple AirDrop clients."""
    files: list[tuple[str, bytes]] = []
    offset = 0
    total = 0
    members = 0
    data_len = len(data)

    while True:
        members += 1
        if members > MAX_FILES * 4:
            raise ValueError("Archive member limit exceeded")
        if offset + 6 > data_len:
            break
        magic = data[offset : offset + 6]
        if magic == b"070707":  # odc format
            header = data[offset : offset + 76]
            if len(header) != 76:
                raise ValueError("Truncated odc header")
            try:
                mode = int(header[18:24], 8)
                namesize = int(header[59:65], 8)
                size = int(header[65:76], 8)
            except ValueError as err:
                raise ValueError("Corrupt odc header fields") from err
            offset += 76
            aligned = False
        elif magic in (b"070701", b"070702"):  # newc format
            header = data[offset : offset + 110]
            if len(header) != 110:
                raise ValueError("Truncated newc header")
            try:
                fields = [int(header[6 + i * 8 : 14 + i * 8], 16) for i in range(13)]
            except ValueError as err:
                raise ValueError("Corrupt newc header fields") from err
            mode, size, namesize = fields[1], fields[6], fields[11]
            offset += 110
            aligned = True
        else:
            break

        if not 1 <= namesize <= 4096 or not 0 <= size <= MAX_ARCHIVE_BYTES or offset + namesize > data_len:
            raise ValueError("Archive entry bounds violated")

        raw_name = data[offset : offset + namesize]
        if not raw_name.endswith(b"\x00"):
            raise ValueError("Non-null-terminated filename in archive")
        filename = raw_name[:-1].decode("utf-8", errors="replace")
        offset += namesize

        if aligned:
            offset = (offset + 3) & ~3
        if offset + size > data_len:
            raise ValueError("Truncated archive entry payload")

        payload = data[offset : offset + size]
        offset += size
        if aligned:
            offset = (offset + 3) & ~3

        if filename == "TRAILER!!!":
            if not files:
                raise ValueError("Archive contained no regular file entries")
            return files

        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            continue  # Ignore symlinks and device files for safety

        clean_file = safe_name(filename)
        if clean_file.startswith("._"):
            continue  # Ignore AppleDouble resource fork

        total += size
        if total > MAX_FILE_BYTES or len(files) >= MAX_FILES:
            raise ValueError("Transfer exceeds size limits")
        files.append((clean_file, payload))

    return files


def generate_self_signed_cert(cert_dir: Path, sid: str) -> tuple[Path, Path]:
    """Create or load cached self-signed RSA certificate matching the receiver's AirDrop session ID."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "airdrop_cert.pem"
    key_path = cert_dir / "airdrop_key.pem"

    if cert_path.exists() and key_path.exists():
        with contextlib.suppress(Exception):
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
            fresh = existing.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)
            if fresh:
                return cert_path, key_path

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sid)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=10))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(private_key, hashes.SHA256())
    )

    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


@dataclass
class AirDropTransferRecord:
    id: str
    sender_name: str
    file_names: list[str]
    total_bytes: int
    received_bytes: int
    status: str  # "pending_consent", "transferring", "completed", "declined", "failed"
    timestamp: float
    time_label: str
    saved_paths: list[str]
    error_message: Optional[str] = None


class AirDropService:
    """Apple AirDrop protocol receiver service integrated into Relais."""

    def __init__(self, computer_name: str = "Relais PC", port: int = 8771) -> None:
        self.computer_name = computer_name
        self.model_name = "MacBookPro18,3"
        self.port = port
        self.sid = uuid.uuid4().hex[:12]
        self._enabled = False
        self._lock = threading.Lock()
        self._http_server: Optional[Any] = None
        self._server_thread: Optional[threading.Thread] = None
        self._mdns_thread: Optional[threading.Thread] = None
        self._mdns_stop = threading.Event()

        app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        self.base_dir = app_data / "Relais" / "AirDrop"
        self.cert_dir = self.base_dir / "certs"
        self.downloads_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads" / "AirDrop"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._pending_consent: dict[str, dict[str, Any]] = {}
        self._transfers: collections.deque[AirDropTransferRecord] = collections.deque(maxlen=60)
        self._transfers_lock = threading.Lock()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            with self._transfers_lock:
                active = [
                    asdict(t)
                    for t in self._transfers
                    if t.status in ("pending_consent", "transferring")
                ]
                recent = [asdict(t) for t in self._transfers]
            return {
                "enabled": self._enabled,
                "computer_name": self.computer_name,
                "model_name": self.model_name,
                "port": self.port,
                "sid": self.sid,
                "downloads_dir": str(self.downloads_dir),
                "active_transfers": active,
                "history": recent[:25],
                "pending_count": len(self._pending_consent),
            }

    def set_computer_name(self, name: str) -> None:
        clean = "".join(c for c in name if c.isprintable()).strip()[:40]
        if clean:
            self.computer_name = clean

    def set_downloads_dir(self, path: str) -> bool:
        try:
            p = Path(path).resolve()
            p.mkdir(parents=True, exist_ok=True)
            self.downloads_dir = p
            return True
        except Exception as err:
            logger.error(f"[AirDrop] Failed to set download directory {path}: {err}")
            return False

    def submit_consent(self, ask_id: str, accept: bool) -> bool:
        with self._lock:
            pending = self._pending_consent.get(ask_id)
            if not pending:
                return False
            pending["result"] = accept
            pending["event"].set()
            return True

    def start(self) -> bool:
        with self._lock:
            if self._enabled:
                return True
            try:
                cert_file, key_file = generate_self_signed_cert(self.cert_dir, self.sid)
                from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

                service_ref = self

                class AirDropHandler(BaseHTTPRequestHandler):
                    protocol_version = "HTTP/1.1"

                    def log_message(self, format: str, *args: Any) -> None:
                        pass

                    def answer_plist(self, status: int, data: dict[str, Any]) -> None:
                        body = plistlib.dumps(data, fmt=plistlib.FMT_BINARY)
                        self.send_response(status)
                        self.send_header("Content-Type", "application/x-apple-binary-plist")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def do_POST(self) -> None:
                        try:
                            if self.path == "/Discover":
                                self.answer_plist(
                                    200,
                                    {
                                        "ReceiverComputerName": service_ref.computer_name,
                                        "ReceiverModelName": service_ref.model_name,
                                        "ReceiverMediaCapabilities": MEDIA_CAPABILITIES_BYTES,
                                        "IsAirDropable": True,
                                    },
                                )
                                return

                            if self.path == "/Ask":
                                length = int(self.headers.get("Content-Length", "0") or "0")
                                if length <= 0 or length > 512 * 1024:
                                    self.send_response(400)
                                    self.end_headers()
                                    return
                                raw_bytes = self.rfile.read(length)
                                meta = plistlib.loads(raw_bytes)
                                if not isinstance(meta, dict):
                                    self.send_response(400)
                                    self.end_headers()
                                    return

                                sender = str(meta.get("SenderComputerName", "iPhone"))
                                files_meta = meta.get("Files", [])
                                if not isinstance(files_meta, list) or not files_meta:
                                    self.send_response(400)
                                    self.end_headers()
                                    return

                                file_names = [safe_name(str(f.get("FileName", "file"))) for f in files_meta]
                                total_bytes = sum(int(f.get("FileSize", 0)) for f in files_meta)
                                ask_id = uuid.uuid4().hex

                                consent_event = threading.Event()
                                record = AirDropTransferRecord(
                                    id=ask_id,
                                    sender_name=sender,
                                    file_names=file_names,
                                    total_bytes=total_bytes,
                                    received_bytes=0,
                                    status="pending_consent",
                                    timestamp=time.time(),
                                    time_label=datetime.datetime.now().strftime("%H:%M:%S"),
                                    saved_paths=[],
                                )

                                with service_ref._transfers_lock:
                                    service_ref._transfers.appendleft(record)

                                with service_ref._lock:
                                    service_ref._pending_consent[ask_id] = {
                                        "event": consent_event,
                                        "result": False,
                                        "record": record,
                                    }

                                # Wait up to 55 seconds for user approval from WinUI 3
                                approved = consent_event.wait(timeout=55.0) and service_ref._pending_consent[ask_id]["result"]
                                with service_ref._lock:
                                    service_ref._pending_consent.pop(ask_id, None)

                                if not approved:
                                    record.status = "declined"
                                    self.send_response(403)
                                    self.end_headers()
                                    return

                                record.status = "transferring"
                                self.answer_plist(
                                    200,
                                    {
                                        "ReceiverComputerName": service_ref.computer_name,
                                        "ReceiverModelName": service_ref.model_name,
                                        "ReceiverMediaCapabilities": MEDIA_CAPABILITIES_BYTES,
                                        "IsAirDropable": True,
                                    },
                                )
                                return

                            if self.path == "/Upload":
                                # Read chunked body into temp file
                                chunked = "chunked" in self.headers.get("Transfer-Encoding", "").lower()
                                content_length = int(self.headers.get("Content-Length", "0") or "0")

                                raw_buffer = io.BytesIO()
                                if chunked:
                                    while True:
                                        line = self.rfile.readline(64)
                                        if not line:
                                            break
                                        size_str = line.split(b";", 1)[0].strip()
                                        if not size_str:
                                            continue
                                        chunk_size = int(size_str, 16)
                                        if chunk_size == 0:
                                            self.rfile.readline(64)  # Read trailing CRLF
                                            break
                                        chunk_data = self.rfile.read(chunk_size)
                                        raw_buffer.write(chunk_data)
                                        self.rfile.read(2)  # Discard chunk trailing CRLF
                                        if raw_buffer.tell() > MAX_ARCHIVE_BYTES:
                                            raise ValueError("Transfer exceeds size limit")
                                elif content_length > 0:
                                    if content_length > MAX_ARCHIVE_BYTES:
                                        raise ValueError("Transfer exceeds size limit")
                                    raw_buffer.write(self.rfile.read(content_length))
                                else:
                                    self.send_response(400)
                                    self.end_headers()
                                    return

                                archive_bytes = raw_buffer.getvalue()
                                raw_buffer.close()

                                # Inflate DVZip and parse CPIO archive
                                inflated = decode_dvzip(archive_bytes)
                                files = parse_cpio(inflated)

                                saved_paths: list[str] = []
                                dest = service_ref.downloads_dir
                                dest.mkdir(parents=True, exist_ok=True)

                                for name, payload in files:
                                    target_name = safe_name(name)
                                    target_file = dest / target_name
                                    # Handle duplicate filenames safely
                                    if target_file.exists():
                                        stem = target_file.stem
                                        suffix = target_file.suffix
                                        target_file = dest / f"{stem}_{int(time.time())}{suffix}"
                                    target_file.write_bytes(payload)
                                    mark_of_the_web(target_file)
                                    saved_paths.append(str(target_file))

                                with service_ref._transfers_lock:
                                    for t in service_ref._transfers:
                                        if t.status == "transferring":
                                            t.status = "completed"
                                            t.received_bytes = len(archive_bytes)
                                            t.saved_paths = saved_paths
                                            break

                                self.send_response(200)
                                self.end_headers()
                                return

                            self.send_response(404)
                            self.end_headers()
                        except Exception as ex:
                            logger.error(f"[AirDrop Handler Error] {ex}")
                            self.send_response(500)
                            self.end_headers()

                server = ThreadingHTTPServer(("0.0.0.0", self.port), AirDropHandler)
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ssl_context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
                server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
                self._http_server = server

                self._server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                self._server_thread.start()

                # Start mDNS broadcaster thread
                self._mdns_stop.clear()
                self._mdns_thread = threading.Thread(target=self._run_mdns_loop, daemon=True)
                self._mdns_thread.start()

                self._enabled = True
                logger.info(f"[AirDrop] Service listening on HTTPS port {self.port} (mDNS active)")
                return True
            except Exception as err:
                logger.error(f"[AirDrop] Failed to start server: {err}")
                return False

    def stop(self) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._mdns_stop.set()
            if self._http_server:
                with contextlib.suppress(Exception):
                    self._http_server.shutdown()
                    self._http_server.server_close()
            self._enabled = False
            logger.info("[AirDrop] Service stopped")

    def _run_mdns_loop(self) -> None:
        """Periodically broadcast mDNS answer for _airdrop._tcp.local. on UDP port 5353."""
        service = "_airdrop._tcp.local."
        instance = f"{self.sid}.{service}"
        hostname = f"{self.sid}.local."

        def dns_name(name: str) -> bytes:
            parts = name.rstrip(".").split(".")
            return b"".join(bytes([len(p.encode())]) + p.encode() for p in parts) + b"\x00"

        def rr(name: str, kind: int, value: bytes, ttl: int = 120) -> bytes:
            return dns_name(name) + struct.pack("!HHIH", kind, 1 if kind == 12 else 0x8001, ttl, len(value)) + value

        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            packed_ip = socket.inet_aton(local_ip)
        except Exception:
            packed_ip = b"\x7f\x00\x00\x01"

        packet = struct.pack("!6H", 0, 0x8400, 0, 4, 0, 0) + b"".join([
            rr(service, 12, dns_name(instance)),
            rr(instance, 33, struct.pack("!HHH", 0, 0, self.port) + dns_name(hostname)),
            rr(instance, 16, b"\x09flags=136"),
            rr(hostname, 1, packed_ip),
        ])

        with contextlib.suppress(Exception):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                while not self._mdns_stop.is_set():
                    with contextlib.suppress(Exception):
                        sock.sendto(packet, ("224.0.0.251", 5353))
                    time.sleep(1.0)


# Global Singleton for Relais
AIRDROP_SERVICE = AirDropService()
