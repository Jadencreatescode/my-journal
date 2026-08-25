#!/usr/bin/env python3
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1280, 640
BACKGROUND = "#0d1315"
PANEL = "#131c1f"
CYAN = "#7bc2cb"
CREAM = "#f3eee3"
MUTED = "#b8b8ad"
GREEN = "#7fb89a"
ORANGE = "#be9678"
BORDER = "#314044"

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "assets" / "my-journal-social-preview.png"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SANS_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, body: str, accent: str) -> None:
    draw.rounded_rectangle(box, radius=12, fill=PANEL, outline=BORDER, width=2)
    x1, y1, _, _ = box
    draw.rectangle((x1, y1, x1 + 6, box[3]), fill=accent)
    draw.text((x1 + 24, y1 + 18), title, font=font(SANS_BOLD, 17), fill=CREAM)
    draw.text((x1 + 24, y1 + 47), body, font=font(SANS, 14), fill=MUTED)


def build() -> Path:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    draw.text((64, 50), "MY JOURNAL FOR HERMES", font=font(SANS_BOLD, 19), fill=CYAN)
    draw.text((64, 108), "See how the work", font=font(SERIF, 62), fill=CREAM)
    draw.text((64, 178), "actually moved forward.", font=font(SERIF, 62), fill=CREAM)
    draw.text((68, 274), "Approved conversations become validated daily Markdown.", font=font(SANS, 24), fill=MUTED)
    draw.text((68, 316), "Progression history, separate from agent memory and raw chat exports.", font=font(SANS, 19), fill=MUTED)

    card(draw, (66, 398, 352, 494), "DECISIONS", "What direction was chosen", CYAN)
    card(draw, (370, 398, 656, 494), "CHANGES", "What implementation moved", ORANGE)
    card(draw, (674, 398, 960, 494), "VERIFICATION", "What was independently checked", GREEN)
    card(draw, (978, 398, 1214, 494), "OPEN WORK", "What still needs attention", MUTED)

    draw.line((66, 548, 1214, 548), fill=BORDER, width=2)
    draw.text((66, 570), "Privacy controlled", font=font(SANS_BOLD, 16), fill=CREAM)
    draw.text((252, 570), "•", font=font(SANS, 16), fill=CYAN)
    draw.text((279, 570), "Evidence backed", font=font(SANS_BOLD, 16), fill=CREAM)
    draw.text((443, 570), "•", font=font(SANS, 16), fill=CYAN)
    draw.text((470, 570), "Portable Markdown", font=font(SANS_BOLD, 16), fill=CREAM)
    draw.text((1020, 570), "JADENCREATESCODE", font=font(SANS_BOLD, 13), fill=CYAN)

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    image.save(TARGET, format="PNG", optimize=True)
    return TARGET


if __name__ == "__main__":
    print(build())
