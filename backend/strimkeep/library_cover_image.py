"""Render the 16:9 primary image that Emby displays for a library."""
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps


SIZE = (1280, 720)
MAX_IMAGE_BYTES = 2 * 1024 * 1024
CoverLayout = Literal['collage', 'grid', 'hero', 'filmstrip', 'stack', 'magazine', 'panorama', 'minimal']
COVER_LAYOUTS = {
    'collage': ('斜向拼贴', 9),
    'grid': ('整齐网格', 9),
    'hero': ('主海报', 1),
    'filmstrip': ('胶片长廊', 6),
    'stack': ('三联叠影', 3),
    'magazine': ('杂志拼版', 7),
    'panorama': ('全幅海报', 1),
    'minimal': ('极简标题', 0),
}
FONTS = (
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    'C:/Windows/Fonts/msyh.ttc',
)


def open_image(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('图片超过 2 MB。')
    try:
        with Image.open(BytesIO(data), formats=('JPEG', 'PNG', 'WEBP')) as source:
            if source.width * source.height > 16_000_000:
                raise ValueError('图片尺寸过大。')
            return ImageOps.exif_transpose(source).convert('RGB')
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('请使用有效的 JPG、PNG 或 WebP 图片。') from exc


def jpeg(image):
    buffer = BytesIO()
    image.save(buffer, 'JPEG', quality=87, optimize=True)
    return buffer.getvalue()


def fit_image(image, size):
    return ImageOps.fit(image, size, Image.Resampling.LANCZOS)


def normalize_upload(data):
    return jpeg(fit_image(open_image(data), SIZE))


def _font(size):
    path = next((name for name in FONTS if Path(name).is_file()), None)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def _title_lines(text, font, width, max_lines=2):
    lines = ['']
    for char in text:
        if font.getlength(lines[-1] + char) <= width:
            lines[-1] += char
        elif len(lines) < max_lines:
            lines.append(char)
        else:
            while lines[-1] and font.getlength(lines[-1] + '…') > width:
                lines[-1] = lines[-1][:-1]
            lines[-1] += '…'
            break
    return lines


def _classic_cover(title, subtitle, color, layout, posters):
    accent = ImageColor.getrgb(color)
    end = tuple(round(v * .58) for v in accent)
    shade = Image.linear_gradient('L').rotate(90).resize(SIZE)
    canvas = Image.composite(Image.new('RGB', SIZE, end), Image.new('RGB', SIZE, '#101725'), shade).convert('RGBA')
    if posters:
        grid = layout == 'grid'
        tiles = Image.new('RGBA', (540, 648) if grid else (790, 830))
        width, height = (138, 207) if grid else (160, 244)
        mask = Image.new('L', (width, height))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=14, fill=255)
        for i, blob in enumerate(posters):
            row, column = divmod(i, 3)
            x, y = (column * 153, row * 219) if grid else (94 + column * 178 + row * 40, 22 + row * 258)
            tiles.paste(fit_image(open_image(blob), mask.size), (x, y), mask)
        if not grid:
            tiles = tiles.rotate(10, resample=Image.Resampling.BICUBIC, expand=True)
        canvas.alpha_composite(tiles, (724, 38) if grid else (452, -102))
    else:
        artwork = ImageDraw.Draw(canvas)
        artwork.rounded_rectangle((786, 154, 1160, 566), radius=45, outline=accent, width=4)
        artwork.rounded_rectangle((824, 192, 1122, 528), radius=32, outline=end, width=3)

    draw = ImageDraw.Draw(canvas)
    title_font = _font(69)
    lines = _title_lines(title, title_font, 475)
    first_y = 278 - 45 * (len(lines) - 1)
    draw.rounded_rectangle((72, first_y - 30, 126, first_y - 22), radius=4, fill=accent)
    for i, line in enumerate(lines):
        draw.text((72, first_y + 86 * i), line, font=title_font, fill='#ffffff')
    if subtitle:
        draw.text((75, first_y + 86 * len(lines) + 18), subtitle, font=_font(31), fill='#d9e1f0')
    return jpeg(canvas.convert('RGB'))


def _card(canvas, poster, box, radius=12, angle=0):
    x, y, width, height = box
    tile = fit_image(poster, (width, height)).convert('RGBA')
    alpha = Image.new('L', tile.size)
    ImageDraw.Draw(alpha).rounded_rectangle((0, 0, width - 1, height - 1), radius, fill=255)
    tile.putalpha(alpha)
    if angle:
        tile = tile.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    canvas.alpha_composite(tile, (x - (tile.width - width) // 2, y - (tile.height - height) // 2))


def _poster_row(canvas, posters, accent):
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((38, 48, 1242, 454), 18, fill='#10141e')
    for x in range(59, 1220, 38):
        for y in (62, 428):
            draw.rounded_rectangle((x, y, x + 18, y + 10), 3, fill=(*accent, 255))
    width = min(220, 1172 // len(posters))
    start = (SIZE[0] - width * len(posters)) // 2
    for i, poster in enumerate(posters):
        _card(canvas, poster, (start + 4 + width * i, 84, width - 8, 330), radius=6)


def _title(canvas, title, subtitle, accent, x, y, width, *, centered=False):
    draw = ImageDraw.Draw(canvas)
    font = _font(70)
    small = _font(28)
    lines = _title_lines(title, font, width)
    anchor = 'mt' if centered else 'lt'
    origin = x - 27 if centered else x
    draw.rounded_rectangle((origin, y - 24, origin + 54, y - 17), 4, fill=accent)
    for i, line in enumerate(lines):
        draw.text((x, y + i * 82), line, font=font, fill='white', anchor=anchor)
    if subtitle:
        text = _title_lines(subtitle, small, width, max_lines=1)[0]
        draw.text((x, y + 82 * len(lines) + 12), text, font=small, fill='#e3e8f2', anchor=anchor)


def render_cover(title, subtitle, color, layout, posters):
    if layout in ('collage', 'grid'):
        return _classic_cover(title, subtitle, color, layout, posters)

    accent = ImageColor.getrgb(color)
    tint = tuple(round(v * .53) for v in accent)
    shade = Image.linear_gradient('L').rotate(90).resize(SIZE)
    canvas = Image.composite(Image.new('RGB', SIZE, tint), Image.new('RGB', SIZE, '#101625'), shade).convert('RGBA')
    posters = [open_image(blob) for blob in posters[:COVER_LAYOUTS[layout][1]]]

    if layout == 'minimal' or not posters:
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((52, 49, 1228, 671), radius=26, outline=accent, width=2)
        _title(canvas, title, subtitle, accent, 640, 319 if len(title) < 12 else 268, 1064, centered=True)
    elif layout == 'filmstrip':
        _poster_row(canvas, posters, accent)
        _title(canvas, title, subtitle, accent, 70, 505, 1140)
    elif layout == 'panorama':
        art = ImageOps.fit(posters[0], SIZE, Image.Resampling.LANCZOS, centering=(.5, .32))
        shadow = Image.linear_gradient('L').resize(SIZE).point(lambda pixel: 20 + int(pixel * .84))
        backdrop = Image.composite(Image.new('RGB', SIZE, '#0b1020'), art, shadow)
        canvas = Image.blend(backdrop, canvas.convert('RGB'), .18).convert('RGBA')
        _title(canvas, title, subtitle, accent, 70, 448, 1140)
    elif layout == 'hero':
        ImageDraw.Draw(canvas).rounded_rectangle((662, 37, 1216, 683), radius=22, outline=accent, width=4)
        _card(canvas, posters[0], (704, 0, 476, 720), radius=14)
        _title(canvas, title, subtitle, accent, 68, 275, 530)
    elif layout == 'stack':
        for index, box, angle in ((2, (585, 150, 260, 392), 11), (1, (960, 150, 260, 392), -11),
                                  (0, (748, 84, 305, 462), 0)):
            if index < len(posters):
                _card(canvas, posters[index], box, radius=16, angle=angle)
        _title(canvas, title, subtitle, accent, 68, 275, 475)
    elif layout == 'magazine':
        _card(canvas, posters[0], (560, 58, 333, 602), radius=10)
        for i, poster in enumerate(posters[1:]):
            row, col = divmod(i, 2)
            _card(canvas, poster, (908 + col * 170, 58 + row * 205, 156, 192), radius=9)
        _title(canvas, title, subtitle, accent, 68, 174, 450)

    return jpeg(canvas.convert('RGB'))
