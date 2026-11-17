# PySide6 Desktop Client Skeleton

Skeleton implementation of the desktop client overlay with microphone capture and Windows capture exclusion (stealth).

## Scope
- UI overlay: Frameless, translucent, always-on-top, non-activating window with pulsing recording dot, transcript turns, partials, and stealth status badge.
- Audio capture: 50 ms PCM S16LE 16 kHz mono frames (1,600 bytes) via `sounddevice.RawInputStream` and bounded async queue.
- Windows stealth: Screen share / capture exclusion via `SetWindowDisplayAffinity` (`WDA_EXCLUDEFROMCAPTURE` / `WDA_MONITOR`), strictly decoupled from click-through.
- Zero business logic, zero backend changes.

---

## Package Architecture

```text
desktop_client/
  ├── __init__.py       # Package metadata (__version__)
  ├── main.py           # qasync entry point, initializes OverlayWindow & streams audio frames
  ├── overlay.py        # QWidget overlay (always-on-top, translucent, click-through, HWND affinity handling)
  ├── audio.py          # sounddevice.RawInputStream async 50ms (1600 bytes) frame capture
  └── stealth.py        # Windows SetWindowDisplayAffinity wrapper & state machine
tests/
  └── test_desktop_client.py  # Unit tests for UI, audio, and stealth
```

---

## Stealth Capture Exclusion Contract (`desktop_client/stealth.py`)

| State | Value | Description |
|---|---|---|
| `EXCLUDED` | `"excluded"` | OS confirmed exclusion via `WDA_EXCLUDEFROMCAPTURE` (`0x11`) |
| `BLACK_BOX` | `"black_box"` | Excluded as opaque rectangle via `WDA_MONITOR` (`0x01`) |
| `UNSUPPORTED` | `"unsupported"` | Non-Windows OS (macOS, Linux) |
| `FAILED` | `"failed"` | API present but call rejected by OS / compositor |

### Re-Application Rules
1. Re-applied in `showEvent`.
2. Native HWND is recreated on `setWindowFlag()` calls (such as toggling `WindowTransparentForInput`); affinity is re-applied via `QTimer.singleShot(0, self._reapply_stealth)`.
3. Handle is passed as a local variable; never cached on the instance.
4. If degraded to `BLACK_BOX`, the background paint alpha is increased toward 255 so the capture-side box appears intentional.

---

## Running and Verification

### 1. Run the client
```bash
python -m desktop_client.main
```
The client prints `captured 1600 bytes` at least once upon receiving the first 50 ms audio frame and streams updates to the overlay.

### 2. Run unit tests
```bash
pytest -v
```

### 3. Verification Checklist
- [x] Overlay renders as frameless, translucent, always-on-top window (`FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowDoesNotAcceptFocus`).
- [x] Click-through toggle works independently (`WindowTransparentForInput`); controls are clickable in interactive mode.
- [x] Audio capture produces 50 ms frames of 1,600 bytes (16 kHz, mono, int16).
- [x] Stealth capture exclusion decoupled from click-through, survives HWND re-creations, and reports state via dynamic badge.
- [x] No backend files or endpoints modified.
