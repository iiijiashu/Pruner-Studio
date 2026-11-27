"""Render the Windows icon family from one approved image."""
import argparse
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(source, output):
    with Image.open(source) as image:
        original = ImageOps.exif_transpose(image).convert("RGBA")
    side = max(original.size)
    square = Image.new("RGBA", (side, side))
    square.paste(original, ((side - original.width) // 2, (side - original.height) // 2))
    frames = {}
    for size in (*SIZES, 512):
        frame = square.resize((size, size), Image.Resampling.LANCZOS)
        if size <= 32:
            frame = frame.filter(ImageFilter.UnsharpMask(radius=0.6, percent=85, threshold=2))
        frames[size] = frame
    output.mkdir(parents=True, exist_ok=True)
    frames[512].save(output / "icon.png")
    frames[256].save(output / "icon.ico", sizes=[(s, s) for s in SIZES],
                     append_images=[frames[s] for s in SIZES[:-1]])
    for size in (32, 64):
        frames[size].save(output / f"favicon-{size}.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "web/assets")
    args = parser.parse_args()
    render(args.source, args.output_dir)
