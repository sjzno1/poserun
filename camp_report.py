"""Two-page camp report: fixed assessment taxonomy, evidence-safe content.

Layout follows the user-supplied camp PDF. Unknown items are not diagnoses.
Long manually supplied text may continue onto extra pages; never truncate it.
"""
from datetime import datetime
import json
from pathlib import Path
from xml.sax.saxutils import escape

from measurement_safety import finite

ITEMS = (
    ('落地', '脚刚触地位置'), ('落地', '膝关节'), ('落地', '跨步'),
    ('落地', '落地框架'), ('落地', '小腿角度'),
    ('离地', '推蹬、重心起伏'), ('离地', '伸腿'),
    ('上身', '摆臂'), ('上身', '晃动'),
    ('整体', '身体姿态'), ('整体', '力线'), ('整体', '弹性'), ('整体', '流畅度'),
)
UNKNOWN = '未知／未验证'
HEADER = Path(__file__).parent / 'static' / 'report' / 'camp_header.png'


def build_camp_data(payload, user_info=None):
    """Do not translate average geometry into a contact-phase diagnosis."""
    user = dict(user_info or {})
    metrics = payload.get('metrics', {})
    rows = []
    for category, item in ITEMS:
        reason = '证据不足，待教练结合对应视角及动作相位复核。'
        if item == '脚刚触地位置':
            reason = '当前关键点不含脚跟与足尖，不能判定触地类型。'
        elif item in ('膝关节', '小腿角度', '身体姿态'):
            key = {'膝关节': 'knee_flexion', '小腿角度': 'shin_angle',
                   '身体姿态': 'body_leang'}[item]
            m = metrics.get(key, {})
            if finite(m.get('value')):
                cov = m.get('coverage')
                coverage = f'{cov:.1%}' if finite(cov) else '未知'
                reason = (f"{m.get('label', item)}：{m['value']:.1f}°；有效帧覆盖率{coverage}。"
                          '仅二维几何，不据此判定技术优劣。')
        rows.append((category, item, UNKNOWN, reason))
    return {
        'title': user.get('report_title') or '姿势跑法学员技术分析评测',
        'name': user.get('name') or '未提供',
        'coach': user.get('coach') or '待教练复核',
        'score': None,  # Caller-supplied score cannot bypass evidence gates.
        'date': user.get('date') or datetime.now().strftime('%Y-%m-%d'),
        'rows': rows,
        'summary': '证据不足的分项待教练复核；不生成未经验证的技术结论或训练处方。',
        'notice': payload.get('notice', ''),
    }


def collect_keyframes(folder):
    """Only real images and neutral frame/time captions; never invent views."""
    if not folder or not Path(folder).is_dir():
        return []
    frames = []
    for image in sorted(Path(folder).glob('keyframe_*.jpg')):
        info = {}
        sidecar = image.with_name(image.stem + '_info.json')
        try:
            info = json.loads(sidecar.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        caption = image.stem
        index, timestamp = info.get('frame_index'), info.get('timestamp')
        if type(index) is int and index >= 0:
            caption = f'帧索引 {index}（从0计）'
        if finite(timestamp) and timestamp >= 0:
            caption += f' · {timestamp:.3f}秒（容器时间）'
        frames.append((str(image), caption + '；动作相位未验证'))
    return frames


def _text(value):
    return '' if value is None else str(value)


def _score(data):
    return _text(data.get('score')) if finite(data.get('score')) else UNKNOWN


def _valid_images(keyframes):
    from PIL import Image
    result = []
    for path, caption in keyframes:
        try:
            with Image.open(path) as im:
                im.verify()
            result.append((str(path), _text(caption)))
        except (OSError, ValueError):
            result.append((None, _text(caption) + '（截图缺失或损坏）'))
    return result


def export_camp_report(data, keyframes, output_path, fmt='pdf'):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == 'pdf':
        _pdf(data, _valid_images(keyframes), str(output))
    elif fmt == 'docx':
        _docx(data, _valid_images(keyframes), str(output))
    else:
        raise ValueError('unsupported camp report format')
    return str(output)


def _pdf(data, keyframes, output):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, PageBreak, Image, KeepTogether)
    from PIL import Image as PILImage
    font = 'STSong-Light'
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    width = 539.28
    body = ParagraphStyle('camp-body', fontName=font, fontSize=8, leading=11,
                          wordWrap='CJK')
    small = ParagraphStyle('camp-small', parent=body, fontSize=6, leading=8)
    centered = ParagraphStyle('camp-center', parent=body, alignment=1)
    title = ParagraphStyle('camp-title', parent=body, fontSize=16, leading=21,
                           alignment=1, textColor=colors.HexColor('#000080'))
    def p(text, style=body):
        return Paragraph(escape(_text(text)).replace('\n', '<br/>'), style)
    def page_header(canvas, doc):
        if HEADER.exists():
            canvas.drawImage(str(HEADER), 28, 785, width=width, height=39,
                             preserveAspectRatio=True, mask='auto')
    doc = SimpleDocTemplate(output, pagesize=(595.28, 841.89), leftMargin=28,
                            rightMargin=28, topMargin=68, bottomMargin=36,
                            title=_text(data.get('title')), author=_text(data.get('coach')))
    flow = [p(data.get('title', '姿势跑法学员技术分析评测'), title), Spacer(1, 12)]
    info = [f"姓名：{data.get('name', '')}", f"评分教练：{data.get('coach', '')}",
            f"得分：{_score(data)}", f"日期：{data.get('date', '')}"]
    basic = Table([[p(x) for x in info]], colWidths=[140, 140, 120, 139.28])
    basic.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0),
                              ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
    flow.append(basic)
    rows = data.get('rows', [])
    cells = [[p(x, centered) for x in ('环节', '分项', '评判结果', '改进建议')]]
    cells += [[p(cat, centered), p(item, centered), p(result), p(suggestion, small)]
              for cat, item, result, suggestion in rows]
    table = Table(cells, colWidths=[45, 68, 85, 341.28],
                  minRowHeights=[28] + [45] * len(rows), repeatRows=1)
    style = [('GRID', (0, 0), (-1, -1), .45, colors.HexColor('#666666')),
             ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#FFC000')),
             ('VALIGN', (0, 0), (1, -1), 'MIDDLE'),
             ('VALIGN', (2, 1), (-1, -1), 'TOP'),
             ('LEFTPADDING', (0, 0), (-1, -1), 4),
             ('RIGHTPADDING', (0, 0), (-1, -1), 4),
             ('TOPPADDING', (0, 0), (-1, -1), 5),
             ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]
    start = 1
    while start <= len(rows):
        end = start
        while end < len(rows) and rows[end][0] == rows[start - 1][0]:
            end += 1
        if end > start:
            style.append(('SPAN', (0, start), (0, end)))
        start = end + 1
    table.setStyle(TableStyle(style))
    flow += [table, PageBreak()]

    def image_grid(entries, first_row=False):
        # Sample: 4 equal columns; the third upper cell spans two columns.
        n = len(entries)
        widths = [width / 4] * 4 if first_row else [width / max(n, 1)] * n
        captions, pictures = [], []
        for path, caption in entries:
            captions.append(p(caption, centered))
            if path:
                with PILImage.open(path) as im:
                    iw, ih = im.size
                scale = min((width / 4 - 16) / iw, 205 / ih)
                pictures.append(Image(path, width=iw * scale, height=ih * scale))
            else:
                pictures.append(p('未提供对应截图', centered))
        commands = [('GRID', (0, 0), (-1, -1), .45, colors.grey),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#FFC000')),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('TOPPADDING', (0, 0), (-1, 0), 7),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 7),
                    ('LEFTPADDING', (0, 1), (-1, -1), 8),
                    ('TOPPADDING', (0, 1), (-1, -1), 10),
                    ('BOTTOMPADDING', (0, 1), (-1, -1), 10)]
        if first_row:
            while len(captions) < 3:
                captions.append(p('未提供对应截图', centered))
                pictures.append(p('未提供对应截图', centered))
            captions.append('')
            pictures.append('')
            commands += [('SPAN', (2, 0), (3, 0)), ('SPAN', (2, 1), (3, 1))]
        grid = Table([captions, pictures], colWidths=widths, minRowHeights=[57, 225])
        grid.setStyle(TableStyle(commands))
        return grid
    flow.append(image_grid(keyframes[:3], True))
    if len(keyframes) > 3:
        flow.append(image_grid(keyframes[3:7]))
    else:
        flow.append(p('未提供其余关键帧；不根据现有截图虚构视角或动作阶段。', small))
    flow += [Spacer(1, 8), p(data.get('summary', '')), Spacer(1, 6),
             p(data.get('notice', ''), small)]
    # Overflow photos continue; nothing silently dropped beyond seven.
    if len(keyframes) > 7:
        flow += [PageBreak(), p('补充关键帧', title)]
        for start in range(7, len(keyframes), 4):
            flow.append(image_grid(keyframes[start:start + 4]))
    doc.build(flow, onFirstPage=page_header, onLaterPages=page_header)


def _docx(data, keyframes, output):
    from docx import Document
    from docx.shared import Cm, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from PIL import Image
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(1)
    section.top_margin, section.bottom_margin = Cm(2.4), Cm(1.2)
    section.header_distance = Cm(.5)
    if HEADER.exists():
        section.header.paragraphs[0].add_run().add_picture(str(HEADER), width=Cm(19))
    normal = doc.styles['Normal']
    normal.font.name = 'Microsoft YaHei'
    normal.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    normal.font.size = Pt(8)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1
    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = t.add_run(_text(data.get('title')))
    run.bold = True
    run.font.size = Pt(16)
    run.font.color.rgb = RGBColor.from_string('000080')
    doc.add_paragraph(f"姓名：{data.get('name', '')}　　评分教练：{data.get('coach', '')}"
                      f"　　得分：{_score(data)}　　日期：{data.get('date', '')}")

    def shade(cell):
        shd = OxmlElement('w:shd')
        shd.set(qn('w:fill'), 'FFC000')
        cell._tc.get_or_add_tcPr().append(shd)
    rows = data.get('rows', [])
    table = doc.add_table(rows=1, cols=4)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = 'Table Grid'
    table.autofit = False
    widths = [1.6, 2.4, 3, 12]
    for col, w in zip(table.columns, widths):
        col.width = Cm(w)
    for cell, text in zip(table.rows[0].cells, ('环节', '分项', '评判结果', '改进建议')):
        cell.text = text
        shade(cell)
    repeat = OxmlElement('w:tblHeader')
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for row in rows:
        r = table.add_row()
        r.height = Cm(1.55)
        for index, (cell, text, w) in enumerate(zip(r.cells, row, widths)):
            cell.width = Cm(w)
            cell.text = _text(text)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if index == 3:
                for run in cell.paragraphs[0].runs:
                    run.font.size = Pt(6)
    start = 1
    while start <= len(rows):
        end = start
        while end < len(rows) and rows[end][0] == rows[start - 1][0]:
            end += 1
        if end > start:
            merged = table.cell(start, 0).merge(table.cell(end, 0))
            merged.text = _text(rows[start - 1][0])
        start = end + 1
    doc.add_page_break()
    batches = [keyframes[:3], keyframes[3:7]]
    if not keyframes:
        batches = [[(None, '未提供对应截图')] * 3]
    for entries in batches:
        if not entries:
            continue
        grid = doc.add_table(rows=2, cols=4 if len(entries) == 3 else len(entries))
        grid.style = 'Table Grid'
        grid.autofit = False
        if len(entries) == 3:
            for row in grid.rows:
                row.cells[2].merge(row.cells[3])
        for index, (path, caption) in enumerate(entries):
            cell = grid.cell(0, index)
            cell.text = caption
            shade(cell)
            pic = grid.cell(1, index)
            if path:
                with Image.open(path) as im:
                    iw, ih = im.size
                scale = min(4.1 / iw, 7.2 / ih)
                pic.paragraphs[0].add_run().add_picture(path, width=Cm(iw * scale), height=Cm(ih * scale))
            else:
                pic.text = '未提供对应截图'
    doc.add_paragraph(_text(data.get('summary')))
    doc.add_paragraph(_text(data.get('notice')))
    for path, caption in keyframes[7:]:
        doc.add_paragraph(caption)
        if path:
            doc.add_picture(path, width=Cm(4))
    doc.save(output)
