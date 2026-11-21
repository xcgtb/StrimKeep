"""Render the 16:9 primary image that Emby displays for a library."""
from colorsys import hls_to_rgb, rgb_to_hls
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps


SIZE = (1280, 720)
MAX_IMAGE_BYTES = 2 * 1024 * 1024
RENDER_VERSION = 4
CoverColorMode = Literal['auto', 'manual']
CoverLayout = Literal['collage', 'grid', 'hero', 'filmstrip', 'stack', 'magazine', 'panorama', 'minimal',
                      'gallery', 'arc', 'split', 'soft', 'cinema-card', 'cinema-cut', 'cinema-wall']
CINEMA_LAYOUTS = ('cinema-card', 'cinema-cut', 'cinema-wall')
COVER_LAYOUTS = {
    'cinema-card': ('影院叠卡', 1),
    'cinema-cut': ('影院斜切', 1),
    'cinema-wall': ('影院九宫格', 9),
    'collage': ('斜向拼贴', 9),
    'grid': ('整齐网格', 9),
    'hero': ('主海报', 1),
    'filmstrip': ('胶片长廊', 6),
    'stack': ('三联叠影', 3),
    'magazine': ('杂志拼版', 7),
    'panorama': ('全幅海报', 1),
    'minimal': ('极简标题', 0),
    'gallery': ('横向画廊', 7),
    'arc': ('弧形陈列', 7),
    'split': ('斜切海报', 1),
    'soft': ('柔焦背景', 1),
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


def cover_poster_limit(layout, posters, color_mode):
    return max(int(color_mode == 'auto'), min(posters, COVER_LAYOUTS[layout][1]))


def _image_color(photo):
    sample = fit_image(photo, (64, 64)).quantize(colors=12)
    palette = sample.getpalette()
    swatches = []
    for count, index in sample.getcolors():
        rgb = palette[index * 3:index * 3 + 3]
        hue, light, saturation = rgb_to_hls(*(value / 255 for value in rgb))
        if light < .10 or light > .90:
            continue
        # A broad area of color should beat tiny, saturated lettering.
        chroma = max(rgb) - min(rgb)
        weight = count * (1 + chroma / 127)
        swatches.append((weight, hue, light, saturation))
    if not swatches:
        neutral = round(photo.resize((1, 1)).convert('L').getpixel((0, 0)) * .24 + 65)
        return neutral, neutral, neutral
    _, hue, light, saturation = max(swatches)
    return tuple(round(value * 255) for value in hls_to_rgb(hue, min(.56, max(.32, light)), min(.72, saturation)))


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


def _poster_wall(canvas, posters, *, angled):
    columns = 2 if len(posters) == 4 else 3
    rows = (len(posters) + columns - 1) // columns
    width = {2: 302, 3: 220}[columns] if angled else {2: 218, 3: 194}[columns]
    height = round(width * 1.5)
    gap = 15
    board = Image.new('RGBA', (columns * (width + gap) - gap, rows * (height + gap) - gap))
    for i, poster in enumerate(posters):
        row, col = divmod(i, columns)
        count = min(columns, len(posters) - row * columns)
        shift = (columns - count) * (width + gap) // 2
        _card(board, poster, (shift + col * (width + gap), row * (height + gap), width, height),
              radius=16)
    if angled:
        board = board.rotate(-15, resample=Image.Resampling.BICUBIC, expand=True)
    center = 965 if angled else 937
    canvas.alpha_composite(board, (center - board.width // 2, 360 - board.height // 2))


def _featured_art(canvas, posters, *, angled=True):
    geometry = (
        (2, (609, 131, 320, 480), 17),
        (1, (905, 95, 328, 492), -10),
        (0, (714, 36, 433, 650), 9),
    )
    if len(posters) == 1:
        geometry = ((0, (733, 29, 438, 657), 12),)
    for index, box, rotation in geometry:
        if index < len(posters):
            _card(canvas, posters[index], box, radius=24, angle=rotation if angled else 0)


def _card(canvas, poster, box, radius=12, angle=0):
    x, y, width, height = box
    tile = fit_image(poster, (width, height)).convert('RGBA')
    alpha = Image.new('L', tile.size)
    ImageDraw.Draw(alpha).rounded_rectangle((0, 0, width - 1, height - 1), radius, fill=255)
    tile.putalpha(alpha)
    if angle:
        tile = tile.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    left = x - (tile.width - width) // 2
    top = y - (tile.height - height) // 2
    padding = 16
    shade = Image.new('L', (tile.width + 2 * padding, tile.height + 2 * padding))
    shade.paste(tile.getchannel('A'), (padding, padding))
    shade = shade.filter(ImageFilter.GaussianBlur(9)).point(lambda value: value // 3)
    shadow = Image.new('RGBA', shade.size)
    shadow.putalpha(shade)
    canvas.alpha_composite(shadow, (left - padding, top - padding + 7))
    canvas.alpha_composite(tile, (left, top))


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


def _title(canvas, title, subtitle, accent, x, y, width, *, centered=False,
           middle=False, maximum=112):
    draw = ImageDraw.Draw(canvas)
    font = _font(maximum)
    natural = font.getlength(title)
    if natural > width:
        font = _font(max(76, int(maximum * width / natural)))
    small = _font(42)
    lines = _title_lines(title, font, width)
    step = round(font.size * 1.03)
    block = step * len(lines) + (48 if subtitle else 0)
    if middle:
        y -= block // 2
    anchor = 'mt' if centered else 'lt'
    stroke = tuple(round(v * .6 + 102) for v in accent)
    origin = x - 36 if centered else x
    draw.rounded_rectangle((origin, y - 28, origin + 72, y - 20), 4, fill=stroke)
    for i, line in enumerate(lines):
        draw.text((x, y + i * step), line, font=font, fill='#fffdf9', anchor=anchor,
                  stroke_width=2, stroke_fill='#fffdf9')
    if subtitle:
        text = _title_lines(subtitle, small, width, max_lines=1)[0]
        draw.text((x, y + step * len(lines) + 7), text, font=small, fill='#dbe3ee', anchor=anchor)


def _blurred_art(photo, accent, color_mode, radius):
    small = fit_image(photo, (160, 90)).filter(ImageFilter.GaussianBlur(radius))
    if color_mode == 'manual':
        highlight = tuple(round(value * .84 + 24) for value in accent)
        small = ImageOps.colorize(small.convert('L'), '#0b1019', highlight)
    return small.resize(SIZE, Image.Resampling.BICUBIC)


def _cover_background(accent, photo=None, color_mode='auto', *, empty=True):
    tint = tuple(round(v * .58) for v in accent)
    shade = Image.linear_gradient('L').rotate(90).resize(SIZE)
    ground = Image.composite(Image.new('RGB', SIZE, tint), Image.new('RGB', SIZE, '#0c0f17'), shade)
    if photo:
        base = _blurred_art(photo, accent, color_mode, 13)
        ground = Image.blend(ground, base, .40)
    elif empty:
        motif = Image.new('RGB', (320, 180))
        draw = ImageDraw.Draw(motif)
        for i, strength in enumerate((.20, .42, .69)):
            draw.polygon(((238 + i * 25, -25), (480, -25), (345, 195), (130 + i * 32, 195)),
                         fill=tuple(round(value * strength) for value in accent))
        motif = motif.filter(ImageFilter.GaussianBlur(7)).resize(SIZE, Image.Resampling.BICUBIC)
        ground = Image.blend(ground, motif, .58)
    return ground.convert('RGBA')


def _title_panel(canvas):
    gradient = Image.linear_gradient('L').rotate(-90).resize((700, 720))
    shade = gradient.point(lambda value: int(value * .40))
    panel = Image.new('RGBA', (700, 720), '#090c12')
    panel.putalpha(shade)
    canvas.alpha_composite(panel)


def _display_row(canvas, posters, *, curved=False):
    width = min(190, 1090 // len(posters))
    mid = (len(posters) - 1) / 2
    for i in sorted(range(len(posters)), key=lambda idx: -abs(idx - mid)):
        offset = i - mid
        gain = abs(offset) / max(1, mid)
        card_width = round(width * (1.13 - .23 * gain))
        if curved:
            x, y, angle = 640 + offset * width * .82, 318 + round(65 * gain), round(-offset * 5)
        else:
            x, y, angle = 640 + offset * width, 332 + round(42 * gain), 0
        _card(canvas, posters[i], (round(x - card_width / 2), y, card_width, round(card_width * 1.51)),
              radius=12, angle=angle)


def _cinema_background(photo, accent, color_mode):
    ambient = tuple(round(value * .65) for value in accent)
    mist = _blurred_art(photo, accent, color_mode, 17)
    return Image.blend(Image.new('RGB', SIZE, ambient), mist, .32).convert('RGBA')


def _cinema_cards(canvas, poster, accent):
    thumb = ImageOps.fit(poster, (468, 468)).filter(ImageFilter.GaussianBlur(23))
    for angle, offset in ((17, -17), (-12, 15)):
        wash = Image.new('RGB', thumb.size, accent if offset < 0 else '#251d29')
        layer = Image.blend(thumb, wash, .57)
        _card(canvas, layer, (717 + offset, 133, 468, 468), radius=48, angle=angle)
    _card(canvas, poster, (717, 126, 468, 468), radius=48)


def _cinema_cut(canvas, image):
    still = ImageOps.fit(image, SIZE, Image.Resampling.LANCZOS, centering=(.52, .34)).convert('RGBA')
    mask = Image.new('L', SIZE)
    ImageDraw.Draw(mask).polygon(((743, 0), (1280, 0), (1280, 720), (536, 720)), fill=255)
    still.putalpha(mask)
    canvas.alpha_composite(still)


def _cinema_cover(title, subtitle, accent, layout, posters, scene, color_mode, blur_background):
    if not posters:
        canvas = _cover_background(accent)
        _title(canvas, title, subtitle, accent, 640, 354, 1080, centered=True, middle=True, maximum=124)
        return canvas

    main = posters[0]
    if layout == 'cinema-wall' and not blur_background:
        canvas = _cover_background(accent, empty=False)
    else:
        canvas = _cinema_background(scene or main, accent, color_mode)
    if layout == 'cinema-card':
        _cinema_cards(canvas, main, accent)
    elif layout == 'cinema-cut':
        _cinema_cut(canvas, scene or main)
    else:
        wall = [posters[index % len(posters)] for index in range(9)]
        _poster_wall(canvas, wall, angled=True)
    _title_panel(canvas)
    _title(canvas, title, subtitle, accent, 292, 357, 454, centered=True, middle=True, maximum=118)
    return canvas


def render_cover(title, subtitle, color, layout, posters, *, size=SIZE, scene=None,
                 color_mode='auto', blur_background=True):
    limit = COVER_LAYOUTS[layout][1]
    art = [open_image(blob) for blob in posters[:cover_poster_limit(layout, len(posters), color_mode)]]
    posters = art[:limit]
    scene = open_image(scene) if scene and layout == 'cinema-cut' else None
    photo = scene or next(iter(art), None)
    accent = _image_color(photo) if photo and color_mode == 'auto' else ImageColor.getrgb(color)
    if layout in CINEMA_LAYOUTS:
        canvas = _cinema_cover(title, subtitle, accent, layout, posters, scene, color_mode, blur_background)
        image = canvas.convert('RGB')
        return jpeg(image if size == SIZE else image.resize(size, Image.Resampling.LANCZOS))

    background_photo = photo if posters and (blur_background or limit == 1) else None
    canvas = _cover_background(accent, background_photo, color_mode, empty=not posters)
    if layout in ('collage', 'grid'):
        if len(posters) >= 4:
            _poster_wall(canvas, posters, angled=layout == 'collage')
        elif posters:
            _featured_art(canvas, posters, angled=layout == 'collage')
        _title_panel(canvas)
        _title(canvas, title, subtitle, accent, 75, 356, 484, middle=True, maximum=120)
    elif layout in ('minimal', 'soft') or not posters:
        _title(canvas, title, subtitle, accent, 640, 356, 1064, centered=True, middle=True, maximum=124)
    elif len(posters) < 4 and layout in ('filmstrip', 'stack', 'magazine', 'gallery', 'arc'):
        _featured_art(canvas, posters, angled=layout in ('stack', 'arc'))
        _title_panel(canvas)
        _title(canvas, title, subtitle, accent, 75, 356, 484, middle=True, maximum=120)
    elif layout in ('gallery', 'arc'):
        _display_row(canvas, posters, curved=layout == 'arc')
        _title(canvas, title, subtitle, accent, 640, 161, 1120, centered=True, middle=True)
    elif layout == 'split':
        art = Image.new('RGBA', SIZE)
        art.paste(fit_image(posters[0], (768, 720)).convert('RGBA'), (512, 0))
        mask = Image.new('L', SIZE)
        ImageDraw.Draw(mask).polygon([(700, 0), (1280, 0), (1280, 720), (512, 720)], fill=255)
        art.putalpha(mask)
        canvas.alpha_composite(art)
        _title(canvas, title, subtitle, accent, 70, 350, 435, middle=True)
    elif layout == 'filmstrip':
        _poster_row(canvas, posters, accent)
        _title(canvas, title, subtitle, accent, 70, 582, 1140, middle=True, maximum=98)
    elif layout == 'panorama':
        art = ImageOps.fit(posters[0], SIZE, Image.Resampling.LANCZOS, centering=(.5, .32))
        shadow = Image.linear_gradient('L').resize(SIZE).point(lambda pixel: 20 + int(pixel * .84))
        backdrop = Image.composite(Image.new('RGB', SIZE, '#0b1020'), art, shadow)
        canvas = Image.blend(backdrop, canvas.convert('RGB'), .18).convert('RGBA')
        _title(canvas, title, subtitle, accent, 70, 530, 1120, middle=True)
    elif layout == 'hero':
        _card(canvas, posters[0], (755, -12, 500, 752), radius=22)
        _title(canvas, title, subtitle, accent, 70, 350, 500, middle=True)
    elif layout == 'magazine':
        _card(canvas, posters[0], (560, 58, 333, 602), radius=10)
        for i, poster in enumerate(posters[1:]):
            row, col = divmod(i, 2)
            _card(canvas, poster, (908 + col * 170, 58 + row * 205, 156, 192), radius=9)
        _title(canvas, title, subtitle, accent, 68, 280, 428, middle=True)

    image = canvas.convert('RGB')
    return jpeg(image if size == SIZE else image.resize(size, Image.Resampling.LANCZOS))
