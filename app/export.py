from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from markdown_it import MarkdownIt
from slugify import slugify

from app.config import EXPORTS_DIR, PROJECT_ROOT
from app.models import PolishedTranscript

logger = logging.getLogger(__name__)

PRINT_CSS_PATH = PROJECT_ROOT / "app" / "static" / "print.css"

try:
    from weasyprint import HTML  # type: ignore
    _weasyprint_error: str | None = None
except (ImportError, OSError) as e:  # missing native libs (Pango, Cairo)
    HTML = None  # type: ignore
    _weasyprint_error = str(e)


def _render_markdown(polished: PolishedTranscript) -> str:
    out: list[str] = [
        f"# {polished.title}",
        "",
        f"> {polished.summary}",
        "",
        "---",
        "",
    ]
    for section in polished.sections:
        out.append(f"## {section.header}")
        out.append(f"_{section.timestamp}_")
        out.append("")
        for p in section.paragraphs:
            prefix = " · ".join(filter(None, [p.speaker, p.timestamp]))
            out.append(f"**{prefix}** {p.text}")
            out.append("")
    return "\n".join(out)


def _wrap_html(body_html: str, title: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f"<title>{title}</title>"
        "</head>\n<body>\n"
        f"{body_html}\n"
        "</body>\n</html>"
    )


def export_md_and_pdf(polished: PolishedTranscript) -> tuple[Path, Path]:
    """Render polished transcript to <slug>_<date>.md and .pdf in EXPORTS_DIR."""
    if HTML is None:
        raise RuntimeError(
            f"WeasyPrint unavailable: {_weasyprint_error}. "
            "On macOS: brew install pango"
        )

    slug = slugify(polished.title, max_length=60) or "transcript"
    date = datetime.now().strftime("%Y-%m-%d")
    stem = f"{slug}_{date}"

    md_path = EXPORTS_DIR / f"{stem}.md"
    pdf_path = EXPORTS_DIR / f"{stem}.pdf"

    md_text = _render_markdown(polished)
    md_path.write_text(md_text)

    md_renderer = MarkdownIt("commonmark", {"breaks": False, "html": False})
    body_html = md_renderer.render(md_text)
    html_doc = _wrap_html(body_html, polished.title)

    HTML(string=html_doc).write_pdf(
        pdf_path,
        stylesheets=[str(PRINT_CSS_PATH)],
    )

    return md_path, pdf_path
