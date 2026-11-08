"""Render live help text as readable, wallpaper-backed cards with bounded layout."""
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat

MAX_TEXT_CHARS = 24000
MAX_ROWS = 120
MAX_HEIGHT = 6500
WIDTH = 1440
GAP = 24
MARGIN = 60


@dataclass
class HelpSection:
    title: str
    rows: list


def parse_help(text, visible=lambda command: True):
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('帮助文件过长，请缩短后重试')
    lines = [line.strip() for line in text.lstrip('\ufeff').splitlines()]
    title = next((line for line in lines if line), '使用帮助')
    intro, footer, sections = [], [], []
    section = None
    count = 0
    for line in lines[lines.index(title)+1:]:
        if not line:
            continue
        if line.startswith('[') and line.endswith(']'):
            section = HelpSection(line[1:-1], [])
            sections.append(section)
        elif line.startswith('/'):
            label, separator, description = line.partition('：')
            command = label.split()[0]
            if visible(label):
                if section is None:
                    section = HelpSection('常用命令', []);sections.append(section)
                section.rows.append((label, description if separator else ''))
                count += 1
                if count > MAX_ROWS:
                    raise ValueError('帮助条目过多，请精简后重试')
        elif line.startswith('提示：'):
            footer.append(line[3:])
        else:
            intro.append(line)
    return title, intro, [s for s in sections if s.rows], footer


def _font(size, bold=False):
    candidates = [Path('C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc'),
                  Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise RuntimeError('帮助图片缺少中文字体')


def _wrap(text, font, width):
    lines, current = [], ''
    for char in text:
        if char == '\n' or (current and font.getlength(current + char) > width and char not in '，。；：！？、）》」』】,.!?;:)]}'):
            lines.append(current);current = ''
        if char != '\n':
            current += char
    if current:
        lines.append(current)
    return lines


def render_help(parsed, background, output):
    title, intro, sections, footer = parsed
    title_font, heading_font = _font(54, True), _font(31, True)
    command_font, body_font, small_font = _font(24, True), _font(23), _font(21)
    column_width = (WIDTH - 2*MARGIN - GAP)//2
    card_width = column_width - 48
    columns = [[], []];heights = [0, 0]
    for section in sections:
        rows = []
        height = 84
        for command, description in section.rows:
            a, b = _wrap(command, command_font, card_width), _wrap(description, body_font, card_width)
            rows.append((a,b));height += len(a)*34 + len(b)*32 + 23
        index = 0 if heights[0] <= heights[1] else 1
        columns[index].append((section.title, rows, height));heights[index] += height + GAP
    intro_lines = _wrap(' '.join(intro), body_font, WIDTH-2*MARGIN)
    footer_lines = [part for text in footer for part in _wrap(text, small_font, WIDTH-2*MARGIN-48)]
    top = 170 + len(intro_lines)*34
    footer_height = 48 + len(footer_lines)*32
    height = max(900, top + max(heights) + footer_height + 64)
    if height > MAX_HEIGHT:
        raise ValueError('帮助排版过长，请减少条目或文字')
    if background:
        with Image.open(background) as source:
            if source.width*source.height > 40_000_000:
                raise ValueError('背景图片尺寸过大')
            canvas = ImageOps.fit(ImageOps.exif_transpose(source).convert('RGB'), (WIDTH,height),
                method=Image.Resampling.LANCZOS, centering=(.5,.42))
    else:
        canvas=Image.new('RGB',(WIDTH,height),(39,49,72))
    luminance = ImageStat.Stat(canvas.resize((64,64)).convert('L')).mean[0]
    light = luminance > 150
    canvas = ImageEnhance.Brightness(canvas).enhance(.72 if light else .60).filter(ImageFilter.GaussianBlur(9)).convert('RGBA')
    overlay=Image.new('RGBA',canvas.size,(0,0,0,0));draw=ImageDraw.Draw(overlay)
    panel = (250,251,255,237) if light else (13,21,38,237)
    fg = (23,33,51,255) if light else (241,246,255,255)
    secondary = (64,79,99,255) if light else (195,208,229,255)
    accent = (33,87,145,255) if light else (142,203,255,255)
    draw.rounded_rectangle((MARGIN-20,32,WIDTH-MARGIN+20,top-22),radius=24,fill=(10,18,33,230))
    draw.text((MARGIN,55),title,font=title_font,fill=(248,251,255,255))
    for index,line in enumerate(intro_lines):
        draw.text((MARGIN,130+index*34),line,font=body_font,fill=(211,225,245,255))
    for column,cards in enumerate(columns):
        x=MARGIN+column*(column_width+GAP);y=top
        for heading,rows,card_height in cards:
            draw.rounded_rectangle((x,y,x+column_width,y+card_height),radius=22,fill=panel)
            draw.rounded_rectangle((x+24,y+26,x+30,y+59),radius=3,fill=accent)
            draw.text((x+44,y+22),heading,font=heading_font,fill=fg)
            cursor=y+79
            for commands,descriptions in rows:
                for line in commands:
                    draw.text((x+24,cursor),line,font=command_font,fill=accent);cursor+=34
                for line in descriptions:
                    draw.text((x+24,cursor),line,font=body_font,fill=secondary);cursor+=32
                cursor+=23
            y+=card_height+GAP
    y=top+max(heights)
    draw.rounded_rectangle((MARGIN,y,WIDTH-MARGIN,y+footer_height),radius=18,fill=panel)
    for index,line in enumerate(footer_lines):
        draw.text((MARGIN+24,y+20+index*32),line,font=small_font,fill=secondary)
    Image.alpha_composite(canvas,overlay).convert('RGB').save(output,'JPEG',quality=91,optimize=True)
    return {'size':(WIDTH,height),'theme':'light' if light else 'dark','background_luminance':round(luminance,1)}
