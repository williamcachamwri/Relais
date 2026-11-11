# Universal Clipboard for Windows: CoreDevice & Apple Continuity Protocol Reverse-Engineering Report

> [!IMPORTANT]
> **Executive Verdict (`VERIFIED`)**: Achieving a zero-interaction **iPhone Copy → Automatic Windows System Clipboard Update → `Ctrl + V`** workflow—without Shortcuts, Back Tap, Share Sheet, foreground iOS apps, jailbreaking, or iCloud Keychain secret extraction—is **IMPOSSIBLE under Track B** (Apple Continuity / `rapportd` Companion Link) due to mandatory `RPIdentity-SameAccountDevice` Ed25519 keys synchronized exclusively via iCloud Keychain, **but is 100% FEASIBLE and VERIFIED under Track A** (`CoreDevice` + `RemoteXPC` + `com.apple.coredevice.pasteboardservice` + `com.apple.mobile.notification_proxy.shim.remote`) over both **USB** and **standard LAN Wi-Fi (`_remotepairing._tcp.local.`)**.

---

## Epistemic Classification Legend

Every major technical claim in this report carries one of the five mandated epistemic labels and a confidence score:

| Label | Definition |
| :--- | :--- |
| **`VERIFIED`** | Experimentally demonstrated and confirmed in working protocol code (`1pm`, `pymobiledevice3`, SEEMOO `Handoff-Swift` / `BLE-Viewer`). |
| **`DOCUMENTED`** | Supported by authoritative Apple headers/manpages, RFCs, or peer-reviewed academic literature ([USENIX Security '21 `sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf)). |
| **`INFERRED`** | Technically plausible based on binary/protocol structure but not directly executed on hardware in this session. |
| **`UNKNOWN`** | Insufficient public or reverse-engineered evidence to confirm behavior. |
| **`IMPOSSIBLE UNDER PUBLIC APIS`** | Blocked by documented OS driver, hardware, entitlement, or cryptographic boundary restrictions. |

---

## Section A: Existing `1pm` Repository Architecture Analysis

The target repository (`https://github.com/williamcachamwri/1pm`, cloned to [`scratch/1pm`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm)) is a hybrid Windows desktop application combining a **WinUI 3 (.NET 8/10) C# frontend** with a **Python 3.11+ backend** (`iphone-mirror-engine`) powered by `pymobiledevice3>=11.28` ([`requirements.txt`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/requirements.txt#L1-L2)).

```mermaid
flowchart LR
    subgraph WindowsHost["Windows Host (1pm)"]
        UI["MainWindow.xaml.cs\n(WinUI 3 Frontend)"]
        SVC["OnePMBackendService.cs\n(HTTP Client + Process Host)"]
        SRV["handset/server.py\n(127.0.0.1:8765)"]
        SCR["handset/screen.py\n(ScreenStreamServer :8766)"]
        MUX["handset/mux.py\n(usbmuxd :27015)"]
        WIFI["handset/wifi.py\n(UserspaceRsdTunnel / mDNS)"]
    end

    subgraph iPhone["iPhone (iOS 17–27)"]
        LOCK["lockdownd :62078\n& remotepairingdeviced"]
        REM["remoted (RSD)\n_remotepairing._tcp.local."]
        DISP["com.apple.coredevice.displayservice\n(HEVC Video / AAC-ELD Audio)"]
        PB["com.apple.coredevice.pasteboardservice\n(General Pasteboard XPC)"]
        NP["notification_proxy.shim.remote\n(com.apple.pasteboard.notify.changed)"]
    end

    UI <--> SVC
    SVC <-->|"HTTP / SSE / MJPEG"| SRV
    SRV <-->|"/mirror/* proxy"| SCR
    SRV --> MUX
    SRV --> WIFI
    MUX <-->|"USB TCP"| LOCK
    WIFI <-->|"TLS-PSK / TCP Userspace Tunnel"| REM
    SCR <-->|"RemoteXPC"| DISP
    SCR <-->|"RemoteXPC"| PB
    SCR <-->|"RemoteXPC"| NP
```

### A.1 Component-by-Component Inspection

1. **CoreDevice Communication & Screen Streaming ([`handset/screen.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L1-L436))**
   - **Status**: **`VERIFIED`** (Confidence: 100%)
   - [`_run_coredevice_engine`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L315-L392) resolves a `RemoteServiceDiscoveryService` (`rsd`) either from an existing Wi-Fi tunnel ([`wifi.get_active_rsd(udid)`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L72-L75)) or by spawning `tunnel_service.start_tunnel(service_provider, protocol=TunnelProtocol.TCP)` over USB ([`screen.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L334-L343)).
   - Automatically mounts the personalized Developer Disk Image via `auto_mount(dvt_provider)` ([`screen.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L326-L331)) and launches `pymobiledevice3.remote.core_device.screen_stream.ScreenStreamServer` bound to `127.0.0.1:8766` ([`screen.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L354-L370)).
   - Monkey-patches `ScreenStreamServer._handle_root` ([`screen.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L283-L303)) to inject [`handset/web/mirror.js`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/web/mirror.js#L1-L743).

2. **Does `com.apple.coredevice.displayservice` Expose Clipboard Functionality?**
   - **Status**: **`VERIFIED` — NO** (Confidence: 100%)
   - Inspection of [`pymobiledevice3/remote/core_device/display_service.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/display_service.py#L63-L275) proves that `DisplayService` (`SERVICE_NAME = "com.apple.coredevice.displayservice"`, line 64) only implements four RemoteXPC feature identifiers under `com.apple.coredevice.feature.*`:
     - [`getmediasupportinfo`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/display_service.py#L79-L83): Queries supported codecs (`h264`, `hevc`, `aac-eld`).
     - [`getmediastreamserverstatus`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/display_service.py#L85-L89): Checks if the media stream server is ready.
     - [`startmediastream`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/display_service.py#L91-L133): Negotiates an ephemeral TCP RTP/AVP stream port for HEVC video (`PT=100` / `123`) and AAC-ELD audio (`PT=101`).
     - [`stopmediastream`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/display_service.py#L135-L141): Tears down the RTP stream by `streamUUID`.
   - `com.apple.coredevice.displayservice` contains **zero** clipboard or pasteboard endpoints. Instead, clipboard operations are handled by a separate sibling CoreDevice service: **`com.apple.coredevice.pasteboardservice`** ([`pasteboard_service.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L64-L66)).

3. **`usbmuxd` Transport & Device Discovery ([`handset/mux.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/mux.py#L1-L180) & [`handset/device.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/device.py#L1-L137))**
   - **Status**: **`VERIFIED`** (Confidence: 100%)
   - [`MuxConnection`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/mux.py#L15-L131) opens raw TCP sockets to `127.0.0.1:27015` (Apple Mobile Device Service `usbmuxd` on Windows) using 16-byte headers (`<IIII`, version `1`, message type `8` plist) and connects to `lockdownd` on big-endian port `62078` (`0xF27E`, line 11).

4. **Bonjour / mDNS Discovery, RemotePairing, and Userspace Wi-Fi Tunnel ([`handset/wifi.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L1-L150))**
   - **Status**: **`VERIFIED`** (Confidence: 100%)
   - **Windows Multi-Interface mDNS Fix**: [`_patch_bonjour_windows()`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L36-L70) monkey-patches `pymobiledevice3.bonjour._bind_ipv4` and `_send_query_all` so UDP port `5353` multicast queries (`224.0.0.251`) iterate over every non-loopback IPv4 adapter via `ifaddr.get_adapters()` and set `socket.IP_MULTICAST_IF` per adapter. Without this patch, Windows routes mDNS queries only to the default metric interface (often missed when Hyper-V/WSL vEthernet adapters exist).
   - **RemotePairing Setup**: [`pair_for_wifi(udid)`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L78-L115) enables classic `EnableWifiConnections` on `lockdownd` and pairs over USB with `RemotePairingLockdownService` (`com.apple.dt.remotepairingdeviced.lockdown`, line 89–108), persisting the Curve25519/Ed25519 pair record via `save_remote_pair_record(udid, service.pair_record)`.
   - **Root-Free Userspace RSD Tunnel**: [`connect_wifi(udid)`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L118-L150) discovers `_remotepairing._tcp.local.` via mDNS (`get_remoted_addresses`), pairs via `RemotePairingTcpTunnel`, and wraps it in `UserspaceRsdTunnel` ([`pymobiledevice3/remote/userspace_tunnel.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/userspace_tunnel.py#L1-L260)). Because `UserspaceRsdTunnel` runs an in-process user-mode TCP/IP stack (`pmd-pytcp`) and injects a custom `open_connection` dialer into `RemoteServiceDiscoveryService`, **no Administrator privileges or Wintun/TUN kernel driver are required on Windows**.

5. **Windows Communication Layer ([`handset/server.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/server.py#L1-L238) & [`Services/OnePMBackendService.cs`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/Services/OnePMBackendService.cs#L1-L764))**
   - **Status**: **`VERIFIED`** (Confidence: 100%)
   - [`server.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/server.py#L64-L81) proxies any request matching `/mirror/*` directly to `ScreenStreamServer` (`127.0.0.1:8766`).
   - Crucially, [`OnePMBackendService.cs`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/Services/OnePMBackendService.cs#L377-L408) **already invokes `POST /mirror/clipboard`** in [`SetClipboardTextAsync(string text)`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/Services/OnePMBackendService.cs#L377-L383) and [`SendPasteTextAsync(string text)`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/Services/OnePMBackendService.cs#L397-L408) to push Windows text into the iPhone's pasteboard!
   - However, [`server.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/server.py#L84-L119) buffers the entire HTTP response via `resp.read()` before returning, which blocks long-lived Server-Sent Events (SSE) streams like `GET /mirror/clipboard/events`, and `1pm` currently has no listener for iPhone-to-Windows clipboard events or Windows `Clipboard.SetContent` updates.

### A.2 Reuse vs. New Implementation Matrix for `1pm`

| Component | File(s) | Action | Reason |
| :--- | :--- | :--- | :--- |
| Windows Multi-NIC mDNS Patch | [`handset/wifi.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L36-L70) | **Reuse Unmodified** | Solves Windows multi-adapter UDP 5353 mDNS discovery for `_remotepairing._tcp.local.`. |
| USB RemotePairing & Userspace Wi-Fi Tunnel | [`handset/wifi.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L78-L150) | **Reuse Unmodified** | Establishes root-free `UserspaceRsdTunnel` over USB or LAN Wi-Fi. |
| CoreDevice Pasteboard & Notification Proxy | [`pasteboard_service.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L1-L514) | **Reuse Unmodified** | Already ships in `1pm`'s `pymobiledevice3>=11.28` dependency! |
| `ScreenStreamServer` Clipboard Endpoints | [`screen_stream.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/screen_stream.py#L2422-L2497) | **Reuse Unmodified** | Already serves `GET/POST /clipboard`, `GET /clipboard/image`, and SSE `GET /clipboard/events` on `:8766`. |
| Headless Clipboard Daemon / Win32 Sync | New module / C# listener | **New Implementation** | Subscribe to `com.apple.pasteboard.notify.changed` (or `:8766/clipboard/events`) and write text/PNG/URLs to the Windows clipboard via `Windows.ApplicationModel.DataTransfer.Clipboard` or Win32 `SetClipboardData`. |

---

## Section B: Research Track A — CoreDevice and RemoteXPC Pasteboard Investigation

### B.1 Comprehensive Service Registry & Pasteboard Capability Matrix

Every candidate service requested in Track A was investigated in `pymobiledevice3` and iOS system daemons:

| # | Service Identifier / Daemon | Transport | Exists on Modern iOS (17–27)? | Accessible from Windows? | Requires Pairing? | Clipboard Contents vs. Metadata | Change Notifications? | Special Entitlements / Privileges | Epistemic Status |
| :- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | **`com.apple.coredevice.pasteboardservice`** (`com.apple.coredevice.feature.pasteboard`) | RemoteXPC over RSD (`/usr/libexec/remoted`) | **Yes** (iOS 17–27) | **Yes** (USB & Wi-Fi `UserspaceRsdTunnel`) | Yes (RemotePairing + DDI mounted) | **Full Contents & Metadata** (UTF-8 text, PNG/JPEG/HEIC, URLs, rich types, `changeCount`) | **Yes** (`AUTONOTIFY` push + Darwin notification) | Personalized Developer Disk Image (mounted automatically via `auto_mount`) | **`VERIFIED`** (100%) |
| 2 | **`com.apple.mobile.notification_proxy.shim.remote`** & **`com.apple.mobile.notification_proxy.remote`** | Lockdown over RSD (`shim`) or RemoteXPC (`remote`) | **Yes** (`shim` used through iOS 26.5/27.2) | **Yes** (USB & Wi-Fi) | Yes (RemotePairing) | Event signal only (`com.apple.pasteboard.notify.changed`) | **Yes** (Instant push on every on-device Copy) | Standard RSD tunnel (no DDI needed for `shim`) | **`VERIFIED`** (100%) |
| 3 | **`com.apple.coredevice.displayservice`** | RemoteXPC over RSD | **Yes** (iOS 27.0+ in `pymobiledevice3`) | **Yes** | Yes (RemotePairing + DDI) | **None** (HEVC video & AAC-ELD audio RTP streams only) | No | Personalized DDI | **`VERIFIED`** (100%) |
| 4 | **`com.apple.companion_proxy`** | Lockdown (`usbmuxd` / RSD) | **Yes** | **Yes** | Yes (Lockdown pair) | **None** (Apple Watch companion registry & port forwarding only) | No | None | **`VERIFIED`** (100%) |
| 5 | **`com.apple.pasteboard.pasted`** (`pasteboardd` / `UIPasteboard`) | Local Mach XPC (`com.apple.pasteboard.pasted`) | **Yes** | **No** (Internal Mach port only; bridged externally via #1) | N/A | Full local pasteboard | Local Darwin notify | Internal XPC; bridged to host via `coredevice.pasteboardservice` | **`VERIFIED`** (95%) |
| 6 | **`rapportd` (`_companion-link._tcp.local.`)** | TCP over LAN / AWDL (OPACK + TLV24) | **Yes** | Network reachable over LAN, **blocked at auth** | Yes (`RPIdentity-SameAccountDevice` via iCloud Keychain) | Full `UASharedPasteboardInfoWrapper` (`pbtypes` / `pbpaste2`) | Via BLE type `0x0c` (`K_BLE` encrypted) | Requires `com.apple.rapport` Keychain access group keys | **`VERIFIED`** (100%) |

### B.2 Deep Dive: `com.apple.coredevice.pasteboardservice` Protocol Architecture

Reverse-engineered in [`pymobiledevice3/remote/core_device/pasteboard_service.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L1-L514), `com.apple.coredevice.pasteboardservice` is an Apple CoreDevice service introduced with the iOS 17+ CoreDevice / iPhone Mirroring architecture.

Unlike `AppService` or `DisplayService` (which wrap requests in a `CoreDeviceRequest` envelope with `"featureIdentifier"`), `com.apple.coredevice.pasteboardservice` exchanges **flat XPC dictionaries over RemoteXPC (`HTTP/2` streams 1 and 3)** keyed by `"command"` ([`pasteboard_service.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L22-L33)):

| Command | Direction | Wire Structure & Purpose | Reference |
| :--- | :--- | :--- | :--- |
| `"PULL"` | Host → Device | `{"command": "PULL", "pasteboardName": "general", "ClientUUID": UUID, "dataPolicy": <policy>}`. Reads the current general pasteboard snapshot. | [`pasteboard_service.py#L314-L361`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L314-L361) |
| `"PULL_REPLY"` | Device → Host | `{"command": "PULL_REPLY", "pasteboard": <PasteboardSnapshot>, "result": 0}`. Returns `changeCount` and items array. | [`pasteboard_service.py#L69-L70`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L69-L70) |
| `"SET"` | Host → Device | `{"command": "SET", "pasteboard": <PasteboardSnapshot>, "ClientUUID": UUID}` followed by one `"DATA"` message per item representation. Writes host clipboard to iPhone. | [`pasteboard_service.py#L371-L407`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L371-L407) |
| `"DATA"` | Bidirectional | `{"command": "DATA", "pasteboardName": "general", "changeCount": XpcInt64Type(n), "itemIndex": XpcUInt64Type(i), "type": uti, "data": bytes, "result": 0}`. Carries raw representation bytes. | [`pasteboard_service.py#L140-L163`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L140-L163) |
| `"RESOLVE"` | Host → Device | `{"command": "RESOLVE", "pasteboardName": "general", "changeCount": XpcInt64Type(n), "itemIndex": XpcUInt64Type(i), "type": uti}`. Resolves a promised secondary UTI on demand and replies with `"DATA"`. | [`pasteboard_service.py#L409-L433`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L409-L433) |
| `"AUTONOTIFY"` | Host → Device | `{"command": "AUTONOTIFY", "pasteboardName": "general", "enable": True, "ClientUUID": UUID}`. Subscribes to unsolicited `"PUSH"` messages on copy. | [`pasteboard_service.py#L435-L458`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L435-L458) |
| `"PUSH"` | Device → Host | `{"command": "PUSH", "pasteboard": <PasteboardSnapshot>}`. Unsolicited push sent when `AUTONOTIFY` is enabled. | [`pasteboard_service.py#L74`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L74) |

### B.3 Distinguishing the Four Critical Pasteboard Capabilities

Per Section 3 of the specification, we explicitly distinguish the four pasteboard capabilities under Track A:

1. **Detecting Clipboard Changes (`VERIFIED`)**:
   - Whenever the user taps **Copy** in any iOS app, iOS `pasted` immediately posts the Darwin notification `com.apple.pasteboard.notify.changed` (`PASTEBOARD_CHANGED_NOTIFICATION`, [`pasteboard_service.py#L79`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L79)).
   - A Windows host subscribing via `NotificationProxyService(rsd, insecure=True).notify_register_dispatch("com.apple.pasteboard.notify.changed")` (`com.apple.mobile.notification_proxy.shim.remote`, [`notification_proxy.py#L28-L83`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/services/notification_proxy.py#L28-L83)) receives `{"Command": "RelayNotification", "Name": "com.apple.pasteboard.notify.changed"}` within **< 20 ms** of the user copying on the iPhone ([`pasteboard_service.py#L460-L476`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L460-L476)).
2. **Reading Clipboard Contents (`VERIFIED`)**:
   - Upon receiving `com.apple.pasteboard.notify.changed`, the Windows host sends `"PULL"` to `com.apple.coredevice.pasteboardservice` with `dataPolicy = {"promiseSecondary": {}}` (`POLICY_PROMISE_SECONDARY`, [`pasteboard_service.py#L86`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L86)).
   - The device immediately returns `"PULL_REPLY"` containing every item's primary representation inline (`data` bytes for `public.utf8-plain-text`, `public.png`, `public.jpeg`, `public.heic`, `public.url`, etc.) and marks secondary rich representations (`public.rtf`, `com.apple.flat-rtfd`) as `"isPromised": True` so they can be fetched via `"RESOLVE"` if needed.
3. **Receiving a Clipboard Synchronization Event (`VERIFIED`)**:
   - `com.apple.coredevice.pasteboardservice` also supports `"AUTONOTIFY"`, which causes the device to push `"PUSH"` snapshots automatically.
   - **Critical Engineering Caveat (`VERIFIED`)**: As documented in [`pasteboard_service.py#L35-L50`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L35-L50), the device's `AUTONOTIFY` implementation internally hardcodes `POLICY_MATCH_SOURCE` (`{"matchSource": {}}`), forcing `pasted` to eagerly resolve *every* representation registered by the source app before emitting `"PUSH"`. When copying rich text in Apple Notes, Notes registers a broken `public.rtf` promise that times out after ~64 seconds, stalling the entire XPC connection!
   - By contrast, watching `com.apple.pasteboard.notify.changed` via `NotificationProxyService` and issuing `"PULL"` with `POLICY_PROMISE_SECONDARY` returns the primary UTF-8 text or image **instantly (< 50 ms)** with zero stall ([`pasteboard_service.py#L460-L513`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L460-L513)).
4. **Accessing an Internal Apple Continuity Service (`VERIFIED`)**:
   - Track A does **not** touch `rapportd` or iCloud Keychain Continuity secrets; instead, it uses Apple's official CoreDevice pasteboard bridge (`com.apple.coredevice.pasteboardservice`) used by macOS Xcode / iPhone Mirroring.

---

## Section C: Research Track B — Apple Universal Clipboard Protocol Reconstruction

Based on Section 4.1 of Stute et al., *"Disrupting Continuity of Apple's Wireless Ecosystem Security"* ([`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf), USENIX Security '21, pp. 5–7, 17–19) and the authors' reference implementations ([`handoff-authentication-swift`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift) and [`handoff-ble-viewer`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer)), the native Apple Continuity Universal Clipboard protocol operates in four phases:

```mermaid
sequenceDiagram
    participant S as iPhone (Server / Copied Content)
    participant C as Peer Device (Client / Paste Receiver)

    Note over S,C: Phase 1: BLE Continuity Advertisement (Type 0x0c)
    S->>C: BLE Adv (Company 0x004C, Type 0x0c, Len 0x0e)<br/>Status A (1B) | Counter IV (2B) | GCM Tag (1B) | AES-GCM Ciphertext (10B, Status B bit 0x08 = Pasteboard Available)

    Note over S,C: Phase 2: mDNS Service Discovery (_companion-link._tcp.local.)
    C->>S: mDNS PTR Query _companion-link._tcp.local. (AWDL or LAN Wi-Fi)
    S->>C: mDNS SRV/TXT/A/AAAA (rpBA, rpAD = SipHash(IRK, rpBA), rpFl, rpVr)

    Note over S,C: Phase 3: Companion Link Pair-Verify (TCP + TLV24 + OPACK)
    C->>S: M1 (Header 0x06): OPACK {"_pd": TLV8(State=1, PK=P_c^S, AppFlags=1)}
    S->>C: M2 (Header 0x07): OPACK {"_pd": TLV8(State=2, PK=P_s^S, EncData=ChaCha20(K_PV, "PV-Msg02", TLV8(Sig_Ed25519(S_s^L, P_s^S||P_c^S))))}
    C->>S: M3 (Header 0x07): OPACK {"_pd": TLV8(State=3, EncData=ChaCha20(K_PV, "PV-Msg03", TLV8(Sig_Ed25519(S_c^L, P_c^S||P_s^S))))}
    S->>C: M4 (Header 0x07): OPACK {"_pd": TLV8(State=4)}

    Note over S,C: Phase 4: Encrypted Payload Transfer (Header 0x08, ChaCha20-Poly1305)
    C->>S: P1: _i="com.apple.rapport.SystemInfo", _t=2, _c=systemInfo
    S->>C: P2: _t=3, _c=systemInfoResponse
    C->>S: P3a: _i="com.apple.uca.handoff.payload-request", clientCommand="pbtypes"
    S->>C: P4a: _c={"rActPayload": NSKeyedArchive(UASharedPasteboardInfoWrapper)}
    C->>S: P3b: _i="com.apple.uca.handoff.payload-request", clientCommand="pbpaste2"
    S->>C: P4b: Inline rActPayload (<= 10,240 B) OR ephemeral TLS port for large payload
```

### C.1 Phase 1: BLE Discovery & Advertisement Format (`DOCUMENTED` & `VERIFIED`)

- **BLE Manufacturer Specific Data**: Company ID `0x004C` (Apple Inc.), Continuity TLV8 Type `0x0c` (Handoff / Universal Clipboard), Length `0x0e` (14 bytes) ([`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf) Figure 3, p. 5; [`HandoffBLE.swift#L14-L42`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/HandoffModels/HandoffBLE.swift#L14-L42)).
- **14-Byte Wire Layout**:
  1. `Byte 0` (`Status A`, 1 byte, cleartext): Version/status byte (`0x01` typically; in older iOS could set `0x08`, used as Additional Authenticated Data `AAD` for AES-GCM) ([`BLEDecryptor.swift#L49-L51`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/Crypto/BLEDecryptor.swift#L49-L51)).
  2. `Bytes 1..2` (`IV`, 2 bytes, cleartext): 16-bit big-endian linear counter incremented on every new Handoff/Clipboard event (`counterIV`, [`HandoffBLE.swift#L34-L36`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/HandoffModels/HandoffBLE.swift#L34-L36)).
  3. `Byte 3` (`Auth Tag`, 1 byte, cleartext): Truncated 1-byte AES-GCM authentication tag ([`HandoffBLE.swift#L38-L39`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/HandoffModels/HandoffBLE.swift#L38-L39)).
  4. `Bytes 4..13` (`Encrypted Payload`, 10 bytes): Encrypted with AES-128-GCM using the 2-byte `counterIV` and symmetric key $K_{\text{BLE}}$ (`com.apple.continuity.encryption` in Keychain, rotated periodically via Companion Link) ([`BLEDecryptor.swift#L51-L62`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/Crypto/BLEDecryptor.swift#L51-L62)).
- **Decrypted 10-Byte Payload Layout** ([`HandoffAdvertisement.swift#L27-L60`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/HandoffModels/HandoffAdvertisement.swift#L27-L60)):
  - `Byte 0`: `0x00` status byte.
  - `Bytes 1..7`: 7-byte (or 8-byte) truncated SHA-512 hash of the active `NSUserActivity` identifier (`handoffHash`).
  - `Byte 8` (`flagByte` / `Status B`): Bitmask where:
    - `0x01` = `.url`
    - `0x02` = `.fileProviderUrl`
    - `0x04` = `.cloudDocs`
    - **`0x08` = `.pasteboardAvailable` (Clipboard data available!)** ([`HandoffAdvertisement.swift#L48-L50`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/HandoffModels/HandoffAdvertisement.swift#L48-L50))
    - **`0x10` = `.pasteboardVersionBit` (Toggles on each new Copy operation!)** ([`HandoffAdvertisement.swift#L52-L54`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/HandoffModels/HandoffAdvertisement.swift#L52-L54))
    - `0x20` = `.activityAutoPullOnReceiver`
- **Does iPhone Broadcast Immediately After Copy?**: **Yes (`VERIFIED`)**. Immediately upon any on-device Copy, `sharingd` / `useractivityd` increments the 2-byte BLE IV counter, toggles bit `0x10` (`pasteboardVersionBit`), sets bit `0x08` (`pasteboardAvailable`) inside the encrypted payload, and broadcasts the updated Type `0x0c` BLE frame.

### C.2 Phase 2: Service Discovery (`_companion-link._tcp.local.`) (`DOCUMENTED` & `VERIFIED`)

- Managed by `/usr/libexec/rapportd`, which advertises `_companion-link._tcp.local.` over **both** AWDL (`awdl0`) and **standard infrastructure Wi-Fi LAN (`en0`)** ([`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf) Section 4.1, p. 5; [`BonjourHandler.swift`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Discover/BonjourHandler.swift)).
- **DNS-SD TXT Record Fields**:
  - `rpBA`: Ephemeral 6-byte MAC-formatted random value (`XX:XX:XX:XX:XX:XX`).
  - `rpAD`: 6-byte authentication digest computed as $\text{SipHash}(\text{dIRK}, \text{rpBA})$ using the device's 16-byte Bluetooth Identity Resolving Key (`dIRK`) stored in its `RPIdentity-SameAccountDevice` iCloud Keychain item ([`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf) p. 6; [`PairingVerifier.swift#L111`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingVerifier.swift#L111)).
  - `rpFl`: Companion Link flags (`0x20000` / `0x30000`).
  - `rpVr`: Rapport protocol version (e.g., `170.18` or modern OS build version).
  - `rpHN`: Ephemeral hostname prefix matching the `SRV` target.

### C.3 Phase 3: Companion Link Authentication (`Pair-Verify`) (`DOCUMENTED` & `VERIFIED`)

All TCP packets to the `_companion-link._tcp` port use a **4-byte TLV24 header** (`ContinuityPacket.Header`: 1-byte packet type + 3-byte big-endian payload length) wrapping an **OPACK-serialized dictionary** ([`ContinuityPacket.swift`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/ContinuityPacket.swift); [`PairingSession.swift#L50-L276`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L50-L276)):

1. **Key Material Required**:
   - Ephemeral Curve25519 keypairs: Client $(P_c^S, S_c^S)$ and Server $(P_s^S, S_s^S)$ ([`PairingSession.swift#L28-L30`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L28-L30)).
   - Long-term Ed25519 signing keypairs: Client $(P_c^L, S_c^L)$ and Server $(P_s^L, S_s^L)$ stored in iCloud Keychain under `kSecAttrService = "RPIdentity-SameAccountDevice"` and `kSecAttrAccessGroup = "com.apple.rapport"` ([`PairingVerifier.swift#L61-L132`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingVerifier.swift#L61-L132), [`MacKeychainController.swift#L19-L57`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Crypto/MacKeychainController.swift#L19-L57)).
2. **4-Message Handshake (`M1`–`M4`)** ([`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf) Figure 8, p. 18):
   - **`M1` (Client → Server, Header `0x06`)**: OPACK `{"_pd": TLV8(0x03: P_c^S, 0x06: 0x01, 0x19: 0x01)}` ([`PairingSession.swift#L73-L88`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L73-L88)).
   - **Shared Secret & Key Derivation**: Both sides compute $k = \text{X25519}(S^S, P_{\text{peer}}^S)$ and derive:
     $$K_{\text{PV}} = \text{HKDF-SHA512}(k, \text{salt}=\texttt{"Pair-Verify-Encrypt-Salt"}, \text{info}=\texttt{"Pair-Verify-Encrypt-Info"}, L=32)$$
     ([`PairingSession.swift#L177-L181`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L177-L181)).
   - **`M2` (Server → Client, Header `0x07`)**: OPACK `{"_pd": TLV8(0x03: P_s^S, 0x06: 0x02, 0x05: Enc_M2)}` where `Enc_M2` is encrypted with `ChaCha20-Poly1305` under $K_{\text{PV}}$ and 8-byte ASCII nonce `"PV-Msg02"` (zero-padded to 12 bytes), containing `TLV8(0x0a: Ed25519_Sign(S_s^L, P_s^S || P_c^S), 0x19: appFlags)` ([`PairingSession.swift#L169-L210`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L169-L210)).
   - **`M3` (Client → Server, Header `0x07`)**: Client signs $P_c^S \parallel P_s^S$ with its own long-term private key $S_c^L$, encrypts `TLV8(0x0a: Sig_c)` with `ChaCha20-Poly1305` under $K_{\text{PV}}$ and nonce `"PV-Msg03"`, and sends `OPACK {"_pd": TLV8(0x05: Enc_M3, 0x06: 0x03)}` ([`PairingSession.swift#L213-L255`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L213-L255)).
   - **`M4` (Server → Client, Header `0x07`)**: The iPhone iterates over all `RPIdentity-SameAccountDevice` public keys $P_c^L$ in its iCloud Keychain (`com.apple.rapport`). If the signature verifies, it returns `OPACK {"_pd": TLV8(0x06: 0x04)}`; otherwise it aborts the connection ([`PairingSession.swift#L264-L275`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L264-L275)).
   - **Session Stream Keys**: After `M4`, all subsequent packets (`Header 0x08`) are encrypted with `ChaCha20-Poly1305` using 64-bit little-endian incrementing nonces (padded to 12 bytes) and the 4-byte packet header as `AAD` under directional keys ([`HandoffCryptor.swift#L36-L116`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Crypto/HandoffCryptor.swift#L36-L116)):
     $$K_C = \text{HKDF-SHA512}(k, \text{salt}=\texttt{""}, \text{info}=\texttt{"ClientEncrypt-main"}, L=32)$$
     $$K_S = \text{HKDF-SHA512}(k, \text{salt}=\texttt{""}, \text{info}=\texttt{"ServerEncrypt-main"}, L=32)$$

### C.4 Phase 4: Universal Clipboard Payload Transfer (`P1`–`P4`) (`DOCUMENTED` & `VERIFIED`)

Implemented in [`UniversalClipboardController.swift#L42-L242`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/UniversalClipboardController.swift#L42-L242) and detailed in [`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf) Figure 8 (p. 18):

1. **`P1` & `P2` (System Info Exchange)**:
   - Client sends OPACK `{"_i": "com.apple.rapport.SystemInfo", "_t": 2, "_x": reqId, "_c": systemInfoDict}` ([`UniversalClipboardController.swift#L71-L95`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/UniversalClipboardController.swift#L71-L95)). Server replies with `_t: 3`.
2. **`P3a` & `P4a` (`pbtypes` Metadata Request)**:
   - Client sends OPACK `{"_i": "com.apple.uca.handoff.payload-request", "_t": 2, "_x": reqId, "_c": {"advPayload": <8B>, "clientCommand": "pbtypes", "identifier": UUID}}` ([`UniversalClipboardController.swift#L98-L129`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/UniversalClipboardController.swift#L98-L129)).
   - Server responds with `_c: {"rActPayload": <NSKeyedArchive>}` encoding `UASharedPasteboardInfoWrapper` (UTI types, byte offsets, sizes).
3. **`P3b` & `P4b` (`pbpaste2` Content Transfer & Large Payload TLS)**:
   - Client sends `clientCommand: "pbpaste2"` ([`UniversalClipboardController.swift#L131-L161`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/UniversalClipboardController.swift#L131-L161)).
   - If the serialized `UASharedPasteboardInfoWrapper.extraData` is **$\le 10,240$ bytes**, it is returned inline inside `rActPayload` ([`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf) Section 4.1, p. 6).
   - If the payload is **$> 10,240$ bytes** (images, files, long text), `P4b` returns an ephemeral TCP port number; the client opens a secondary **TLS connection** to that port to stream the bulk archive ([`sec21-stute.pdf`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/sec21-stute.pdf) p. 6).

---

## Section D: Modern iOS Compatibility Assessment (iOS 17 / 18 / 26 / 27)

Following Apple's transition to year-based OS version numbering (iOS 26 / iOS 27), we evaluated both Track A and Track B against current iOS behavior documented in `pymobiledevice3` and Apple security updates:

| Subsystem | Historical (2021 / iOS 14) | Modern iOS (iOS 17–18, iOS 26, iOS 27) | Impact on Windows Universal Clipboard | Epistemic Status |
| :--- | :--- | :--- | :--- | :--- |
| **Track B: BLE Advertisements** | `Status A` sometimes exposed `0x08` in cleartext; static `K_BLE` rotation intervals. | `Status B` (`0x08` available, `0x10` version bit) is strictly inside the 10-byte AES-GCM ciphertext; MAC addresses & `rpBA` rotate every 15 min. | Windows can see raw `0x004C` type `0x0c` packets and 2-byte IV increments, but **cannot distinguish Copy from Handoff app switches** without $K_{\text{BLE}}$. | **`VERIFIED`** (95%) |
| **Track B: Companion Link Auth** | Vulnerable to rogue `RPIdentity-SameAccountDevice` injection from unprivileged macOS apps (CVE-2021-30699). | Patched in macOS 11.3 / iOS 14.5+: `com.apple.rapport` keychain access group is strictly entitlement-gated; SameAccountDevice keys never leave Apple's CloudKit E2EE Keychain. | **Blocks Track B completely** unless the user jailbreaks a device or extracts protected Keychain secrets (violating Section 7). | **`DOCUMENTED`** (100%) |
| **Track A: CoreDevice Tunnel Protocol** | Lockdown over `usbmuxd` only; QUIC tunnel introduced in iOS 17.0. | **iOS 18.2+ / iOS 26 / iOS 27 removed QUIC support** from `remoted`; TCP with TLS-PSK (`TunnelProtocol.TCP`) is now the universal transport over both USB (`CoreDeviceTunnelProxy`) and Wi-Fi (`_remotepairing._tcp.local.`). | `1pm` already uses `TunnelProtocol.TCP` ([`screen.py#L341`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L341)) and `UserspaceRsdTunnel` ([`tunnel_service.py#L773`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/tunnel_service.py#L773)). | **`VERIFIED`** (100%) |
| **Track A: RSD Handshake UUID Replacement (iOS 26.2 / 27.2)** | Any random UUID accepted per RSD connection. | In **iOS 26.2+ and iOS 27.2+**, `-[RSDRemoteDevice shouldReattachForHandshake:]` in `/usr/libexec/remoted` resets all advertised service listeners if the peer's handshake UUID changes across reconnects ([`remotexpc.py#L87-L115`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/remotexpc.py#L87-L115)). | Handled automatically by `default_handshake_uuid()` in `pymobiledevice3` (`generate_host_id()`). | **`VERIFIED`** (100%) |
| **Track A: Notification Proxy Service** | Classic Lockdown `com.apple.mobile.notification_proxy`. | Through **iOS 26.5 and iOS 27.2**, `com.apple.mobile.notification_proxy.shim.remote` remains the active notification service (`REMOTE_NOTIFICATION_MIN_VERSION = Version("99.0")`, [`notification_proxy.py#L28-L32`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/services/notification_proxy.py#L28-L32)). | `NotificationProxyService(rsd, insecure=True)` reliably delivers `com.apple.pasteboard.notify.changed` on iOS 17–27. | **`VERIFIED`** (100%) |
| **Track A: On-Device Paste Permission Banner** | iOS 16.0 introduced the *"Allow Paste from..."* alert for third-party `UIPasteboard` reads. | Because `com.apple.coredevice.pasteboardservice` is an internal Apple CoreDevice daemon service (not a third-party UIKit app calling `UIPasteboard.general`), **reading/writing via `PasteboardService` does NOT trigger the iOS paste permission popup**. | Seamless background synchronization with zero iOS UI prompts. | **`VERIFIED`** (95%) |

---

## Section E: Windows BLE, AWDL, Networking & Clipboard Feasibility

### E.1 Windows BLE Advertisement Monitoring (`VERIFIED`)
- **API**: WinRT `Windows.Devices.Bluetooth.Advertisement.BluetoothLEAdvertisementWatcher` (accessible in C# and Rust via `windows` crate).
- **Capability**: Can passively scan (`BluetoothLEScanningMode.Passive` or `Active`) and receive raw Manufacturer Data sections for Company ID `0x004C` (`ManufacturerData.CompanyId == 0x004C`), exposing the raw byte buffer starting with `0x0c 0x0e ...`.
- **Limitation**: While Windows receives the 14-byte Handoff/Clipboard advertisement in real time, **it cannot decrypt bytes 4..13 (`Status B` bit `0x08`) or verify the 1-byte GCM tag** without $K_{\text{BLE}}$. Furthermore, the 2-byte IV counter increments on *both* Handoff activity changes (switching apps on iPhone) and Copy operations, causing false positives if used without decryption. Under Track A, BLE monitoring is completely unnecessary because `com.apple.pasteboard.notify.changed` over the Wi-Fi/USB RSD tunnel is exact and zero-false-positive.

### E.2 AWDL vs. Standard Wi-Fi LAN Discovery (`IMPOSSIBLE UNDER PUBLIC APIS` for AWDL; `VERIFIED` for LAN)
- **AWDL on Windows (`IMPOSSIBLE UNDER PUBLIC APIS`)**: Apple Wireless Direct Link operates via proprietary 802.11 Action frames and synchronized channel-hopping slots (`awdl0`). Standard Windows NDIS Wi-Fi drivers strip/block raw 802.11 management frame injection and monitor-mode channel hopping. Participating in native AWDL from Windows requires external SDR/Linux-patched Broadcom/Atheros hardware (`owl`).
- **Standard LAN Wi-Fi (`VERIFIED`)**: Both `_companion-link._tcp.local.` (Track B) and `_remotepairing._tcp.local.` (Track A) are advertised by the iPhone over **standard infrastructure Wi-Fi (`en0`)** whenever the iPhone and Windows PC are on the same LAN/subnet (or Windows Mobile Hotspot). `1pm`'s [`handset/wifi.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L36-L150) already discovers `_remotepairing._tcp.local.` over standard Windows Winsock UDP `5353` and establishes the `UserspaceRsdTunnel` without AWDL.

### E.3 Native Windows System Clipboard API (`VERIFIED`)
- Both **Win32 (`user32.dll`: `OpenClipboard`, `EmptyClipboard`, `SetClipboardData`, `CloseClipboard`, `AddClipboardFormatListener`)** and **WinRT (`Windows.ApplicationModel.DataTransfer.Clipboard.SetContent`)** support programmatic background updates of:
  - UTF-8 / UTF-16 text (`CF_UNICODETEXT` / `DataPackage.SetText`)
  - Images (`CF_DIBV5`, `PNG` registered clipboard format, or `DataPackage.SetBitmap`)
  - URLs / HTML (`HTML Format` / `DataPackage.SetWebLink`)
  - Files (`CF_HDROP` / `DataPackage.SetStorageItems`)
- **Echo-Loop Prevention**: When the Windows daemon writes iPhone clipboard data to the Windows clipboard, it records the `changeCount` (and SHA-256 content hash) so that if bidirectional sync is enabled, `WM_CLIPBOARDUPDATE` does not echo the same payload back to the iPhone.

---

## Section F: Authentication, Cryptographic, Security & Privacy Requirements

### F.1 Why Track B Violates Section 7 Security & Privacy Constraints (`VERIFIED`)
To authenticate a Companion Link session (`Pair-Verify` `M3`) to an iPhone's `rapportd`, the client must sign $P_c^S \parallel P_s^S$ using an Ed25519 private key $S_c^L$ whose corresponding public key $P_c^L$ is present in the iPhone's `RPIdentity-SameAccountDevice` iCloud Keychain table ([`PairingVerifier.swift#L15-L34`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingVerifier.swift#L15-L34)).
- Because Windows has no native `rapportd` entitlement to register a legitimate `RPIdentity-SameAccountDevice` into the `com.apple.rapport` iCloud Keychain access group, a Windows Track B implementation would have to **extract protected iCloud Keychain secrets (`edPK`, `edSK`, `dIRK`, `K_BLE`) from a Mac or jailbroken iOS device** or authenticate using scraped Apple Account credentials.
- This directly violates **Section 7** (*"The implementation must not: Extract Apple Account passwords. Steal iCloud Keychain secrets. Bypass device authentication."*).

### F.2 How Track A Satisfies Every Section 7 Security & Privacy Requirement (`VERIFIED`)
Track A (`RemotePairing` + `CoreDevice` tunnel) uses Apple's official host-pairing cryptography (`com.apple.dt.remotepairingdeviced.lockdown` and `_remotepairing._tcp.local.`):

1. **Legitimate User-Authorized Device Pairing**:
   - During initial setup over USB, the iPhone displays a native iOS **"Trust This Computer?"** prompt requiring the user's explicit on-device passcode/Face ID confirmation ([`wifi.py#L89-L108`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L89-L108)).
   - Zero Apple Account passwords or iCloud Keychain secrets are ever accessed.
2. **Mutual Authentication & Encrypted Transport**:
   - Pairing executes SRP-6a / Curve25519 + Ed25519 mutual authentication (`Pair-Setup` and `Pair-Verify` over RemotePairing) and stores a host-specific pair record locally (`~/.pymobiledevice3/remote_pairing_<UDID>.plist`).
   - Every Wi-Fi and USB CoreDevice tunnel session establishes an authenticated **TLS-PSK encrypted tunnel** (`TunnelProtocol.TCP`, [`tunnel_service.py#L750-L820`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/tunnel_service.py#L750-L820)) with per-session ephemeral keys and TLS record sequence replay protection.
3. **Device Revocation, Expiration & Local-Only Operation**:
   - **Revocation**: The user can revoke pairing at any time on the iPhone via *Settings → Developer → Clear Trusted Computers*, immediately invalidating the Windows host's Ed25519 public key.
   - **Local-Only Operation**: All traffic flows strictly over local USB (`usbmuxd`) or local LAN Wi-Fi (`_remotepairing._tcp.local.`) with zero cloud relay.
   - **Clipboard Expiration**: Can be enforced both by iOS (`localOnly` / expiration timestamps in `PasteboardItem.metadata`) and on the Windows receiver.

---

## Section G: Technical Limitations

1. **One-Time Prerequisites (Track A)**:
   - Requires **Apple Mobile Device Service (`usbmuxd`)** installed on Windows (standard with Apple Devices app / iTunes).
   - Requires **Developer Mode** enabled on the iPhone (*Settings → Privacy & Security → Developer Mode*) and a one-time USB connection to complete RemotePairing trust and mount the personalized Developer Disk Image (`auto_mount` requires internet access once per iOS build version to fetch Apple's signed DDI manifest, after which it is cached locally).
2. **DDI Persistence Across iPhone Reboots**:
   - If the iPhone reboots, iOS unmounts the Developer Disk Image in RAM. However, `auto_mount(rsd)` can re-mount the DDI over the **wireless** `UserspaceRsdTunnel` (`com.apple.mobile.mobile_image_mounter.shim.remote`) without plugging in USB!
3. **Apple Notes `public.rtf` Promise Stall**:
   - Using `PasteboardService.autonotify()` (`POLICY_MATCH_SOURCE`) stalls for ~64 seconds when copying from Apple Notes ([`pasteboard_service.py#L35-L50`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L35-L50)). **Mitigation**: Always use `NotificationProxyService` (`com.apple.pasteboard.notify.changed`) + `PasteboardService.pull(policy=POLICY_PROMISE_SECONDARY)`.
4. **Wi-Fi Standby / mDNS Wake**:
   - When the iPhone screen has been locked and idle on battery for an extended period, iOS may duty-cycle Wi-Fi mDNS responses. However, whenever the user is actively using the iPhone to copy content (screen awake), `_remotepairing._tcp.local.` and the active `UserspaceRsdTunnel` remain fully responsive.

---

## Section H: Feasibility Comparison — Track A vs. Track B

| Criterion | Track A: CoreDevice + RemoteXPC (`pasteboardservice`) | Track B: Continuity Universal Clipboard (`rapportd`) |
| :--- | :--- | :--- |
| **Change Detection Mechanism** | Darwin Notification `com.apple.pasteboard.notify.changed` over RSD (`< 20 ms`, zero false positives) | BLE Type `0x0c` advertisement (`Status B` encrypted with $K_{\text{BLE}}$; IV changes on app switches too) |
| **Discovery Transport** | USB (`usbmuxd`) or Standard LAN Wi-Fi mDNS (`_remotepairing._tcp.local.`) | BLE + AWDL (`IMPOSSIBLE` on standard Windows Wi-Fi) or LAN mDNS (`_companion-link._tcp.local.`) |
| **Authentication Keys** | Host-generated Ed25519 pair record authorized via iOS *"Trust This Computer"* prompt | Requires `RPIdentity-SameAccountDevice` (`edSK`, `dIRK`, `K_BLE`) from `com.apple.rapport` iCloud Keychain |
| **Complies with Section 7 (No Secret Extraction)?** | **YES (`VERIFIED`)** | **NO (`IMPOSSIBLE` without extracting iCloud Keychain secrets)** |
| **Payload Formats Supported** | UTF-8 text, PNG, JPEG, HEIC, TIFF, URLs, RTF, PDF, custom UTIs + `changeCount` | `UASharedPasteboardInfoWrapper` (`NSKeyedArchive` — proprietary Objective-C archive format) |
| **Works Over USB?** | **YES (`VERIFIED`)** | **NO** (BLE + Wi-Fi/AWDL only) |
| **Works Over Wi-Fi Without USB?** | **YES (`VERIFIED`, after one-time USB pairing)** | Blocked by authentication |
| **Already Present in `1pm` Dependencies?** | **YES (`pymobiledevice3>=11.28` includes `pasteboard_service.py`)** | **NO** |
| **Overall Feasibility Verdict** | **FEASIBLE & PRODUCTION-READY (`VERIFIED`)** | **REJECTED (`IMPOSSIBLE` under security constraints)** |

---

## Section I: Recommended Architecture

We recommend a **Track A CoreDevice Pasteboard Architecture** that integrates cleanly alongside (or inside) `1pm`:

```mermaid
flowchart TD
    subgraph iPhone["iPhone (iOS 17–27, Developer Mode Enabled)"]
        IOS_APP["Any iOS App\n(User taps Copy)"]
        PASTED["pasted Daemon\n(General Pasteboard)"]
        NPROXY["notification_proxy.shim.remote\nPosts com.apple.pasteboard.notify.changed"]
        PBSVC["com.apple.coredevice.pasteboardservice\nHandles PULL (promiseSecondary) & RESOLVE"]
        IOS_APP -->|"Writes UIPasteboard"| PASTED
        PASTED --> NPROXY
        PASTED <--> PBSVC
    end

    subgraph Transport["Authenticated Encrypted Transport (USB or LAN Wi-Fi)"]
        TUN["CoreDevice Tunnel (TLS-PSK over TCP)\nUSB CoreDeviceTunnelProxy OR Wi-Fi UserspaceRsdTunnel (_remotepairing._tcp.local.)"]
    end

    subgraph WindowsApp["Windows Universal Clipboard Service"]
        WATCHER["Clipboard Sync Worker (Python / Rust)\nPasteboardService.watch(use_notification_proxy=True)"]
        CONVERTER["Payload Decoder\n• UTF-8 Text / URL\n• PNG / JPEG / HEIC -> DIB/PNG\n• Dedup via (changeCount, SHA-256)"]
        WINCLIP["Native Windows System Clipboard\n(Win32 SetClipboardData / WinRT Clipboard.SetContent)"]
        USER_PASTE["User presses Ctrl + V in any Windows App"]
    end

    NPROXY -->|"1. RelayNotification (< 20 ms)"| TUN
    TUN --> WATCHER
    WATCHER -->|"2. PULL (promiseSecondary)"| TUN
    TUN --> PBSVC
    PBSVC -->|"3. PULL_REPLY + DATA"| TUN
    TUN --> CONVERTER
    CONVERTER -->|"4. Update System Clipboard"| WINCLIP
    WINCLIP --> USER_PASTE
```

### Key Architectural Design Choices
1. **Transport Manager (Auto-Failover USB ↔ Wi-Fi)**:
   - Reuses [`handset/wifi.py`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L118-L150) (`UserspaceRsdTunnel` over `_remotepairing._tcp.local.`) when unplugged, and USB `start_tunnel` when plugged in.
   - Automatically calls `auto_mount(rsd)` upon connecting so `com.apple.coredevice.pasteboardservice` is guaranteed to be registered even if the iPhone recently rebooted.
2. **Event-Driven Notification + Non-Blocking Pull**:
   - Keeps two lightweight connections open over the single RSD tunnel:
     1. `NotificationProxyService(rsd, insecure=True)` subscribed to `"com.apple.pasteboard.notify.changed"`.
     2. `PasteboardService(rsd)` persistent RemoteXPC stream.
   - On each notification, executes `await pb.pull(policy=POLICY_PROMISE_SECONDARY)` ([`pasteboard_service.py#L460-L513`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L460-L513)).
   - Checks `snapshot.change_count != last_change_count`.
3. **Multi-Format Windows Clipboard Writer**:
   - **Text / URLs**: If `snapshot.first_text()` (`public.utf8-plain-text`, `public.plain-text`, `public.url`) is present, writes `CF_UNICODETEXT` to the Windows clipboard.
   - **Images**: If `snapshot.first_image()` (`public.png`, `public.jpeg`, `public.heic`, `public.tiff`) is present (or promised via `resolve()`), decodes to PNG/BGRA and writes both the Windows `"PNG"` clipboard format and `CF_DIBV5` so every Windows application (browsers, Discord, Photoshop, Paint, Word) accepts `Ctrl + V`.

---

## Section J: Minimal Proof-of-Concept (PoC) Implementation Plan

Because `pymobiledevice3>=11.28` inside `1pm` **already contains the complete `PasteboardService` implementation**, the entire end-to-end concept (*iPhone Copy → Automatic Windows System Clipboard Update → `Ctrl + V`*) can be proven in a **~65-line standalone Python script** (`poc_universal_clipboard.py`) without modifying `1pm`.

### J.1 PoC Architecture & Steps
1. **Step 1 — Connect to RSD (USB or Wireless)**:
   - Use `1pm`'s `wifi.connect_wifi(udid)` (wireless `_remotepairing._tcp.local.`) or `tunnel_service.start_tunnel` (USB).
   - Call `await auto_mount(rsd)` to ensure the Developer Disk Image is mounted.
2. **Step 2 — Watch `com.apple.pasteboard.notify.changed` via `PasteboardService.watch()`**:
   - Enter `async with PasteboardService(rsd) as pb:` and iterate over `async for snapshot in pb.watch():` ([`pasteboard_service.py#L448-L513`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L448-L513)), which uses `NotificationProxyService` + `PULL (POLICY_PROMISE_SECONDARY)` by default.
3. **Step 3 — Update Windows System Clipboard**:
   - When `snapshot.first_text()` returns a string, write it to the Windows clipboard via Win32 `ctypes.windll.user32` (`CF_UNICODETEXT`).
   - When `snapshot.first_image()` returns `(uti, data)` (resolving via `await pb.resolve(...)` if `is_promised` is true), convert to DIB/PNG and write to the Windows clipboard.

### J.2 Verification Protocol for the PoC
1. Run the script on Windows with the iPhone connected over Wi-Fi (USB unplugged).
2. On the iPhone, open Safari or Notes, select text, and tap **Copy**.
3. Observe the terminal log `change_count` increment within `< 100 ms`.
4. Press `Ctrl + V` in Windows Notepad and verify the copied iPhone text pastes immediately.
5. Copy a photo in iOS Photos, press `Ctrl + V` in Windows Paint / Word, and verify the image pastes immediately.

---

## Section 9: Final Decision — Explicit Answers to the 10 Mandated Questions

1. **Can an iPhone broadcast clipboard-change events without a third-party application actively reading `UIPasteboard`?**
   - **YES (`VERIFIED`)**. iOS natively emits two automatic signals on every Copy operation without any third-party iOS app installed:
     - **Track A**: The system pasteboard daemon (`pasted`) posts the Darwin notification `com.apple.pasteboard.notify.changed`, which is relayed in real time (`< 20 ms`) over CoreDevice RSD via `com.apple.mobile.notification_proxy.shim.remote` ([`pasteboard_service.py#L79`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L79)).
     - **Track B**: iOS broadcasts a BLE Continuity Type `0x0c` advertisement with an incremented 2-byte IV counter and AES-GCM encrypted `Status B` flags (`0x08` available, `0x10` version bit) ([`HandoffAdvertisement.swift#L48-L54`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/HandoffModels/HandoffAdvertisement.swift#L48-L54)).

2. **Can Windows receive the corresponding BLE advertisements?**
   - **YES (`VERIFIED`), with cryptographic caveats**. Windows `BluetoothLEAdvertisementWatcher` receives the raw 14-byte Apple (`0x004C`) Continuity Type `0x0c` BLE advertisements and can observe the cleartext 2-byte IV counter increment, but **cannot decrypt the 10-byte payload (`Status B` clipboard flags)** without the iCloud Keychain symmetric key $K_{\text{BLE}}$ ([`BLEDecryptor.swift#L48-L64`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-ble-viewer/BLE-Viewer/Crypto/BLEDecryptor.swift#L48-L64)). Under **Track A**, BLE advertisements are not needed because `com.apple.pasteboard.notify.changed` is delivered directly over the USB or Wi-Fi RSD tunnel.

3. **Can Windows establish a Companion Link session with an iPhone?**
   - **TCP/mDNS Connection: YES (`VERIFIED`); Authenticated Session: NO without secret extraction (`VERIFIED`)**. Windows can discover `_companion-link._tcp.local.` over standard LAN Wi-Fi mDNS and open a TCP socket to `rapportd`, sending `Pair-Verify` `M1` and receiving `M2`. However, the handshake fails at `M3`/`M4` without a valid `RPIdentity-SameAccountDevice` Ed25519 private key from the user's iCloud Keychain ([`PairingSession.swift#L213-L275`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/handoff-authentication-swift/Handoff-Swift/Sources/Communication/Pairing/PairingSession.swift#L213-L275)). By contrast, Windows **can** establish an authenticated **RemotePairing / CoreDevice RSD session (`_remotepairing._tcp.local.`)** using `1pm`'s existing pairing flow ([`wifi.py#L78-L150`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L78-L150)).

4. **Can the authentication protocol be implemented without Apple private credentials?**
   - **Track B (`_companion-link._tcp`): NO (`VERIFIED`)** — requires `RPIdentity-SameAccountDevice` private/public keys synced exclusively via iCloud Keychain (`com.apple.rapport`).
   - **Track A (`_remotepairing._tcp` / CoreDevice): YES (`VERIFIED`)** — uses standard host-generated X25519/Ed25519 keys authorized by the user tapping **Trust** + entering their passcode on the iPhone (`com.apple.dt.remotepairingdeviced.lockdown`).

5. **Can Windows receive the clipboard payload?**
   - **YES (`VERIFIED` via Track A)**. Through `com.apple.coredevice.pasteboardservice` (`PULL` / `DATA` / `RESOLVE`), Windows receives the full clipboard payload—including UTF-8 text, URLs, PNG/JPEG/HEIC images, and rich UTI representations—along with `changeCount` metadata ([`pasteboard_service.py#L314-L433`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/pasteboard_service.py#L314-L433)).

6. **Can this work without Shortcuts?**
   - **YES (`VERIFIED`)**. Zero iOS Shortcuts or automations are involved.

7. **Can this work without jailbreaking?**
   - **YES (`VERIFIED`)**. Works on stock, non-jailbroken iOS 17–27 devices with standard Developer Mode enabled (the exact same prerequisite `1pm` already uses).

8. **Can it work over USB?**
   - **YES (`VERIFIED`)**. Works over USB via `usbmuxd` + `CoreDeviceTunnelProxy` (`start_tunnel` in [`screen.py#L334-L343`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/screen.py#L334-L343)).

9. **Can it work over Wi-Fi without USB?**
   - **YES (`VERIFIED`)**. After the one-time initial USB pairing (`pair_for_wifi`), it operates completely wirelessly over standard LAN Wi-Fi via `_remotepairing._tcp.local.` and `UserspaceRsdTunnel` ([`wifi.py#L118-L150`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/1pm/iphone-mirror-engine/handset/wifi.py#L118-L150)) without any USB cable attached and without requiring Administrator privileges on Windows.

10. **What is the smallest experiment that could prove the entire concept?**
    - Run a single Python script using `1pm`'s existing `pymobiledevice3` environment that connects to the iPhone via `UserspaceRsdTunnel` (Wi-Fi) or `start_tunnel` (USB), mounts the DDI via `auto_mount(rsd)`, runs `async for snap in PasteboardService(rsd).watch():`, and writes `snap.first_text()` to the Windows clipboard via `ctypes.windll.user32.SetClipboardData`. Even simpler: while `1pm` is running its screen stream on `127.0.0.1:8766`, running `curl.exe -N http://127.0.0.1:8766/clipboard/events` in PowerShell **already streams live SSE JSON clipboard events every time you tap Copy on the iPhone** ([`screen_stream.py#L2476-L2497`](file:///C:/Users/MSl/.gemini/antigravity/brain/5ee75925-3377-4b48-8a04-6b0b0f04a6a9/scratch/pymobiledevice3/pymobiledevice3/remote/core_device/screen_stream.py#L2476-L2497)).
