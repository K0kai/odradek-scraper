"""Render saved cover letters as upload-ready PDF documents."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


# ReportLab ships this font, so exports work on Windows and hosted deployments.
pdfmetrics.registerFont(TTFont(
    "CoverLetter", str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf")
))


def render_cover_letter_pdf(body: str) -> bytes:
    """Preserve plain-text paragraphs and wrap/paginate without truncating."""
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=54, leftMargin=54,
        topMargin=54, bottomMargin=54, title="Cover letter",
    )
    style = ParagraphStyle("Letter", fontName="CoverLetter", fontSize=11, leading=16)
    story = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        story.append(Paragraph(escape(line), style) if line.strip() else Spacer(1, 12))
    document.build(story)
    return output.getvalue()
