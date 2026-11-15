"""產生 PWA 圖示（原創圖樣：資料表卡片 + 紙飛機）。

用法：python tools/make_icons.py
輸出：pwa/icon-192.png、icon-512.png、icon-maskable-512.png、apple-touch-icon.png、favicon.png
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "pwa"
BG = (14, 124, 102)       # 深青綠
CARD = (255, 255, 255)
LINE = (190, 226, 216)
ACCENT = (255, 183, 3)    # 琥珀黃


def draw_icon(size: int = 1024, content_scale: float = 1.0, rounded: bool = True) -> Image.Image:
    """content_scale<1 時內容縮小（maskable 安全區）；rounded=False 為滿版方形背景。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=BG)
    else:
        d.rectangle([0, 0, size, size], fill=BG)

    s = size * content_scale
    ox = oy = (size - s) / 2

    def P(x, y):  # 0~1 座標 → 實際像素
        return (ox + x * s, oy + y * s)

    # 資料表卡片
    d.rounded_rectangle([*P(0.18, 0.20), *P(0.70, 0.74)], radius=int(s * 0.06), fill=CARD)
    for i, w in enumerate((0.40, 0.34, 0.40)):
        y = 0.30 + i * 0.13
        d.rounded_rectangle([*P(0.24, y), *P(0.24 + w, y + 0.05)], radius=int(s * 0.025), fill=LINE)
    # 紙飛機（投放）
    plane = [P(0.50, 0.62), P(0.90, 0.45), P(0.62, 0.84), P(0.60, 0.70), P(0.50, 0.62)]
    d.polygon(plane, fill=ACCENT)
    d.polygon([P(0.60, 0.70), P(0.90, 0.45), P(0.66, 0.66)], fill=(224, 150, 0))
    return img


def save(img: Image.Image, name: str, px: int, flatten: bool = False) -> None:
    out = img.resize((px, px), Image.LANCZOS)
    if flatten:
        bg = Image.new("RGB", out.size, BG)
        bg.paste(out, mask=out.split()[3])
        out = bg
    out.save(OUT / name, optimize=True)
    print("wrote", OUT / name)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    normal = draw_icon(1024)
    save(normal, "icon-512.png", 512)
    save(normal, "icon-192.png", 192)
    save(normal, "favicon.png", 64)
    save(draw_icon(1024, content_scale=0.72, rounded=False), "icon-maskable-512.png", 512, flatten=True)
    save(draw_icon(1024, content_scale=0.80, rounded=False), "apple-touch-icon.png", 180, flatten=True)
