"""Render the 16:9 primary image that Emby displays for a library."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps


SIZE = (1280, 720)
MAX_IMAGE_BYTES = 2 * 1024 * 1024
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


def _title_lines(text, font, width):
    lines = ['']
    for char in text:
        if font.getlength(lines[-1] + char) <= width:
            lines[-1] += char
        elif len(lines) == 1:
            lines.append(char)
        else:
            while lines[-1] and font.getlength(lines[-1] + '…') > width:
                lines[-1] = lines[-1][:-1]
            lines[-1] += '…'
            break
    return lines


def render_cover(title, subtitle, color, layout, posters):
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
