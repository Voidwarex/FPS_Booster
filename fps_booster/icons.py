"""Process icons: the program's own .exe icon on Windows, a letter badge otherwise.

Everything here returns Pillow images and is safe to call from a worker
thread; the UI wraps the images in CTkImage on the main thread.
"""

from __future__ import annotations

import colorsys
import hashlib
import sys
import threading

from PIL import Image, ImageDraw, ImageFont, ImageOps

ICON_PX = 32  # source resolution; the UI scales it down to the row size

_cache: dict[str, Image.Image] = {}
_lock = threading.Lock()


def get_icon(name: str, exe: str | None = None) -> Image.Image:
    """Icon for a process, cached by executable path (or name if unknown)."""
    key = (exe or name).lower()
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    image = None
    if exe and sys.platform == "win32":
        try:
            image = _windows_exe_icon(exe)
        except Exception:
            image = None  # odd/protected exe: fall back to the letter badge
    if image is None:
        image = letter_icon(name)

    with _lock:
        _cache[key] = image
    return image


# ---- Fallback: coloured rounded square with the first letter -------------


def _font(size: int) -> ImageFont.ImageFont:
    for face in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(face, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def letter_icon(name: str) -> Image.Image:
    # Same name -> same colour every time, so badges are recognisable.
    digest = hashlib.md5(name.lower().encode()).digest()
    hue = digest[0] / 255
    r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.85))
    letter = next((c for c in name if c.isalnum()), "?").upper()

    image = Image.new("RGBA", (ICON_PX, ICON_PX), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, ICON_PX - 1, ICON_PX - 1), radius=8, fill=(r, g, b, 255))
    draw.text(
        (ICON_PX / 2, ICON_PX / 2), letter, fill=(255, 255, 255, 255),
        font=_font(int(ICON_PX * 0.6)), anchor="mm",
    )
    return image


# ---- Windows: pull the icon out of the .exe ------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32")
    _gdi32 = ctypes.WinDLL("gdi32")
    _shell32 = ctypes.WinDLL("shell32")

    class _ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    class _BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", wintypes.LONG),
            ("bmWidth", wintypes.LONG),
            ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", wintypes.LPVOID),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    # Handles are pointer-sized: declare prototypes so 64-bit values aren't truncated.
    _shell32.ExtractIconExW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT,
    ]
    _shell32.ExtractIconExW.restype = wintypes.UINT
    _user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(_ICONINFO)]
    _user32.GetIconInfo.restype = wintypes.BOOL
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.DestroyIcon.restype = wintypes.BOOL
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int
    _gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID]
    _gdi32.GetObjectW.restype = ctypes.c_int
    _gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        wintypes.LPVOID, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
    ]
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL

    def _bitmap_bgra(hdc, hbitmap, width: int, height: int) -> bytes:
        """Read any bitmap back as top-down 32-bit BGRA pixels."""
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # negative = top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(width * height * 4)
        if not _gdi32.GetDIBits(hdc, hbitmap, 0, height, buf, ctypes.byref(info), 0):
            raise OSError("GetDIBits failed")
        return buf.raw

    def _hicon_to_image(hicon) -> Image.Image | None:
        info = _ICONINFO()
        if not _user32.GetIconInfo(hicon, ctypes.byref(info)):
            return None
        try:
            if not info.hbmColor:
                return None  # monochrome icon; the letter badge looks better
            bmp = _BITMAP()
            _gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp))
            width, height = bmp.bmWidth, bmp.bmHeight
            hdc = _user32.GetDC(None)
            try:
                color = _bitmap_bgra(hdc, info.hbmColor, width, height)
                mask = _bitmap_bgra(hdc, info.hbmMask, width, height) if info.hbmMask else None
            finally:
                _user32.ReleaseDC(None, hdc)
        finally:
            if info.hbmColor:
                _gdi32.DeleteObject(info.hbmColor)
            if info.hbmMask:
                _gdi32.DeleteObject(info.hbmMask)

        image = Image.frombuffer("RGBA", (width, height), color, "raw", "BGRA", 0, 1)
        if image.getchannel("A").getextrema() == (0, 0) and mask is not None:
            # Old-style icon without alpha: the AND mask is white where transparent.
            mask_img = Image.frombuffer("RGBA", (width, height), mask, "raw", "BGRA", 0, 1)
            image = image.copy()
            image.putalpha(ImageOps.invert(mask_img.getchannel("R")))
        if image.size != (ICON_PX, ICON_PX):
            image = image.resize((ICON_PX, ICON_PX), Image.LANCZOS)
        return image

    def _windows_exe_icon(path: str) -> Image.Image | None:
        large = wintypes.HICON()
        if not _shell32.ExtractIconExW(path, 0, ctypes.byref(large), None, 1) or not large:
            return None
        try:
            return _hicon_to_image(large)
        finally:
            _user32.DestroyIcon(large)

else:

    def _windows_exe_icon(path: str) -> Image.Image | None:
        return None
