# Universal Clipboard for Windows (CoreDevice / RemoteXPC PoC)

Automatic, zero-interaction **iPhone Copy → Windows System Clipboard Update → `Ctrl + V`** built on Apple's native **CoreDevice / RemoteXPC** infrastructure via `pymobiledevice3`.

- **No iOS Shortcuts, Back Tap, or Share Sheet.**
- **No third-party iOS companion app required.**
- **No jailbreak required.**
- **Does not require `1pm` screen mirroring (`ScreenStreamServer`) to be running.**
- **Supports both USB (`CoreDeviceTunnelProxy`) and cable-free LAN Wi-Fi (`_remotepairing._tcp.local.`) without Administrator privileges.**

---

## 1. Repository Structure & Deliverables

| File | Description |
| :--- | :--- |
| [`universalClipboard.py`](file:///d:/UniversalClipboard/universalClipboard.py) | Universal Clipboard entrypoint forwarding to `CoreDeviceBridge.bridgeService`. |
| [`CoreDeviceBridge/`](file:///d:/UniversalClipboard/CoreDeviceBridge) | Modular technical package: `bridgeService.py`, `continuityCoordinator.py`, `winClipboard.py`, `payloadModels.py`, `deviceTunnel.py`, `historyStore.py`, `bridgeServer.py`. |
| [`test_universal_clipboard.py`](file:///d:/UniversalClipboard/test_universal_clipboard.py) | Automated test suite verifying Win32 `CF_UNICODETEXT` (Vietnamese, Chinese, Japanese, Emoji), `HTML Format`, `UniformResourceLocatorW`, `CF_DIBV5` / `CF_DIB` / `PNG` image conversion (PNG, JPEG, HEIC), and live hardware USB/Wi-Fi sync. |
| [`requirements.txt`](file:///d:/UniversalClipboard/requirements.txt) | Python dependencies (`pymobiledevice3>=11.50.3`, `Pillow>=11.1.0`, `pillow-heif>=1.8.1`, `ifaddr>=0.2.0`, `pywin32>=306`). |
| [`docs/RESEARCH_REPORT.md`](file:///d:/UniversalClipboard/docs/RESEARCH_REPORT.md) | Phase 0/1 reverse-engineering report covering `1pm`, CoreDevice `pasteboardservice`, and Apple Continuity (`rapportd`) protocol analysis. |

---

## 2. Phase 1 Inspection Summary (`pymobiledevice3` `11.50.3`)

Inspection of the installed `pymobiledevice3` (`v11.50.3`) and `1pm` revealed four critical implementation details that [`poc_universal_clipboard.py`](file:///d:/UniversalClipboard/poc_universal_clipboard.py) handles directly:

1. **Actual `PasteboardService` API Signatures in `pymobiledevice3==11.50.3`**:
   - `PasteboardService` (`com.apple.coredevice.pasteboardservice`) exposes `await pb.get(pasteboard_name, data_policy)` and `await pb.set(items, pasteboard_name)`.
   - `NotificationProxyService(rsd)` opens `com.apple.mobile.notification_proxy.shim.remote` over RSD and streams `NotificationEvent` dicts via `receive_notification()` after `await notif.notify_register_dispatch("com.apple.pasteboard.notify.changed")`.
2. **Safari & Apple Notes Primary `public.html` vs. 64-Second `public.rtf` Stall**:
   - On iOS 27.0 (`iPhone17,5`), copying text in Safari or Notes places `public.html` **first** (primary) in `item["types"]` (`['public.html', 'public.rtf', 'public.utf8-plain-text', 'com.apple.flat-rtfd']`).
   - Under `POLICY_PROMISE_SECONDARY` (`{"promiseSecondary": {}}`), the iOS `pasted` daemon returns `public.html` inline in **~21–40 ms**, while `public.utf8-plain-text` is promised behind `public.rtf`.
   - Calling `RESOLVE` on `public.utf8-plain-text` forces `pasted` to evaluate `public.rtf` first, which stalls for **64.5 seconds** on Apple Notes copies.
   - [`poc_universal_clipboard.py`](file:///d:/UniversalClipboard/poc_universal_clipboard.py) extracts both clean Unicode plain text (`CF_UNICODETEXT`) and rich HTML (`HTML Format`) directly from inline `public.html` / `com.apple.webarchive` in **< 1 ms**, and only issues `RESOLVE` when no inline representation is usable.
3. **Windows `bonjour.Address.full_ip` IDNA Crash Fix**:
   - On Windows, `ifaddr` populates `adapter.nice_name` with a >63-character human-readable description (e.g. `"Qualcomm FastConnect 7800 Wi-Fi 7 High Band Simultaneous (HBS) Network Adapter"`), which `pymobiledevice3.bonjour.Address.full_ip` appends to `fe80::` addresses (`fe80::...%<nice_name>`), raising `UnicodeEncodeError: 'idna' codec can't encode characters in position 0-65: label too long` inside `socket.getaddrinfo`.
   - `patch_windows_bonjour()` in [`poc_universal_clipboard.py`](file:///d:/UniversalClipboard/poc_universal_clipboard.py) sanitizes `Address.full_ip` on Windows and sorts IPv4 addresses ahead of link-local IPv6 addresses.
4. **Windows Port 5353 `AppleMobileDeviceLauncher` Interception & AP Multicast Isolation Bypass**:
   - On Windows, `AppleMobileDeviceLauncher.exe` binds specifically to `<LAN_IP>:5353`, intercepting unicast mDNS replies sent to port `5353` before `0.0.0.0:5353` receives them.
   - `patch_windows_bonjour()` opens an ephemeral-port UDP socket per local IPv4 interface (`bind((ip, 0))`) and sends `CLASS_QU` (`unicast=True`) queries both to `224.0.0.251:5353` and directly to local ARP-neighbor IPv4 addresses, discovering `_remotepairing._tcp.local.` in **< 150 ms**.

---

## 3. Setup Instructions

### Prerequisites
1. **Windows 10 / 11 (64-bit)** with **Apple Mobile Device Service (`usbmuxd`)** installed (included with Apple Devices app or iTunes).
2. **iPhone running iOS 17.4+ (tested on iOS 27.0)** with **Developer Mode** enabled (*Settings → Privacy & Security → Developer Mode*).
3. **Python 3.11+** (tested on Python 3.15.2).

### Install Dependencies
```powershell
pip install -r requirements.txt
```

### One-Time Wireless RemotePairing Setup (With USB Cable Connected)
If your iPhone is not yet paired for wireless RemotePairing (`~/.pymobiledevice3/remote_<UDID>.plist`), plug in the USB cable once, unlock the iPhone, and run:
```powershell
python poc_universal_clipboard.py --pair-wifi
```

---

## 4. USB Testing Instructions

1. Connect the iPhone to the Windows PC via USB cable and unlock the iPhone.
2. Start the Universal Clipboard PoC in USB mode (or `auto` mode):
   ```powershell
   python poc_universal_clipboard.py --transport usb
   ```
   *(Optional: pass `--sync-on-connect` to immediately copy the iPhone's current pasteboard onto Windows at startup.)*
3. Wait for the log line:
   ```text
   [SUBSCRIBED] transport=USB notification=com.apple.pasteboard.notify.changed service=com.apple.coredevice.pasteboardservice policy=POLICY_PROMISE_SECONDARY
   ```
4. Copy any text, URL, or image on the iPhone.
5. Observe `[NOTIFICATION]` and `[CLIPBOARD_UPDATED] status=SUCCESS` in the terminal within **~25–45 ms**, then press `Ctrl + V` in any Windows application (Notepad, Word, Paint, browser, etc.).

---

## 5. Wi-Fi Testing Instructions (Cable-Free)

1. Ensure the iPhone and Windows PC are connected to the same local network (LAN / Wi-Fi) and you have run `--pair-wifi` once over USB.
2. **Unplug the USB cable** from the iPhone.
3. Start the Universal Clipboard PoC in Wi-Fi mode (or `auto` mode, which automatically selects Wi-Fi when USB is unplugged):
   ```powershell
   python poc_universal_clipboard.py --transport wifi
   ```
4. Wait for the log line:
   ```text
   [CONNECTED] device=iPhone17,5 ios=27.0 udid=00008140-001135821181801C transport=Wi-Fi rsd_addr=...
   [SUBSCRIBED] transport=Wi-Fi notification=com.apple.pasteboard.notify.changed service=com.apple.coredevice.pasteboardservice policy=POLICY_PROMISE_SECONDARY
   ```
5. Copy any text, URL, or photo on the iPhone and press `Ctrl + V` on Windows.

---

## 6. Actual Hardware Test Results (`iPhone17,5`, iOS 27.0, Build `24A5370h`)

All tests below were executed against physical hardware (`iPhone17,5`, iOS `27.0`, UDID `00008140-001135821181801C`) connected to this Windows PC (`192.168.1.21` ↔ iPhone `192.168.1.156`).

| Case | Test Scenario | Transport | Observed UTI / Format | Pull + Write Latency | Result | Notes & Verification Details |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **A** | **Copy plain text** | USB & Wi-Fi | `public.utf8-plain-text` → `CF_UNICODETEXT` | `31.3 ms` pull + `3.1 ms` write (`34.5 ms` total) | **PASS** | Verified notification-driven pull (`changeCount=15511`) and exact Win32 `GetClipboardData(CF_UNICODETEXT)` readback. |
| **B** | **Copy rich text in Notes / Safari** | USB & Wi-Fi | `public.html` (primary, with `public.rtf` secondary) → `CF_UNICODETEXT` + `HTML Format` | `21.2 ms` (USB) / `40.9 ms` (Wi-Fi) | **PASS** | Tested both with real on-device rich copy (`changeCount=15510`, `7,304 B`, `4,038` chars) and HTML pasteboard updates (`changeCount=15512`), bypassing the 64s Notes `public.rtf` stall. |
| **C** | **Copy Chinese, Vietnamese, Japanese & Emoji text** | USB & Wi-Fi | `public.utf8-plain-text` → UTF-16LE `CF_UNICODETEXT` | `40.1 ms` (USB) / `41.0 ms` (Wi-Fi) | **PASS** | Verified lossless UTF-16LE encoding and surrogate pairs (`Tiếng Việt`, `中文`, `日本語`, `🚀🎉🍎`) on both USB (`changeCount=15513`) and Wi-Fi (`changeCount=15522`). |
| **D** | **Copy image (HEIC / PNG / JPEG)** | USB & Wi-Fi | `public.heic` / `public.png` / `public.jpeg` → `CF_DIBV5` + `CF_DIB` + `PNG` | `42.1 ms` pull + `3.9 ms` write (`46.1 ms` total) | **PASS** | Decoded `public.heic` (`changeCount=15514`) via `pillow-heif` and `public.png` (`changeCount=15523`) into 32-bit BGRA `BITMAPV5HEADER` (`CF_DIBV5`), `CF_DIB`, and `PNG`. |
| **E** | **Copy a URL** | USB & Wi-Fi | `public.url` + `public.utf8-plain-text` → `CF_UNICODETEXT` + `HTML Format` + `UniformResourceLocatorW` | `23.1 ms` pull + `1.1 ms` write (`24.3 ms` total) | **PASS** | Verified URL text preservation and clickable `<a href="...">` `CF_HTML` generation (`changeCount=15515`). |
| **F** | **Copy several items rapidly (5-item burst)** | USB | 5 consecutive `public.utf8-plain-text` copies (`changeCount` `15516` → `15520`) | `20.7 ms`–`32.9 ms` per coalesced pull | **PASS** | Event coalescing (`pull_trigger.clear()` before pull) handled back-to-back notifications without overlapping XPC reads and converged to item #5 (`changeCount=15520`) with duplicate suppression. |
| **G** | **Lock and unlock iPhone** | Wi-Fi & USB | `com.apple.springboard.lockstate` + `POLICY_PROMISE_SECONDARY` | `< 50 ms` catch-up on unlock | **PASS** | Observed on physical hardware that iOS returns empty item data dicts (`{}`) when `PULL` runs while the iPhone is locked (`15:39:37`), and returns full data (`7,304 B`) once unlocked (`15:40:38`). Added lock-state notification catch-up so locked pulls do not advance `last_change_id`. |
| **H** | **Disconnect and reconnect Wi-Fi** | Wi-Fi | `_remotepairing._tcp.local.` (`UserspaceRsdTunnel`) | `~0.8 s` tunnel re-establishment | **PASS** | Verified automatic teardown and reconnection across multiple Wi-Fi RSD tunnel lifecycles (`rsd_addr` ports `65346`, `65348`, `65353`, `65359`). |
| **I** | **Disconnect and reconnect USB** | USB ↔ Wi-Fi | `usbmuxd` (`CoreDeviceTunnelProxy`) | `2.0 s` retry interval + `~0.4 s` connect | **PASS** | Verified live physical USB cable disconnect at `15:38:42` (`ConnectionError: iPhone ... is not connected via USB cable — retrying in 2.0s`), automatic Wi-Fi fallback in `auto` mode (`15:38:47`), and USB reconnect at `15:40:38`. |
| **J** | **Restart the Windows PoC** | USB & Wi-Fi | Clean `AsyncExitStack` teardown & startup baseline | `~0.5 s` (USB) / `~0.8 s` (Wi-Fi) | **PASS** | Verified across 6 consecutive process restarts; `UserspaceRsdTunnel` cleanly releases the `pmd-pytcp` singleton stack and re-establishes on restart. |

---

## 7. Known Limitations

1. **Developer Mode & DDI Requirement**:
   - `com.apple.coredevice.pasteboardservice` is part of Apple's CoreDevice Developer Disk Image (`Cryptex1` / `PersonalizedDMG`). Developer Mode must be enabled on the iPhone, and if the iPhone reboots, `poc_universal_clipboard.py` automatically re-mounts the DDI on the next connection.
2. **iOS Locked-Screen Pasteboard Protection**:
   - While the iPhone screen is locked, iOS `pasted` redacts item representation data (`data: {}`) until the user unlocks the phone. `poc_universal_clipboard.py` detects empty locked snapshots and automatically re-pulls when `com.apple.springboard.lockstate` / `com.apple.mobile.keybagd.lock_status` signals device unlock.
3. **Inline Images Embedded Inside Rich Apple Notes Selections**:
   - When copying an image *together with text* inside Apple Notes, Notes places `public.rtf` ahead of `com.apple.webarchive` and fails to produce `public.rtf` for ~64 seconds. Standard image copies from **Photos, Safari, Files, Screenshots, and Messages** place the image UTI (`public.heic`, `public.png`, `public.jpeg`) as the primary representation and sync in **< 50 ms**. To also resolve secondary representations behind `public.rtf` in Notes (accepting the ~64s iOS daemon delay), pass `--allow-slow-resolve`.
4. **Single `UserspaceRsdTunnel` Per Python Process**:
   - `pymobiledevice3`'s `pmd-pytcp` userspace stack is a process-global singleton (`UserspaceRsdTunnel`). Because `poc_universal_clipboard.py` runs as its own standalone OS process, it opens its own independent `UserspaceRsdTunnel` without conflicting with other processes.
