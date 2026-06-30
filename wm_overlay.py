"""Build full-frame RGBA watermark overlay for GPU Direct pipeline."""
import hashlib
import json
import os
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False

_overlay_cache = {}


def _default_font():
    if os.name == "nt":
        return r"C:\Windows\Fonts\arial.ttf"
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _pil_font(size, bold=False):
    if not PIL_OK:
        return None
    candidates = (
        [r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
         r"C:\Windows\Fonts\segoeui.ttf"]
        if os.name == "nt" else
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


def _parse_color(color, default_alpha=255):
    s = (color or "white").strip()
    alpha = default_alpha
    if "@" in s:
        s, a = s.rsplit("@", 1)
        try:
            alpha = int(float(a) * 255)
        except Exception:
            pass
    named = {"white": (255, 255, 255), "black": (0, 0, 0),
             "red": (255, 0, 0), "yellow": (255, 255, 0)}
    s = s.lower()
    if s in named:
        r, g, b = named[s]
    elif s.startswith("#"):
        h = s.lstrip("#")
        r, g, b = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    else:
        r, g, b = 255, 255, 255
    return (r, g, b, alpha)


def _paste_rgba(canvas, img_path, x, y, scale_w=None, opacity=1.0):
    if not img_path or not os.path.isfile(img_path):
        return
    im = Image.open(img_path).convert("RGBA")
    if scale_w and scale_w > 0:
        ratio = scale_w / im.width
        nh = max(1, int(im.height * ratio))
        im = im.resize((int(scale_w), nh), Image.Resampling.LANCZOS)
    if opacity < 1.0:
        r, g, b, a = im.split()
        a = a.point(lambda p: int(p * opacity))
        im = Image.merge("RGBA", (r, g, b, a))
    canvas.paste(im, (int(x), int(y)), im)


def _draw_text(canvas, draw, text, size, color, x, y, font_path):
    text = (text or "").strip()
    if not text:
        return
    try:
        font = ImageFont.truetype(font_path, int(size))
    except Exception:
        font = _pil_font(int(size))
        if font is None:
            return
    fill = _parse_color(color)
    draw.text((int(x), int(y)), text, font=font, fill=fill)


def build_overlay_rgba(cfg, width, height):
    """Return HxWx4 uint8 RGBA overlay (transparent bg) for full video frame."""
    if not PIL_OK:
        return None, "Can Pillow: pip install Pillow"

    width = int(width) - (int(width) % 2)
    height = int(height) - (int(height) % 2)
    key = hashlib.md5(
        json_dumps_cfg(cfg, width, height).encode("utf-8")
    ).hexdigest()
    if key in _overlay_cache:
        return _overlay_cache[key].copy(), ""

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font_p = cfg.get("font_file") or _default_font()
    mx = int(cfg.get("logo_margin_x", 15))
    my = int(cfg.get("logo_margin_y", 15))

    if os.path.isfile(cfg.get("logo_image", "")):
        _paste_rgba(canvas, cfg["logo_image"], mx, my,
                    cfg.get("logo_scale_w", 160), cfg.get("logo_opacity", 0.7))

    corners = [
        ("tl", "text_top_left", "top_left_size", "top_left_color",
         mx, my),
        ("tr", "text_top_right", "top_right_size", "top_right_color",
         None, my),
        ("bl", "text_bottom_left", "bottom_left_size", "bottom_left_color",
         mx, None),
        ("br", "text_bottom_right", "bottom_right_size", "bottom_right_color",
         None, None),
    ]
    for corner, tk, sk, ck, cx, cy in corners:
        lp = cfg.get(f"logo_{corner}", "")
        if cfg.get(f"enable_{corner}") and lp and os.path.isfile(lp):
            wx = cx if cx is not None else width - mx - cfg.get(f"logo_{corner}_w", 120)
            wy = cy if cy is not None else height - my - cfg.get(f"logo_{corner}_w", 120)
            _paste_rgba(canvas, lp, wx, wy,
                        cfg.get(f"logo_{corner}_w", 120), cfg.get(f"logo_{corner}_op", 0.7))
            continue
        txt = cfg.get(tk, "")
        if not txt:
            continue
        sz = cfg.get(sk, 22)
        col = cfg.get(ck, "white")
        tmp = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        try:
            font = ImageFont.truetype(font_p, int(sz))
        except Exception:
            font = _pil_font(int(sz))
        if font is None:
            continue
        bbox = td.textbbox((0, 0), txt, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = 10 if cx is not None else width - tw - 10
        ty = 10 if cy is not None else height - th - 10
        _draw_text(canvas, draw, txt, sz, col, tx, ty, font_p)

    if cfg.get("enable_center") and (cfg.get("center_text") or "").strip():
        ct = cfg["center_text"].strip()
        cs = int(cfg.get("center_size", 28))
        op = float(cfg.get("center_opacity", 0.15))
        try:
            font = ImageFont.truetype(font_p, cs)
        except Exception:
            font = _pil_font(cs)
        if font:
            bbox = draw.textbbox((0, 0), ct, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = int((width - tw) / 4)
            y = int((height - th) / 2)
            draw.text((x, y), ct, font=font,
                      fill=(255, 255, 255, int(op * 255)))

    import numpy as np
    arr = np.array(canvas, dtype=np.uint8)
    _overlay_cache[key] = arr
    cache_dir = os.path.join(tempfile.gettempdir(), "gpu_wm_text")
    os.makedirs(cache_dir, exist_ok=True)
    canvas.save(os.path.join(cache_dir, f"gpu_direct_{key[:12]}.png"))
    return arr.copy(), ""


def json_dumps_cfg(cfg, width, height):
    keys = (
        "logo_image", "logo_scale_w", "logo_opacity", "logo_margin_x", "logo_margin_y",
        "enable_tl", "logo_tl", "logo_tl_w", "logo_tl_op",
        "enable_tr", "logo_tr", "logo_tr_w", "logo_tr_op",
        "enable_bl", "logo_bl", "logo_bl_w", "logo_bl_op",
        "enable_br", "logo_br", "logo_br_w", "logo_br_op",
        "text_top_left", "text_top_right", "text_bottom_left", "text_bottom_right",
        "top_left_size", "top_right_size", "bottom_left_size", "bottom_right_size",
        "top_left_color", "top_right_color", "bottom_left_color", "bottom_right_color",
        "enable_center", "center_text", "center_size", "center_opacity", "font_file",
    )
    payload = {k: cfg.get(k) for k in keys}
    payload["_wh"] = (width, height)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def clear_overlay_cache():
    global _overlay_cache
    _overlay_cache = {}
