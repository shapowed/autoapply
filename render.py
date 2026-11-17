"""Render a (tailored) profile to PDF and DOCX in the original resume's layout.

ATS-safe by design: single column, no tables/text boxes/headers-footers/images,
real text (not outlined), standard section names, contact info in the body.
Adjust STYLE to match your original exactly.
"""
import os

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

STYLE = {
    "font": "Calibri",          # DOCX font; PDF uses Carlito (metric-identical to Calibri)
    "name_size": 20, "header_size": 11.5, "body_size": 10, "margin_in": 0.5,
}

_FONT_DIRS = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"), "/usr/share/fonts/truetype/crosextra", "/usr/share/fonts/google-carlito-fonts",
              "/usr/share/fonts/carlito", os.path.expanduser("~/.local/share/fonts"), os.path.expanduser("~/Library/Fonts"),
              "/Library/Fonts", "C:/Windows/Fonts", "fonts"]


def _register_fonts():
    for d in _FONT_DIRS:
        reg, bold = os.path.join(d, "Carlito-Regular.ttf"), os.path.join(d, "Carlito-Bold.ttf")
        if not os.path.exists(reg):
            reg, bold = os.path.join(d, "calibri.ttf"), os.path.join(d, "calibrib.ttf")
        if os.path.exists(reg) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("Body", reg))
            pdfmetrics.registerFont(TTFont("Body-Bold", bold))
            pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold")
            return "Body", "Body-Bold"
    return "Helvetica", "Helvetica-Bold"


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _contact_line(p):
    c = p["contact"]
    return "  •  ".join([c["email"], c["phone"], c["location"], c.get("linkedin_label", "LinkedIn")])


def _skill_lines(p):
    return [(cat, ", ".join(sk["name"] for sk in items)) for cat, items in p["skills"].items()]


# ------------------------------------------------------------------ PDF
def render_pdf(p, path, body_size=None):
    reg, bold = _register_fonts()
    size = body_size or STYLE["body_size"]
    lead = size * 1.18
    ss = {
        "name": ParagraphStyle("name", fontName=bold, fontSize=STYLE["name_size"], leading=STYLE["name_size"] * 1.2, alignment=TA_CENTER),
        "contact": ParagraphStyle("contact", fontName=reg, fontSize=size, leading=lead, alignment=TA_CENTER),
        "h": ParagraphStyle("h", fontName=bold, fontSize=STYLE["header_size"], leading=STYLE["header_size"] * 1.2, spaceBefore=6),
        "body": ParagraphStyle("body", fontName=reg, fontSize=size, leading=lead),
        "job": ParagraphStyle("job", fontName=bold, fontSize=size, leading=lead, spaceBefore=3),
        "bullet": ParagraphStyle("bullet", fontName=reg, fontSize=size, leading=lead, leftIndent=12, bulletIndent=2,
                                 bulletFontName=reg, bulletFontSize=size),
    }
    m = STYLE["margin_in"] * inch
    doc = SimpleDocTemplate(path, pagesize=LETTER, leftMargin=m, rightMargin=m, topMargin=m, bottomMargin=m,
                            title=f"{p['name']} Resume", author=p["name"])
    link = p["contact"].get("linkedin_url")
    contact = _esc(_contact_line(p))
    if link:
        lab = _esc(p["contact"].get("linkedin_label", "LinkedIn"))
        contact = contact.replace(lab, f'<link href="{_esc(link)}" color="blue">{lab}</link>')

    def header(t):
        return [Paragraph(t, ss["h"]), HRFlowable(width="100%", thickness=0.6, spaceBefore=1, spaceAfter=3)]

    f = [Paragraph(_esc(p["name"]), ss["name"]), Paragraph(contact, ss["contact"])]
    f += header("SUMMARY") + [Paragraph(_esc(p["summary"]), ss["body"])]
    f += header("PROFESSIONAL EXPERIENCE")
    for e in p["experience"]:
        f.append(Paragraph(_esc(f"{e['title']} | {e['company']} | {e['location']} | {e['dates']}"), ss["job"]))
        f += [Paragraph(_esc(b["text"]), ss["bullet"], bulletText="•") for b in e["bullets"]]
    f += header("EDUCATION")
    for ed in p["education"]:
        f.append(Paragraph(f"<b>{_esc(ed['school'])}</b>", ss["body"]))
        f.append(Paragraph(_esc(ed["detail"]), ss["body"]))
        if ed.get("extra"):
            f.append(Paragraph(_esc(ed["extra"]), ss["body"]))
        f.append(Spacer(1, 2))
    f += header("CERTIFICATIONS")
    f += [Paragraph(f"{_esc(n)} — {_esc(d)}", ss["body"]) for n, d in p["certifications"]]
    f += header("TECHNICAL SKILLS")
    f += [Paragraph(f"<b>{_esc(c)}:</b> {_esc(v)}", ss["body"]) for c, v in _skill_lines(p)]
    doc.build(f)
    return path


def page_count(path):
    from pypdf import PdfReader
    return len(PdfReader(path).pages)


def render_pdf_one_page(p, path):
    """Shrink body text slightly (down to 9pt) if needed to stay on one page."""
    for size in (STYLE["body_size"], 9.75, 9.5, 9.25, 9):
        render_pdf(p, path, size)
        if page_count(path) == 1:
            return path, size
    return path, size


# ------------------------------------------------------------------ DOCX
def _hrule(par):
    pPr = par._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    for k, v in {"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": "000000"}.items():
        b.set(qn(k), v)
    bdr.append(b)
    pPr.append(bdr)


def render_docx(p, path, body_size=None):
    size = Pt(body_size or STYLE["body_size"])
    d = Document()
    for s in d.sections:
        s.left_margin = s.right_margin = s.top_margin = s.bottom_margin = Inches(STYLE["margin_in"])
    st = d.styles["Normal"]
    st.font.name, st.font.size = STYLE["font"], size
    st.element.rPr.rFonts.set(qn("w:eastAsia"), STYLE["font"])
    st.paragraph_format.space_after = Pt(0)
    st.paragraph_format.space_before = Pt(0)

    def para(text="", bold=False, sz=None, center=False):
        par = d.add_paragraph()
        if text:
            r = par.add_run(text)
            r.bold, r.font.size = bold, (Pt(sz) if sz else size)
        if center:
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        return par

    def header(t):
        par = para(t, bold=True, sz=STYLE["header_size"])
        par.paragraph_format.space_before = Pt(6)
        par.paragraph_format.space_after = Pt(3)
        _hrule(par)

    para(p["name"], bold=True, sz=STYLE["name_size"], center=True)
    para(_contact_line(p), center=True)
    header("SUMMARY")
    para(p["summary"])
    header("PROFESSIONAL EXPERIENCE")
    for e in p["experience"]:
        par = para(f"{e['title']} | {e['company']} | {e['location']} | {e['dates']}", bold=True)
        par.paragraph_format.space_before = Pt(3)
        for b in e["bullets"]:
            bp = d.add_paragraph(style="List Bullet")
            bp.add_run(b["text"]).font.size = size
    header("EDUCATION")
    for ed in p["education"]:
        para(ed["school"], bold=True)
        para(ed["detail"])
        if ed.get("extra"):
            para(ed["extra"])
    header("CERTIFICATIONS")
    for n, dt in p["certifications"]:
        para(f"{n} — {dt}")
    header("TECHNICAL SKILLS")
    for c, v in _skill_lines(p):
        par = d.add_paragraph()
        par.add_run(f"{c}: ").bold = True
        par.add_run(v)
    d.save(path)
    return path
