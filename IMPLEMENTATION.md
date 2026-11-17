# PySide6 Desktop Client — Full Implementation Guide (Zero to All)

This document provides a comprehensive, end-to-end technical overview of the `desktop_client` skeleton implementation: its design principles, architecture, component contracts, lifecycle management, verification results, and roadmap for future iterations.

---

## 1. Executive Summary & Design Principles

### Scope and Boundaries
- **Scope:** Skeleton only — PySide6 desktop UI overlay, asynchronous microphone audio capture (50 ms frames), and OS-level window capture exclusion (stealth).
- **Platform Focus:** Windows is the primary platform for capture exclusion (`SetWindowDisplayAffinity`). macOS and Linux render identically and support all UI and audio features, but report capture exclusion as `UNSUPPORTED`.
- **Zero Backend Touched:** No server code, backend WebSocket routes, or existing contracts were modified.
- **Display-Only:** Display only; no text-to-speech (TTS), no automatic audio injection.

### Two Orthogonal Axes
The client strictly decouples input behavior from capture visibility:

| Axis | Mechanism | Effect |
|---|---|---|
| **Capture Visibility (Stealth)** | `SetWindowDisplayAffinity` (Windows API) | Window omitted from screen shares, recordings, screenshots |
| **Input Transparency (Click-Through)** | `WindowTransparentForInput` (Qt Window Flag) | Mouse clicks pass through to background applications |

*Note:* Stealth is **never** implemented via `WA_TransparentForMouseEvents`, and click-through toggling never breaks the native handle's display affinity.

---

## 2. Package Architecture

```text
ai_desktop/
├── pyproject.toml              # Project dependencies, packaging, and pytest configuration
├── README.md                   # Quickstart instructions and status checklist
├── IMPLEMENTATION.md           # Full technical documentation (this file)
├── desktop_client/             # Core client package
│   ├── __init__.py             # Version metadata
│   ├── stealth.py              # Windows SetWindowDisplayAffinity wrapper & state machine
│   ├── audio.py                # sounddevice.RawInputStream capture & 50 ms framing
│   ├── overlay.py              # PySide6 translucent always-on-top overlay widget
│   └── main.py                 # Async application entry point powered by qasync
└── tests/
    └── test_desktop_client.py  # Comprehensive automated unit test suite (19 tests)
```

---

## 3. Detailed Component Breakdown

### 3.1 Stealth Capture Exclusion (`desktop_client/stealth.py`)

#### State Model
```python
class StealthState(Enum):
    EXCLUDED    = "excluded"     # OS confirmed complete capture exclusion (WDA_EXCLUDEFROMCAPTURE)
    BLACK_BOX   = "black_box"    # Excluded as opaque black rectangle (WDA_MONITOR)
    UNSUPPORTED = "unsupported"  # Non-Windows platform (macOS/Linux)
    FAILED      = "failed"       # Windows API present but call rejected by OS/compositor
```

#### Constants
- `WDA_NONE = 0x00`: Remove all display affinity (window is fully capture-visible).
- `WDA_MONITOR = 0x01`: Window is rendered as black box on non-monitor captures.
- `WDA_EXCLUDEFROMCAPTURE = 0x11`: Requests capture exclusion. Behavior varies by OS build, compositor, and capture tool; verify the matrix in `initialize-plan.md` §10.6. Not a guarantee.

#### Implementation Details & Rules
1. **Thread Discipline:** Must only be called on the GUI thread after `widget.show()`.
2. **Handle Lifecycle:** The native window handle (`int(widget.winId())`) is **never cached** on widget instances because Qt destroys and recreates HWNDs whenever window flags change.
3. **Build Version Gating:**
   - On Windows 10 2004+ (Build ≥ 19041) and Windows 11: Attempts `0x11` first.
   - If `0x11` fails or on older Windows builds: Falls back to `0x01` (`BLACK_BOX`).
   - If both fail: Logs `GetLastError()` and returns `FAILED`.
4. **Platform Isolation:** On non-Windows platforms, returns `UNSUPPORTED` immediately without attempting to load `ctypes.windll`.
5. **Idempotent Clear:** `clear(widget)` applies `WDA_NONE` safely.

---

### 3.2 Translucent Overlay (`desktop_client/overlay.py`)

The overlay is implemented as a specialized `QWidget` subclass designed to float unobtrusively over fullscreen applications and video conferences.

#### Window Configuration & Attributes
- **Flags:**
  - `Qt.WindowType.FramelessWindowHint`: Removes standard OS title bar and borders.
  - `Qt.WindowType.WindowStaysOnTopHint`: Keeps overlay pinned above video calls and apps.
  - `Qt.WindowType.Tool`: Excludes overlay from Alt-Tab switchers and taskbar enumeration.
  - `Qt.WindowType.WindowDoesNotAcceptFocus`: Prevents stealing keyboard focus from active work.
- **Attributes:**
  - `Qt.WidgetAttribute.WA_TranslucentBackground`: Allows per-pixel alpha transparency.
  - `Qt.WidgetAttribute.WA_ShowWithoutActivating`: Shows window without taking system focus.
- **Neutral Window Title:** Titled `"Overlay"` rather than sensitive names to prevent heuristic window-title matching by detector tools.

#### Layout Structure
1. **Status Row:**
   - `PulsingDot`: Custom animated widget oscillating alpha between 90 and 255 at ~25 FPS.
   - `status_label`: Displays `"listening"` or current operational status.
   - `stealth_badge`: Pill badge displaying live state:
     - `EXCLUDED` → `"hidden from share"` (dim green `#4ade80`, bg `#14532d`)
     - `BLACK_BOX` → `"hidden (opaque)"` (dim amber `#fbbf24`, bg `#78350f`)
     - `FAILED` → `"capture exclusion failed"` (dim red `#f87171`, bg `#7f1d1d`)
     - `UNSUPPORTED` → `"visible to share"` (dim gray `#9ca3af`, bg `#374151`)
   - `model_label`: Right-aligned model indicator (e.g. `"gemini-2.5-flash"`).
2. **Transcript Area:**
   - `transcript_edit`: Read-only `QTextEdit` with transparent background for finalized turns. Auto-scrolls to newest turn.
   - `partial_label`: Dedicated `QLabel` for interim partial transcripts. Isolating partials here avoids expensive text layout reflows of the entire transcript on every speech delta.
3. **Controls Row:**
   - Click-through toggle (`F8`).
   - Stealth toggle (`F9`).
   - `"Stop"` and `"Close"` buttons.

#### Re-Application Rules Across HWND Re-Creation (§10.3)
When `setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)` is called:
1. Qt destroys the old native HWND and creates a new one asynchronously.
2. The display affinity drops immediately.
3. To resolve this, `OverlayWindow` schedules stealth re-application via `QTimer.singleShot(0, self._reapply_stealth)`.
4. In `showEvent()`, the same `singleShot(0, ...)` rule ensures affinity is applied immediately upon presentation.
5. In `paintEvent()`, if the current stealth state is `BLACK_BOX`, the background alpha is dynamically raised from `200` to `250` so that capture viewers see an intentional, crisp dark box rather than broken semi-transparent artifacts.

#### Click-Through Mechanics
- Enabled: Sets `WindowTransparentForInput` flag at the window level. Clicks pass cleanly to windows underneath.
- Disabled (Interactive mode): Clears `WindowTransparentForInput`.
- *Key correction applied:* Does **not** set `WA_TransparentForMouseEvents` on the window instance, as Qt propagates that attribute to child buttons and disables their click handlers.

#### Dragging
- In interactive mode, clicking and dragging any non-button region moves the window.
- Prefers `windowHandle().startSystemMove()` where supported, with manual position delta tracking fallback.

---

### 3.3 Audio Capture Pipeline (`desktop_client/audio.py`)

Produces raw audio chunks formatted precisely for the backend STT provider (AssemblyAI / Google Speech).

#### Format Contract
- **Sample Rate:** 16,000 Hz (16 kHz).
- **Channels:** 1 (Mono).
- **Data Type:** Signed 16-bit Linear PCM, Little-Endian (`int16` / S16LE).
- **Frame Duration:** 50 ms.
- **Samples per Frame:** $16000 \times 0.05 = 800$ samples.
- **Bytes per Frame:** $800 \times 1 \text{ channel} \times 2 \text{ bytes/sample} = 1,600$ bytes.

#### Thread-Safe Async Generator
- **Engine:** `sounddevice.RawInputStream`.
- **Bridge:** PortAudio invokes `audio_callback` on a real-time high-priority OS thread.
- **Queue:** Bounded `asyncio.Queue` (max 40 frames ≈ 2.0 seconds of audio buffer).
- **Backpressure Handling:** If the asyncio consumer falls behind and the queue fills up, the callback schedules queue maintenance on the event loop via `loop.call_soon_threadsafe`, drops the oldest frame, and enqueues the latest with a warning. The callback never blocks.
- **Silence Padding:** On a graceful stop, any residual partial frame is padded with null bytes to 1,600 bytes before the generator returns. Cancellation closes the stream without yielding from `finally` (avoids `RuntimeError: async generator ignored GeneratorExit`).
- **Environment Agility:** Uses sounddevice's normal PortAudio loader; install `libportaudio2` on Linux for real capture. `--simulate-audio` enables synthetic frames for headless verification; normal runs do not silently fall back.

---

### 3.4 Application Lifecycle (`desktop_client/main.py`)

- **Event Loop Integration:** Uses `qasync.QEventLoop` to unify the Qt GUI event loop and Python's `asyncio` event loop.
- **Pattern:** explicit QEventLoop integration; no broad exception fallback:
  ```python
  loop = QEventLoop(app)
  asyncio.set_event_loop(loop)
  try:
      loop.run_until_complete(async_main(app, simulate_audio=args.simulate_audio))
  finally:
      loop.close()
  ```
- **Execution Flow:**
  1. Instantiates `OverlayWindow` and invokes `.show()`.
  2. Spawns `stream_audio_to_overlay` async background task.
  3. Awaits the first 1,600-byte frame and prints:
     ```text
     captured 1600 bytes
     ```
  4. Updates the overlay's partial label with streaming frame counters.
  5. `closeEvent` emits `closed`; the async main cancels the audio task and exits. `Stop` emits `stop_requested`, ending capture gracefully (padded tail) without closing the window.

---

## 4. Verification and Quality Assurance

### 4.1 Automated Test Suite (`tests/test_desktop_client.py`)

The test suite contains 19 unit tests executed using `pytest` and `pytest-asyncio`:

| Component | Test Case | Target Checked | Result |
|---|---|---|---|
| **Stealth** | `test_stealth_enum_and_constants` | Enum values and hexadecimal flags (`0x00`, `0x01`, `0x11`) | PASS |
| **Stealth** | `test_stealth_capability_non_windows` | Returns `UNSUPPORTED` on Linux/macOS | PASS |
| **Stealth** | `test_stealth_apply_clear_non_windows` | Safe no-op and returns `UNSUPPORTED` on non-Windows | PASS |
| **Stealth** | `test_stealth_capability_windows_mock` | Probes `EXCLUDED` on build 19045, `BLACK_BOX` on build 18362 | PASS |
| **Stealth** | `test_stealth_apply_windows_success_excluded` | Calls `SetWindowDisplayAffinity(hwnd, 0x11)` successfully | PASS |
| **Stealth** | `test_stealth_apply_windows_fallback_black_box` | Retries `0x01` upon `0x11` failure and transitions to `BLACK_BOX` | PASS |
| **Stealth** | `test_stealth_apply_windows_both_failed` | Returns `FAILED` if both 0x11 and 0x01 fail | PASS |
| **Stealth** | `test_stealth_clear_windows` | Verifies `clear()` sets `WDA_NONE` (`0x00`) | PASS |
| **Overlay** | `test_overlay_creation_and_flags` | Flags: frameless, top, tool, no-focus; Attributes: translucent, no-activate | PASS |
| **Overlay** | `test_overlay_api_methods` | Transcript, append, partial, status, model, and stealth badge updates | PASS |
| **Overlay** | `test_overlay_click_through_toggle` | Toggles `WindowTransparentForInput` flag cleanly | PASS |
| **Overlay** | `test_overlay_stealth_toggle` | Toggles stealth state on/off | PASS |
| **Overlay** | `test_overlay_close_signal` | `closeEvent` emits `closed` so the app can exit | PASS |
| **Overlay** | `test_overlay_stop_signal` | `Stop` emits `stop_requested` without closing | PASS |
| **Overlay** | `test_overlay_stealth_disabled_badge_is_visible_to_share` | Disabled stealth reports visible-to-share | PASS |
| **Audio** | `test_audio_constants` | Validates 16 kHz, mono, 50 ms, 800 samples, 1600 bytes, queue size 40 | PASS |
| **Audio** | `test_mic_frames_yields_1600_byte_frames` | Verifies exact 1600-byte PCM frame generation | PASS |
| **Audio** | `test_mic_frames_tail_padding_on_stop` | Real fake-stream test: partial tail is padded to 1600 bytes on graceful stop | PASS |
| **Audio** | `test_mic_frames_aclose_does_not_raise` | Closing the generator does not yield from `finally` | PASS |

Run command:
```bash
pytest -v
# Output: 19 passed
```

### 4.2 CLI Verification
Execution of the package module:
```bash
python -m desktop_client.main --simulate-audio
# Stdout verified: captured 1600 bytes
```

Real capture requires PortAudio (`libportaudio2` on Debian/Ubuntu) and a microphone.

---

## 5. Next Iteration: Full Backend Integration Roadmap

When moving from the skeleton to the live session flow, the following contracts are to be connected:

```
+-----------------------------------------------------------------------------------------+
|                                    Client Overlay                                       |
+-----------------------------------------------------------------------------------------+
       |                                      |                                   ^
 1. WS Connect (Subscribe)              2. Binary Audio Chunks              SSE Answer Tokens
       |                                      |                                   |
       v                                      v                                   |
+--------------------------+        +--------------------------+        +--------------------------+
| WS /sessions/{id}/events |        | WS /sessions/{id}/audio  |        | POST /practice/answer    |
+--------------------------+        +--------------------------+        +--------------------------+
| - session.status_changed |        | - Raw PCM S16LE (1.6KB)  |        | Body:                    |
| - speech.transcript_delta|        | - {"type":"client.       |        |  {question_turn_id,      |
| - speech.transcript_final|        |     audio_end"}          |        |   candidate_turn_id}     |
| - client.heartbeat (20s) |        | Receives: audio.closed   |        | Receives: answer_token   |
+--------------------------+        +--------------------------+        +--------------------------+
```

### Connection Sequence
1. **Preflight & Session Setup:**
   - `GET /api/v1/policies/acceptable-use` → read `version`.
   - `POST /api/v1/sessions` with `kind: "live"` and `acknowledged_policy_version`.
2. **WebSocket Order:**
   - **First:** Connect `WS /api/v1/sessions/{id}/events` (with `Sec-WebSocket-Protocol: bearer.<jwt>`). Consume the initial `session.status_changed` snapshot.
   - **Second:** Connect `WS /api/v1/sessions/{id}/audio` and begin streaming the 1,600-byte frames generated by `audio.mic_frames()`.
3. **Heartbeats:**
   - Emit `{"type": "client.heartbeat"}` every 15–20 seconds on `/events` (server drops connection after 65 seconds of silence).
4. **Live Turn Delivery & Answers:**
   - On `speech.transcript_delta` → update `overlay.set_partial(delta_text)`.
   - On `speech.transcript_final` → append finalized text with `turn_id` via `overlay.append_transcript(final_text)`.
   - POST `turn_id` to `/api/v1/sessions/{id}/practice/answer` to stream live answers via Server-Sent Events (SSE).
5. **Teardown & Debrief:**
   - Send `{"type": "client.audio_end"}` on `/audio` and wait for `audio.closed`.
   - Call `POST /api/v1/sessions/{id}/end` to initiate debrief and memory persistence.
