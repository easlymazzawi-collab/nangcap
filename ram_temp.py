"""RAM-only temp — bat buoc RAM disk/tmpfs, KHONG fallback SSD khi ram_mode."""
import os
import shutil
import string
import subprocess
import sys
import tempfile
import threading

_cache = {}
_lock = threading.Lock()
_created_mounts = []  # o ImDisk tool tu tao (de detach khi can)

_DRIVE_RAMDISK = 6
_DRIVE_REMOVABLE = 2

RAM_REQUIRED_MSG = (
    "RAM mode BAT BUOC co RAM disk. Windows khong co san — cai ImDisk Toolkit "
    "(free) roi chay lai, hoac tat RAM mode. Tool KHONG ghi temp len SSD."
)


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
        return "", 0, 0
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
            return "", 0, 0
        total = ctypes.c_ulonglong(0)
        free = ctypes.c_ulonglong(0)
        k.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
        return vol.value, int(total.value or 0), int(free.value or 0)
    except Exception:
        return "", 0, 0


def _linux_ram_paths():
    for p in ("/dev/shm", "/run/shm"):
        if os.path.isdir(p) and os.access(p, os.W_OK):
            return p
    return None


def _free_drive_letter():
    if sys.platform != "win32":
        return None
    for letter in "RTUVWXYZ":
        if not os.path.exists(f"{letter}:\\"):
            return letter
    return None


def _find_imdisk():
    exe = shutil.which("imdisk") or shutil.which("imdisk.exe")
    if exe and os.path.isfile(exe):
        return exe
    for p in (
        r"C:\Windows\System32\imdisk.exe",
        r"C:\Program Files\ImDisk\imdisk.exe",
        r"C:\Program Files (x86)\ImDisk\imdisk.exe",
    ):
        if os.path.isfile(p):
            return p
    return None


def try_create_imdisk(size_mb=None):
    """Thu tao RAM disk bang ImDisk (can quyen Admin). Tra (path, label) hoac (None, err)."""
    if sys.platform != "win32":
        return None, "Chi Windows moi tu tao ImDisk"
    imdisk = _find_imdisk()
    if not imdisk:
        return None, "Khong tim thay imdisk.exe — cai ImDisk Toolkit"
    letter = _free_drive_letter()
    if not letter:
        return None, "Het o drive trong (T-Z)"
    mb = int(size_mb or 4096)
    mount = f"{letter}:"
    cmd = [
        imdisk, "-a", "-t", "vm", "-s", f"{mb}M", "-m", mount,
        "-p", "/fs:ntfs /q /y",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return None, f"ImDisk chay loi: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-300:]
        if "access" in tail.lower() or "denied" in tail.lower():
            return None, "ImDisk can quyen Admin — mo app Run as Administrator"
        return None, f"ImDisk that bai: {tail or 'rc=' + str(r.returncode)}"
    sub = os.path.join(f"{letter}:\\", "GPUWM")
    try:
        os.makedirs(sub, exist_ok=True)
    except OSError as e:
        return None, f"Khong tao duoc thu muc tren RAM disk: {e}"
    with _lock:
        if mount not in _created_mounts:
            _created_mounts.append(mount)
    return sub, f"RAM disk vua tao ImDisk ({mount}, {mb}MB)"


def scan_ram_volumes():
    """Tim volume RAM that (Windows + Linux)."""
    found = []

    if sys.platform != "win32":
        p = _linux_ram_paths()
        if p:
            found.append((p, 0, "tmpfs", 100))
        return [(a, b, c) for a, b, c, _ in found]

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
        label, total, free = _win_volume_info(root)
        label_l = (label or "").lower()
        score = 0

        if dtype == _DRIVE_RAMDISK:
            score = 100
        elif any(k in label_l for k in ("ram", "imdisk", "osf", "softperfect", "ramdrive")):
            score = 90
        elif dtype != _DRIVE_REMOVABLE and total and total <= 16 * 1024 ** 3 and letter != "C":
            if letter in "RTUVWXYZ" and free > 512 * 1024 ** 2:
                score = 40

        if score > 0:
            found.append((root.rstrip("\\"), total, label or f"type{dtype}", score))

    found.sort(key=lambda x: (-x[3], -x[1]))
    return [(a, b, c) for a, b, c, _ in found]


def get_ram_status(cfg=None, probe_create=False):
    """
    Kiem tra RAM disk co dung duoc khong.
    Tra dict de log/UI hien thi.
    """
    cfg = cfg or {}
    imdisk = _find_imdisk() if sys.platform == "win32" else None
    volumes = scan_ram_volumes()

    if volumes:
        path = os.path.join(volumes[0][0], "GPUWM")
        return {
            "ok": True,
            "available": True,
            "path": path,
            "volumes": [v[0] for v in volumes],
            "imdisk": bool(imdisk),
            "message": f"Co RAM disk: {volumes[0][0]} ({volumes[0][2]})",
            "hint": "Bat 'RAM → Up → Xóa' de encode output len RAM",
        }

    if sys.platform != "win32":
        shm = _linux_ram_paths()
        if shm:
            return {
                "ok": True, "available": True, "path": os.path.join(shm, "gpuwm"),
                "volumes": [shm], "imdisk": False,
                "message": f"Co RAM tmpfs: {shm}",
                "hint": "Bat RAM mode de dung",
            }
        return {
            "ok": False, "available": False, "path": None, "volumes": [],
            "imdisk": False,
            "message": "Khong co /dev/shm",
            "hint": "Linux can tmpfs",
        }

    if probe_create and imdisk:
        path, msg = try_create_imdisk(cfg.get("ram_disk_mb"))
        if path:
            return {
                "ok": True, "available": True, "path": path, "volumes": [],
                "imdisk": True,
                "message": msg,
                "hint": "RAM disk vua duoc tao tu dong",
            }
        return {
            "ok": False, "available": False, "path": None, "volumes": [],
            "imdisk": True,
            "message": msg,
            "hint": "Chay app Run as Administrator de tu tao RAM disk",
        }

    if imdisk:
        return {
            "ok": False, "available": False, "path": None, "volumes": [],
            "imdisk": True,
            "message": "Co ImDisk nhung chua co o RAM — can Run as Administrator",
            "hint": "Chuot phai app → Run as Administrator, bat RAM mode, chay lai",
        }

    return {
        "ok": False, "available": False, "path": None, "volumes": [],
        "imdisk": False,
        "message": "Khong co RAM disk / ImDisk",
        "hint": "Cai ImDisk Toolkit (free) + chay Admin, hoac TAT 'RAM → Up → Xóa'",
    }


def resolve_temp_base(cfg=None, ram_mode=False):
    """
    ram_mode=True: CHI RAM — tra (path, mo_ta) hoac (None, loi). Khong fallback SSD.
    ram_mode=False: temp thuong (co the dung %TEMP%).
    """
    cfg = cfg or {}
    override = (cfg.get("temp_dir") or "").strip()
    cache_key = (ram_mode, override, cfg.get("ram_disk_mb"))

    with _lock:
        if cache_key in _cache:
            return _cache[cache_key]

    if override and ram_mode:
        try:
            os.makedirs(override, exist_ok=True)
            if os.path.isdir(override):
                result = (override, f"RAM override: {override}")
                with _lock:
                    _cache[cache_key] = result
                return result
        except OSError as e:
            result = (None, f"Temp override loi: {e}")
            with _lock:
                _cache[cache_key] = result
            return result

    if ram_mode:
        # Linux tmpfs
        if sys.platform != "win32":
            shm = _linux_ram_paths()
            if shm:
                sub = os.path.join(shm, "gpuwm")
                os.makedirs(sub, exist_ok=True)
                result = (sub, f"RAM tmpfs ({sub})")
                with _lock:
                    _cache[cache_key] = result
                return result
            result = (None, "Khong co /dev/shm — RAM mode that bai")
            with _lock:
                _cache[cache_key] = result
            return result

        # Windows: quet RAM disk co san
        volumes = scan_ram_volumes()
        if volumes:
            path = volumes[0][0]
            sub = os.path.join(path, "GPUWM")
            os.makedirs(sub, exist_ok=True)
            lbl = volumes[0][2]
            result = (sub, f"RAM disk ({sub}, {lbl})")
            with _lock:
                _cache[cache_key] = result
            return result

        # Thu tu tao ImDisk
        path, lbl = try_create_imdisk(cfg.get("ram_disk_mb"))
        if path:
            result = (path, lbl)
            with _lock:
                _cache[cache_key] = result
            return result

        err = lbl or RAM_REQUIRED_MSG
        result = (None, err)
        with _lock:
            _cache[cache_key] = result
        return result

    # Che do thuong (khong bat buoc RAM)
    base = os.path.join(tempfile.gettempdir(), "GPUWM_work")
    os.makedirs(base, exist_ok=True)
    result = (base, f"temp ({base})")
    with _lock:
        _cache[cache_key] = result
    return result


def require_ram_temp(cfg=None):
    """Tra (path, label) hoac raise RuntimeError."""
    path, msg = resolve_temp_base(cfg, ram_mode=True)
    if not path:
        raise RuntimeError(msg or RAM_REQUIRED_MSG)
    return path, msg


def clear_cache():
    with _lock:
        _cache.clear()


def detach_created_imdisks():
    """Go ImDisk tool da tu tao (optional khi tat app)."""
    if sys.platform != "win32":
        return
    imdisk = _find_imdisk()
    if not imdisk:
        return
    with _lock:
        mounts = list(_created_mounts)
        _created_mounts.clear()
    for m in mounts:
        try:
            subprocess.run(
                [imdisk, "-d", "-m", m],
                capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
