"""Generate the extension icons.

Kept as a script so the artwork is reproducible rather than a binary blob in
version control. Requires Pillow:

    python3 tools/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent.parent / "extension" / "icons"
ACCENT = (214, 59, 47, 255)
WHITE = (255, 255, 255, 255)
SHADOW = (0, 0, 0, 40)


def rounded(size: int, radius_ratio: float = 0.22) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = size * radius_ratio
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=ACCENT)
    return image


def draw_icon(size: int) -> Image.Image:
    """A play triangle with a download arrow: 'grab this video'."""
    image = rounded(size)
    draw = ImageDraw.Draw(image)
    unit = size / 48.0  # everything below is expressed in 48px units

    # Play triangle, upper area.
    triangle = [
        (17 * unit, 9 * unit),
        (17 * unit, 25 * unit),
        (30 * unit, 17 * unit),
    ]
    draw.polygon(triangle, fill=WHITE)

    # Download arrow: shaft plus head, lower area.
    shaft = (21.5 * unit, 26 * unit, 26.5 * unit, 37 * unit)
    draw.rounded_rectangle(shaft, radius=2.5 * unit, fill=WHITE)
    draw.polygon(
        [
            (17 * unit, 34 * unit),
            (31 * unit, 34 * unit),
            (24 * unit, 43 * unit),
        ],
        fill=WHITE,
    )

    # Baseline the arrow lands on.
    draw.rounded_rectangle(
        (13 * unit, 43 * unit, 35 * unit, 45 * unit), radius=1 * unit, fill=WHITE
    )
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (48, 96, 128):
        icon = draw_icon(size)
        icon.save(OUT / f"icon-{size}.png")
        print(f"wrote {OUT / f'icon-{size}.png'}")


if __name__ == "__main__":
    main()
