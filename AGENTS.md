# GPU Watermark Studio

Single-purpose desktop app that batch-watermarks videos/images with FFmpeg (logo PNG + 4-corner text + center text + optional outro) and can auto-upload results to Telegram. It is built primarily for **Windows + NVIDIA GPU** but also runs on Linux.

- `WatermarkStudiov15.py` — all backend logic + the `pywebview` desktop window (entry point: `python3 WatermarkStudiov15.py`).
- `ui.html` — the UI; it is read from the same directory as the script at startup, so the two files must stay together.
- Runtime config/presets are saved to `wm_config.json` next to the script (created by the app when you save a preset; not committed).

## Cursor Cloud specific instructions

Environment is Linux (Ubuntu, no NVIDIA GPU) with an X display on `DISPLAY=:1`. The startup update script installs the Python deps (`pywebview`, `Pillow`, `pyrogram`, `tgcrypto`). The GTK3 + WebKit2 backend, `python3-dev`, and `ffmpeg`/`ffprobe` are provided by the VM image.

### Running the app
- Launch the GUI with: `DISPLAY=:1 WEBKIT_DISABLE_COMPOSITING_MODE=1 python3 WatermarkStudiov15.py`
  - `pywebview` uses the GTK/WebKit2 backend here. `WEBKIT_DISABLE_COMPOSITING_MODE=1` avoids GPU-compositing issues in the headless-rendered desktop.
- `DEFAULT_CONFIG` ships Windows paths (e.g. `ffmpeg_path` = `C:\...\ffmpeg.exe`). On this VM set `ffmpeg_path` to `/usr/bin/ffmpeg` (the UI has a field for it, or pre-seed `wm_config.json`). The Linux default font is `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`.

### No GPU — expect NVENC fallback
- There is no NVIDIA GPU, so `h264_nvenc`/NVDEC/`*_cuda` filters fail at runtime. The app catches this and **auto-falls back to `libx264` (CPU)**, logging `⚠️ ... fallback libx264 (CPU)`. This is normal here; output is still produced correctly.
- To skip the GPU attempt entirely, run a CPU worker (`cpu_workers > 0`). Gotcha: setting `gpu_workers: 0` in a saved preset does **not** stick — the UI loads it as `c.gpu_workers || 5` and `0` is falsy in JS, so it becomes `5`. Either set GPU workers in the UI directly or just rely on the CPU fallback.

### Quirks worth knowing
- Encoded output MP4s contain **two** video streams: stream 0 = watermarked (the filtergraph output), stream 1 = the raw re-encoded source (because the encode commands pass both the unlabeled `-filter_complex` output and `-map 0:v:0`). Players use the first/watermarked stream; when extracting frames with `ffmpeg`, use `-map 0:v:0` to get the watermarked one.
- Corner/center text only renders for `t >= enable_time` (default `8`). For short test clips, lower `enable_time` or the text won't appear.
- Telegram is optional: Pyrogram (API upload) works cross-platform but needs `tg_api_id`/`tg_api_hash`/login; the "Telegram Desktop" clipboard-automation path is Windows-only and is disabled on Linux (`DESKTOP_UP_OK=False`).

### Lint / tests
- No linter config or automated test suite exists. Use `python3 -m py_compile WatermarkStudiov15.py` as a basic syntax check.
