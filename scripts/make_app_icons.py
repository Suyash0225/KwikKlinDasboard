"""Phone par install ke PNG icons — staff app aur owner dashboard dono.

KYUN: Android ka install (WebAPK) aur iOS ka home-screen icon SVG par
bharosa nahi karte; 192 aur 512 ke PNG chahiye. Dono apps ek hi "KK"
nishaan rakhte hain par rang ulte hain — ek hi phone par dono install
hon (owner aksar dono rakhta hai) to pehchan mein galti na ho:
  staff  : kaale (ink) dabbe mein narangi tile  — login screen jaisa
  owner  : poora narangi, safed "KK"

CHALAO (sirf tab jab nishaan badle — PNG repo mein commit hote hain):
    python -m scripts.make_app_icons
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "app" / "static"
INK = "#131820"      # tokens.css --ink: staff boot screen / splash ka rang
BRAND = "#ea580c"    # tokens.css --brand: login wala "KK" ka dabba
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _kk(draw: ImageDraw.ImageDraw, size: int, box: float, fill: str) -> None:
    """Beech mein "KK", `box` hissa chauda."""
    font = ImageFont.truetype(FONT, int(size * box * 0.42))
    draw.text((size / 2, size / 2), "KK", font=font, fill=fill, anchor="mm")


def staff_tile(size: int, *, maskable: bool) -> Image.Image:
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


def owner_tile(size: int, *, maskable: bool) -> Image.Image:
    """Poora narangi, safed KK. Maskable mein nishaan safe zone ke andar."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle((0, 0, size, size), fill=BRAND)
        _kk(d, size, 0.6, "#ffffff")
    else:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size * 0.22, fill=BRAND)
        _kk(d, size, 0.8, "#ffffff")
    return img


def badge(size: int) -> Image.Image:
    """Android notification badge: sirf alpha padha jaata hai — safed
    nishaan, khali background."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _kk(ImageDraw.Draw(img), size, 1.0, "#ffffff")
    return img


def main() -> None:
    files = {
        "staff-icon-192.png": staff_tile(192, maskable=False),
        "staff-icon-512.png": staff_tile(512, maskable=False),
        "staff-icon-maskable-512.png": staff_tile(512, maskable=True),
        # iOS kone khud gol karta hai; transparent kona wahan kaala dikhta
        "staff-apple-touch-180.png": staff_tile(180, maskable=True).convert("RGB"),
        "staff-badge-96.png": badge(96),
        "admin-icon-192.png": owner_tile(192, maskable=False),
        "admin-icon-512.png": owner_tile(512, maskable=False),
        "admin-icon-maskable-512.png": owner_tile(512, maskable=True),
        "admin-apple-touch-180.png": owner_tile(180, maskable=True).convert("RGB"),
    }
    for name, img in files.items():
        img.save(OUT / name, optimize=True)
        print(f"wrote app/static/{name} {img.size[0]}x{img.size[1]}")


if __name__ == "__main__":
    main()
