"""Staff app ke PNG icons — phone par install ke liye.

KYUN: Android ka install (WebAPK) aur iOS ka home-screen icon SVG par
bharosa nahi karte; 192 aur 512 ke PNG chahiye. Ye script wahi "KK"
nishaan banati hai jo /staff/icon.svg aur login screen par hai, taaki
icon har jagah ek jaisa dikhe.

CHALAO (sirf tab jab nishaan badle — PNG repo mein commit hote hain):
    python -m scripts.make_staff_icons
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "app" / "static"
INK = "#131820"      # tokens.css --ink: boot screen / splash ka rang
BRAND = "#ea580c"    # tokens.css --brand: login wala "KK" ka dabba
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _kk(draw: ImageDraw.ImageDraw, size: int, box: float, fill: str) -> None:
    """Beech mein "KK", `box` hissa chauda."""
    font = ImageFont.truetype(FONT, int(size * box * 0.42))
    draw.text((size / 2, size / 2), "KK", font=font, fill=fill, anchor="mm")


def tile(size: int, *, maskable: bool) -> Image.Image:
    """Ink ke upar narangi dabba. Maskable: poora kinare tak bhara, aur
    nishaan 80% "safe zone" ke andar — launcher gol/squircle kaatega."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle((0, 0, size, size), fill=INK)
        inset, radius = size * 0.2, size * 0.12
    else:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size * 0.22, fill=INK)
        inset, radius = size * 0.135, size * 0.18
    d.rounded_rectangle((inset, inset, size - inset, size - inset), radius=radius, fill=BRAND)
    _kk(d, size, 1 - 2 * inset / size, "#ffffff")
    return img


def badge(size: int) -> Image.Image:
    """Android notification badge: sirf alpha padha jaata hai — safed
    nishaan, khali background."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _kk(ImageDraw.Draw(img), size, 1.0, "#ffffff")
    return img


def main() -> None:
    files = {
        "staff-icon-192.png": tile(192, maskable=False),
        "staff-icon-512.png": tile(512, maskable=False),
        "staff-icon-maskable-512.png": tile(512, maskable=True),
        # iOS kone khud gol karta hai; transparent kona wahan kaala dikhta
        "staff-apple-touch-180.png": tile(180, maskable=True).convert("RGB"),
        "staff-badge-96.png": badge(96),
    }
    for name, img in files.items():
        img.save(OUT / name, optimize=True)
        print(f"wrote app/static/{name} {img.size[0]}x{img.size[1]}")


if __name__ == "__main__":
    main()
