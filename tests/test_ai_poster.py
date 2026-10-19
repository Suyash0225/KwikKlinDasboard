import base64
import io

from PIL import Image

from app.services import ai_poster


def _png_bytes(size=(300, 300)):
    import random

    rng = random.Random(7)
    image = Image.new("RGB", size)
    image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                   for _ in range(size[0] * size[1])])
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_image_part_extracts_valid_inline_png():
    raw = _png_bytes()
    part = ai_poster._image_part({
        "candidates": [{
            "content": {"parts": [{
                "inlineData": {"mimeType": "image/png", "data": base64.b64encode(raw).decode()}
            }]}
        }]
    })
    assert part is not None
    assert part[0] == "image/png"
    assert part[1] == raw


def test_image_part_rejects_missing_or_oversized_image():
    assert ai_poster._image_part({"candidates": []}) is None
    huge = base64.b64encode(b"x" * (ai_poster.MAX_IMAGE_BYTES + 1)).decode()
    assert ai_poster._image_part({
        "candidates": [{"content": {"parts": [{
            "inline_data": {"mime_type": "image/png", "data": huge}
        }]}}]
    }) is None


def test_composed_poster_uses_square_layout_and_real_logo(tmp_path):
    background = tmp_path / "background.png"
    logo = tmp_path / "logo.png"
    out = tmp_path / "poster.png"
    Image.new("RGB", (500, 300), "#89cbd4").save(background)
    Image.new("RGBA", (80, 80), (10, 35, 70, 255)).save(logo)

    ai_poster.compose_poster(
        background, logo, out, "Garment Care",
        "Give Clothes Thoughtful Care", "Laundry and dry cleaning in Varanasi",
    )

    with Image.open(out) as result:
        assert result.size == (1080, 1080)
        assert result.format == "PNG"



def test_fallback_background_is_square_png(tmp_path):
    path = tmp_path / "fallback.png"
    ai_poster.create_fallback_background(path)
    with Image.open(path) as result:
        assert result.size == (1080, 1080)
        assert result.format == "PNG"
