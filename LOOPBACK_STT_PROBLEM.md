# Loopback STT Produces No Transcript

## Summary

When running the desktop client with `DESKTOP_STT_PROVIDER=moonshine` and `DESKTOP_AUDIO_SOURCE=loopback`, Moonshine receives audio frames but emits **no transcript events**. The same Moonshine STT path transcribes successfully for `DESKTOP_AUDIO_SOURCE=mic` and for a known speech WAV file, so the failure is isolated to the loopback capture/input signal, not the Moonshine transcription pipeline.

## Environment

- OS: Windows (win32)
- Repo: `e:\ai_interview_desktop`, branch `main`
- STT: Moonshine local (`moonshine-voice`), model `medium-streaming-en/quantized_26_11_02`, arch 5
- Backend: `http://127.0.0.1:8000` (reachable; session/create/end succeeded)
- Default output device: `Speaker (Realtek(R) Audio)` (active)
- Captured loopback device: `Stereo Mix (Realtek(R) Audio)` (device 12, Windows WASAPI, native 48000 Hz, 2 ch -> 16000 Hz mono)
- Active capture endpoints observed: Stereo Mix (Realtek) and Microphone Array (Realtek)
- Browser present during test: Chrome with YouTube tab playing

## Reproduction

1. Configure desktop client for Moonshine loopback:
   - `DESKTOP_STT_PROVIDER=moonshine`
   - `DESKTOP_AUDIO_SOURCE=loopback`
2. Start client:
   ```powershell
   cd /e/ai_interview_desktop
   .venv/Scripts/python.exe -m desktop_client.main
   ```
3. Play YouTube audio through system output while session is active.
4. Observe logs: audio frames are fed, but no Moonshine line/transcript events are emitted.

Optional debug capture (added during investigation):
```powershell
$env:DESKTOP_DEBUG_AUDIO_DUMP="C:\Users\USER\AppData\Local\Temp\commandcode\e--ai-interview-desktop\ee0e8cd2-c813-4520-9b7c-82cb1f5e44ba\scratchpad\loopback.wav"
$env:DESKTOP_DEBUG_AUDIO_DUMP_SECONDS="15"
.venv/Scripts/python.exe -m desktop_client.main
```

## Observed Evidence

### Runtime logs

- Loopback stream opens: `Loopback stream started (device 12, native 48000 Hz, 2 ch -> 16000 Hz mono)`.
- Audio is fed continuously: `fed_frames=548 fed_bytes=876800 rms_max=1819.6` (example run).
- Moonshine stream is active but produces no text:
  - `has_stream=True`
  - `listeners=1`
  - `event_count=0`
  - `transcript lines=0` on updates
- No transcript/final turn is ingested.

### Captured debug WAV analysis

File: `scratchpad/loopback.wav` (15s, 16 kHz, mono, s16le)

| Metric | Value |
|---|---:|
| Duration | 15.0 s |
| RMS | 0.04818 |
| Max abs | 0.07507 |
| P99 abs | 0.07404 |
| Mean abs | 0.03997 |
| Zero-crossing rate | ~176.8 Hz |

Per-second RMS was nearly constant (~0.052) for ~13s, then dropped to near silence. This is consistent with low-level hum/noise, not intelligible speech.

### Moonshine control tests

- **Known speech WAV (`moonshine_voice/assets/beckett.wav`) through same Moonshine session path**: produced partial/final transcript events.
- **Dumped `loopback.wav` through same Moonshine session path**: produced **0 events**.
- **Live mic mode (`DESKTOP_AUDIO_SOURCE=mic`)**: reported by user as working well.

## Impact

- Local Moonshine STT cannot transcribe interviewer/system audio in loopback mode.
- The desktop client appears to be capturing/routing the wrong signal from Stereo Mix, so no transcript turns reach the backend ingest path.

## What Is Not the Cause (based on current evidence)

- **Moonshine model/package broken**: ruled out by successful mic transcription and successful known-WAV transcription.
- **No audio frames reaching Moonshine**: ruled out by fed frame counts, RMS, and advancing stream time.
- **Physical speakers required for digital loopback**: ruled out in principle; Windows loopback captures the endpoint mix, not acoustic output. Physical speaker damage is not sufficient to explain the captured non-speech signal.

## Likely Problem Areas

1. **Windows audio routing / per-app output**: Chrome or the playing app may not be routed to `Speaker (Realtek(R) Audio)`, so Stereo Mix does not contain that audio.
2. **Stereo Mix source/configuration**: Stereo Mix may be enabled but capturing the wrong mix, muted/low-level path, or driver-affected signal.
3. **Captured content mismatch**: the captured signal may be non-speech/music/noise that Moonshine cannot decode as English speech.
4. **Device selection ambiguity**: the current loopback fallback picks a loopback-capable input device (Stereo Mix). If multiple endpoints/routes exist, the selected device may not correspond to the active playback path.

## Open Questions / Next Checks

- [ ] Play `scratchpad/loopback.wav` back: does it sound like the expected YouTube speech, noise, or near-silence?
- [ ] Confirm Chrome/playing app output device in Windows Volume mixer is `Speaker (Realtek(R) Audio)`.
- [ ] Verify Stereo Mix properties (level, enabled, enhancements/AGC off, default format) in Windows Sound settings.
- [ ] Run a controlled test by playing a known local speech WAV through Realtek Speakers and capturing loopback again; compare dump to expected audio.
- [ ] If routing is correct but capture is still wrong, add/selectable `DESKTOP_LOOPBACK_DEVICE` (or explicit device name/index) and log the selected WASAPI endpoint/device ID.
- [ ] Re-test with debug dump disabled after resolving routing, confirming Moonshine emits line events and finalized transcript turns are ingested.

## Reported Context (not independently verified)

- User reported that loopback worked before a recent edit when playing YouTube audio. Current evidence shows mic and Moonshine model work now; the captured loopback signal is non-speech. The "worked before" claim should be rechecked against the exact prior config/build.