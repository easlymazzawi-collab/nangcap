"""Tu dong chon thu muc temp — RAM disk / tmpfs neu co, khong can user nhap R:\\."""
import os
import string
import sys
import tempfile
import threading

_cache = {}
_lock = threading.Lock()

# Windows GetDriveType
_DRIVE_RAMDISK = 6
_DRIVE_REMOVABLE = 2


def _win_drive_type(root):
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except Exception:
        return None


def _win_volume_info(root):
    if sys.platform != "win32":
        return "", 0
    try:
        import ctypes
        from ctypes import wintypes

        vol = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_len = wintypes.DWORD()
        flags = wintypes.DWORD()
        k = ctypes.windll.kernel32
        if not k.GetVolumeInformationW(
            root, vol, 261, ctypes.byref(serial),
            ctypes.byref(max_len), ctypes.byref(flags), fs, 261,
        ):
            return "", 0
        total = ctypes.c_ulonglong(0)
        free = ctypes.c_ulonglong(0)
        k.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
        return vol.value, int(total.value or 0)
    except Exception:
        return "", 0


def _linux_ram_paths():
    for p in ("/dev/shm", "/run/shm"):
        if os.path.isdir(p) and os.access(p, os.W_OK):
            return p
    return None


def scan_ram_volumes():
    """Tim o/volume co kha nang la RAM (Windows + Linux)."""
    found = []

    if sys.platform != "win32":
        p = _linux_ram_paths()
        if p:
            found.append((p, 0, "tmpfs"))
        return found

    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            if not os.access(root, os.W_OK):
                continue
        except OSError:
            continue

        dtype = _win_drive_type(root)
        label, total = _win_volume_info(root)
        label_l = (label or "").lower()
        score = 0

        if dtype == _DRIVE_RAMDISK:
            score = 100
        elif any(k in label_l for k in ("ram", "imdisk", "osf", "softperfect")):
            score = 90
        elif letter in "RTXZ" and total and total <= 32 * 1024 ** 3 and letter != "C":
            # ImDisk thuong gan o nho, type co the la FIXED
            if dtype != _DRIVE_REMOVABLE and total <= 16 * 1024 ** 3:
                score = 50

        if score > 0:
            found.append((root.rstrip("\\"), total, label or f"type{dtype}", score))

    found.sort(key=lambda x: (-x[3], -x[1]))
    return [(a, b, c) for a, b, c, _ in found]


def resolve_temp_base(cfg=None, ram_mode=False):
    """
    Tu chon noi ghi file tam.
    Tra (path, mo_ta) — user khong can cau hinh duong dan.
    """
    cfg = cfg or {}
    override = (cfg.get("temp_dir") or "").strip()
    cache_key = (ram_mode, override)

    with _lock:
        if cache_key in _cache:
            return _cache[cache_key]

    if override:
        try:
            os.makedirs(override, exist_ok=True)
            if os.path.isdir(override):
                result = (override, f"temp (override): {override}")
                with _lock:
                    _cache[cache_key] = result
                return result
        except OSError:
            pass

    if ram_mode:
        if sys.platform != "win32":
            shm = _linux_ram_paths()
            if shm:
                sub = os.path.join(shm, "gpuwm")
                os.makedirs(sub, exist_ok=True)
                result = (sub, f"RAM tmpfs tu dong ({sub})")
                with _lock:
                    _cache[cache_key] = result
                return result

        volumes = scan_ram_volumes()
        if volumes:
            path = volumes[0][0]
            sub = os.path.join(path, "GPUWM")
            os.makedirs(sub, exist_ok=True)
            lbl = volumes[0][2]
            result = (sub, f"RAM disk tu dong ({sub}, {lbl})")
            with _lock:
                _cache[cache_key] = result
            return result

    # Fallback: temp he thong — file xoa ngay sau upload
    base = os.path.join(tempfile.gettempdir(), "GPUWM_ram")
    os.makedirs(base, exist_ok=True)
    result = (
        base,
        f"temp tu dong ({base}) — khong luu output, xoa sau up",
    )
    with _lock:
        _cache[cache_key] = result
    return result


def clear_cache():
    with _lock:
        _cache.clear()
