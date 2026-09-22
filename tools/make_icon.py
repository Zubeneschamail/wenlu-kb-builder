"""Generate Builder's vector-based icon. Developer tool; requires Pillow only here."""
from pathlib import Path
import re

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'assets'
SCALE = 8
SIZE = 128 * SCALE
LEFT = 'M 26 35 C 38 31 52 33 64 41 L 64 99 C 52 91 38 87 26 92 C 24 93 22 91 22 88 L 22 41 C 22 38 23 36 26 35 Z'
RIGHT = 'M 102 35 C 90 31 76 33 64 41 L 64 99 C 76 91 90 87 102 92 C 104 93 106 91 106 88 L 106 41 C 106 38 105 36 102 35 Z'


def points(data):
    tokens = iter(re.findall(r'[MLCZ]|-?\d+(?:\.\d+)?', data))
    result = []
    current = (0, 0)
    def pair():
        return float(next(tokens)), float(next(tokens))
    for command in tokens:
        if command in ('M', 'L'):
            current = pair()
            result.append(current)
        elif command == 'C':
            a, b, end = pair(), pair(), pair()
            for step in range(1, 33):
                t, u = step / 32, 1 - step / 32
                result.append(tuple(u**3 * current[i] + 3*u*u*t*a[i] + 3*u*t*t*b[i] + t**3*end[i] for i in (0, 1)))
            current = end
    return [(round(x*SCALE), round(y*SCALE)) for x, y in result]


def line(draw, coords, color, width):
    converted = [(int(x*SCALE), int(y*SCALE)) for x, y in coords]
    draw.line(converted, fill=color, width=int(width*SCALE), joint='curve')
    radius = width*SCALE/2
    for x, y in (converted[0], converted[-1]):
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)


def main():
    ASSETS.mkdir(exist_ok=True)
    image = Image.new('RGBA', (SIZE, SIZE))
    gradient = Image.new('RGBA', (SIZE, SIZE))
    gd = ImageDraw.Draw(gradient)
    top, bottom = (130, 112, 245), (78, 67, 191)
    for y in range(SIZE):
        ratio = y/(SIZE-1)
        color = tuple(round(a+(b-a)*ratio) for a, b in zip(top, bottom)) + (255,)
        gd.line((0, y, SIZE, y), fill=color)
    mask = Image.new('L', image.size)
    ImageDraw.Draw(mask).rounded_rectangle((4*SCALE, 4*SCALE, 124*SCALE, 124*SCALE), radius=27*SCALE, fill=255)
    image.paste(gradient, (0, 0), mask)
    draw = ImageDraw.Draw(image)
    # The spine and two sloped pages remain legible at the 16 px taskbar size.
    for path in (LEFT, RIGHT):
        shadow = [(x, y+3*SCALE) for x, y in points(path)]
        draw.polygon(shadow, fill='#4438A7')
    draw.polygon(points(LEFT), fill='#FFFFFF')
    draw.polygon(points(RIGHT), fill='#EAE7FF')
    line(draw, [(64, 42), (64, 96)], '#B1A7EE', 2)
    for coords in ([(33, 48), (44, 49), (53, 52)], [(33, 60), (44, 61), (53, 64)],
                   [(75, 52), (84, 49), (95, 48)], [(75, 64), (84, 61), (95, 60)]):
        line(draw, coords, '#9286D8', 3.5)
    # Small spine accent suggests a bookmark without adding a separate badge.
    draw.polygon([(82*SCALE, 34*SCALE), (91*SCALE, 33*SCALE), (91*SCALE, 47*SCALE),
                  (86.5*SCALE, 44*SCALE), (82*SCALE, 48*SCALE)], fill='#5D50C3')
    for size in (24, 32, 256):
        image.resize((size, size), Image.Resampling.LANCZOS).save(ASSETS / f'knowledge-{size}.png')
    image.resize((256, 256), Image.Resampling.LANCZOS).save(ASSETS / 'knowledge.ico',
        sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" fill="none">
  <title>闻录知识库 — 独立书本标识</title>
  <defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="128" gradientUnits="userSpaceOnUse"><stop stop-color="#8270F5"/><stop offset="1" stop-color="#4E43BF"/></linearGradient></defs>
  <rect x="4" y="4" width="120" height="120" rx="27" fill="url(#bg)"/>
  <g transform="translate(0 3)" fill="#4438A7"><path d="{LEFT}"/><path d="{RIGHT}"/></g>
  <path d="{LEFT}" fill="white"/>
  <path d="{RIGHT}" fill="#EAE7FF"/>
  <path d="M64 42V96" stroke="#B1A7EE" stroke-width="2" stroke-linecap="round"/>
  <path d="M33 48L44 49L53 52M33 60L44 61L53 64M75 52L84 49L95 48M75 64L84 61L95 60" stroke="#9286D8" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M82 34L91 33V47L86.5 44L82 48Z" fill="#5D50C3"/>
</svg>
'''
    (ASSETS / 'knowledge.svg').write_text(svg, encoding='utf-8')
    print('Generated knowledge icon: 16–256 px ICO, PNG and editable SVG.')


if __name__ == '__main__':
    main()
