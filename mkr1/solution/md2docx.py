"""Конвертація Markdown-звіту у .docx."""
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

FONT, SIZE = "Times New Roman", 12
CODE_FONT, CODE_SIZE = "Consolas", 8.5
MAX_W, MAX_H = 16.0, 20.0
TITLE_LEFT = ("Студент", "Група", "Варіант", "Викладач", "Виконав", "Перевірив")


def _run(par, text, size=SIZE, bold=None, font=FONT, italic=None):
    r = par.add_run(text)
    r.font.name, r.font.size = font, Pt(size)
    r.font.color.rgb = RGBColor(0, 0, 0)
    r._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), font)
    r.bold, r.italic = bold, italic
    return r


def _inline(par, text, size=SIZE, bold=None):
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            _run(par, part[2:-2], size, bold=True)
        elif part.startswith("`") and part.endswith("`"):
            _run(par, part[1:-1], size, bold)
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            _run(par, part[1:-1], size, bold, italic=True)
        else:
            _run(par, part, size, bold)


def _title_line(doc, text):
    text = text.replace("**", "").lstrip("# ").strip()
    if text.startswith(TITLE_LEFT):
        label, _, value = text.partition(":")
        p = _para(doc, WD_ALIGN_PARAGRAPH.LEFT, after=2, spacing=1)
        _run(p, label + ": ", bold=True)
        _run(p, value.strip())
        return
    p = _para(doc, WD_ALIGN_PARAGRAPH.CENTER, after=2, spacing=1)
    first = not any(par.text.strip() for par in doc.paragraphs[:-1])
    if first:
        _run(p, text, 13, bold=True)
    elif text.startswith("Лабораторна робота"):
        p.paragraph_format.space_before = Pt(10)
        _run(p, text, 15, bold=True)
    elif text.startswith(("Дисципліна", "Тема")):
        _run(p, text, 13, bold=True)
    elif text.startswith("Спеціальність"):
        _run(p, text, italic=True)
    else:
        _run(p, text)


def _para(doc, align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=8, spacing=1.15):
    p = doc.add_paragraph()
    p.alignment = align
    f = p.paragraph_format
    f.space_before, f.space_after, f.line_spacing = Pt(0), Pt(after), spacing
    return p


def _picture(doc, path: Path):
    w, h = MAX_W, None
    try:
        from PIL import Image
        with Image.open(path) as im:
            ratio = im.height / im.width
        if MAX_W * ratio > MAX_H:
            w, h = None, MAX_H
    except Exception:
        pass
    p = _para(doc, WD_ALIGN_PARAGRAPH.CENTER, after=4, spacing=1)
    p.paragraph_format.keep_with_next = True
    p.add_run().add_picture(str(path), width=Cm(w) if w else None, height=Cm(h) if h else None)


def convert(markdown: str, out: Path, base: Path) -> int:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin, sec.right_margin, sec.top_margin, sec.bottom_margin = Cm(2.5), Cm(1.5), Cm(2), Cm(2)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(SIZE)

    lines = markdown.splitlines()
    first_h2 = next((i for i, l in enumerate(lines) if l.startswith("## ")), 0)
    fig, i = 0, 0
    while i < len(lines):
        s = lines[i].strip()
        title = i < first_h2

        if s.startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                p = _para(doc, WD_ALIGN_PARAGRAPH.LEFT, after=0, spacing=1)
                _run(p, lines[i].rstrip(), CODE_SIZE, font=CODE_FONT)
                i += 1
            doc.paragraphs[-1].paragraph_format.space_after = Pt(8)

        elif m := re.match(r"!\[([^\]]*)\]\(([^)]+)\)", s):
            img = (base / m.group(2)).resolve()
            if img.exists():
                fig += 1
                _picture(doc, img)
                cap = _para(doc, WD_ALIGN_PARAGRAPH.CENTER)
                _run(cap, f"Рисунок {fig} — {m.group(1)}", SIZE)

        elif s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            i -= 1
            t = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            t.style = "Table Grid"
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    cp = t.cell(r, c).paragraphs[0]
                    _inline(cp, val, SIZE - 2, bold=True if r == 0 else None)
            _para(doc, after=0)

        elif m := re.match(r"(#{1,4}) (.+)", s):
            text = m.group(2).replace("**", "")
            if title:
                _title_line(doc, text)
            else:
                p = doc.add_heading(level=min(len(m.group(1)) - 1, 3) or 1)
                p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(12), Pt(8)
                p.paragraph_format.keep_with_next = True
                _run(p, text, 14, bold=True)

        elif m := re.match(r"[-*] (.+)", s):
            p = doc.add_paragraph(style="List Bullet")
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.space_after, p.paragraph_format.line_spacing = Pt(2), 1.15
            _inline(p, m.group(1))

        elif s:
            if title:
                _title_line(doc, s)
            elif m := re.match(r"(?:\*\*)?(\d+\.\d+\.)(?:\*\*)? (.+)", s):
                p = _para(doc, after=4)
                p.paragraph_format.left_indent = Cm(0.6)
                _run(p, m.group(1) + " ", bold=True)
                _inline(p, m.group(2))
            elif re.match(r"Файл \S+:$", s.replace("`", "")):
                p = _para(doc, WD_ALIGN_PARAGRAPH.LEFT, after=2)
                p.paragraph_format.keep_with_next = True
                _run(p, s.replace("`", ""), bold=True)
            elif re.match(r"Крок \d+\.", s):
                p = _para(doc, WD_ALIGN_PARAGRAPH.LEFT, after=5)
                p.paragraph_format.keep_with_next = True
                _inline(p, s, bold=True)
            else:
                _inline(_para(doc), s)

        if title and i + 1 == first_h2 and doc.paragraphs:
            doc.paragraphs[-1].paragraph_format.space_after = Pt(14)
        i += 1
    doc.save(out)
    return fig
