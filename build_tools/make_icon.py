"""アプリの絵（assets/vctrans.ico と assets/vctrans.png）を作る。

  python build_tools/make_icon.py

紺の角丸の地に、吹き出しを 2 つ（白に「A」、水色に「あ」）。小さく出ても形がわかるよう、16〜32 px は線を太めに描き直す。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
FONT = "C:/Windows/Fonts/YuGothB.ttc"

BG_TOP, BG_BOTTOM = (31, 58, 104), (18, 32, 61)
WHITE, CYAN = (255, 255, 255), (125, 214, 255)
INK, INK2 = (22, 40, 74), (12, 52, 84)


def bubble(d, box, fill, tail_left, s):
    x0, y0, x1, y1 = box
    r = (y1 - y0) * 0.32
    d.rounded_rectangle(box, radius=r, fill=fill)
    w = (x1 - x0)
    if tail_left:
        tx = x0 + w * 0.22
        d.polygon([(tx, y1 - 2 * s), (tx + w * 0.16, y1 - 2 * s), (tx - w * 0.04, y1 + (y1 - y0) * 0.30)], fill=fill)
    else:
        tx = x1 - w * 0.22
        d.polygon([(tx, y1 - 2 * s), (tx - w * 0.16, y1 - 2 * s), (tx + w * 0.04, y1 + (y1 - y0) * 0.30)], fill=fill)


def draw(size):
    S = 1024
    s = S / 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # 地の縦の濃淡
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=52 * s, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    small = size <= 32
    # 左上: 白い吹き出しに A（相手の英語）
    b1 = (30 * s, 40 * s, 158 * s, 132 * s) if not small else (22 * s, 30 * s, 166 * s, 136 * s)
    bubble(d, b1, WHITE, True, s)
    # 右下: 水色の吹き出しに あ（日本語）
    b2 = (98 * s, 118 * s, 226 * s, 210 * s) if not small else (90 * s, 112 * s, 234 * s, 218 * s)
    d.rounded_rectangle((b2[0] - 6 * s, b2[1] - 6 * s, b2[2] + 6 * s, b2[3] + 6 * s), radius=(b2[3] - b2[1]) * 0.36,
                        fill=BG_BOTTOM)  # 重なりの縁取り
    bubble(d, b2, CYAN, False, s)
    f1 = ImageFont.truetype(FONT, int((b1[3] - b1[1]) * 0.78))
    f2 = ImageFont.truetype(FONT, int((b2[3] - b2[1]) * 0.70))
    for text, box, font, ink, dy in (("A", b1, f1, INK, -0.04), ("あ", b2, f2, INK2, -0.06)):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2 + (box[3] - box[1]) * dy
        d.text((cx, cy), text, font=font, fill=ink, anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(exist_ok=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [draw(n) for n in sizes]
    imgs[-1].save(OUT / "vctrans.ico", sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
    draw(512).save(OUT / "vctrans.png")
    sheet = Image.new("RGBA", (sum(sizes) + 10 * len(sizes), 256), (240, 240, 240, 255))
    x = 0
    for im in imgs:
        sheet.paste(im, (x, 0), im)
        x += im.width + 10
    sheet.save(ROOT / "build_tools" / "icon_preview.png")
    print("wrote", OUT / "vctrans.ico")


if __name__ == "__main__":
    main()
