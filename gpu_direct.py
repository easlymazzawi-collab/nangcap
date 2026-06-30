"""
GPU Direct Pipeline — NVDEC → RGB overlay → NVENC (PyNvVideoCodec).
Khong dung FFmpeg filter graph. Fallback: None (caller dung FFmpeg Turbo).
"""
import os
import subprocess
import tempfile
import time

import numpy as np

from wm_overlay import build_overlay_rgba, clear_overlay_cache
from ram_temp import resolve_temp_base

try:
    import PyNvVideoCodec as nvc
    PYNVC_OK = True
except ImportError:
    nvc = None
    PYNVC_OK = False

try:
    import cupy as cp
    CUPY_OK = True
except ImportError:
    cp = None
    CUPY_OK = False

_BATCH = 8


def is_available():
    return PYNVC_OK


def capability_info():
    parts = []
    if PYNVC_OK:
        parts.append("PyNvVideoCodec OK")
    else:
        parts.append("PyNvVideoCodec MISSING (pip install PyNvVideoCodec)")
    if CUPY_OK:
        parts.append("CuPy OK (GPU overlay batch)")
    else:
        parts.append("CuPy optional (pip install cupy-cuda12x)")
    return " | ".join(parts)


def _rgb_to_nv12(rgb):
    """RGB uint8 HxWx3 -> NV12 uint8 flat buffer for NVENC."""
    h, w = rgb.shape[:2]
    yuv = _rgb_to_yuv420(rgb)
    y = yuv[:h, :]
    u = yuv[h:h + h // 4, :].reshape(h // 2, w // 2)
    v = yuv[h + h // 4:, :].reshape(h // 2, w // 2)
    uv = np.empty((h // 2, w), dtype=np.uint8)
    uv[:, 0::2] = u
    uv[:, 1::2] = v
    return np.concatenate([y.reshape(-1), uv.reshape(-1)])


def _rgb_to_yuv420(rgb):
    h, w = rgb.shape[:2]
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    y = (0.257 * r + 0.504 * g + 0.098 * b + 16).clip(0, 255).astype(np.uint8)
    u = (-0.148 * r - 0.291 * g + 0.439 * b + 128).clip(0, 255).astype(np.uint8)
    v = (0.439 * r - 0.368 * g - 0.071 * b + 128).clip(0, 255).astype(np.uint8)
    u_ds = u.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3)).astype(np.uint8)
    v_ds = v.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3)).astype(np.uint8)
    out = np.empty((h + h // 2, w), dtype=np.uint8)
    out[:h, :] = y
    out[h:h + h // 2, 0::2] = u_ds
    out[h:h + h // 2, 1::2] = v_ds
    return out


def _frame_to_numpy(frame):
    if isinstance(frame, np.ndarray):
        return frame
    if hasattr(frame, "__dlpack__"):
        try:
            import torch
            t = torch.from_dlpack(frame)
            if t.is_cuda:
                return t.cpu().numpy()
            return t.numpy()
        except Exception:
            pass
    if hasattr(frame, "shape"):
        return np.asarray(frame, dtype=np.uint8)
    raise TypeError(f"Khong doc duoc frame type: {type(frame)}")


def _blend_batch_cpu(frames_rgb, overlay_rgba):
    """frames_rgb: list HxWx3 uint8. overlay_rgba: HxWx4."""
    out = []
    ov_rgb = overlay_rgba[:, :, :3].astype(np.float32)
    ov_a = overlay_rgba[:, :, 3:4].astype(np.float32) / 255.0
    for fr in frames_rgb:
        bg = fr.astype(np.float32)
        blended = bg * (1.0 - ov_a) + ov_rgb * ov_a
        out.append(blended.clip(0, 255).astype(np.uint8))
    return out


def _blend_batch_gpu(frames_rgb, overlay_rgba):
    ov = cp.asarray(overlay_rgba)
    ov_rgb = ov[:, :, :3].astype(cp.float32)
    ov_a = ov[:, :, 3:4].astype(cp.float32) / 255.0
    out = []
    for fr in frames_rgb:
        bg = cp.asarray(fr, dtype=cp.float32)
        blended = bg * (1.0 - ov_a) + ov_rgb * ov_a
        out.append(cp.asnumpy(blended.clip(0, 255).astype(cp.uint8)))
    return out


def _mux_audio(ffmpeg, inp, video_only, outp, norm_audio=False):
    if not ffmpeg or not os.path.isfile(ffmpeg):
        os.replace(video_only, outp)
        return True, ""
    if norm_audio:
        cmd = [
            ffmpeg, "-hide_banner", "-y",
            "-i", video_only, "-i", inp,
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k",
            "-movflags", "+faststart", outp,
        ]
    else:
        cmd = [
            ffmpeg, "-hide_banner", "-y",
            "-i", video_only, "-i", inp,
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "copy", "-c:a", "copy",
            "-movflags", "+faststart", outp,
        ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            tail = (r.stderr or "")[-200:]
            return False, f"mux audio loi: {tail}"
        return True, ""
    except Exception as e:
        return False, str(e)


def _encoder_config(cfg):
    cq = int(cfg.get("nvenc_cq", 23))
    preset = (cfg.get("nvenc_preset") or "p1").upper()
    if not preset.startswith("P"):
        preset = "P" + preset.lstrip("pP")
    return {
        "gpu_id": int(cfg.get("gpu_id", 0)),
        "codec": "h264",
        "preset": preset,
        "tuning_info": "high_quality",
        "ratecontrolmode": "constqp",
        "constqp": cq,
        "gop": 250,
    }


def process(cfg, inp, outp, ffprobe_fn=None, trim=0, dur_limit=None,
            norm_audio=False, tmp_dir=None, progress_cb=None):
    """
    GPU Direct watermark encode.
    Tra (ok: bool, err: str, meta: dict).
    """
    if not PYNVC_OK:
        return False, "PyNvVideoCodec chua cai (pip install PyNvVideoCodec)", {}

    if not os.path.isfile(inp):
        return False, f"Khong tim thay input: {inp}", {}

    t0 = time.time()
    meta = {"engine": "gpu_direct", "frames": 0}

    try:
        otype = nvc.OutputColorType.RGB
    except AttributeError:
        otype = getattr(nvc, "OutputColorType", None)
        otype = getattr(otype, "RGB", "RGB") if otype else "RGB"

    try:
        decoder = nvc.SimpleDecoder(
            inp,
            gpu_id=int(cfg.get("gpu_id", 0)),
            use_device_memory=CUPY_OK,
            output_color_type=otype,
        )
    except Exception as e:
        return False, f"SimpleDecoder loi: {e}", meta

    try:
        total = len(decoder)
    except Exception:
        total = 0

    if total <= 0:
        return False, "Video khong co frame", meta

    # Lay kich thuoc tu frame dau
    try:
        first = _frame_to_numpy(decoder[0])
    except Exception as e:
        return False, f"Doc frame loi: {e}", meta

    height, width = first.shape[:2]
    overlay, oerr = build_overlay_rgba(cfg, width, height)
    if overlay is None:
        return False, oerr or "Tao overlay loi", meta

    fps = float(cfg.get("_fps") or 30.0)
    if ffprobe_fn:
        try:
            fps = float(ffprobe_fn(inp) or fps)
        except Exception:
            pass

    skip_frames = int(max(0, float(trim or 0)) * fps)
    max_frames = total
    if dur_limit is not None:
        max_frames = min(total, skip_frames + int(float(dur_limit) * fps))
    elif trim and trim > 0:
        max_frames = total  # decode all then skip — SimpleDecoder seek limited

    enc_cfg = _encoder_config(cfg)
    try:
        encoder = nvc.CreateEncoder(
            width=width,
            height=height,
            format="NV12",
            usecpuinputbuffer=True,
            **enc_cfg,
        )
    except TypeError:
        encoder = nvc.CreateEncoder(
            width, height, "NV12", True, **enc_cfg
        )
    except Exception as e:
        return False, f"CreateEncoder loi: {e}", meta

    blend = _blend_batch_gpu if CUPY_OK else _blend_batch_cpu
    tmp_h264 = os.path.join(
        tmp_dir or tempfile.gettempdir(),
        f"gpu_direct_{abs(hash(outp))}.h264",
    )
    frames_done = 0
    idx = skip_frames

    try:
        with open(tmp_h264, "wb") as bitstream:
            while idx < max_frames:
                batch_n = min(_BATCH, max_frames - idx)
                try:
                    if idx == 0 and skip_frames == 0:
                        raw_frames = decoder.get_batch_frames(batch_n)
                    else:
                        indices = list(range(idx, idx + batch_n))
                        raw_frames = decoder.get_batch_frames_by_index(indices)
                except Exception:
                    raw_frames = [decoder[i] for i in range(idx, idx + batch_n)]

                if not raw_frames:
                    break

                rgb_list = [_frame_to_numpy(f) for f in raw_frames]
                if rgb_list[0].shape[2] == 4:
                    rgb_list = [x[:, :, :3] for x in rgb_list]

                blended = blend(rgb_list, overlay)
                for fr in blended:
                    nv12 = _rgb_to_nv12(fr)
                    pkt = encoder.Encode(nv12)
                    if pkt:
                        bitstream.write(bytearray(pkt))
                    frames_done += 1
                    if progress_cb and frames_done % 120 == 0:
                        progress_cb(frames_done, max_frames - skip_frames)

                idx += batch_n

            tail = encoder.EndEncode()
            if tail:
                bitstream.write(bytearray(tail))
    except Exception as e:
        return False, f"Encode pipeline loi: {e}", meta
    finally:
        try:
            del decoder
        except Exception:
            pass

    meta["frames"] = frames_done
    meta["elapsed"] = round(time.time() - t0, 2)

    if frames_done <= 0:
        return False, "Khong encode duoc frame nao", meta

    video_mp4 = tmp_h264 + ".mp4"
    ffmpeg = cfg.get("ffmpeg_path", "")
    muxed = False
    if ffmpeg and os.path.isfile(ffmpeg):
        cmd = [
            ffmpeg, "-hide_banner", "-y",
            "-framerate", str(fps),
            "-i", tmp_h264,
            "-c:v", "copy",
            "-movflags", "+faststart",
            video_mp4,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        muxed = r.returncode == 0 and os.path.isfile(video_mp4)

    vid_src = video_mp4 if muxed else tmp_h264
    ok_mux, mux_err = _mux_audio(ffmpeg, inp, vid_src, outp, norm_audio=norm_audio)
    for f in (tmp_h264, video_mp4):
        if os.path.isfile(f):
            try:
                os.remove(f)
            except OSError:
                pass

    if not ok_mux:
        return False, mux_err, meta

    meta["fps"] = fps
    return True, "", meta


def ram_temp_path(cfg, basename):
    base, msg = resolve_temp_base(cfg, ram_mode=True)
    if not base:
        raise RuntimeError(msg or "RAM mode: khong co RAM disk")
    return os.path.join(base, basename)
