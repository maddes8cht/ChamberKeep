"""Generate colored logo PNGs from logo-light.svg.

Dev-time tool only - run it whenever the SVG changes:

    pip install resvg-py Pillow
    python generate_icons.py

The generated PNGs are committed so the runtime script only depends on
pystray and Pillow. The SVG is monochrome black with per-path opacity
(shading); recoloring replaces the black fill/stroke with the target
color while keeping the opacity layers.
"""

import io
import os

import resvg_py
from PIL import Image

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SVG_PATH = os.path.join(SCRIPT_DIR, "logo-light.svg")
OUTPUT_SIZE = 256


def build_variants():
    """Return a list of (filename, fill_color, stroke_color).

    fill/stroke of None keeps the original black values.
    """
    variants = [
        ("logo-green.png", "#00cc00", "#00cc00"),
        ("logo-red.png", "#cc0000", "#cc0000"),
        ("logo-yellow.png", "#cccc00", "#cccc00"),
        ("logo-blue.png", "#0066cc", "#0066cc"),
        ("logo-original.png", None, None),
        ("logo-inverted.png", "#ffffff", "#ffffff"),
    ]
    return variants


def recolor(svg, fill, stroke):
    if fill:
        svg = svg.replace('fill="black"', 'fill="%s"' % fill)
    if stroke:
        svg = svg.replace('stroke="black"', 'stroke="%s"' % stroke)
    return svg


def main():
    with open(SVG_PATH, encoding="utf-8") as f:
        svg = f.read()
    for filename, fill, stroke in build_variants():
        source = recolor(svg, fill, stroke)
        png_bytes = resvg_py.svg_to_bytes(
            svg_string=source, width=OUTPUT_SIZE, height=OUTPUT_SIZE
        )
        image = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        out_path = os.path.join(SCRIPT_DIR, filename)
        image.save(out_path)
        print("wrote %s (%dx%d)" % (out_path, image.width, image.height))


if __name__ == "__main__":
    main()
