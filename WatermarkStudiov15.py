"""
GPU Watermark Studio v12
Batch watermark video/image (overlay_cuda LUON BAT) + logo + 4 goc chu + outro
+ Auto upload Telegram Desktop.

UI tach rieng o file ui.html cung thu muc.
"""
import os, sys, json, time, threading, subprocess, hashlib, tempfile, shutil, asyncio, webview

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False

# Pyrogram cho upload Telegram (optional — neu chua cai thi tat tinh nang upload)
_TG_THREAD_PARAM = None  # ten tham so de gui vao topic, dò theo ban Pyrogram
try:
    from pyrogram import Client
    from pyrogram.errors import (
        SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, FloodWait,
    )
    PYRO_OK = True
    # Do xem send_video nhan tham so topic nao:
    #   - fork moi (pyrofork/kurigram): message_thread_id
    #   - Pyrogram goc 2.x: reply_to_message_id (forum topic = message thread)
    try:
        import inspect as _insp
        _sv_params = _insp.signature(Client.send_video).parameters
        if "message_thread_id" in _sv_params:
            _TG_THREAD_PARAM = "message_thread_id"
        elif "reply_to_message_id" in _sv_params:
            _TG_THREAD_PARAM = "reply_to_message_id"
    except Exception:
        _TG_THREAD_PARAM = "reply_to_message_id"
except ImportError:
    PYRO_OK = False

# ==============================================================================
#  TELEGRAM DESKTOP UPLOADER (tu dong hoa Telegram Desktop)
#  Cach hoat dong: focus cua so Telegram -> copy file vao clipboard dang
#  CF_HDROP (file-drop) -> Ctrl+V -> Enter. Telegram tu queue upload.
#  KHONG dung API, KHONG dung pyrogram, KHONG can pywinauto.
#  -> Mo Telegram Desktop, click san vao chat dich truoc khi chay.
# ==============================================================================
import struct as _struct
try:
    import ctypes as _ct
    from ctypes import wintypes as _wt
    import win32clipboard as _wcb
    import win32con as _wcon
    _u32 = _ct.windll.user32
    _k32 = _ct.windll.kernel32
    _u32.GetWindowTextW.argtypes = [_wt.HWND, _wt.LPWSTR, _ct.c_int]
    _u32.GetWindowThreadProcessId.argtypes = [_wt.HWND, _ct.POINTER(_wt.DWORD)]
    _u32.GetWindowRect.argtypes = [_wt.HWND, _ct.POINTER(_wt.RECT)]
    _k32.QueryFullProcessImageNameW.argtypes = [
        _wt.HANDLE, _wt.DWORD, _wt.LPWSTR, _ct.POINTER(_wt.DWORD)]
    _EnumWindowsProc = _ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    _PROC_QUERY = 0x1000
    _SW_RESTORE = 9
    _KEYUP      = 0x0002
    DESKTOP_UP_OK  = True
    DESKTOP_UP_ERR = ""
except Exception as _e:           # noqa: BLE001
    DESKTOP_UP_OK  = False
    DESKTOP_UP_ERR = str(_e)

_TELEGRAM_EXE = {"telegram.exe", "telegramdesktop.exe",
                 "tdesktop.exe", "telegram portable.exe"}

def _proc_name(hwnd):
    pid = _wt.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, _ct.byref(pid))
    if not pid.value:
        return ""
    h = _k32.OpenProcess(_PROC_QUERY, False, pid.value)
    if not h:
        return ""
    try:
        buf = _ct.create_unicode_buffer(1024)
        sz  = _wt.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, _ct.byref(sz)):
            return os.path.basename(buf.value)
    finally:
        _k32.CloseHandle(h)
    return ""

def find_telegram_window():
    """Tim cua so Telegram Desktop lon nhat dang hien. Tra (hwnd, title)."""
    found = []
    def cb(hwnd, lparam):
        if not _u32.IsWindowVisible(hwnd):
            return True
        tb = _ct.create_unicode_buffer(512)
        _u32.GetWindowTextW(hwnd, tb, 512)
        if not tb.value:
            return True
        r = _wt.RECT()
        _u32.GetWindowRect(hwnd, _ct.byref(r))
        if (r.right - r.left) < 300 or (r.bottom - r.top) < 300:
            return True
        if _proc_name(hwnd).lower() in _TELEGRAM_EXE:
            found.append((hwnd, tb.value, (r.right-r.left)*(r.bottom-r.top)))
        return True
    _u32.EnumWindows(_EnumWindowsProc(cb), 0)
    if not found:
        return None, None
    found.sort(key=lambda x: -x[2])
    return found[0][0], found[0][1]

def _focus_window(hwnd):
    _u32.ShowWindow(hwnd, _SW_RESTORE)
    time.sleep(0.1)
    fg = _u32.GetForegroundWindow()
    fg_thread = _u32.GetWindowThreadProcessId(fg, None)
    cur = _k32.GetCurrentThreadId()
    attached = False
    if fg_thread and fg_thread != cur:
        if _u32.AttachThreadInput(cur, fg_thread, True):
            attached = True
    try:
        _u32.BringWindowToTop(hwnd)
        _u32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _u32.AttachThreadInput(cur, fg_thread, False)
    time.sleep(0.25)
    return _u32.GetForegroundWindow() == hwnd

def _clip_files(paths):
    """Dat danh sach file vao clipboard dang CF_HDROP (giong Ctrl+C tren file)."""
    header = _struct.pack("Iiiii", 20, 0, 0, 0, 1)
    body   = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    _wcb.OpenClipboard()
    try:
        _wcb.EmptyClipboard()
        _wcb.SetClipboardData(_wcon.CF_HDROP, header + body)
    finally:
        _wcb.CloseClipboard()

def _key(vk, up=False):
    _u32.keybd_event(vk, 0, _KEYUP if up else 0, 0)

def _send_paste():
    _key(0x11); _key(0x56); _key(0x56, True); _key(0x11, True)   # Ctrl+V

def _send_send(ctrl_enter=False):
    if ctrl_enter:
        _key(0x11); _key(0x0D); _key(0x0D, True); _key(0x11, True)  # Ctrl+Enter
    else:
        _key(0x0D); _key(0x0D, True)                               # Enter

def desktop_upload(hwnd, files, send_key="enter", dialog_wait=1.0):
    """Focus -> paste files -> send. Tra (ok, msg)."""
    if not files:
        return True, "rong"
    try:
        if not _focus_window(hwnd):
            # van thu paste — doi khi GetForegroundWindow tra sai nhung van focus ok
            pass
        _clip_files(files)
        time.sleep(0.15)
        _send_paste()
        time.sleep(max(0.3, float(dialog_wait)))   # cho dialog "gui media" hien
        _send_send(send_key == "ctrl_enter")
        return True, ""
    except Exception as e:                          # noqa: BLE001
        return False, str(e)

def split_chunks(files, max_size=10):
    """Chia danh sach file thanh cac album <= max_size."""
    n = len(files)
    if n <= max_size:
        return [list(files)]
    import math as _m
    n_chunks = _m.ceil(n / max_size)
    sz = _m.ceil(n / n_chunks)
    return [files[i:i+sz] for i in range(0, n, sz)]

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
APP_DIR     = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
CONFIG_PATH = os.path.join(APP_DIR, "wm_config.json")
UI_PATH     = os.path.join(APP_DIR, "ui.html")
SESSION_DIR = os.path.join(APP_DIR, "sessions")

VIDEO_EXTS = {".mp4",".mkv",".avi",".mov",".wmv",".flv",".ts",".m4v",".webm",".3gp",".mpeg"}
IMAGE_EXTS = {".jpg",".jpeg",".png"}
ALL_EXTS   = VIDEO_EXTS | IMAGE_EXTS

def _load_ui():
    """Doc ui.html cung thu muc. Neu thieu -> bao loi ro rang."""
    try:
        with open(UI_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ("<html><body style='background:#0e0f11;color:#e8eaed;"
                "font-family:sans-serif;padding:40px'>"
                "<h2>Khong tim thay ui.html</h2>"
                "<p>Dat file <b>ui.html</b> cung thu muc voi file chuong trinh.</p>"
                "</body></html>")

# ==============================================================================
#  CONFIG
# ==============================================================================

DEFAULT_CONFIG = {
    "ffmpeg_path":   r"C:\Users\Admin\Downloads\ffmpeg-update\bin\ffmpeg.exe",
    "input_folder":  "input",
    "output_folder": "output_videos",
    "output_suffix": "_vnclip",
    "logo_image":    "logo.png",
    "font_file":     (r"C:/Windows/Fonts/arial.ttf" if sys.platform == "win32"
                     else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    # Logo PNG — goc man hinh, vi tri dung margin
    "logo_scale_w":  160,
    "logo_opacity":  0.7,
    "logo_margin_x": 15,
    "logo_margin_y": 15,
    # 4 goc logo PNG — moi (v7)
    "enable_tl": False, "logo_tl": "", "logo_tl_w": 120, "logo_tl_op": 0.7, "logo_tl_x": 15, "logo_tl_y": 15,
    "enable_tr": False, "logo_tr": "", "logo_tr_w": 120, "logo_tr_op": 0.7, "logo_tr_x": 15, "logo_tr_y": 15,
    "enable_bl": False, "logo_bl": "", "logo_bl_w": 120, "logo_bl_op": 0.7, "logo_bl_x": 15, "logo_bl_y": 15,
    "enable_br": False, "logo_br": "", "logo_br_w": 120, "logo_br_op": 0.7, "logo_br_x": 15, "logo_br_y": 15,
    # Chu watermark giua man hinh (tinh)
    "enable_center":  False,
    "center_text":    "t.me/VnKong",
    "center_size":    25,
    "center_opacity": 0.15,
    # HS preset — hieu ung dong (drawtext CPU)
    "hs_mode":         False,
    "enable_center_fade": False,
    "fade_in":        1.5,
    "hold":           3.0,
    "fade_out":       1.5,
    "hide_duration":  5.0,
    "prime_x":        173,
    "prime_y":        137,
    "position_margin": 30,
    "enable_time":    8,
    "enable_dvd":      False, "dvd_text":  "t.me/VnKong",
    "dvd_size":        22,    "dvd_opacity": 0.10,
    "dvd_speed_x":     30,    "dvd_speed_y": 20, "dvd_margin": 20,
    "enable_moving":   False, "moving_text": "t.me/rauhong",
    "moving_size":     30,    "moving_opacity": 0.05,
    "enable_bouncing": False, "bouncing_text":  "bounce",
    "bouncing_size":   30,    "bouncing_opacity": 0.20,
    # Encode — overlay_cuda luon bat (fast_mode=True mac dinh, khong co toggle UI)
    "fast_mode":  True,
    "nvenc_cq":       23,
    "nvenc_preset":   "p1",
    "max_workers":    2,
    "gpu_workers":    2,
    "turbo_nvdec":    True,
    "cpu_workers":    0,
    "x264_preset":    "medium",
    "min_output_kb":  50,
    "filter_threads": 8,
    # Xu ly
    "trim_start":           0,
    "split_parts":          1,
    "enable_logo_limit":    False,
    "long_video_threshold": 300,
    "encode_ratio":         0.20,
    "skip_done":  True,
    # Outro
    "enable_outro":    False,
    "outro_text":      "Cam on da xem!\nt.me/rauhong_bot",
    "outro_duration":  5.0,
    "outro_bg_color":  "#d2dcff",
    "qr_image":        "QR.png",
    "outro_font_size": 36,
    # Auto upload Telegram Desktop
    "auto_upload":     False,
    "album_mode":      False,
    "upload_send_key": "enter",
    "upload_dialog_wait": 1.0,
    "album_max":       10,
    "upload_delay":    0.5,
    "nvenc_sessions":  5,
    "temp_dir":        "",
    # Backward compat
    "tg_enable": False, "tg_api_id": "", "tg_api_hash": "", "tg_phone": "",
    "tg_target": "", "tg_topic_id": "", "tg_auto_thumb": True, "tg_delete_after": True,
}

PRESETS = {
    "Fix ảnh": {
        # Preset chinh: overlay_cuda, watermark tinh 4 goc
        "enable_tl": False, "text_top_left": "",
        "enable_tr": True,  "text_top_right": "Telegram @Rauhong", "top_right_size": 22,
        "enable_bl": True,  "text_bottom_left": "Bot @rauhong_bot", "bottom_left_size": 22,
        "enable_br": False, "text_bottom_right": "",
        "enable_center": True, "center_text": "t.me/VnKong", "center_size": 28, "center_opacity": 0.15,
        "logo_scale_w": 160,
        "input_folder": "down", "output_folder": "output_videosvn", "output_suffix": "_vnclip",
        "trim_start": 0, "split_parts": 1,
        "fast_mode": True,
        "hs_mode": False,
        "enable_center_fade": False, "enable_dvd": False, "enable_moving": False, "enable_bouncing": False,
        "tg_enable": False,
    },
    "Học sinh": {
        # HS preset: WM dong (drawtext CPU)
        "enable_tl": False, "text_top_left": "",
        "enable_tr": True,  "text_top_right": "Telegram @rauhong", "top_right_size": 20,
        "enable_bl": True,  "text_bottom_left": "Bot @rauhong_bot", "bottom_left_size": 20,
        "enable_br": False, "text_bottom_right": "",
        "enable_center": True, "center_text": "t.me/VnKong", "center_size": 30, "center_opacity": 0.15,
        "logo_scale_w": 300,
        "input_folder": "down", "output_folder": "output_videoshs", "output_suffix": "_vnclip",
        "trim_start": 0, "enable_time": 5, "filter_threads": 6, "split_parts": 1,
        "fast_mode": False,
        "hs_mode": True,
        "enable_dvd": True, "enable_moving": True, "enable_bouncing": True,
        "tg_enable": False,
    },
}

# ==============================================================================
#  GLOBAL STATE
# ==============================================================================

_window           = None
_running          = False
_cancel_flag      = threading.Event()
_outro_cache      = {}
_center_png_cache = {}
_text_png_cache   = {}
_ffmpeg_log_lines = []
_upload_log_lines = []   # log rieng cho upload Telegram

# Gioi han so phien NVENC chay dong thoi (card consumer nhu RTX 3060 chi cho
# 3-5 phien encode cung luc). Dat o dau moi lan chay tu cfg["nvenc_sessions"].
_NVENC_SEM = None

def _run_nvenc(cmd, timeout=600):
    """Chay 1 lenh ffmpeg DUNG NVENC, co gioi han so phien dong thoi."""
    sem = _NVENC_SEM
    if sem is not None:
        sem.acquire()
    try:
        return _run_ffmpeg(cmd, timeout=timeout)
    finally:
        if sem is not None:
            sem.release()

# ==============================================================================
#  DONE LOG — ghi lai file da upload thanh cong de skip khi resume
# ==============================================================================

def _done_log_path(inp_dir):
    return os.path.join(inp_dir, ".wm_done_log.json")

def _load_done_log(inp_dir):
    try:
        with open(_done_log_path(inp_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()

def _save_done_log(inp_dir, done_set):
    try:
        with open(_done_log_path(inp_dir), "w", encoding="utf-8") as f:
            json.dump(sorted(done_set), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _mark_done(inp_dir, filename, done_set, lock):
    with lock:
        done_set.add(filename)
        _save_done_log(inp_dir, done_set)


def _emit_upload(line):
    """Ghi 1 dong vao log upload (co timestamp), gioi han 2000 dong."""
    global _upload_log_lines
    line = (line or "").rstrip()
    if not line:
        return
    stamp = time.strftime("%H:%M:%S")
    _upload_log_lines.append(f"[{stamp}] {line}")
    if len(_upload_log_lines) > 2000:
        _upload_log_lines = _upload_log_lines[-2000:]

# ==============================================================================
#  FFMPEG RUNNER
# ==============================================================================

def _emit_ffmpeg(line):
    global _ffmpeg_log_lines
    line = line.rstrip()
    if not line:
        return
    _ffmpeg_log_lines.append(line)
    if len(_ffmpeg_log_lines) > 2000:
        _ffmpeg_log_lines = _ffmpeg_log_lines[-2000:]

def _run_ffmpeg(cmd, timeout=600):
    """Chay ffmpeg, push log ra UI (global) VA giu stderr rieng cua process nay.

    Tra ve (returncode, stderr_text). stderr_text la log cuc bo cua DUNG
    process nay — khong lan voi cac process khac khi chay song song, dung
    cho viec detect loi fast-mode mot cach an toan.
    """
    local_lines = []
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=CREATE_NO_WINDOW)
    def _drain():
        buf = b""
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                buf += chunk
                parts = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
                buf = parts[-1]
                for p in parts[:-1]:
                    line = p.decode(errors="replace")
                    local_lines.append(line)
                    _emit_ffmpeg(line)
            if buf:
                line = buf.decode(errors="replace")
                local_lines.append(line)
                _emit_ffmpeg(line)
        except Exception:
            pass
    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    t.join(timeout=5)
    # Gioi han stderr cuc bo de khong ngon RAM voi file dai
    if len(local_lines) > 400:
        local_lines = local_lines[-400:]
    return proc.returncode, "\n".join(local_lines)

def _run_probe(cmd):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=CREATE_NO_WINDOW)
        return r.stdout.decode(errors="ignore")
    except Exception:
        return ""

# ==============================================================================
#  PROBE HELPERS
# ==============================================================================

def _ffprobe_path(cfg):
    cand = os.path.join(os.path.dirname(cfg["ffmpeg_path"]), "ffprobe.exe")
    if os.path.isfile(cand):
        return cand
    # Fallback: ffprobe trong PATH (phong ffmpeg_path khong co ffprobe.exe ben canh)
    return "ffprobe.exe" if sys.platform == "win32" else "ffprobe"

def _get_duration(ffprobe, path):
    out = _run_probe([ffprobe, "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=noprint_wrappers=1:nokey=1", path])
    try:
        return float(out.strip())
    except Exception:
        return None

def _probe_wh(ffprobe, path):
    out = _run_probe([ffprobe, "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=width,height,pix_fmt", "-of", "json", path])
    try:
        s   = json.loads(out).get("streams", [{}])[0]
        w   = int(s.get("width",  0))
        h   = int(s.get("height", 0))
        pix = s.get("pix_fmt", "")
        return (w, h, pix) if w and h else (None, None, "")
    except Exception:
        return None, None, ""

def _get_audio_sr(ffprobe, path):
    out = _run_probe([ffprobe, "-v", "error", "-select_streams", "a:0",
                      "-show_entries", "stream=sample_rate",
                      "-of", "default=noprint_wrappers=1:nokey=1", path])
    try:
        return int(out.strip())
    except Exception:
        return 44100

def _get_fps(ffprobe, path):
    out = _run_probe([ffprobe, "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=r_frame_rate",
                      "-of", "default=noprint_wrappers=1:nokey=1", path])
    try:
        s = out.strip()
        if "/" in s:
            n, d = s.split("/")
            return float(n) / float(d) if float(d) else 30.0
        return float(s) if s else 30.0
    except Exception:
        return 30.0

def _get_rotation(ffprobe, path):
    """Tra ve goc xoay video doc tu Display Matrix metadata (0, 90, -90, 180...).
    iPhone/Android thuong co rotation = -90 khi quay doc.
    """
    out = _run_probe([ffprobe, "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream_side_data=rotation",
                      "-of", "json", path])
    try:
        d = json.loads(out)
        st = d.get("streams", [{}])[0]
        for sd in st.get("side_data_list", []):
            rot = sd.get("rotation")
            if rot is not None:
                return int(float(rot))
    except Exception:
        pass
    return 0


def _is_valid(path, min_kb, is_img=False):
    if not os.path.exists(path):
        return False
    sz = os.path.getsize(path)
    return sz > 0 if is_img else sz / 1024 >= min_kb

TEN_BIT = {"yuv420p10le","yuv420p10be","yuv422p10le","yuv422p10be",
           "yuv444p10le","yuv444p10be","p010le","p010be"}

# Fields that belong to a "logo option" (quick-switch sub-preset).
LOGO_OPTION_KEYS = [
    "logo_image","logo_scale_w","logo_opacity","logo_margin_x","logo_margin_y",
    "enable_tl","logo_tl","logo_tl_w","logo_tl_op","logo_tl_x","logo_tl_y",
    "enable_tr","logo_tr","logo_tr_w","logo_tr_op","logo_tr_x","logo_tr_y",
    "enable_bl","logo_bl","logo_bl_w","logo_bl_op","logo_bl_x","logo_bl_y",
    "enable_br","logo_br","logo_br_w","logo_br_op","logo_br_x","logo_br_y",
    "enable_center","center_text","center_size","center_opacity",
    "text_bottom_left","bottom_left_size","text_top_right","top_right_size",
    "font_file",
]

# ==============================================================================
#  FILTER BUILDERS
# ==============================================================================

def _alpha_expr(et, fi, h, fo, hd, ma):
    show  = fi + h + fo
    cycle = show + hd
    p     = f"mod(t-{et}\\,{cycle})"
    return (f"if(lt(t\\,{et})\\,0\\,"
            f"if(lt({p}\\,{fi})\\,({p}/{fi})*{ma}\\,"
            f"if(lt({p}\\,{fi+h})\\,{ma}\\,"
            f"if(lt({p}\\,{show})\\,(({show}-{p})/{fo})*{ma}\\,0))))")

def _bounce(speed, margin, dim, tdim):
    return (f"{margin}+({dim}-{tdim}-{2*margin})"
            f"*abs(mod(t*{speed}/({dim}-{tdim}-{2*margin})\\,2)-1)")

def _escape_font(font):
    """Escape Windows drive-letter colon so FFmpeg filter parser does not split on it.

    FFmpeg's filter option syntax uses ':' as the key=value separator, so a path
    like ``C:/Windows/Fonts/arial.ttf`` is misread as ``fontfile=C`` followed by
    the unknown option ``/Windows/...``.  Replacing the drive colon with ``\\:``
    tells FFmpeg to treat it as a literal character.
    """
    if len(font) >= 2 and font[1] == ':':
        return font[0] + '\\:' + font[2:]
    return font

def _default_font():
    """Return a usable font path for the current platform."""
    if sys.platform == "win32":
        return r"C:/Windows/Fonts/arial.ttf"
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def _drawtext(font, txt, color, size, x, y, ec=""):
    ef = _escape_font(font)
    return (f"drawtext=fontfile='{ef}':text='{txt}':"
            f"fontsize={size}:fontcolor={color}:x={x}:y={y}{ec}")

def build_filter_complex(cfg, is_img, cut=None):
    font = _escape_font(cfg["font_file"])
    et   = cfg["enable_time"]
    m    = cfg["position_margin"]

    # Enable clauses
    if cut is not None:
        ec_logo = f":enable='between(t,0,{cut})'"
        ec_text = f":enable='between(t,{et},{cut})'"
    elif not is_img:
        ec_logo = ""
        ec_text = f":enable='gte(t,{et})'"
    else:
        ec_logo = ec_text = ""

    # Logo PNG: luon goc, dung margin
    lx = str(cfg.get("logo_margin_x", 15))
    ly = str(cfg.get("logo_margin_y", 15))

    # Chu center watermark
    center_fade = cfg.get("enable_center_fade", False)
    if is_img:
        cx, cy = "(w-tw)/2", "(h-th)/2"
        alpha  = str(cfg.get("center_opacity", 0.15))
    elif center_fade:
        show  = cfg["fade_in"] + cfg["hold"] + cfg["fade_out"]
        cycle = show + cfg["hide_duration"]
        cn    = f"floor((t-{et})/{cycle})"
        cx    = f"{m}+mod({cn}*{cfg.get('prime_x',173)}\\,(w-tw-{2*m}))"
        cy    = f"{m}+mod({cn}*{cfg.get('prime_y',137)}\\,(h-th-{2*m}))"
        alpha = _alpha_expr(et, cfg["fade_in"], cfg["hold"],
                            cfg["fade_out"], cfg["hide_duration"], cfg.get("center_opacity", 0.15))
    else:
        cx    = "(w-tw)/4"
        cy    = "(h-th)/2"
        alpha = str(cfg.get("center_opacity", 0.15))

    ct = cfg.get("center_text", "")
    cz = cfg.get("center_size", 25)
    layers = []
    if ct:
        ct_layer = (f"drawtext=fontfile='{font}':text='{ct}':"
                    f"fontsize={cz}:fontcolor=white:alpha='{alpha}':x={cx}:y={cy}{ec_text}")
        layers.append(ct_layer)
    if cfg.get("enable_dvd") and not is_img:
        dx = _bounce(cfg["dvd_speed_x"], cfg["dvd_margin"], "w", "tw")
        dy = _bounce(cfg["dvd_speed_y"], cfg["dvd_margin"], "h", "th")
        layers.append(_drawtext(font, cfg["dvd_text"],
                                f"white@{cfg['dvd_opacity']}", cfg["dvd_size"], dx, dy, ec_text))

    layers.append(_drawtext(font, cfg.get("text_bottom_left",""),
                            cfg.get("bottom_left_color","white"), cfg.get("bottom_left_size",22), "10", "h-th-10", ec_text))
    layers.append(_drawtext(font, cfg.get("text_top_right",""),
                            cfg.get("top_right_color","white"), cfg.get("top_right_size",22), "w-tw-10", "10", ec_text))

    if cfg.get("enable_moving") and not is_img:
        mx = r"mod(t*40\,w+tw)-tw"
        layers.append(_drawtext(font, cfg["moving_text"],
                                f"white@{cfg['moving_opacity']}", cfg["moving_size"], mx, "h-th-50", ec_text))

    if cfg.get("enable_bouncing") and not is_img:
        layers.append(_drawtext(font, cfg["bouncing_text"],
                                f"white@{cfg['bouncing_opacity']}", cfg["bouncing_size"],
                                "30", r"80+sin(t*2)*15", ec_text))

    dt   = ",".join(layers)
    logo = f"[1:v]scale={cfg['logo_scale_w']}:-1,colorchannelmixer=aa={cfg['logo_opacity']}[logo]"
    ov   = f"[0:v][logo]overlay=x={lx}:y={ly}{ec_logo}"
    # format=yuv420p o CUOI: ep ve 8-bit -> video 10-bit/HDR khong lam h264_nvenc
    # bao loi -22 (Invalid argument / Conversion failed).
    return f"{logo};{ov},{dt},format=yuv420p"

def _has_dynamic_wm(cfg):
    """Co bat hieu ung dong khong? Neu co -> buoc dung drawtext CPU (build_video_cmd),
    khong dung duoc fast mode (PNG tinh). Cac hieu ung dong:
      - center fade di chuyen/nhap nhay
      - DVD bounce, moving text, bouncing text
    """
    return bool(cfg.get("enable_center_fade") or cfg.get("enable_dvd")
                or cfg.get("enable_moving") or cfg.get("enable_bouncing"))

def _audio_args(norm_audio):
    """Chuan hoa audio sang AAC 48k stereo khi:
      - enable_outro=True  (de ghep concat khop codec)
      - trim > 0           (copy audio sau input-seek hay bi glitch/lech dau doan)
      - dur_limit != None  (chia part — tuong tu trim)
    Cac truong hop tren duoc xu ly boi encode_segment (dat norm_a=True).
    Khi khong co dieu kien tren: copy nguyen goc (nhanh, khong mat chat).

    NOTE: ham nay phai duoc dat NGAY SAU -map audio trong command list,
    TRUOC cac -c:v va -t args, de ffmpeg gan codec audio dung stream.
    """
    if norm_audio:
        return ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k"]
    return ["-c:a", "copy"]

def build_video_cmd(cfg, inp, outp, fc, trim=None, dur_limit=None, norm_audio=False):
    ss     = ["-ss", str(trim)] if trim and trim > 0 else []
    t_args = ["-t", str(dur_limit)] if dur_limit is not None else []
    # Decode tren GPU de giai phong CPU cho cac job song song.
    # Khong dung hwaccel_output_format=cuda vi drawtext la filter CPU,
    # can frame o system memory; chi decode bang GPU roi tu dong tai ve.
    return [
        cfg["ffmpeg_path"], "-hide_banner",
        "-hwaccel", "cuda",
        *ss, "-i", inp, "-i", cfg["logo_image"],
        "-filter_complex", fc,
        "-filter_threads", str(int(cfg.get("filter_threads", 8))),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "h264_nvenc", "-preset", cfg["nvenc_preset"],
        "-rc", "vbr", "-cq", str(cfg["nvenc_cq"]),
        "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M",
        "-gpu", "0",
        *t_args,
        *_audio_args(norm_audio), "-movflags", "+faststart", "-y", outp,
    ]

# ==============================================================================
#  TURBO RENDER — center/corner text PNG + NVDEC decode + overlay_cuda
#  Pipeline: NVDEC -> crop_cuda -> overlay_cuda -> NVENC (~40-55x)
#  Fix ảnh preset: chu giua + chu 4 goc = PNG overlay, khong drawtext CPU.
# ==============================================================================

def _display_wh(w, h, rotation):
    rot = int(rotation or 0)
    if rot in (-90, 90, 270, -270):
        return h, w
    return w, h

def _parse_font_color(color, default_alpha=255):
    s = (color or "white").strip()
    alpha = default_alpha
    if "@" in s:
        s, a = s.rsplit("@", 1)
        try:
            alpha = int(float(a) * 255)
        except Exception:
            pass
    s = s.lower()
    named = {"white": (255, 255, 255), "black": (0, 0, 0),
             "red": (255, 0, 0), "yellow": (255, 255, 0)}
    if s in named:
        r, g, b = named[s]
    elif s.startswith("#"):
        r, g, b = _hex_to_rgb(s)
    else:
        r, g, b = 255, 255, 255
    return (r, g, b, alpha)

def render_text_png(text, size, color, font_path, out_path, pad=4):
    if not PIL_OK:
        return False, "Can Pillow"
    text = (text or "").strip()
    if not text:
        return False, "rong"
    size = int(size or 22)
    fill = _parse_font_color(color)
    key  = hashlib.md5(
        f"{text}|{size}|{color}|{font_path}|{pad}".encode("utf-8")
    ).hexdigest()
    cache_dir = os.path.join(tempfile.gettempdir(), "gpu_wm_text")
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, f"tx_{key}.png")
    if key in _text_png_cache and os.path.isfile(_text_png_cache[key]):
        cached = _text_png_cache[key]
    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        if cached != out_path:
            shutil.copy2(cached, out_path)
        return True, ""
    try:
        font = ImageFont.truetype(font_path, size)
    except Exception:
        font = _pil_font(size)
        if font is None:
            return False, "font loi"
    tmp  = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tmp)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw   = bbox[2] - bbox[0] + pad * 2
    th   = bbox[3] - bbox[1] + pad * 2
    img  = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=fill)
    img.save(cached, "PNG")
    _text_png_cache[key] = cached
    if cached != out_path:
        shutil.copy2(cached, out_path)
    return True, ""

def _corner_text_overlays(cfg):
    font_p = cfg.get("font_file", _default_font())
    cache_dir = os.path.join(tempfile.gettempdir(), "gpu_wm_text")
    os.makedirs(cache_dir, exist_ok=True)
    defs = [
        ("tl", "text_top_left",     "top_left_size",     "top_left_color",
         "10", "10"),
        ("tr", "text_top_right",    "top_right_size",    "top_right_color",
         "main_w-overlay_w-10", "10"),
        ("bl", "text_bottom_left",  "bottom_left_size",  "bottom_left_color",
         "10", "main_h-overlay_h-10"),
        ("br", "text_bottom_right", "bottom_right_size", "bottom_right_color",
         "main_w-overlay_w-10", "main_h-overlay_h-10"),
    ]
    out = []
    for corner, text_k, size_k, color_k, ox, oy in defs:
        txt = (cfg.get(text_k) or "").strip()
        if not txt:
            continue
        if cfg.get(f"enable_{corner}") and os.path.isfile(cfg.get(f"logo_{corner}", "")):
            continue
        png = os.path.join(cache_dir,
                           f"corner_{corner}_{hashlib.md5(txt.encode()).hexdigest()[:8]}.png")
        ok, _ = render_text_png(txt, cfg.get(size_k, 22), cfg.get(color_k, "white"),
                                font_p, png)
        if ok:
            out.append((png, ox, oy))
    return out

def render_center_text_png(cfg, width, height, out_path):
    if not PIL_OK:
        return False, "Can Pillow"
    text = (cfg.get("center_text") or "").strip()
    if not text:
        return False, "rong"
    width  = int(width)  - (int(width)  % 2)
    height = int(height) - (int(height) % 2)
    size   = int(cfg.get("center_size", 25))
    op     = float(cfg.get("center_opacity", 0.15))
    font_p = cfg.get("font_file", _default_font())
    key    = hashlib.md5(
        f"{text}|{size}|{op}|{width}|{height}|{font_p}".encode("utf-8")
    ).hexdigest()
    cache_dir = os.path.join(tempfile.gettempdir(), "gpu_wm_text")
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, f"ct_{key}.png")
    if key in _center_png_cache and os.path.isfile(_center_png_cache[key]):
        cached = _center_png_cache[key]
    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        if cached != out_path:
            shutil.copy2(cached, out_path)
        return True, ""
    try:
        font = ImageFont.truetype(font_p, size)
    except Exception:
        font = _pil_font(size)
        if font is None:
            return False, "font loi"
    img  = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw   = bbox[2] - bbox[0]
    th   = bbox[3] - bbox[1]
    x    = int((width - tw) / 4)
    y    = int((height - th) / 2)
    draw.text((x, y), text, font=font, fill=(255, 255, 255, int(op * 255)))
    img.save(cached, "PNG")
    _center_png_cache[key] = cached
    if cached != out_path:
        shutil.copy2(cached, out_path)
    return True, ""

def build_fast_cmd(cfg, inp, outp, wm_png=None, trim=None, dur_limit=None,
                   norm_audio=False, mode=None, rotation=0, video_w=None, video_h=None):
    """Turbo: NVDEC + overlay_cuda (logo + chu PNG) -> NVENC.
    Tra ve None neu can drawtext dong (center fade / HS preset).
    """
    has_center = cfg.get("enable_center") and (cfg.get("center_text") or "").strip()
    has_static_center = has_center and not cfg.get("enable_center_fade")
    if has_center and not has_static_center:
        return None

    ss     = ["-ss", str(trim)] if trim and trim > 0 else []
    t_args = ["-t", str(dur_limit)] if dur_limit is not None else []
    gpu_w    = max(1, int(cfg.get("gpu_workers", cfg.get("max_workers", 2)) or 2))
    surfaces = max(4, 32 // gpu_w)
    mx  = str(cfg.get("logo_margin_x", 15))
    my  = str(cfg.get("logo_margin_y", 15))
    et  = cfg.get("enable_time", 0)
    et_pad = f",tpad=start_duration={et}:start_mode=add:color=black@0" if et and et > 0 else ""

    logo_list = []
    if os.path.isfile(cfg.get("logo_image", "")):
        logo_list.append((
            cfg["logo_image"],
            cfg.get("logo_scale_w", 160),
            cfg.get("logo_opacity", 0.7),
            mx, my
        ))
    def _cx(corner): return str(cfg.get(f"logo_{corner}_x", int(mx)))
    def _cy(corner): return str(cfg.get(f"logo_{corner}_y", int(my)))
    corner_defs = [
        ("enable_tl","logo_tl","logo_tl_w","logo_tl_op", _cx("tl"),                              _cy("tl")),
        ("enable_tr","logo_tr","logo_tr_w","logo_tr_op", f"main_w-overlay_w-{_cx('tr')}",        _cy("tr")),
        ("enable_bl","logo_bl","logo_bl_w","logo_bl_op", _cx("bl"),                              f"main_h-overlay_h-{_cy('bl')}"),
        ("enable_br","logo_br","logo_br_w","logo_br_op", f"main_w-overlay_w-{_cx('br')}",        f"main_h-overlay_h-{_cy('br')}"),
    ]
    for en_k, p_k, w_k, op_k, ox, oy in corner_defs:
        if cfg.get(en_k) and os.path.isfile(cfg.get(p_k, "")):
            logo_list.append((cfg[p_k], cfg.get(w_k, 120), cfg.get(op_k, 0.7), ox, oy))

    corner_texts = _corner_text_overlays(cfg)
    extra_inputs = []
    fc_parts     = []
    stream_in    = "[0:v]"

    rot = int(rotation or 0)
    if rot in (-90, 270):
        xpose = "transpose=1,"
    elif rot in (90, -270):
        xpose = "transpose=2,"
    elif rot in (180, -180):
        xpose = "transpose=1,transpose=1,"
    else:
        xpose = ""

    disp_w, disp_h = _display_wh(video_w, video_h, rot) if video_w and video_h else (None, None)
    use_turbo = (cfg.get("turbo_nvdec", True) and rot == 0
                 and disp_w and disp_h)

    if use_turbo:
        fc_parts.append(f"[0:v]crop_cuda={disp_w}:{disp_h}[base]")
        stream_in = "[base]"
        hw_in = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    else:
        fc_parts.append(f"[0:v]{xpose}format=yuv420p,hwupload[base]")
        stream_in = "[base]"
        hw_in = []

    for i, (lpath, lw, lop, ox, oy) in enumerate(logo_list):
        idx = i + 1
        lbl = f"lg{i}"
        out = f"v{i}"
        extra_inputs += ["-i", lpath]
        fc_parts.append(
            f"[{idx}:v]scale={lw}:-1,format=rgba,"
            f"colorchannelmixer=aa={lop}"
            f"{et_pad},"
            f"format=yuva420p,hwupload[{lbl}]"
        )
        fc_parts.append(
            f"{stream_in}[{lbl}]overlay_cuda=x={ox}:y={oy}[{out}]"
        )
        stream_in = f"[{out}]"

    next_idx = len(logo_list) + 1
    for j, (tpath, tox, toy) in enumerate(corner_texts):
        idx = next_idx + j
        lbl = f"tx{j}"
        out = f"vt{j}"
        extra_inputs += ["-i", tpath]
        fc_parts.append(
            f"[{idx}:v]format=rgba"
            f"{et_pad},"
            f"format=yuva420p,hwupload[{lbl}]"
        )
        fc_parts.append(
            f"{stream_in}[{lbl}]overlay_cuda=x={tox}:y={toy}[{out}]"
        )
        stream_in = f"[{out}]"

    if has_static_center and disp_w and disp_h:
        center_png = os.path.join(tempfile.gettempdir(), "gpu_wm_text", "_center_live.png")
        ok_ct, _ = render_center_text_png(cfg, disp_w, disp_h, center_png)
        if not ok_ct:
            return None
        cidx = len(logo_list) + len(corner_texts) + 1
        extra_inputs += ["-i", center_png]
        fc_parts.append(f"[{cidx}:v]format=yuva420p,hwupload[ct]")
        out_ct = "vct"
        fc_parts.append(f"{stream_in}[ct]overlay_cuda=x=0:y=0[{out_ct}]")
        stream_in = f"[{out_ct}]"

    if not logo_list and not has_static_center and not corner_texts:
        if use_turbo:
            fc_parts = [f"[0:v]crop_cuda={disp_w}:{disp_h}[base]"]
        else:
            fc_parts = [f"[0:v]{xpose}format=yuv420p,hwupload[base]"]
        stream_in = "[base]"

    return [
        cfg["ffmpeg_path"], "-hide_banner",
        "-init_hw_device", "cuda=gpu:0",
        "-filter_hw_device", "gpu",
        *ss,
        *hw_in,
        "-i", inp,
        *extra_inputs,
        "-filter_complex", ";".join(fc_parts),
        "-map", stream_in,
        "-map", "0:a:0?",
        *_audio_args(norm_audio),
        "-c:v", "h264_nvenc", "-preset", cfg["nvenc_preset"],
        "-rc", "constqp", "-qp", str(cfg["nvenc_cq"]),
        "-surfaces", str(surfaces),
        "-gpu", "0",
        *t_args,
        "-movflags", "+faststart", "-y", outp,
    ]
# Cache ket qua probe overlay_cuda — chi test 1 lan / lan chay.
# _FAST_CUDA_MODE: None=chua probe, 0=khong cong thuc nao chay, 1/2=cong thuc dung duoc.
_FAST_CUDA_MODE = None
_FAST_CUDA_OK   = None

def fast_cuda_supported(cfg):
    """v7: Luon dung pipeline NVDEC+overlay CPU+NVENC.
    Khong can probe overlay_cuda nua vi khong dung scale_npp.
    """
    global _FAST_CUDA_OK, _FAST_CUDA_MODE
    if _FAST_CUDA_OK is not None:
        return _FAST_CUDA_OK
    # Test nhanh: NVENC co chay khong
    ff = cfg.get("ffmpeg_path", "")
    if not ff or not os.path.isfile(ff):
        _FAST_CUDA_OK = False
        return False
    try:
        cmd = [ff, "-hide_banner", "-y",
               "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.5",
               "-c:v", "h264_nvenc", "-preset", "p1",
               "-f", "null", "-"]
        rc, _ = _run_ffmpeg(cmd, timeout=15)
        _FAST_CUDA_OK = (rc == 0)
        _FAST_CUDA_MODE = 1 if _FAST_CUDA_OK else 0
    except Exception:
        _FAST_CUDA_OK = False
        _FAST_CUDA_MODE = 0
    return _FAST_CUDA_OK

def build_cpu_cmd(cfg, inp, outp, fc, trim=None, dur_limit=None, norm_audio=False):
    """Encode bang libx264 tren CPU. Dung cho CPU worker (hybrid pool).
    Decode + filter + encode deu tren CPU — khong dung NVENC/NVDEC,
    nen chay song song duoc voi cac job GPU ma khong tranh chip encode.
    """
    ss     = ["-ss", str(trim)] if trim and trim > 0 else []
    t_args = ["-t", str(dur_limit)] if dur_limit is not None else []
    return [
        cfg["ffmpeg_path"], "-hide_banner",
        *ss, "-i", inp, "-i", cfg["logo_image"],
        "-filter_complex", fc,
        "-filter_threads", str(int(cfg.get("filter_threads", 4))),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", cfg.get("x264_preset", "medium"),
        "-crf", str(cfg["nvenc_cq"]),
        "-maxrate", "20M", "-bufsize", "40M",
        *t_args,
        *_audio_args(norm_audio), "-movflags", "+faststart", "-y", outp,
    ]

def encode_segment(cfg, ffprobe_p, inp, outp, fc, trim=None, dur_limit=None, use_gpu=True):
    """Encode 1 segment.
      - use_gpu=False -> libx264 CPU (cho CPU worker), bo qua fast mode.
      - use_gpu=True  -> tu dong chon:
          khong hieu ung dong -> fast mode (PNG tinh pre-render + overlay CPU + NVENC)
          co hieu ung dong     -> drawtext CPU + logo overlay + NVENC encode
    Tra ve (success, used_fast, reason). reason = "" neu thanh cong.
    """
    min_kb = cfg["min_output_kb"]
    # Re-encode audio AAC 48k khi: bat outro (de ghep copy khop), HOAC co tua/cat
    # (-ss trim > 0) HOAC chia part (dur_limit) — vi copy audio luc seek/cat hay
    # bi lech/giat tieng ("chet chet") o dau doan. Video giu nguyen -> khong sinh loi.
    norm_a = bool(cfg.get("enable_outro")) or bool(trim and trim > 0) or (dur_limit is not None)

    # Timeout co gian theo do dai segment -> video DAI khong bi kill giua chung
    # (truoc day mac dinh 600s -> video >10 phut encode bi cat con tieng).
    _seg = dur_limit
    if _seg is None:
        _d = _get_duration(ffprobe_p, inp)
        _seg = (_d - (trim or 0)) if _d else None
    enc_to = int(max(600, (_seg or 0) * 6 + 180))   # tran thoi gian, chi kill khi treo that

    def _corrupt(txt):
        t = (txt or "").lower()
        return ("invalid data found" in t or "error opening input" in t
                or "moov atom not found" in t or "could not find codec" in t)

    # --- CPU worker: libx264 ---
    if not use_gpu:
        cmd = build_cpu_cmd(cfg, inp, outp, fc, trim, dur_limit, norm_audio=norm_a)
        rc, stderr_txt = _run_ffmpeg(cmd, timeout=enc_to)
        if rc == 0 and _is_valid(outp, min_kb):
            return True, False, ""
        reason = _encode_fail_reason(rc, stderr_txt, outp, min_kb, "")
        if os.path.exists(outp):
            try: os.remove(outp)
            except: pass
        return False, False, reason

    # --- GPU worker: fast mode (overlay logo PNG goc truc tiep + NVENC) ---
    # v7.3: bo render_wm_template, build_fast_cmd tu lay logo PNG tu cfg.
    # HS preset co dynamic WM (DVD/bounce) -> fallback drawtext CPU tu dong.
    want_fast = (not _has_dynamic_wm(cfg) and cfg.get("_fast_ok", True))
    last_err = ""
    if want_fast:
        w, h, pix = _probe_wh(ffprobe_p, inp)
        if w and h and pix not in TEN_BIT:
            rot = _get_rotation(ffprobe_p, inp)
            cmd = build_fast_cmd(cfg, inp, outp, None, trim, dur_limit,
                                 norm_audio=norm_a, rotation=rot,
                                 video_w=w, video_h=h)
            if cmd is not None:
                rc, stderr_txt = _run_nvenc(cmd, timeout=enc_to)
                low = stderr_txt.lower()
                bad = ("failed to configure","unsupported","can't overlay",
                       "error reinitializing","not implemented",
                       "error while filtering","no such filter",
                       "crop_cuda","hwaccel")
                if rc == 0 and _is_valid(outp, min_kb) and not any(b in low for b in bad):
                    return True, True, ""
                if cfg.get("turbo_nvdec", True) and rot == 0 and any(
                        b in low for b in ("crop_cuda", "hwaccel")):
                    cfg_fb = dict(cfg)
                    cfg_fb["turbo_nvdec"] = False
                    cmd = build_fast_cmd(cfg_fb, inp, outp, None, trim, dur_limit,
                                         norm_audio=norm_a, rotation=rot,
                                         video_w=w, video_h=h)
                    if cmd is not None:
                        rc, stderr_txt = _run_nvenc(cmd, timeout=enc_to)
                        low = stderr_txt.lower()
                        if rc == 0 and _is_valid(outp, min_kb) and not any(
                                b in low for b in bad if b not in ("crop_cuda", "hwaccel")):
                            return True, True, ""
                if _corrupt(stderr_txt):
                    return False, False, _encode_fail_reason(rc, stderr_txt, outp, min_kb, "file loi/hong")
                last_err = f"fast loi (rc={rc})"
                if os.path.exists(outp):
                    try: os.remove(outp)
                    except: pass
            else:
                last_err = "can drawtext dong"

    # Lop 2: drawtext + NVENC
    cmd = build_video_cmd(cfg, inp, outp, fc, trim, dur_limit, norm_audio=norm_a)
    rc, stderr_txt = _run_nvenc(cmd, timeout=enc_to)
    if rc == 0 and _is_valid(outp, min_kb):
        return True, False, ""
    if _corrupt(stderr_txt):
        return False, False, _encode_fail_reason(rc, stderr_txt, outp, min_kb, "file loi/hong")
    if os.path.exists(outp):
        try: os.remove(outp)
        except: pass

    # Lop 3 (FALLBACK CUOI): NVENC loi (vd het phien encode) -> encode CPU libx264.
    # Dam bao van ra file thay vi bao loi.
    nv_err = last_err or f"nvenc loi (rc={rc})"
    cmd = build_cpu_cmd(cfg, inp, outp, fc, trim, dur_limit, norm_audio=norm_a)
    rc, stderr_txt = _run_ffmpeg(cmd, timeout=enc_to)
    if rc == 0 and _is_valid(outp, min_kb):
        return True, False, ""
    reason = _encode_fail_reason(rc, stderr_txt, outp, min_kb, nv_err)
    if os.path.exists(outp):
        try: os.remove(outp)
        except: pass
    return False, False, reason

def _encode_fail_reason(rc, stderr_txt, outp, min_kb, last_err):
    """Trich ly do fail tu rc + stderr de bao cao len UI."""
    tail = ""
    if stderr_txt:
        lines = [l for l in stderr_txt.strip().splitlines() if l.strip()]
        tail = lines[-1] if lines else ""
    if rc != 0:
        return f"encode rc={rc}" + (f": {tail[:80]}" if tail else "")
    if not _is_valid(outp, min_kb):
        return f"output < {min_kb}KB (file qua nho/rong)"
    return last_err or "khong ro"

# ==============================================================================
#  OUTRO
# ==============================================================================

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _pil_font(size, bold=False):
    if not PIL_OK:
        return None
    candidates = (
        [r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
         r"C:\Windows\Fonts\segoeui.ttf"]
        if sys.platform == "win32" else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
         else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None

def create_outro_video(cfg, width, height, fps, out_path):
    if not PIL_OK:
        return False, "Can cai Pillow: pip install Pillow"
    ff       = cfg["ffmpeg_path"]
    text     = cfg.get("outro_text", "Cam on da xem!")
    duration = float(cfg.get("outro_duration", 5.0))
    bg_rgb   = _hex_to_rgb(cfg.get("outro_bg_color", "#d2dcff"))
    qr_path  = cfg.get("qr_image", "QR.png")
    font_sz  = int(cfg.get("outro_font_size", 36))
    width    = width  - (width  % 2)
    height   = height - (height % 2)

    base_img    = Image.new("RGBA", (width, height), bg_rgb + (255,))
    qr_bottom_y = int(height * 0.55)
    if qr_path and os.path.exists(qr_path):
        try:
            qr      = Image.open(qr_path).convert("RGBA")
            max_dim = int(min(width, height) * 0.38)
            qr_w    = min(max_dim, qr.width)
            qr_h    = int(qr.height * qr_w / qr.width)
            qr      = qr.resize((qr_w, qr_h), Image.LANCZOS)
            qr_x    = (width - qr_w) // 2
            qr_y    = int(height * 0.08)
            base_img.paste(qr, (qr_x, qr_y), qr)
            qr_bottom_y = qr_y + qr_h + 20
        except Exception:
            pass

    font         = _pil_font(font_sz, bold=True)
    total_frames = max(1, int(duration * fps))
    lines        = text.split("\n")
    total_chars  = sum(len(ln) + 1 for ln in lines)
    type_frames  = max(1, int(total_frames * 0.65))

    def draw_frame(chars_visible):
        frame = base_img.copy()
        draw  = ImageDraw.Draw(frame)
        try:
            lh = draw.textbbox((0, 0), "Ag", font=font)[3] + int(font_sz * 0.4)
        except Exception:
            lh = font_sz + 14
        count, y = 0, qr_bottom_y
        for ln in lines:
            visible = ""
            for ch in ln:
                if count >= chars_visible: break
                visible += ch; count += 1
            count += 1
            if visible:
                try:
                    tw = draw.textbbox((0, 0), visible, font=font)[2]
                except Exception:
                    tw = len(visible) * font_sz // 2
                x = (width - tw) // 2
                draw.text((x+2, y+2), visible, font=font, fill=(0,0,0,140))
                draw.text((x,   y),   visible, font=font, fill=(30,30,70,255))
            y += lh
        return frame.convert("RGB").tobytes()

    tmp_dir   = tempfile.mkdtemp(prefix="outro_")
    video_tmp = os.path.join(tmp_dir, "v.mp4")
    try:
        # FIX v7.3: Render outro bang NVENC (khong phai libx264) de concat step khong
        # phai re-encode format lai. Outro yuv420p h264_nvenc khop voi watermarked video.
        # NVENC nhan rawvideo qua pipe stdin - mot so driver cu co the khong ho tro,
        # nen co fallback ve libx264 neu NVENC loi.
        cmd_v = [
            ff, "-hide_banner", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{width}x{height}", "-pix_fmt", "rgb24", "-r", str(fps),
            "-i", "pipe:0", "-an",
            "-c:v", "h264_nvenc", "-preset", "p1", "-rc", "constqp", "-qp", "20",
            "-pix_fmt", "yuv420p", video_tmp
        ]
        cmd_v_fallback = [
            ff, "-hide_banner", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{width}x{height}", "-pix_fmt", "rgb24", "-r", str(fps),
            "-i", "pipe:0", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", video_tmp
        ]
        def _pipe_frames(cmd):
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 creationflags=CREATE_NO_WINDOW)
            try:
                for fi in range(total_frames):
                    cv = int(total_chars * min(fi / type_frames, 1.0))
                    p.stdin.write(draw_frame(cv))
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                p.wait(timeout=300)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
                return -1, b"timeout"
            return p.returncode, p.stderr.read()

        rc_v, stderr_v = _pipe_frames(cmd_v)
        if rc_v != 0:
            # NVENC pipe that bai (driver cu hoac loi khac) -> fallback libx264
            if os.path.exists(video_tmp):
                try: os.remove(video_tmp)
                except: pass
            rc_v, stderr_v = _pipe_frames(cmd_v_fallback)
        if rc_v != 0:
            return False, f"Render outro loi (code={rc_v})"
        # Outro phai KHOP main de ghep COPY: video h264 yuv420p (da co o tren),
        # audio AAC 48000 stereo. Neu main khong co audio -> outro cung khong audio.
        with_audio = cfg.get("_with_audio", True)
        if with_audio:
            cmd_mix = [
                ff, "-hide_banner", "-y", "-i", video_tmp,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k",
                "-t", str(duration), "-shortest", "-movflags", "+faststart", out_path
            ]
        else:
            cmd_mix = [
                ff, "-hide_banner", "-y", "-i", video_tmp,
                "-c:v", "copy", "-an",
                "-t", str(duration), "-movflags", "+faststart", out_path
            ]
        rc, _ = _run_ffmpeg(cmd_mix, timeout=60)
        if rc != 0:
            return False, f"Mix audio loi (code={rc})"
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def concat_outro(ff, ffprobe_p, watermarked, outp, cfg, tmp_dir):
    """Noi outro vao cuoi video bang RE-ENCODE NVENC.

    FIX v7.1 — Sua loi chong tieng:
    - Cu: concat=n=2:v=1:a=0 (chi concat video), audio lay tu main qua apad/aresample
      -> apad keo dai audio main de "lap" qua phan outro -> tieng bi chong/kep.
    - Moi: concat=n=2:v=1:a=1 (concat ca video lan audio tu CA HAI segment).
      Outro da co silent audio (anullsrc) tu create_outro_video -> ghep sach.
    - Bo -shortest: tranh cat video truoc khi audio concat xong het.
    - Khi main khong co audio: van concat video-only (a=0), khong sinh loi.
    """
    w, h, _ = _probe_wh(ffprobe_p, watermarked)
    fps     = _get_fps(ffprobe_p, watermarked)
    if not w or not h:
        return False, "Khong probe duoc resolution"

    # Video da watermark co audio that su khong?
    a_check   = _run_probe([ffprobe_p, "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=index", "-of", "csv=p=0", watermarked])
    has_audio = bool(a_check.strip())

    # Outro cache theo (w, h, co audio). Re-encode nen codec outro khong can khop.
    key = (w, h, has_audio)
    if key in _outro_cache and os.path.isfile(_outro_cache[key]):
        outro = _outro_cache[key]
    else:
        outro_dir = os.path.join(tempfile.gettempdir(), "gpu_wm_outro")
        os.makedirs(outro_dir, exist_ok=True)
        cfg_o = dict(cfg)
        cfg_o["_with_audio"] = has_audio
        outro = os.path.join(outro_dir, f"outro_{w}x{h}_{'a' if has_audio else 'na'}.mp4")
        ok, err = create_outro_video(cfg_o, w, h, fps, outro)
        if not ok:
            return False, f"Tao outro that bai: {err}"
        _outro_cache[key] = outro

    # ---- Re-encode concat (NVENC). Timeout co gian theo do dai video. ----
    dur = _get_duration(ffprobe_p, watermarked) or 600.0
    to  = int(max(180, dur * 2.0 + 120))

    # FIX v7.3: Dung hwaccel cuda + hwaccel_output_format=cuda de NVDEC decode ca 2 input.
    # Concat filter nhan cuda frame tu NVDEC:
    #   - Video watermarked: da la h264 yuv420p -> NVDEC decode sang nv12 cuda.
    #   - Video outro:       da la h264 yuv420p (libx264) -> NVDEC decode sang nv12 cuda.
    # Bo filter fps/format/setsar thừa (ca 2 video da cung fps/format/SAR tu buoc truoc).
    # Chu y: concat filter tren cuda frame (nv12) -> NVENC nhan truc tiep, bo pix_fmt arg.
    gpu_w    = max(1, int(cfg.get("gpu_workers", cfg.get("max_workers", 2)) or 2))
    surfaces = max(4, 32 // gpu_w)

    if has_audio:
        # concat=n=2:v=1:a=1 — ghep DONG THOI video va audio ca 2 segment.
        # [0:a] = audio main (da AAC 48k stereo tu encode_segment norm_a=True)
        # [1:a] = audio outro (anullsrc AAC 48k stereo tu create_outro_video)
        fc = (f"[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];"
              f"[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
              f"[0:v][a0][1:v][a1]concat=n=2:v=1:a=1[vout][aout]")
        maps = ["-map", "[vout]", "-map", "[aout]"]
    else:
        # Khong co audio: chi concat video
        fc = "[0:v][1:v]concat=n=2:v=1:a=0[vout]"
        maps = ["-map", "[vout]"]

    cmd = [ff, "-hide_banner",
           "-init_hw_device", "cuda=gpu:0",
           "-filter_hw_device", "gpu",
           "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
           "-fflags", "+genpts",
           "-i", watermarked,
           "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
           "-i", outro,
           "-filter_complex", fc, *maps,
           "-c:v", "h264_nvenc", "-preset", cfg["nvenc_preset"],
           "-rc", "vbr", "-cq", str(cfg["nvenc_cq"]),
           "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M",
           "-surfaces", str(surfaces), "-gpu", "0"]
    if has_audio:
        cmd += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", "-y", outp]
    rc, stderr_concat = _run_nvenc(cmd, timeout=to)
    if rc != 0:
        # Fallback: CPU concat (an toan hon, chau frame không khop hwaccel context)
        if has_audio:
            fc_cpu = (f"[0:v]fps={fps:.3f},format=yuv420p,setsar=1[v0];"
                      f"[1:v]fps={fps:.3f},format=yuv420p,setsar=1[v1];"
                      f"[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];"
                      f"[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
                      f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]")
            maps_cpu = ["-map", "[vout]", "-map", "[aout]"]
        else:
            fc_cpu = (f"[0:v]fps={fps:.3f},format=yuv420p,setsar=1[v0];"
                      f"[1:v]fps={fps:.3f},format=yuv420p,setsar=1[v1];"
                      f"[v0][v1]concat=n=2:v=1:a=0[vout]")
            maps_cpu = ["-map", "[vout]"]
        cmd_cpu = [ff, "-hide_banner", "-fflags", "+genpts",
                   "-i", watermarked, "-i", outro,
                   "-filter_complex", fc_cpu, *maps_cpu,
                   "-c:v", "h264_nvenc", "-preset", cfg["nvenc_preset"],
                   "-rc", "vbr", "-cq", str(cfg["nvenc_cq"]),
                   "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M",
                   "-pix_fmt", "yuv420p", "-surfaces", str(surfaces), "-gpu", "0"]
        if has_audio:
            cmd_cpu += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k"]
        cmd_cpu += ["-movflags", "+faststart", "-y", outp]
        rc, _ = _run_nvenc(cmd_cpu, timeout=to)
    if rc == 0 and _is_valid(outp, cfg["min_output_kb"]):
        return True, ""
    return False, f"Concat outro loi (code={rc})"

# ==============================================================================
#  TELEGRAM UPLOAD
# ==============================================================================

def make_thumbnail(ff, ffprobe_p, video, out_jpg, at_sec=1.0):
    """Trich 1 frame lam thumbnail JPEG. Mac dinh lay o giay thu 1 (qua doan
    fade-in den dau video). Scale 320 giu ti le. Tra ve path hoac None.
    Cach nay giong tool da chay dep (thumbnail khong bi den)."""
    cmd = [ff, "-hide_banner", "-loglevel", "error",
           "-ss", str(at_sec), "-i", video,
           "-vframes", "1",
           "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
           "-q:v", "5", "-y", out_jpg]
    try:
        rc, _ = _run_ffmpeg(cmd, timeout=30)
        if rc == 0 and os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 0:
            return out_jpg
    except Exception:
        pass
    # Fallback: neu giay 1 fail (video ngan hon 1s) -> lay frame dau
    if at_sec > 0:
        return make_thumbnail(ff, ffprobe_p, video, out_jpg, at_sec=0)
    return None


class TelegramUploader:
    """Quan ly 1 phien Pyrogram userbot tren event loop rieng (1 thread).

    Login OTP 2 buoc:
      send_code(api_id, api_hash, phone) -> luu phone_code_hash
      sign_in(code[, password])          -> tao session, luu lai
    Sau khi login: upload_video(path, ...) up tuan tu, tu retry FloodWait.
    """
    def __init__(self):
        self.loop    = None
        self.thread  = None
        self.client  = None
        self._phone_code_hash = None
        self._phone  = None
        self._started = False   # client da start() day du chua (khong chi connect)
        self._resolved = set()  # cac chat da nap peer cache (tranh resolve lai)
        self._lock   = threading.Lock()
        self._ready  = threading.Event()

    # --- Event loop chay tren thread rieng ---
    def _ensure_loop(self):
        if self.loop and self.thread and self.thread.is_alive():
            return
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._loop_runner, daemon=True)
        self.thread.start()
        self._ready.wait(timeout=5)

    def _loop_runner(self):
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def _run_coro(self, coro, timeout=300):
        """Chay 1 coroutine tren event loop rieng, cho ket qua (blocking)."""
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    async def _ensure_started(self):
        """Dam bao client da start() day du (co thong tin me)."""
        if self._started:
            return
        if self.client.is_connected:
            try: await self.client.disconnect()
            except Exception: pass
        await self.client.start()
        self._started = True

    # --- Login ---
    def send_code(self, api_id, api_hash, phone):
        os.makedirs(SESSION_DIR, exist_ok=True)
        self._phone = phone
        async def _do():
            name = "wm_" + "".join(c for c in phone if c.isdigit())
            self.client = Client(name, api_id=int(api_id), api_hash=api_hash,
                                 workdir=SESSION_DIR)
            await self.client.connect()
            # Neu da co session hop le -> khoi nhap OTP
            try:
                me = await self.client.get_me()
                if me:
                    return {"ok": True, "already": True}
            except Exception:
                pass
            sent = await self.client.send_code(phone)
            self._phone_code_hash = sent.phone_code_hash
            return {"ok": True, "already": False}
        try:
            return self._run_coro(_do(), timeout=60)
        except Exception as e:
            return {"ok": False, "msg": f"Gui ma loi: {e}"}

    def sign_in(self, code, password=""):
        async def _do():
            try:
                await self.client.sign_in(self._phone, self._phone_code_hash, code)
            except SessionPasswordNeeded:
                if not password:
                    return {"ok": False, "need_password": True}
                await self.client.check_password(password)
            # Da authorize xong. Client dang o trang thai connect() (login thu cong).
            # Disconnect roi start() lai de khoi tao DAY DU (peer cache, me) —
            # tranh loi 'NoneType has no is_premium' khi send_video.
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
                await self.client.start()
                self._started = True
            except Exception:
                pass
            return {"ok": True}
        try:
            return self._run_coro(_do(), timeout=60)
        except (PhoneCodeInvalid, PhoneCodeExpired):
            return {"ok": False, "msg": "Ma OTP sai hoac het han"}
        except Exception as e:
            return {"ok": False, "msg": f"Dang nhap loi: {e}"}

    def connect_existing(self, api_id, api_hash, phone):
        """Ket noi bang session da luu (khong can OTP) qua start() de khoi tao
        day du (peer cache + thong tin me). connect() khong du -> me=None."""
        os.makedirs(SESSION_DIR, exist_ok=True)
        async def _do():
            name = "wm_" + "".join(c for c in phone if c.isdigit())
            if self.client is None:
                self.client = Client(name, api_id=int(api_id), api_hash=api_hash,
                                     workdir=SESSION_DIR)
            if self._started:
                me = await self.client.get_me()
                return bool(me)
            # Neu dang ket noi (sau send_code) ma chua start -> disconnect roi start lai
            if self.client.is_connected:
                try: await self.client.disconnect()
                except Exception: pass
            try:
                await self.client.start()   # khong hoi OTP neu session da co
                self._started = True
                me = await self.client.get_me()
                return bool(me)
            except Exception:
                return False
        try:
            return self._run_coro(_do(), timeout=30)
        except Exception:
            return False

    def is_logged_in(self):
        if not self.client:
            return False
        async def _do():
            try:
                if not self._started:
                    if self.client.is_connected:
                        try: await self.client.disconnect()
                        except Exception: pass
                    await self.client.start()
                    self._started = True
                return bool(await self.client.get_me())
            except Exception:
                return False
        try:
            return self._run_coro(_do(), timeout=20)
        except Exception:
            return False

    # --- Upload ---
    def _resolve_chat(self, chat):
        """Nap peer vao cache truoc khi gui (fix 'Peer id invalid').
        Pyrogram chi gui duoc toi peer da biet. get_chat() resolve + cache.
        Neu that bai -> duyet get_dialogs de nap toan bo peer da tham gia."""
        if chat in self._resolved:
            return True
        async def _try_get_chat():
            await self.client.get_chat(chat)
        try:
            self._run_coro(_try_get_chat(), timeout=30)
            self._resolved.add(chat)
            return True
        except Exception:
            pass
        # Fallback: duyet dialogs de nap peer cache (cho kenh da tham gia)
        async def _scan_dialogs():
            async for _ in self.client.get_dialogs():
                pass
        try:
            self._run_coro(_scan_dialogs(), timeout=120)
        except Exception:
            pass
        # Thu get_chat lai sau khi quet dialogs
        try:
            self._run_coro(_try_get_chat(), timeout=30)
            self._resolved.add(chat)
            return True
        except Exception:
            return False

    def upload_video(self, path, target, topic_id=None, thumb=None,
                     width=None, height=None, duration=None):
        """Up 1 video, tuan tu (1 lock). Tra ve (ok, msg)."""
        async def _do():
            # Dam bao client da start() day du (co thong tin me) truoc khi up.
            if not self._started:
                if self.client.is_connected:
                    try: await self.client.disconnect()
                    except Exception: pass
                await self.client.start()
                self._started = True
            kwargs = {"supports_streaming": True}
            if topic_id:
                # Pyrogram goc (2.x cua Dan) khong co message_thread_id -> dung
                # reply_to_message_id = topic_id (forum topic = message thread).
                # Cac fork moi (pyrofork/kurigram) co message_thread_id.
                if _TG_THREAD_PARAM:
                    kwargs[_TG_THREAD_PARAM] = int(topic_id)
            if thumb:
                kwargs["thumb"] = thumb
            if width:    kwargs["width"]    = int(width)
            if height:   kwargs["height"]   = int(height)
            if duration: kwargs["duration"] = int(duration)
            # Resolve target: ID so hoac @username
            chat = target
            if isinstance(target, str) and target.lstrip("-").isdigit():
                chat = int(target)
            await self.client.send_video(chat, path, **kwargs)
            return {"ok": True}
        with self._lock:   # up tuan tu, tranh race
            base = os.path.basename(path)
            try:
                sz_mb = os.path.getsize(path) / (1024*1024)
            except Exception:
                sz_mb = 0
            dest = f"{target}" + (f" / topic {topic_id}" if topic_id else "")
            # Resolve target int truoc khi nap peer
            _chat = target
            if isinstance(target, str) and target.lstrip("-").isdigit():
                _chat = int(target)
            # Dam bao started + nap peer cache (fix 'Peer id invalid')
            try:
                if not self._started:
                    self._run_coro(self._ensure_started(), timeout=30)
                if not self._resolve_chat(_chat):
                    _emit_upload(f"  ✗ Khong resolve duoc kenh {target} — "
                                 f"tai khoan da tham gia kenh nay chua?")
                    return False, f"Peer id invalid: {target} (chua tham gia kenh?)"
            except Exception as e:
                _emit_upload(f"  ⚠ Loi chuan bi: {str(e)[:80]}")
            _emit_upload(f"→ Bat dau up: {base} ({sz_mb:.1f} MB) → {dest}")
            t0 = time.time()
            attempt = 0
            while attempt < 5:
                try:
                    self._run_coro(_do(), timeout=1800)
                    dt = time.time() - t0
                    spd = (sz_mb / dt) if dt > 0 else 0
                    _emit_upload(f"  ✓ Xong: {base} | {dt:.1f}s | {spd:.1f} MB/s")
                    return True, ""
                except FloodWait as e:
                    wait = int(getattr(e, "value", 30))
                    _emit_upload(f"  ⏳ FloodWait {wait}s (lan {attempt+1}/5): {base} — Telegram gioi han, dang cho…")
                    time.sleep(wait + 2)
                    attempt += 1
                except Exception as e:
                    _emit_upload(f"  ✗ Loi up {base}: {str(e)[:120]}")
                    return False, str(e)
            _emit_upload(f"  ✗ Bo cuoc {base}: FloodWait lien tuc sau 5 lan")
            return False, "Het luot retry (FloodWait lien tuc)"

    def send_test(self, target, topic_id=None):
        """Gui 1 tin nhan text test toi dich (+ topic). Tra ve (ok, msg).
        Dung de kiem tra ket noi/dich/topic truoc khi chay batch."""
        _chat = target
        if isinstance(target, str) and target.lstrip("-").isdigit():
            _chat = int(target)
        async def _do():
            if not self._started:
                if self.client.is_connected:
                    try: await self.client.disconnect()
                    except Exception: pass
                await self.client.start()
                self._started = True
            kwargs = {}
            if topic_id and _TG_THREAD_PARAM:
                kwargs[_TG_THREAD_PARAM] = int(topic_id)
            txt = "✅ Test ket noi tu Watermark Studio v5 — " + time.strftime("%H:%M:%S")
            msg = await self.client.send_message(_chat, txt, **kwargs)
            return msg
        with self._lock:
            try:
                if not self._resolve_chat(_chat):
                    return False, f"Khong resolve duoc kenh {target} (tai khoan da tham gia chua?)"
                self._run_coro(_do(), timeout=60)
                return True, ""
            except FloodWait as e:
                return False, f"FloodWait {int(getattr(e,'value',30))}s — thu lai sau"
            except Exception as e:
                return False, str(e)

    def stop(self):
        if self.client and self.loop:
            async def _do():
                try:
                    if self._started:
                        await self.client.stop()
                    elif self.client.is_connected:
                        await self.client.disconnect()
                except Exception:
                    pass
                self._started = False
            try:
                self._run_coro(_do(), timeout=15)
            except Exception:
                pass

# ==============================================================================
#  API
# ==============================================================================

class Api:

    def __init__(self):
        self.uploader = TelegramUploader() if PYRO_OK else None

    # --- Telegram login (OTP 2 buoc + 2FA) ---
    def tg_available(self):
        return {"ok": PYRO_OK}

    def tg_send_code(self, api_id, api_hash, phone):
        if not self.uploader:
            return {"ok": False, "msg": "Chua cai pyrogram (pip install pyrogram tgcrypto)"}
        return self.uploader.send_code(api_id, api_hash, phone)

    def tg_sign_in(self, code, password=""):
        if not self.uploader:
            return {"ok": False, "msg": "Chua cai pyrogram"}
        return self.uploader.sign_in(code, password)

    def tg_check_login(self, api_id, api_hash, phone):
        """Thu ket noi bang session da luu. Tra ve {logged_in: bool}."""
        if not self.uploader:
            return {"ok": False, "logged_in": False}
        ok = self.uploader.connect_existing(api_id, api_hash, phone)
        return {"ok": True, "logged_in": ok}

    def tg_test_send(self, target, topic_id):
        """Gui 1 tin test toi dich de kiem tra ket noi truoc khi chay."""
        if not self.uploader:
            return {"ok": False, "msg": "Chua cai pyrogram"}
        if not (target or "").strip():
            return {"ok": False, "msg": "Chua nhap kenh dich"}
        ok, msg = self.uploader.send_test((target or "").strip(),
                                          (topic_id or "").strip() or None)
        return {"ok": ok, "msg": msg}

    def run_benchmark(self, config_json):
        """Chay thu cac buoc pipeline, bao cao ket qua tung buoc."""
        cfg      = {**DEFAULT_CONFIG, **json.loads(config_json)}
        ff       = cfg["ffmpeg_path"]
        ffprobe  = _ffprobe_path(cfg)
        results  = []

        def chk(name, ok, msg="", speed=None):
            results.append({"name": name, "ok": ok, "msg": msg, "speed": speed})

        # 1. ffmpeg ton tai
        if not os.path.isfile(ff):
            chk("ffmpeg.exe", False, f"Không tìm thấy: {ff}")
            return {"results": results}
        chk("ffmpeg.exe", True, ff)

        # 2. ffprobe ton tai
        chk("ffprobe.exe", os.path.isfile(ffprobe), "" if os.path.isfile(ffprobe) else "Không tìm thấy")

        # 3. Logo PNG ton tai
        has_logo = os.path.isfile(cfg.get("logo_image",""))
        chk("Logo PNG", has_logo, cfg.get("logo_image","") if has_logo else "Không tìm thấy — fast mode sẽ không có logo")

        # 4. NVENC hoat dong (encode 1s video test)
        import tempfile as _tmp
        test_out = os.path.join(_tmp.gettempdir(), "_wm_bench_nvenc.mp4")
        try:
            cmd = [ff, "-hide_banner", "-y",
                   "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=2",
                   "-c:v", "h264_nvenc", "-preset", "p1",
                   "-f", "null", "-"]
            t0 = time.time()
            rc, stderr = _run_ffmpeg(cmd, timeout=15)
            elapsed = time.time() - t0
            if rc == 0:
                chk("NVENC h264_nvenc", True, f"OK ({elapsed:.1f}s)")
            else:
                err = [l for l in stderr.splitlines() if "error" in l.lower() or "failed" in l.lower()]
                chk("NVENC h264_nvenc", False, (err[-1][:80] if err else stderr[-80:]))
        except Exception as e:
            chk("NVENC h264_nvenc", False, str(e))

        # 5. Fast mode: overlay logo PNG + NVENC encode thu 3 giay
        test_src = os.path.join(_tmp.gettempdir(), "_wm_bench_src.mp4")
        test_ovl = os.path.join(_tmp.gettempdir(), "_wm_bench_ovl.mp4")
        try:
            cmd_src = [ff, "-hide_banner", "-y",
                       "-f", "lavfi", "-i", "color=c=blue:s=1280x720:d=3",
                       "-c:v", "libx264", "-preset", "ultrafast", test_src]
            _run_ffmpeg(cmd_src, timeout=10)
            if os.path.exists(test_src):
                cfg_test = dict(cfg)
                cfg_test["gpu_workers"] = 1
                t0 = time.time()
                cmd_ovl = build_fast_cmd(cfg_test, test_src, test_ovl, None,
                                         video_w=1280, video_h=720)
                rc, stderr = _run_ffmpeg(cmd_ovl, timeout=20)
                elapsed = time.time() - t0
                if rc == 0 and os.path.exists(test_ovl) and os.path.getsize(test_ovl) > 1000:
                    speed = round(3.0 / elapsed, 1) if elapsed > 0 else 0
                    chk("Fast mode overlay+encode", True, "", speed)
                else:
                    err = [l for l in stderr.splitlines() if "error" in l.lower() or "invalid" in l.lower()]
                    chk("Fast mode overlay+encode", False, err[-1][:80] if err else "output rong")
            else:
                chk("Fast mode overlay+encode", False, "Khong tao duoc clip test")
        except Exception as e:
            chk("Fast mode overlay+encode", False, str(e))
        finally:
            for f in [test_src, test_ovl]:
                try: os.remove(f)
                except: pass

        # 8. Test max NVENC concurrent sessions
        try:
            import queue as _q
            max_ok  = 0
            results_q = _q.Queue()
            def _try_session(n):
                cmd = [ff, "-hide_banner", "-y",
                       "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=3",
                       "-c:v", "h264_nvenc", "-preset", "p1",
                       "-f", "null", "-"]
                rc, stderr = _run_ffmpeg(cmd, timeout=20)
                ok = rc == 0 and "openencodesessionex failed" not in stderr.lower()
                results_q.put((n, ok, stderr[-80:] if not ok else ""))

            # Chay 1..12 session song song
            threads = []
            for n in range(1, 13):
                t = threading.Thread(target=_try_session, args=(n,), daemon=True)
                t.start(); threads.append(t)
            for t in threads: t.join(timeout=25)

            session_results = {}
            while not results_q.empty():
                n, ok, msg = results_q.get()
                session_results[n] = (ok, msg)

            max_ok = sum(1 for n in session_results if session_results[n][0])
            failed = [n for n in session_results if not session_results[n][0]]
            msg = f"{max_ok}/12 session OK"
            if failed: msg += f" — lỗi session {sorted(failed)}"
            chk(f"NVENC max sessions", max_ok > 0, msg)
        except Exception as e:
            chk("NVENC max sessions", False, str(e))

        # 7. Input folder ton tai
        inp_ok = os.path.isdir(cfg.get("input_folder",""))
        chk("Input folder", inp_ok, cfg.get("input_folder","") if inp_ok else "Không tìm thấy — cần chọn đúng thư mục")

        return {"results": results}

    def copy_to_clipboard(self, text):
        """Copy text vao clipboard Windows dung subprocess."""
        try:
            proc = subprocess.Popen(
                ["clip"], stdin=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW)
            proc.communicate(input=text.encode("utf-16-le"))
            return {"ok": True, "msg": ""}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def kill_ffmpeg(self):
        """Kill toan bo process ffmpeg.exe dang chay (Windows)."""
        try:
            import subprocess as _sp
            _sp.Popen(["taskkill", "/F", "/IM", "ffmpeg.exe"],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                      creationflags=CREATE_NO_WINDOW)
            return {"ok": True, "msg": "💀 Đã gửi lệnh kill ffmpeg.exe"}
        except Exception as e:
            return {"ok": False, "msg": f"Kill lỗi: {e}"}

    def pick_logo(self):
        r = _window.create_file_dialog(webview.OPEN_DIALOG, file_types=('PNG (*.png)',))
        return r[0] if r else None
    def pick_input_folder(self):
        r = _window.create_file_dialog(webview.FOLDER_DIALOG)
        return r[0] if r else None
    def pick_output_folder(self):
        r = _window.create_file_dialog(webview.FOLDER_DIALOG)
        return r[0] if r else None
    def pick_ffmpeg(self):
        r = _window.create_file_dialog(webview.OPEN_DIALOG, file_types=('Executable (*.exe)',))
        return r[0] if r else None
    def pick_font(self):
        r = _window.create_file_dialog(webview.OPEN_DIALOG, file_types=('Font (*.ttf;*.otf)',))
        return r[0] if r else None
    def pick_qr(self):
        r = _window.create_file_dialog(webview.OPEN_DIALOG, file_types=('PNG (*.png)',))
        return r[0] if r else None

    def scan_folder(self, folder):
        if not folder or not os.path.isdir(folder):
            return []
        result = []
        for f in sorted(os.listdir(folder)):
            ext = os.path.splitext(f)[1].lower()
            if ext in ALL_EXTS:
                sz = os.path.getsize(os.path.join(folder, f)) / (1024 * 1024)
                result.append({"name": f, "size": f"{sz:.1f} MB",
                                "ext": ext.lstrip("."), "path": os.path.join(folder, f)})
        return result

    def get_presets_info(self):
        """Trả về danh sách preset names + last_preset đã lưu."""
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            last = data.get("last_preset", list(PRESETS.keys())[0])
            saved_names = list(data.get("presets", {}).keys())
        except Exception:
            last = list(PRESETS.keys())[0]
            saved_names = []
        # Hardcoded presets first, then user-created ones not already listed
        names = list(PRESETS.keys())
        for n in saved_names:
            if n not in names:
                names.append(n)
        return {"names": names, "last_preset": last}
    def load_preset(self, name):
        """Load preset: ưu tiên từ file lưu, fallback về hardcode."""
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_presets = data.get("presets", {})
            if name in saved_presets:
                return {**DEFAULT_CONFIG, **PRESETS.get(name, {}), **saved_presets[name]}
        except Exception:
            pass
        return {**DEFAULT_CONFIG, **PRESETS.get(name, {})}

    def save_config(self, preset_name, cfg_json):
        try:
            cfg = json.loads(cfg_json)
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {"presets": {}, "last_preset": preset_name}
            if "presets" not in data:
                data["presets"] = {}
            data["presets"][preset_name] = cfg
            data["last_preset"] = preset_name
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    # ── Logo quick-options (sub-preset) ──────────────────────────────────────

    def _load_data(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_data(self, data):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def get_logo_options(self, preset_name):
        """Trả về danh sách tùy chọn logo của preset hiện tại."""
        data = self._load_data()
        opts = data.get("logo_options", {}).get(preset_name, {})
        return {"names": list(opts.keys()), "options": opts}

    def save_logo_option(self, preset_name, option_name, full_cfg_json):
        """Lưu toàn bộ config hiện tại như một option nhỏ của preset."""
        try:
            cfg = json.loads(full_cfg_json)
            data = self._load_data()
            if "logo_options" not in data:
                data["logo_options"] = {}
            if preset_name not in data["logo_options"]:
                data["logo_options"][preset_name] = {}
            data["logo_options"][preset_name][option_name] = cfg
            return self._save_data(data)
        except Exception:
            return False

    def delete_logo_option(self, preset_name, option_name):
        """Xóa một tùy chọn logo."""
        try:
            data = self._load_data()
            opts = data.get("logo_options", {}).get(preset_name, {})
            if option_name in opts:
                del opts[option_name]
                data.setdefault("logo_options", {})[preset_name] = opts
                return self._save_data(data)
            return False
        except Exception:
            return False

    def load_logo_option(self, preset_name, option_name):
        """Trả về logo cfg của một option cụ thể."""
        data = self._load_data()
        return data.get("logo_options", {}).get(preset_name, {}).get(option_name, {})

    def get_ffmpeg_log_since(self, offset):
        lines = _ffmpeg_log_lines
        total = len(lines)
        if offset >= total:
            return {"lines": [], "total": total}
        return {"lines": list(lines[offset:]), "total": total}

    def get_upload_log_since(self, offset):
        lines = _upload_log_lines
        total = len(lines)
        if offset >= total:
            return {"lines": [], "total": total}
        return {"lines": list(lines[offset:]), "total": total}

    def snap_preview(self, config_json):
        """Xuat 1 frame PNG co watermark/logo de xem thu vi tri.
        Tim video dau tien trong input_folder, lay frame giay thu 3,
        overlay logo len bang ffmpeg (CPU, nhanh), luu vao temp, mo bang
        Windows Photo Viewer / explorer de nguoi dung xem ngay.
        Tra ve {ok, path, msg}.
        """
        import tempfile as _tmp
        cfg = {**DEFAULT_CONFIG, **json.loads(config_json)}
        ff  = cfg["ffmpeg_path"]
        if not os.path.isfile(ff):
            return {"ok": False, "msg": f"Khong tim thay ffmpeg: {ff}"}

        # Tim video mau trong input_folder
        inp_dir = cfg.get("input_folder", "")
        sample  = None
        video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".webm"}
        if os.path.isdir(inp_dir):
            for fn in sorted(os.listdir(inp_dir)):
                if os.path.splitext(fn)[1].lower() in video_exts:
                    sample = os.path.join(inp_dir, fn)
                    break
        if not sample:
            return {"ok": False, "msg": "Khong tim thay video nao trong thu muc input"}

        out_png = os.path.join(_tmp.gettempdir(), "_wm_snap_preview.png")
        try:
            os.remove(out_png)
        except Exception:
            pass

        # Build filter_complex giong fast_cmd nhung dung CPU overlay (khong can CUDA)
        # va xuat 1 frame PNG (nhanh, khong can encode ca video)
        mx  = str(cfg.get("logo_margin_x", 15))
        my  = str(cfg.get("logo_margin_y", 15))
        def _cx(c): return str(cfg.get(f"logo_{c}_x", int(mx)))
        def _cy(c): return str(cfg.get(f"logo_{c}_y", int(my)))

        logo_list = []  # (path, scale_w, opacity, ox_expr, oy_expr)
        if os.path.isfile(cfg.get("logo_image", "")):
            logo_list.append((cfg["logo_image"], cfg.get("logo_scale_w", 160),
                              cfg.get("logo_opacity", 0.7), mx, my))
        for corner, ox_fn, oy_fn in [
            ("tl", _cx("tl"),                         _cy("tl")),
            ("tr", f"main_w-overlay_w-{_cx('tr')}",   _cy("tr")),
            ("bl", _cx("bl"),                         f"main_h-overlay_h-{_cy('bl')}"),
            ("br", f"main_w-overlay_w-{_cx('br')}",   f"main_h-overlay_h-{_cy('br')}"),
        ]:
            if cfg.get(f"enable_{corner}") and os.path.isfile(cfg.get(f"logo_{corner}", "")):
                logo_list.append((cfg[f"logo_{corner}"], cfg.get(f"logo_{corner}_w", 120),
                                  cfg.get(f"logo_{corner}_op", 0.7), ox_fn, oy_fn))

        extra_inputs = []
        fc_parts     = []
        stream_in    = "[0:v]"

        for i, (lpath, lw, lop, ox, oy) in enumerate(logo_list):
            idx = i + 1
            lbl = f"lg{i}"
            out = f"v{i}"
            extra_inputs += ["-i", lpath]
            fc_parts.append(
                f"[{idx}:v]scale={lw}:-1,"
                f"colorchannelmixer=aa={lop}[{lbl}]"
            )
            fc_parts.append(
                f"{stream_in}[{lbl}]overlay=x={ox}:y={oy}[{out}]"
            )
            stream_in = f"[{out}]"

        # Seek giay 3 (tranh doan den dau), lay 1 frame -> PNG
        cmd = [ff, "-hide_banner", "-loglevel", "error",
               "-ss", "3", "-i", sample,
               *extra_inputs]
        if fc_parts:
            cmd += ["-filter_complex", ";".join(fc_parts),
                    "-map", stream_in]
        cmd += ["-vframes", "1", "-y", out_png]

        try:
            rc, stderr = _run_ffmpeg(cmd, timeout=30)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

        if rc != 0 or not os.path.exists(out_png) or os.path.getsize(out_png) < 100:
            err_lines = [l for l in stderr.splitlines() if "error" in l.lower() or "invalid" in l.lower()]
            msg = err_lines[-1][:120] if err_lines else stderr[-120:]
            # Fallback: thu lai voi ss=0 (video ngan hon 3s)
            cmd2 = [ff, "-hide_banner", "-loglevel", "error",
                    "-i", sample, *extra_inputs]
            if fc_parts:
                cmd2 += ["-filter_complex", ";".join(fc_parts), "-map", stream_in]
            cmd2 += ["-vframes", "1", "-y", out_png]
            try:
                rc2, _ = _run_ffmpeg(cmd2, timeout=30)
            except Exception:
                rc2 = -1
            if rc2 != 0 or not os.path.exists(out_png) or os.path.getsize(out_png) < 100:
                return {"ok": False, "msg": msg or "Xuat frame that bai"}

        # Mo anh bang Windows (explorer /select mo file, hoac os.startfile)
        try:
            os.startfile(out_png)
        except Exception:
            try:
                subprocess.Popen(["explorer", out_png])
            except Exception:
                pass

        return {"ok": True, "path": out_png, "msg": "OK"}

    def create_outro_preview(self, config_json):
        cfg = {**DEFAULT_CONFIG, **json.loads(config_json)}
        out = os.path.join(APP_DIR, "outro_preview.mp4")
        ok, err = create_outro_video(cfg, 1280, 720, 30.0, out)
        return {"ok": ok, "path": out if ok else "", "msg": err}

    def start_process(self, config_json):
        global _running
        if _running:
            return {"ok": False, "msg": "Dang chay roi"}
        cfg = {**DEFAULT_CONFIG, **json.loads(config_json)}
        _cancel_flag.clear()
        _running = True
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()
        return {"ok": True}

    def cancel_process(self):
        _cancel_flag.set()
        # Kill tat ca ffmpeg ngay lap tuc (khong cho file hien tai chay xong)
        try:
            subprocess.Popen(["taskkill", "/F", "/IM", "ffmpeg.exe"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        return {"ok": True}

    def _emit(self, event, data):
        try:
            _window.evaluate_js(f"window._backend('{event}', {json.dumps(data)})")
        except Exception:
            pass

    def _worker(self, cfg):
        global _running
        try:
            self._run(cfg)
        finally:
            _running = False

    def _run(self, cfg):
        ff        = cfg["ffmpeg_path"]
        ffprobe_p = _ffprobe_path(cfg)
        inp_dir   = cfg["input_folder"]
        out_dir   = cfg["output_folder"]
        suffix    = cfg["output_suffix"]
        min_kb    = int(cfg["min_output_kb"])
        trim      = int(cfg.get("trim_start", 0) or 0)
        parts     = max(1, int(cfg.get("split_parts", 1) or 1))
        thr       = float(cfg.get("long_video_threshold", 300)) if cfg.get("enable_logo_limit") else 0
        ratio     = float(cfg.get("encode_ratio", 0.20))
        # Hybrid pool: gpu_workers (NVENC) + cpu_workers (libx264) chay song song.
        # Tuong thich nguoc: neu config cu chi co max_workers thi dung lam gpu_workers.
        gpu_workers = int(cfg.get("gpu_workers", cfg.get("max_workers", 2)) or 0)
        cpu_workers = int(cfg.get("cpu_workers", 0) or 0)
        if gpu_workers <= 0 and cpu_workers <= 0:
            gpu_workers = 1

        # v7: nvenc_sessions = gpu_workers (UI da gop 2 thanh 1)
        global _NVENC_SEM
        _NVENC_SEM = threading.Semaphore(gpu_workers)

        # ==== UPLOAD: tu dong hoa TELEGRAM DESKTOP (focus -> paste -> Enter) ====
        # Khong dung API. Mo Telegram Desktop + click san chat dich truoc khi chay.
        tg_on      = False   # API upload da go bo hoan toan
        auto_up    = bool(cfg.get("auto_upload"))
        album_mode = bool(cfg.get("album_mode"))
        up_sendkey = cfg.get("upload_send_key", "enter")
        up_wait    = float(cfg.get("upload_dialog_wait", 1.0) or 1.0)
        album_max  = max(1, int(cfg.get("album_max", 10) or 10))
        up_delay   = float(cfg.get("upload_delay", 0.5) or 0.0)
        tele_hwnd  = None
        if auto_up and not DESKTOP_UP_OK:
            auto_up = False
            self._emit("log", {"cls": "err",
                "msg": f"Auto up TAT: thieu thu vien Windows ({DESKTOP_UP_ERR}). "
                       f"Cai: pip install pywin32"})
        if auto_up:
            tele_hwnd, ttl = find_telegram_window()
            if not tele_hwnd:
                auto_up = False
                self._emit("log", {"cls": "err",
                    "msg": "Auto up TAT: khong thay cua so Telegram Desktop. "
                           "Mo Telegram + click vao chat dich roi chay lai. "
                           "(Lan nay chi watermark, file nam o Output)"})
            else:
                che_do = "ALBUM — up het khi xong toan bo" if album_mode \
                         else "TUNG FILE — xong cai nao up cai do"
                self._emit("log", {"cls": "info",
                    "msg": f"Auto up desktop BAT → cua so: {ttl} | che do: {che_do}"})
        else:
            self._emit("log", {"cls": "info",
                "msg": "Auto up TAT — file watermark se nam o thu muc Output."})

        # Khoa up: thao tac focus->paste->Enter la TOAN CUC, phai tuan tu
        # (khong cho 2 luong up cung luc). album_files: gom file khi che do album.
        up_lock       = threading.Lock()
        album_files   = []
        album_lock    = threading.Lock()
        album_counter = [0]
        _album_expect    = {}
        _album_done_cnt  = {}
        _album_done_lock = threading.Lock()
        _album_up_set    = set()

        def _do_upload_chunks(chunks, label_prefix=""):
            for ch in chunks:
                if _cancel_flag.is_set():
                    break
                album_counter[0] += 1
                ci = album_counter[0]
                lbl = f"{label_prefix} " if label_prefix else ""
                with up_lock:
                    ok_up, msg_up = desktop_upload(tele_hwnd, ch, up_sendkey, up_wait)
                if ok_up:
                    self._emit("log", {"cls": "ok",
                        "msg": f"  ⬆ Album {ci} {lbl}đẩy {len(ch)} file OK"})
                else:
                    self._emit("log", {"cls": "err",
                        "msg": f"  ⬆ Album {ci} {lbl}lỗi: {msg_up[:80]}"})
                if up_delay > 0:
                    time.sleep(max(up_delay, 0.8))

        def _try_upload_prefix(prefix, produced_files):
            """Gom produced vao prefix bucket.
            Neu album_mode + prefix hop le: up ngay khi prefix do xong het file input.
            Neu khong co prefix (ten file khong match so dau): gom vao buffer chung."""
            if not album_mode or prefix not in _album_expect:
                # Fallback: gom buffer chung, flush khi du album_max
                with album_lock:
                    album_files.extend(produced_files)
                    if len(album_files) >= album_max:
                        to_up = list(album_files)
                        album_files.clear()
                    else:
                        return
                _do_upload_chunks(split_chunks(to_up, album_max))
                return
            with _album_done_lock:
                _album_done_cnt[prefix] = _album_done_cnt.get(prefix, 0) + 1
                done_cnt = _album_done_cnt[prefix]
                expect   = _album_expect[prefix]
                already  = prefix in _album_up_set
            if already or done_cnt < expect:
                return
            # Prefix nay xong het -> scan output lay file cua prefix nay
            with _album_done_lock:
                _album_up_set.add(prefix)
            import re as _re2
            _px_re = _re2.compile(rf"^{_re2.escape(prefix)}")
            ready = sorted(
                os.path.join(out_dir, f)
                for f in os.listdir(out_dir)
                if _px_re.match(f)
                and os.path.splitext(f)[1].lower()
                    in {".mp4", ".avi", ".mkv", ".mov", ".jpg", ".jpeg", ".png"}
                and os.path.getsize(os.path.join(out_dir, f)) > 0
            )
            if not ready:
                return
            self._emit("log", {"cls": "info",
                "msg": f"  ⬆ Prefix [{prefix}] xong {expect} file — up {len(ready)} file ngay..."})
            _do_upload_chunks(split_chunks(ready, album_max), f"[{prefix}]")

        # Thu muc temp render: neu user tro vao RAM disk (vd R:\) thi dung,
        # khong thi None = temp he thong mac dinh (SSD).
        _tmp_base = None
        td = (cfg.get("temp_dir") or "").strip()
        if td:
            if os.path.isdir(td):
                _tmp_base = td
                self._emit("log", {"cls":"info","msg":f"Temp render: {td}"})
            else:
                self._emit("log", {"cls":"warn",
                    "msg":f"Temp '{td}' khong ton tai -> dung temp he thong"})

        errors = []
        if not os.path.isfile(ff):
            errors.append(f"Khong tim thay ffmpeg: {ff}")
        if not os.path.isdir(inp_dir):
            errors.append(f"Thu muc input khong ton tai: {inp_dir}")
        # Logo chi bat buoc khi KHONG dung fast mode (drawtext mode can logo rieng)
        # Fast mode da baked logo vao WM PNG roi, khong can check o day
        if not cfg.get("fast_mode") and not os.path.isfile(cfg["logo_image"]):
            errors.append(f"Khong tim thay logo: {cfg['logo_image']}")
        if errors:
            for e in errors:
                self._emit("log", {"cls": "err", "msg": e})
            self._emit("done", {"ok": 0, "err": len(errors), "total": 0})
            return

        os.makedirs(out_dir, exist_ok=True)
        files = [f for f in sorted(os.listdir(inp_dir))
                 if os.path.splitext(f)[1].lower() in ALL_EXTS]

        # --- Album-prefix grouping (dung cho album_mode up ngay khi xong prefix) ---
        import re as _re
        _PREFIX_RE = _re.compile(r"^(\d{3,6})")
        for _fn in files:
            _m = _PREFIX_RE.match(_fn)
            if _m:
                _p = _m.group(1)
                _album_expect[_p] = _album_expect.get(_p, 0) + 1
                _album_done_cnt[_p] = 0

        # Load done_log (dung khi auto upload + delete_after)
        done_set  = _load_done_log(inp_dir) if tg_on and cfg.get("tg_delete_after") else set()
        done_lock = threading.Lock()

        if cfg.get("skip_done") and not (tg_on and cfg.get("tg_delete_after")):
            parts = max(1, int(cfg.get("split_parts", 1) or 1))
            keep = []
            for f in files:
                name, ext = os.path.splitext(f.lower())
                is_img    = ext in IMAGE_EXTS
                if is_img:
                    # Image: check 1 file duy nhat
                    out_chk = os.path.join(out_dir, name + suffix + ".jpg")
                    if not _is_valid(out_chk, min_kb, True):
                        keep.append(f)
                elif parts > 1:
                    # Split: check tat ca part files ton tai va hop le
                    all_done = all(
                        _is_valid(
                            os.path.join(out_dir, f"{name}_part{i:02d}{suffix}.mp4"),
                            min_kb
                        )
                        for i in range(1, parts + 1)
                    )
                    if not all_done:
                        keep.append(f)
                else:
                    # 1 file duy nhat
                    out_chk = os.path.join(out_dir, name + suffix + ".mp4")
                    if not _is_valid(out_chk, min_kb):
                        keep.append(f)
            skipped = len(files) - len(keep)
            files   = keep
            if skipped:
                self._emit("log", {"cls": "info", "msg": f"Bo qua {skipped} file da co output"})
        elif cfg.get("skip_done") and tg_on and cfg.get("tg_delete_after"):
            # Skip theo done_log (file da upload + xoa)
            before = len(files)
            files  = [f for f in files if f not in done_set]
            skipped = before - len(files)
            if skipped:
                self._emit("log", {"cls": "info", "msg": f"Bo qua {skipped} file da upload (done_log)"})
            if done_set:
                self._emit("log", {"cls": "info", "msg": f"Done log: {len(done_set)} file da ghi nhan"})
        elif cfg.get("skip_done") and tg_on:
            self._emit("log", {"cls": "info", "msg": "Skip-done tat khi auto upload + xoa file (khong co file de doi chieu)"})

        total = len(files)
        if total == 0:
            self._emit("log", {"cls": "warn", "msg": "Khong co file nao can xu ly"})
            self._emit("done", {"ok": 0, "err": 0, "total": 0})
            return

        global _outro_cache, _center_png_cache, _text_png_cache, _ffmpeg_log_lines, _upload_log_lines
        _outro_cache      = {}
        _center_png_cache = {}
        _text_png_cache   = {}
        _ffmpeg_log_lines = []
        _upload_log_lines = []
        if tg_on:
            _emit_upload(f"=== Auto upload BAT → {cfg.get('tg_target')} ===")

        dynamic = _has_dynamic_wm(cfg)
        cfg["_fast_ok"] = True
        if not dynamic:
            self._emit("log", {"cls":"info","msg":"Dang kiem tra NVENC…"})
            if fast_cuda_supported(cfg):
                turbo = " + NVDEC" if cfg.get("turbo_nvdec", True) else ""
                self._emit("log", {"cls":"ok","msg":
                    f"✓ Turbo Render: overlay_cuda + PNG chữ giữa/góc{turbo} 🚀"})
            else:
                cfg["_fast_ok"] = False
                self._emit("log", {"cls":"warn","msg":
                    "⚠ NVENC khong chay duoc → fallback libx264 CPU."})
        else:
            self._emit("log", {"cls":"info","msg":"✨ HS preset: WM dong (drawtext CPU)"})
            cfg["_fast_ok"] = False
        self._emit("total", {"total": total})
        wk_info = f"{gpu_workers} GPU"
        if cpu_workers > 0:
            wk_info += f" + {cpu_workers} CPU(x264 {cfg.get('x264_preset','medium')})"
        info = [f"Bat dau {total} file", f"trim={trim}s",
                wk_info,
                "logo " + (f"AUTO {int(ratio*100)}% (>{int(thr)}s)" if thr > 0 else "FULL")]
        if parts > 1:               info.append(f"VA Pro {parts} phan")
        if not dynamic:
            info.append("🚀 Turbo overlay_cuda")
        else:
            info.append("✨ WM dong (HS preset)")
        if cfg.get("enable_outro"): info.append("🎬 outro")
        self._emit("log", {"cls": "info", "msg": " | ".join(info)})

        # --- Trang thai dung chung giua cac luong ---
        state = {"ok": 0, "err": 0, "done": 0}
        lock  = threading.Lock()
        t_all = time.time()

        # ------------------------------------------------------------------
        #  Xu ly 1 file (chay tren 1 luong cua pool). Tu chua tmp_dir rieng.
        # ------------------------------------------------------------------
        def _process_one(idx, filename, use_gpu=True):
            inp       = os.path.join(inp_dir, filename)
            name, ext = os.path.splitext(filename.lower())
            is_img    = ext in IMAGE_EXTS

            hw = "GPU" if use_gpu else "CPU"
            with lock:
                self._emit("file_start", {"idx": idx, "name": filename})
                self._emit("log", {"cls": "", "msg": f"▶ [{hw}] {filename}"})

            t0 = time.time(); success = False; mode = ""; duration = None
            produced = []   # cac file output da tao thanh cong (de auto up)

            if is_img:
                # Anh luon encode nhanh bang 1 frame, khong phan biet GPU/CPU
                outp = os.path.join(out_dir, name + suffix + ".jpg")
                fc   = build_filter_complex(cfg, is_img=True)
                cmd  = [ff, "-hide_banner", "-i", inp, "-i", cfg["logo_image"],
                        "-filter_complex", fc,
                        "-filter_threads", str(int(cfg.get("filter_threads", 4))),
                        "-frames:v", "1", "-update", "1", "-y", outp]
                rc, _   = _run_ffmpeg(cmd, timeout=60)
                success = rc == 0 and _is_valid(outp, min_kb, True)
                if success: produced.append(outp)
                mode    = "anh"
            else:
                duration   = _get_duration(ffprobe_p, inp)
                tmp_dir    = tempfile.mkdtemp(prefix="gpu_wm_", dir=_tmp_base)
                # Render thang ra out_dir, file temp chi dung cho concat_outro.
                dest_dir   = out_dir
                outp_final = os.path.join(dest_dir, name + suffix + ".mp4")
                fail_reason = ""
                # Canh bao som neu khong probe duoc duration (file loi/path xau)
                if duration is None:
                    fail_reason = "ffprobe khong doc duoc (file loi hoac path co ky tu la)"
                try:
                    def _do_part(p_start, p_limit, p_outp):
                        nonlocal fail_reason
                        cut = None
                        if thr > 0 and duration and duration > thr:
                            raw = (p_limit or (duration - (p_start or 0))) * ratio
                            cut = round(raw, 2)
                        fc_p   = build_filter_complex(cfg, is_img=False, cut=cut)
                        wm_tmp = (os.path.join(tmp_dir, f"wm_{abs(hash(p_outp))}.mp4")
                                  if cfg.get("enable_outro") else p_outp)
                        s, fast, reason = encode_segment(cfg, ffprobe_p, inp, wm_tmp, fc_p,
                                                         p_start, p_limit, use_gpu=use_gpu)
                        if not s and reason:
                            fail_reason = reason
                        tag = " [⚡fast]" if fast else (" [x264]" if not use_gpu else "")
                        if s and cfg.get("enable_outro"):
                            s2, emsg = concat_outro(ff, ffprobe_p, wm_tmp, p_outp, cfg, tmp_dir)
                            if not s2:
                                with lock:
                                    self._emit("log", {"cls":"warn","msg":f"  Outro loi: {emsg} -> dung ban khong outro"})
                                try: shutil.copy2(wm_tmp, p_outp)
                                except Exception as ce: fail_reason = f"copy fallback loi: {ce}"
                            s   = _is_valid(p_outp, min_kb)
                            if not s and not fail_reason:
                                fail_reason = "sau outro: output khong hop le"
                            tag += " +outro" if s else ""
                        # --- Auto upload Telegram (neu bat) ---
                        # File output da xong (dang o out_dir). Ghi nhan de auto up.
                        if s:
                            produced.append(p_outp)
                        return s, tag

                    if parts > 1 and duration:
                        avail    = max(0.0, float(duration) - float(trim))
                        n_parts  = max(1, int(avail)) if avail < parts else parts
                        part_len = avail / n_parts
                        ok_p     = 0
                        for i in range(1, n_parts + 1):
                            p_start = trim + (i-1) * part_len
                            p_limit = part_len if i < n_parts else None
                            p_outp  = os.path.join(dest_dir, f"{name}_part{i:02d}{suffix}.mp4")
                            s, _    = _do_part(p_start, p_limit, p_outp)
                            if s: ok_p += 1
                        success = ok_p == n_parts
                        mode    = f"VA Pro {n_parts}p ({ok_p} OK)"
                    else:
                        success, tag = _do_part(trim if trim else None, None, outp_final)
                        cut_info = ""
                        if thr > 0 and duration and duration > thr:
                            cut      = round((duration - (trim or 0)) * ratio, 2)
                            cut_info = f" logo {int(ratio*100)}% ({cut:.0f}s)"
                        mode = (f"{duration:.0f}s" if duration else "?") + cut_info + tag

                    if not success and fail_reason:
                        mode = (mode + f" | {fail_reason}") if mode else fail_reason

                    if not success:
                        # Don file output loi (chi trong out_dir; temp se bi xoa o finally)
                        for f2 in ([os.path.join(out_dir, name + suffix + ".mp4")] +
                                   [os.path.join(out_dir, f"{name}_part{i:02d}{suffix}.mp4")
                                    for i in range(1, parts+1)]):
                            if os.path.exists(f2):
                                try: os.remove(f2)
                                except: pass
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            enc_t = time.time() - t0
            # Speed THAT: ti le so voi realtime (giong ffmpeg). Anh khong co duration.
            if duration and enc_t > 0:
                speed = round(duration / enc_t, 1)
            else:
                speed = 0.0

            with lock:
                if success:
                    state["ok"]  += 1; st, lb, lc = "ok",  "✅ OK",  "ok"
                else:
                    state["err"] += 1; st, lb, lc = "err", "❌ Loi", "err"
                state["done"] += 1
                done    = state["done"]
                elapsed = time.time() - t_all
                # ETA theo throughput trung binh thuc te (wall-clock / so file xong)
                eta     = int((elapsed / done) * (total - done)) if done else 0
                self._emit("file_done", {"idx":idx,"status":st,"label":lb,
                    "encode_time":round(enc_t,1),"mode":mode})
                self._emit("progress", {"done":done,"total":total,
                    "ok":state["ok"],"err":state["err"],
                    "fallback":0,"eta":eta,"speed":speed})
                self._emit("log", {"cls":lc,
                    "msg":f"  {lb} [{mode}] — {enc_t:.1f}s — {speed:.1f}x — {filename}"})

            # ---- AUTO UPLOAD (Telegram Desktop) ----
            if success and auto_up and produced:
                if album_mode:
                    # Che do ALBUM theo prefix: up ngay khi prefix xong het
                    _m = _PREFIX_RE.match(filename)
                    _prefix = _m.group(1) if _m else None
                    _try_upload_prefix(_prefix, produced)
                else:
                    # Che do THUONG: up LE tung file — ke ca khi chia part
                    # thi moi part up rieng 1 lan (khong gop album).
                    with up_lock:               # tuan tu: chi 1 luong up 1 luc
                        for f1 in produced:
                            if _cancel_flag.is_set():
                                break
                            ok_up, msg_up = desktop_upload(
                                tele_hwnd, [f1], up_sendkey, up_wait)
                            with lock:
                                if ok_up:
                                    self._emit("log", {"cls":"ok",
                                        "msg":f"  ⬆ Da day vao Telegram: {os.path.basename(f1)}"})
                                else:
                                    self._emit("log", {"cls":"err",
                                        "msg":f"  ⬆ Up loi: {msg_up[:80]} — file van o Output"})
                            if up_delay > 0:
                                time.sleep(up_delay)

        # ------------------------------------------------------------------
        #  Dispatch hybrid: 1 hang doi chung, GPU + CPU worker cung lay viec.
        #  Ai ranh lay truoc -> tu can bang tai (CPU cham se lay it file hon).
        # ------------------------------------------------------------------
        import queue as _queue
        task_q = _queue.Queue()
        for idx, filename in enumerate(files):
            task_q.put((idx, filename))

        def _worker_loop(use_gpu):
            while not _cancel_flag.is_set():
                try:
                    idx, filename = task_q.get_nowait()
                except _queue.Empty:
                    return
                try:
                    _process_one(idx, filename, use_gpu=use_gpu)
                except Exception as e:
                    with lock:
                        self._emit("log", {"cls":"err","msg":f"  Loi luong: {e}"})
                finally:
                    task_q.task_done()

        threads = []
        for _ in range(gpu_workers):
            t = threading.Thread(target=_worker_loop, args=(True,), daemon=True)
            t.start(); threads.append(t)
        for _ in range(cpu_workers):
            t = threading.Thread(target=_worker_loop, args=(False,), daemon=True)
            t.start(); threads.append(t)

        for t in threads:
            t.join()

        if _cancel_flag.is_set():
            self._emit("log", {"cls": "warn", "msg": "Da huy (cac job dang chay da chay not)"})

        # ---- AUTO UPLOAD che do ALBUM: flush buffer chung con le (file khong co prefix) ----
        if auto_up and album_mode and album_files and not _cancel_flag.is_set():
            album_files.sort()
            self._emit("log", {"cls":"info",
                "msg":f"⬆ Up phan le: {len(album_files)} file khong co prefix"})
            _do_upload_chunks(split_chunks(album_files, album_max))

        total_time = round(time.time() - t_all, 1)
        self._emit("done", {"ok":state["ok"],"err":state["err"],
            "fallback":0,"total":total,"elapsed":total_time})
        self._emit("log", {"cls":"info",
            "msg":f"🎉 Hoan tat {state['ok']} OK / {state['err']} loi | {total_time}s"})

    def _upload_part(self, path, name, out_dir, cfg, ff, ffprobe_p,
                     inp_dir=None, orig_filename=None, done_set=None, done_lock=None):
        """Up 1 file len Telegram. Retry trong uploader. Tra ve (ok, tag).
        Fail -> chuyen file ra out_dir (o cung that) de khong mat."""
        target   = cfg.get("tg_target", "")
        topic_id = cfg.get("tg_topic_id") or None
        thumb    = None
        thumb_tmp = None
        # Auto thumbnail (mac dinh tat) — trich frame giua tranh anh den
        if cfg.get("tg_auto_thumb"):
            _tb = (cfg.get("temp_dir") or "").strip()
            _tb = _tb if (_tb and os.path.isdir(_tb)) else tempfile.gettempdir()
            thumb_tmp = os.path.join(_tb, f"thumb_{abs(hash(path))}.jpg")
            thumb = make_thumbnail(ff, ffprobe_p, path, thumb_tmp)
            _emit_upload(f"  📷 Thumbnail {'OK' if thumb else 'loi (up khong thumb)'}: {os.path.basename(path)}")
        w, h, _  = _probe_wh(ffprobe_p, path)
        dur      = _get_duration(ffprobe_p, path)

        # GUARD chong "video den chi con tieng": Pyrogram send_video mac dinh
        # w=0/h=0 -> Telegram dung khung 0x0 -> video den. Bat buoc phai co W/H.
        if not (w and h):
            # Probe lai 1 lan (phong loi ffprobe tam thoi / path)
            w2, h2, _ = _probe_wh(ffprobe_p, path)
            d2        = _get_duration(ffprobe_p, path)
            w, h = (w2 or w), (h2 or h)
            dur  = dur or d2
        if not (w and h):
            _emit_upload(f"  ✗ Khong doc duoc kich thuoc (W/H) cua {os.path.basename(path)} "
                         f"— BO QUA de tranh up video DEN. Kiem tra ffprobe.exe co ben canh ffmpeg.exe khong.")
            if thumb_tmp and os.path.exists(thumb_tmp):
                try: os.remove(thumb_tmp)
                except: pass
            return False, "Khong doc duoc W/H (tranh up video den)"

        ok, msg = self.uploader.upload_video(path, target, topic_id=topic_id,
                                             thumb=thumb, width=w, height=h,
                                             duration=int(dur or 0))
        if thumb_tmp and os.path.exists(thumb_tmp):
            try: os.remove(thumb_tmp)
            except: pass

        if ok:
            self._emit("log", {"cls":"ok","msg":f"  ⬆ Da up: {os.path.basename(path)}"})
            if inp_dir and orig_filename and done_set is not None and done_lock is not None:
                _mark_done(inp_dir, orig_filename, done_set, done_lock)
            return True, " ⬆up"
        else:
            self._emit("log", {"cls":"err","msg":f"  ⬆ Up loi: {msg[:80]} — file van o Output"})
            return False, " ⬆fail"


# ==============================================================================
#  ENTRY POINT
# ==============================================================================

def main():
    global _window
    _window = webview.create_window(
        title="GPU Watermark Studio v12",
        html=_load_ui(),
        js_api=Api(),
        width=1280, height=820,
        min_size=(1000, 660),
        background_color="#090b10",
    )
    webview.start(debug=False)

if __name__ == "__main__":
    main()
