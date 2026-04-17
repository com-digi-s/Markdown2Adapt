#!/usr/bin/env python3
"""md2adapt.py — Generate Adapt Framework JSON from Markdown.

Usage:
  python md2adapt_hardened_no404.py INPUT.md --out course/en --lang en --menu "Main menu"

This version keeps the original structure but hardens asset migration and fixes
several correctness issues:
- no third-party runtime dependencies
- deterministic asset naming without basename collisions
- optional multi-root asset lookup via --asset-root, plus fallback moved-asset search
- robuster inline/reference markdown link and image rewriting
- skips fenced code blocks and inline code when rewriting markdown
- course-relative URLs only (never filesystem paths)
- unresolved assets are non-fatal by default; original references are kept
- optional --strict-assets to fail on unresolved assets
- optional remote vendoring with timeouts and size limits
- fixed duplicate/contradictory functions and a few type/correctness bugs
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

HEX24_RE = re.compile(r"^[0-9a-f]{24}$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
MARKER_RE = re.compile(r"^\[(\w+)\]\s*(.*)$", re.IGNORECASE)
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
REFERENCE_DEF_RE = re.compile(
    r'^\s{0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|([^\s]+))(?:\s+("[^"]*"|\([^)]*\)|\'[^\']*\'))?\s*$'
)

# -------- Minimal Markdown-to-HTML (safe default) --------
def md_to_html(md: str) -> str:
    """Very small markdown→html converter (paragraphs + lists + links + images)."""
    lines = md.strip().splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        heading = HEADING_RE.match(line.strip())
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline_md(heading.group(2).strip())}</h{level}>")
            i += 1
            continue
        if re.match(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$", line):
            m = re.match(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
            assert m is not None
            out.append(render_image_html(m.group(2), m.group(1), block=True))
            i += 1
            continue
        fence = FENCE_RE.match(line)
        if fence:
            fence_char = fence.group(1)[0]
            code_lines = []
            i += 1
            while i < len(lines) and not (FENCE_RE.match(lines[i]) and FENCE_RE.match(lines[i]).group(1)[0] == fence_char):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            code = html.escape("\n".join(code_lines))
            out.append(f"<pre><code>{code}</code></pre>")
            continue
        if re.match(r"^\s*>\s?", line):
            quote_lines = []
            while i < len(lines) and re.match(r"^\s*>\s?", lines[i]):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]).rstrip())
                i += 1
            out.append("<blockquote>" + md_to_html("\n".join(quote_lines)) + "</blockquote>")
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                item = re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip()
                items.append(f"<li>{inline_md(item)}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                item = re.sub(r"^\s*\d+\.\s+", "", lines[i]).strip()
                items.append(f"<li>{inline_md(item)}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:-]+\|[\s|:-]*\|?\s*$", lines[i + 1]):
            header_cells = [inline_md(cell.strip()) for cell in line.strip().strip("|").split("|")]
            i += 2
            body_rows = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                row_cells = [inline_md(cell.strip()) for cell in lines[i].strip().strip("|").split("|")]
                body_rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row_cells) + "</tr>")
                i += 1
            out.append(
                "<table><thead><tr>"
                + "".join(f"<th>{cell}</th>" for cell in header_cells)
                + "</tr></thead><tbody>"
                + "".join(body_rows)
                + "</tbody></table>"
            )
            continue
        para = [line]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not HEADING_RE.match(lines[i].strip())
            and not re.match(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$", lines[i])
            and not re.match(r"^\s*>\s?", lines[i])
            and not re.match(r"^\s*[-*+]\s+", lines[i])
            and not re.match(r"^\s*\d+\.\s+", lines[i])
            and not ("|" in lines[i] and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:-]+\|[\s|:-]*\|?\s*$", lines[i + 1]))
        ):
            para.append(lines[i].rstrip())
            i += 1
        out.append("<p>" + inline_md(" ".join(para).strip()) + "</p>")
    return "".join(out)


def inline_md(text: str) -> str:
    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', lambda m: render_image_html(m.group(2), m.group(1)), text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    def _link(m: re.Match[str]) -> str:
        label = m.group(1)
        href = m.group(2)
        if href.startswith(("http://", "https://")):
            return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'
        if href.startswith(("#", "mailto:", "tel:")):
            return f'<a href="{href}">{label}</a>'
        return f'<a href="{href}" download>{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^\s)]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\)", _link, text)

    # Autolink bare URLs (not already inside an href="…" or >…</a>)
    def _autolink(m: re.Match[str]) -> str:
        url = m.group(0)
        safe = html.escape(url, quote=True)
        return f'<a href="{safe}" class="md-url" target="_blank" rel="noopener noreferrer">{safe}</a>'

    text = re.sub(r'(?<!["\'=>])\bhttps?://[^\s<>"\')\]]+', _autolink, text)
    return text


def render_image_html(src: str, alt: str, block: bool = False) -> str:
    safe_src = html.escape(src, quote=True)
    safe_alt = html.escape(alt.strip(), quote=True)
    img = (
        f'<img src="{safe_src}" alt="{safe_alt}" title="{safe_alt}" '
        f'class="md-image{" md-image--block" if block else ""}" style="max-width:100%">'
    )
    if not block:
        return img
    caption = ""
    if safe_alt:
        caption = (
            '<figcaption class="md-image__caption" '
            'style="display:block;margin-top:0.5rem;font-size:0.9em;line-height:1.4;">'
            f"{safe_alt}"
            "</figcaption>"
        )
    return '<figure class="md-image-figure" style="margin:1rem 0;">' + img + caption + "</figure>"


def extract_first_image(md: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    match = IMAGE_RE.search(md)
    if not match:
        return md, None
    updated_md = md[: match.start()] + md[match.end() :]
    return updated_md, (match.group(1).strip(), match.group(2).strip())


def build_hero_markdown(title: str, image: Optional[Tuple[str, str]]) -> str:
    parts: List[str] = []
    clean_title = title.strip()
    if clean_title:
        parts.append(f"# {clean_title}")
    if image:
        alt, src = image
        parts.append(f"![{alt}]({src})")
    return "\n\n".join(parts).strip()


def split_marker(title: str) -> Tuple[Optional[str], str]:
    m = MARKER_RE.match(title.strip())
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return None, title.strip()


def gen_hex24(n: int) -> str:
    h = format(n, "x")[-24:]
    return h.rjust(24, "0")


class IdSpace:
    def __init__(self) -> None:
        self._next = 1

    def new(self) -> str:
        i = self._next
        self._next += 1
        return gen_hex24(i)


# -------- Parse sections --------
@dataclass
class Section:
    level: int
    title: str
    start: int
    end: Optional[int] = None


def parse_headings(md: str) -> List[Section]:
    lines = md.splitlines()
    sections: List[Section] = []
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            sections.append(Section(level=len(m.group(1)), title=m.group(2).strip(), start=idx))
    for i, s in enumerate(sections):
        s.end = sections[i + 1].start if i + 1 < len(sections) else len(lines)
    return sections


def slice_text(md: str, start: int, end: Optional[int]) -> str:
    lines = md.splitlines()[start : len(md.splitlines()) if end is None else end]
    if lines and HEADING_RE.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def get_section_extent(section: Section, sections: List[Section], total_lines: int) -> int:
    for s in sections:
        if s.start > section.start and s.level <= section.level:
            return s.start
    return total_lines


def get_first_child_start(section: Section, sections: List[Section], total_lines: int) -> int:
    section_end = get_section_extent(section, sections, total_lines)
    for s in sections:
        if section.start < s.start < section_end and s.level > section.level:
            return s.start
    return section_end


def get_section_text(md: str, section: Section, sections: List[Section], total_lines: int) -> str:
    return slice_text(md, section.start, get_section_extent(section, sections, total_lines))


def get_section_intro(md: str, section: Section, sections: List[Section], total_lines: int) -> str:
    lines = md.splitlines()
    start = section.start + 1
    end = get_first_child_start(section, sections, total_lines)
    return "\n".join(lines[start:end]).strip()


# -------- Adapt object builders --------
def course_template(
    title: str,
    lang: str,
    total_score: int,
    total_correct: int,
    pass_score_ratio: float = 0.6,
    is_percentage_based: bool = True,
) -> Dict[str, Any]:
    return {
        "_id": "course",
        "_type": "course",
        "title": title,
        "displayTitle": "",
        "description": "",
        "_defaultLanguage": lang,
        "_defaultDirection": "ltr",
        "_mcq": {
            "ariaRegion": "Multiple-Choice-Frage",
            "ariaCorrectAnswer": "Die richtige Antwort lautet {{{correctAnswer}}}",
            "ariaCorrectAnswers": "Die richtigen Antworten lauten {{{correctAnswer}}}",
            "ariaUserAnswer": "Ihre gewählte Antwort war {{{userAnswer}}}",
            "ariaUserAnswers": "Ihre gewählten Antworten waren {{{userAnswer}}}",
        },
        "_buttons": {
            "_submit": {"buttonText": "Antwort abgeben", "ariaLabel": "Antwort abgeben"},
            "_reset": {"buttonText": "Zurücksetzen", "ariaLabel": "Zurücksetzen"},
            "_showCorrectAnswer": {"buttonText": "Richtige Antwort anzeigen", "ariaLabel": "Richtige Antwort anzeigen"},
            "_hideCorrectAnswer": {"buttonText": "Meine Antwort anzeigen", "ariaLabel": "Meine Antwort anzeigen"},
            "_showFeedback": {"buttonText": "Feedback anzeigen", "ariaLabel": "Feedback anzeigen"},
            "remainingAttemptsText": "Verbleibende Versuche",
            "remainingAttemptText": "Letzter Versuch",
            "disabledAriaLabel": "Dieser Button ist derzeit nicht verfügbar.",
        },
        "_assessment": {
            "_scoreToPass": int(pass_score_ratio * 100) if is_percentage_based else int(pass_score_ratio * total_score),
            "_correctToPass": int(pass_score_ratio * 100) if is_percentage_based else int(pass_score_ratio * total_correct),
            "_isPercentageBased": is_percentage_based,
        },
        "_requireCompletionOf": -1,
        "_globals": {
            "_accessibility": {
                "skipNavigationText": "Navigation überspringen",
                "_ariaLabels": {
                    "answeredIncorrectly": "Sie haben falsch geantwortet",
                    "answeredCorrectly": "Sie haben richtig geantwortet",
                    "selectedAnswer": "ausgewählt",
                    "unselectedAnswer": "nicht ausgewählt",
                    "skipNavigation": "Navigation überspringen",
                    "previous": "Zurück",
                    "navigationDrawer": "Öffne Zusatzmaterialien",
                    "close": "Schließen",
                    "closeDrawer": "Drawer schließen",
                    "closeResources": "Zusatzmaterialien schließen",
                    "drawer": "Anfang des Drawers",
                    "closePopup": "Pop-up schließen",
                    "next": "Nächstes Element",
                    "done": "Fertig",
                    "complete": "Abgeschlossen",
                    "incomplete": "Nicht vollständig",
                    "incorrect": "Falsch",
                    "correct": "Richtig",
                    "locked": "Nicht verfügbar",
                    "visited": "Besucht",
                    "required": "Required",
                    "optional": "Optional",
                },
                "altFeedbackTitle": "Feedback",
            }
        },
    }


def menu_template(_id: str, title: str) -> Dict[str, Any]:
    return {"_type": "menu", "_id": _id, "_parentId": "course", "title": title, "displayTitle": title, "linkText": "Starten"}


def page_template(_id: str, parent_id: str, title: str) -> Dict[str, Any]:
    return {"_type": "page", "_id": _id, "_parentId": parent_id, "title": title, "displayTitle": title, "linkText": "Starten"}


def article_template(_id: str, parent_id: str, title: str, body: str) -> Dict[str, Any]:
    title = title.strip()
    d: Dict[str, Any] = {
        "_id": _id,
        "_parentId": parent_id,
        "_type": "article",
        "body": md_to_html(body),
        "_articleBlockSlider": {"_isEnabled": False, "_hasTabs": False},
        "_assessment": {
            "_isEnabled": False,
            "_id": "",
            "_suppressMarking": False,
            "_scoreToPass": 60,
            "_correctToPass": 60,
            "_isPercentageBased": True,
            "_includeInTotalScore": False,
            "_assessmentWeight": 1,
            "_isResetOnRevisit": False,
            "_attempts": 1,
            "_allowResetIfPassed": False,
            "_scrollToOnReset": False,
            "_banks": {"_isEnabled": False, "_split": "", "_randomisation": False},
            "_randomisation": {"_isEnabled": False, "_blockCount": 0},
            "_questions": {"_resetType": "soft", "_canShowFeedback": True, "_canShowMarking": True, "_canShowModelAnswer": False},
        },
    }
    if title:
        d["title"] = title
        d["displayTitle"] = title
    else:
        d["title"] = ""
        d["displayTitle"] = ""
        d["_isDisplayTitle"] = False
    return d


def block_template(_id: str, parent_id: str, track_id: int, title: str, body: str) -> Dict[str, Any]:
    return {
        "_id": _id,
        "_parentId": parent_id,
        "_type": "block",
        "title": title,
        "body": md_to_html(body),
        "displayTitle": title,
        "_trackingId": track_id,
        "_pageLevelProgress": {"_isEnabled": False},
    }


def component_common(_id: str, parent_id: str, comp: str, title: str) -> Dict[str, Any]:
    title = title.strip()
    d: Dict[str, Any] = {
        "_type": "component",
        "_component": comp,
        "_id": _id,
        "_parentId": parent_id,
        "_layout": "full",
        "_classes": "",
        "_isOptional": False,
        "_isAvailable": True,
        "_isHidden": False,
        "_isVisible": True,
        "_isResetOnRevisit": False,
    }
    if title:
        d["title"] = title
        d["displayTitle"] = title
    else:
        d["title"] = ""
        d["displayTitle"] = ""
        d["_isDisplayTitle"] = False
    return d


def text_component(_id: str, parent_id: str, title: str, markdown_body: str) -> Dict[str, Any]:
    d = component_common(_id, parent_id, "text", title)
    d["body"] = md_to_html(markdown_body)
    return d


def get_mcq_button_object(show_feedback: bool = False) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "_submit": {"buttonText": "", "ariaLabel": ""},
        "_reset": {"buttonText": "Zurücksetzen", "ariaLabel": ""},
        "_showCorrectAnswer": {"buttonText": "", "ariaLabel": ""},
        "_hideCorrectAnswer": {"buttonText": "", "ariaLabel": ""},
        "remainingAttemptsText": "",
        "remainingAttemptText": "",
    }
    if show_feedback:
        d["_showFeedback"] = {"buttonText": "", "ariaLabel": ""}
    return d


def get_slider_button_object() -> Dict[str, Any]:
    return {"_submit": {"buttonText": "", "ariaLabel": ""}}


def mcq_component(
    _id: str,
    parent_id: str,
    title: str,
    instruction_html: str,
    items: List[Tuple[str, bool]],
    feedback: Optional[str] = None,
) -> Dict[str, Any]:
    d = component_common(_id, parent_id, "mcq", title)
    d["body"] = ""
    d["instruction"] = instruction_html or ""
    defaults = {"_isPartlyCorrect": False, "feedback": ""}
    d["_items"] = [dict(text=t, _shouldBeSelected=ok, **defaults) for t, ok in items]
    d["_attempts"] = 1
    d["_selectable"] = 1
    d["_shouldDisplayAttempts"] = False
    d["_canShowModelAnswer"] = True
    d["_canShowMarking"] = True
    d["_isRandom"] = True
    d["_recordInteraction"] = True
    d["_hasItemScoring"] = False
    d["_questionWeight"] = 1
    d["_tutor"] = {
        "_isInherited": True,
        "_type": "inline",
        "_classes": "",
        "_hasNotifyBottomButton": False,
        "_button": {"text": "{{_globals._extensions._tutor.hideFeedback}}", "ariaLabel": "{{_globals._extensions._tutor.hideFeedback}}"},
    }
    if feedback is None:
        d["_canShowFeedback"] = False
    else:
        d["_canShowFeedback"] = True
        d["_feedback"] = {
            "title": "Feedback",
            "correct": f"Korrekt! {feedback}",
            "_incorrect": {"final": f"Leider nicht. {feedback}", "notFinal": ""},
            "_partlyCorrect": {"final": f"Zum Teil richtig. {feedback}", "notFinal": ""},
        }
    d["_buttons"] = get_mcq_button_object(bool(feedback))
    return d


def slider_component(_id: str, parent_id: str, title: str, min_v: int, max_v: int, label_start: str, label_end: str, body: str = "") -> Dict[str, Any]:
    d = component_common(_id, parent_id, "slider", title)
    d["body"] = md_to_html(body) if body else ""
    d["_scaleStart"] = int(min_v)
    d["_scaleEnd"] = int(max_v)
    d["_scaleStep"] = 1
    d["labelStart"] = label_start
    d["labelEnd"] = label_end
    d["instruction"] = ""
    d["_correctRange"] = {"_bottom": int(min_v), "_top": int(max_v)}
    d["_buttons"] = get_slider_button_object()
    d["_canShowFeedback"] = False
    return d


def matching_component(
    _id: str,
    parent_id: str,
    title: str,
    instruction_html: str,
    items: List[Dict[str, Any]],
    feedback: Optional[str] = None,
    placeholder: str = "Bitte wählen Sie eine Option",
) -> Dict[str, Any]:
    d = component_common(_id, parent_id, "matching", title)
    d["instruction"] = instruction_html or ""
    d["ariaQuestion"] = title
    d["_attempts"] = 1
    d["_shouldDisplayAttempts"] = False
    d["_shouldResetAllAnswers"] = True
    d["_isRandom"] = False
    d["_isRandomQuestionOrder"] = False
    d["_questionWeight"] = 1
    d["_canShowModelAnswer"] = True
    d["_canShowCorrectness"] = False
    d["_canShowFeedback"] = False
    d["_canShowMarking"] = True
    d["_recordInteraction"] = True
    d["placeholder"] = placeholder
    d["_allowOnlyUniqueAnswers"] = False
    d["_hasItemScoring"] = False
    d["_items"] = items
    if feedback is not None:
        d["_canShowFeedback"] = True
        d["_feedback"] = {
            "title": "Feedback",
            "correct": "Korrekt! " + feedback,
            "_incorrect": {"final": "Leider nicht. " + feedback, "notFinal": ""},
            "_partlyCorrect": {"final": "Zum Teil richtig. " + feedback, "notFinal": ""},
        }
    return d


def reflection_component(
    _id: str,
    parent_id: str,
    title: str,
    prompt_html: str,
    placeholder: str = "Schreiben Sie hier Ihre Gedanken…",
    feedback: Optional[str] = None,
) -> Dict[str, Any]:
    d = component_common(_id, parent_id, "textinput", title)
    d["_classes"] = "md-reflection"
    d["body"] = prompt_html
    d["instruction"] = ""
    d["ariaQuestion"] = title
    d["_attempts"] = 1
    d["_shouldDisplayAttempts"] = False
    d["_isRandom"] = False
    d["_questionWeight"] = 0
    d["_canShowModelAnswer"] = False
    d["_canShowCorrectness"] = False
    d["_canShowMarking"] = False
    d["_canShowFeedback"] = True
    d["_recordInteraction"] = False
    d["_allowsAnyCase"] = True
    d["_allowsPunctuation"] = True
    d["_items"] = [{"placeholder": placeholder, "_answers": [""]}]
    d["_buttons"] = {"_submit": {"buttonText": "Abgeben", "ariaLabel": "Abgeben"}}
    d["_tutor"] = {
        "_isInherited": False,
        "_isEnabled": True,
        "_type": "notify",
        "_classes": "",
        "_hasNotifyBottomButton": False,
        "_button": {"text": "{{_globals._extensions._tutor.hideFeedback}}", "ariaLabel": "{{_globals._extensions._tutor.hideFeedback}}"},
    }
    fb = feedback or "Vielen Dank für Ihre Gedanken!"
    d["_feedback"] = {
        "title": "Feedback",
        "correct": fb,
        "_incorrect": {"final": fb, "notFinal": ""},
        "_partlyCorrect": {"final": fb, "notFinal": ""},
    }
    return d


MCQ_OPT_RES = [
    re.compile(r"^\s*[-*+]\s*\[(x|X| )\]\s*(.+)\s*$"),
    re.compile(r"^\s*\[(x|X| )\]\s*(.+)\s*$"),
]
INSTR_RES = [
    re.compile(r"^\s*(?:\*\*)?\s*Instruction\s*:\s*(.*)$", re.IGNORECASE),
    re.compile(r"^\s*(?:\*\*)?\s*Anweisung\s*:\s*(.*)$", re.IGNORECASE),
]


def parse_mcq_chunk(md: str) -> Tuple[str, List[Tuple[str, bool]], Optional[str]]:
    lines = [ln.rstrip() for ln in md.strip().splitlines()]
    instruction_lines: List[str] = []
    items: List[Tuple[str, bool]] = []
    feedback: Optional[str] = None
    saw_option = False

    for ln in lines:
        stripped = ln.strip()
        if stripped.lower().startswith("feedback:"):
            feedback = stripped[len("feedback:") :].strip()
            continue

        matched = False
        for rx in MCQ_OPT_RES:
            m = rx.match(ln)
            if m:
                items.append((m.group(2).strip(), m.group(1).lower() == "x"))
                saw_option = True
                matched = True
                break
        if matched:
            continue

        if not saw_option:
            for ri in INSTR_RES:
                mi = ri.match(ln)
                if mi:
                    instruction_lines.append(mi.group(1).strip())
                    matched = True
                    break
            if matched:
                continue
            if stripped:
                instruction_lines.append(ln)

    instruction_html = md_to_html("\n".join(instruction_lines)) if instruction_lines else ""
    return instruction_html, items, feedback


SL_SCALE_RE = re.compile(r"^\s*scale\s*:\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*$", re.IGNORECASE)
SL_LABEL_RE = re.compile(r'^\s*(labelStart|labelEnd)\s*:\s*"(.*)"\s*$', re.IGNORECASE)


def parse_slider_chunk(md: str) -> Tuple[int, int, str, str, str]:
    min_v, max_v = 1, 10
    label_start, label_end = "1", "10"
    preamble_lines: List[str] = []
    for ln in md.strip().splitlines():
        m = SL_SCALE_RE.match(ln)
        if m:
            min_v, max_v = int(m.group(1)), int(m.group(2))
            continue
        m2 = SL_LABEL_RE.match(ln)
        if m2:
            key, val = m2.group(1), m2.group(2)
            if key.lower() == "labelstart":
                label_start = val
            else:
                label_end = val
            continue
        if ln.strip():
            preamble_lines.append(ln)
    return min_v, max_v, label_start, label_end, "\n".join(preamble_lines).strip()


def parse_matching_chunk(md: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    lines = md.splitlines()
    instruction_lines: List[str] = []
    feedback: Optional[str] = None
    items: List[Dict[str, Any]] = []
    current_question: Optional[str] = None
    current_options: List[Dict[str, Any]] = []
    saw_question = False

    for ln in lines:
        stripped = ln.strip()
        if stripped.lower().startswith("type:") and "matching" in stripped.lower():
            continue
        if stripped.lower().startswith("instruction:"):
            instruction_lines.append(stripped[len("instruction:") :].strip())
            continue
        if stripped.lower().startswith("feedback:"):
            feedback = stripped[len("feedback:") :].strip()
            continue
        m_q = re.match(r"^-\s+(.+)$", ln)
        if m_q:
            if current_question is not None and current_options:
                items.append({"text": current_question, "_options": current_options})
            current_question = m_q.group(1).strip()
            current_options = []
            saw_question = True
            continue
        m_opt = re.match(r"^[ \t]+[-*+] \[(x| )\]\s*(.+)", ln)
        if m_opt and current_question is not None:
            current_options.append({"text": m_opt.group(2).strip(), "_isCorrect": m_opt.group(1).lower() == "x"})
            continue
        if not saw_question and stripped:
            instruction_lines.append(ln)

    if current_question is not None and current_options:
        items.append({"text": current_question, "_options": current_options})

    instruction_html = md_to_html("\n".join(instruction_lines)) if instruction_lines else ""
    return instruction_html, items, feedback


def _looks_like_matching(md: str) -> bool:
    return any(ln.strip().lower() == "type: matching" for ln in md.strip().splitlines())


def _looks_like_reflection(md: str) -> bool:
    return any(re.match(r"^\s*Type\s*:\s*Reflection\s*$", ln, re.IGNORECASE)
               for ln in md.strip().splitlines())


def parse_reflection_chunk(md: str) -> Tuple[str, str, Optional[str]]:
    """Return (prompt_html, placeholder, feedback_or_None)."""
    lines = md.strip().splitlines()
    prompt_lines: List[str] = []
    placeholder = "Write your thoughts here…"
    feedback: Optional[str] = None
    for ln in lines:
        if re.match(r"^\s*Type\s*:\s*Reflection\s*$", ln, re.IGNORECASE):
            continue
        m = re.match(r"^\s*Placeholder\s*:\s*(.+)$", ln, re.IGNORECASE)
        if m:
            placeholder = m.group(1).strip().strip('"')
            continue
        m = re.match(r"^\s*Feedback\s*:\s*(.+)$", ln, re.IGNORECASE)
        if m:
            feedback = m.group(1).strip()
            continue
        prompt_lines.append(ln)
    prompt_html = md_to_html("\n".join(prompt_lines)) if prompt_lines else ""
    return prompt_html, placeholder, feedback


def _looks_like_mcq(md: str) -> bool:
    if _looks_like_matching(md) or _looks_like_reflection(md):
        return False
    return any(rx.match(ln) for ln in md.strip().splitlines() for rx in MCQ_OPT_RES)


def _looks_like_slider(md: str) -> bool:
    return any(SL_SCALE_RE.match(ln) or SL_LABEL_RE.match(ln) for ln in md.strip().splitlines())


_ACCORDION_ITEM_RE = re.compile(r"^\*\*(.+)\*\*\s*$")

def _looks_like_accordion(md: str) -> bool:
    return any(re.match(r"^\s*Type\s*:\s*Accordion\s*$", ln, re.IGNORECASE)
               for ln in md.strip().splitlines())


def parse_accordion_chunk(md: str) -> Tuple[str, List[Dict[str, str]]]:
    """Return (preamble_md, items) where preamble is text before the first
    **Bold Title** and items are {title, body} dicts for each accordion entry."""
    items: List[Dict[str, str]] = []
    current_title: Optional[str] = None
    body_lines: List[str] = []
    preamble_lines: List[str] = []

    for ln in md.strip().splitlines():
        if re.match(r"^\s*Type\s*:\s*Accordion\s*$", ln, re.IGNORECASE):
            continue
        m = _ACCORDION_ITEM_RE.match(ln.strip())
        if m:
            if current_title is not None:
                items.append({"title": current_title, "body": md_to_html("\n".join(body_lines).strip())})
            current_title = m.group(1).strip()
            body_lines = []
        else:
            if current_title is not None:
                body_lines.append(ln)
            else:
                preamble_lines.append(ln)

    if current_title is not None:
        items.append({"title": current_title, "body": md_to_html("\n".join(body_lines).strip())})

    return "\n".join(preamble_lines).strip(), items


def accordion_component(
    _id: str,
    parent_id: str,
    title: str,
    preamble: str,
    items: List[Dict[str, str]],
) -> Dict[str, Any]:
    d = component_common(_id, parent_id, "accordion", title)
    d["body"] = md_to_html(preamble) if preamble else ""
    d["instruction"] = ""
    d["_items"] = [
        {
            "title": item["title"],
            "body": item["body"],
            "_graphic": {"src": "", "alt": ""},
            "_classes": "",
        }
        for item in items
    ]
    return d


def _looks_like_component(md: str) -> bool:
    return _looks_like_mcq(md) or _looks_like_slider(md) or _looks_like_matching(md) or _looks_like_reflection(md) or _looks_like_accordion(md)


def _dispatch_component(comp_id: str, block_id: str, title: str, chunk: str) -> Dict[str, Any]:
    """Return the right component object based on chunk content."""
    if _looks_like_mcq(chunk):
        instr_html, items, feedback = parse_mcq_chunk(chunk)
        if items:
            return mcq_component(comp_id, block_id, title, instr_html, items, feedback)
    elif _looks_like_slider(chunk):
        min_v, max_v, lstart, lend, body = parse_slider_chunk(chunk)
        return slider_component(comp_id, block_id, title, min_v, max_v, lstart, lend, body)
    elif _looks_like_matching(chunk):
        instr_html, items, feedback = parse_matching_chunk(chunk)
        if items:
            return matching_component(comp_id, block_id, title, instr_html, items, feedback)
    elif _looks_like_reflection(chunk):
        prompt_html, placeholder, feedback = parse_reflection_chunk(chunk)
        return reflection_component(comp_id, block_id, title, prompt_html, placeholder, feedback)
    elif _looks_like_accordion(chunk):
        preamble, acc_items = parse_accordion_chunk(chunk)
        if acc_items:
            return accordion_component(comp_id, block_id, title, preamble, acc_items)
    return text_component(comp_id, block_id, title, chunk)


# -------- Tiny front-matter (optional) --------
def strip_frontmatter(md: str) -> str:
    lines = md.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return md
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1 :]).lstrip("\n")
    return md


def _extract_meta_titles(md: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for line in md.splitlines():
        stripped = line.strip()
        if HEADING_RE.match(line):
            break
        if stripped in ("---", "") or stripped.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z][\w-]*):\s*"(.*)"\s*$', stripped)
        if m:
            meta[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^([A-Za-z][\w-]*):\s*(.+)$", stripped)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


# -------- Helpers for the H2-[block] special case --------
def find_h2_block_sections(sections: List[Section], total_lines: int) -> List[Section]:
    result: List[Section] = []
    for i, s in enumerate(sections):
        if s.level == 2:
            marker, _ = split_marker(s.title)
            if marker == "block":
                end = total_lines
                for j in range(i + 1, len(sections)):
                    if sections[j].level == 2:
                        end = sections[j].start
                        break
                result.append(Section(level=2, title=s.title, start=s.start, end=end))
    return result


def _get_all_h2_sections(sections: List[Section], total_lines: int) -> List[Section]:
    result: List[Section] = []
    h2s = [s for s in sections if s.level == 2]
    for i, s in enumerate(h2s):
        end = h2s[i + 1].start if i + 1 < len(h2s) else total_lines
        result.append(Section(level=2, title=s.title, start=s.start, end=end))
    return result


def _get_h3_sections_within_h2(sections: List[Section], h2_sections: List[Section], total_lines: int) -> List[Section]:
    nested: List[Section] = []
    h3s = [s for s in sections if s.level == 3]
    for h3 in h3s:
        for h2 in h2_sections:
            h2_end = h2.end if h2.end is not None else total_lines
            if h2.start < h3.start < h2_end:
                nested.append(h3)
                break
    return nested


def _has_front_matter_block_structure(md: str, h1s: List[Section], h2_sections: List[Section], sections: List[Section]) -> bool:
    if len(h1s) != 1 or not h2_sections:
        return False
    if any(s.level == 4 for s in sections):
        return False
    meta = _extract_meta_titles(md)
    return any(key in meta for key in ("courseTitle", "parentMenuTitle", "pageTitle", "articleTitle"))


def _matches_sample_block_structure(h1s: List[Section], h2_sections: List[Section], nested_h3s: List[Section], sections: List[Section]) -> bool:
    return len(h1s) == 1 and len(h2_sections) == 1 and bool(nested_h3s) and not any(s.level == 4 for s in sections)


def _should_use_h2_blocks(md: str, sections: List[Section], total_lines: int) -> bool:
    h2_sections = _get_all_h2_sections(sections, total_lines)
    if not h2_sections:
        return False
    if any(split_marker(s.title)[0] == "block" for s in h2_sections):
        return True
    h1s = [s for s in sections if s.level == 1]
    if _has_front_matter_block_structure(md, h1s, h2_sections, sections):
        return True
    nested_h3s = _get_h3_sections_within_h2(sections, h2_sections, total_lines)
    if not nested_h3s:
        return False
    for h3 in nested_h3s:
        if _looks_like_component(get_section_text(md, h3, sections, total_lines)):
            return True
    return _matches_sample_block_structure(h1s, h2_sections, nested_h3s, sections)


# -------- Asset migration --------
class AssetError(Exception):
    pass


@dataclass(frozen=True)
class AssetConfig:
    project_root: Path
    course_assets_rel: str   # filesystem path relative to project_root (e.g. src/course/en/assets)
    course_assets_url: str   # URL path served by HTTP (e.g. course/en/assets, no leading src/)
    lookup_roots: Tuple[Path, ...]
    download_remote_assets: bool = False
    allow_missing_assets: bool = True
    remote_timeout: int = 15
    max_remote_bytes: int = 25_000_000


@dataclass
class AssetMigrationReport:
    copied: List[Dict[str, str]]
    downloaded: List[Dict[str, str]]
    skipped_remote: List[str]
    unresolved: List[str]


class AssetResolved(NamedTuple):
    url: str
    copied: bool
    downloaded: bool


SAFE_SCHEMES = ("http", "https", "mailto", "tel", "data", "javascript")

UNRESOLVED_IMAGE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450" viewBox="0 0 800 450" role="img" aria-label="Missing image"><rect width="800" height="450" fill="#f4f4f4"/><rect x="24" y="24" width="752" height="402" fill="#ffffff" stroke="#cccccc" stroke-width="2"/><path d="M120 330l110-110 80 80 120-140 170 170" fill="none" stroke="#9aa0a6" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/><circle cx="260" cy="150" r="34" fill="#d0d7de"/><text x="400" y="392" text-anchor="middle" font-family="Arial, sans-serif" font-size="26" fill="#666666">Missing image</text></svg>"""


def _normalize_lookup_roots(md_dir: Path, extra_roots: Sequence[Path]) -> Tuple[Path, ...]:
    seen: Dict[str, Path] = {}
    roots = [md_dir, *extra_roots]
    for root in roots:
        try:
            rp = root.resolve()
        except FileNotFoundError:
            rp = root.absolute()
        seen[str(rp)] = rp
    return tuple(seen.values())


def _is_remote_ref(ref: str) -> bool:
    scheme = urllib.parse.urlparse(ref).scheme.lower()
    return scheme in ("http", "https")


def _is_special_nonfile_ref(ref: str) -> bool:
    if ref.startswith("#"):
        return True
    scheme = urllib.parse.urlparse(ref).scheme.lower()
    return bool(scheme) and scheme in SAFE_SCHEMES and scheme not in ("http", "https")


def _sanitize_segment(segment: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", segment.strip())
    cleaned = cleaned.strip(".-")
    return cleaned or "item"


def _hashed_filename(name: str, key: str) -> str:
    p = Path(name)
    stem = _sanitize_segment(p.stem) or "asset"
    suffix = p.suffix if p.suffix and len(p.suffix) <= 10 else ""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"{stem}--{digest}{suffix}"


def _relative_dest(kind: str, source_ref: str, local_path: Optional[Path], root: Optional[Path], course_assets_rel: str, course_assets_url: str) -> Tuple[str, str]:
    """Return (fs_rel, url_rel) — filesystem path and URL path for the asset.

    fs_rel  is relative to project_root  (e.g. src/course/en/assets/foo.png)
    url_rel is the URL the browser uses  (e.g.     course/en/assets/foo.png)
    """
    fs_base  = Path(course_assets_rel)
    url_base = Path(course_assets_url)
    if local_path is not None:
        filename = _hashed_filename(local_path.name, source_ref)
        return (
            str(fs_base  / filename).replace("\\", "/"),
            str(url_base / filename).replace("\\", "/"),
        )

    parsed = urllib.parse.urlparse(source_ref)
    basename = Path(urllib.parse.unquote(parsed.path)).name or "remote"
    filename = _hashed_filename(basename, source_ref)
    return (
        str(fs_base  / filename).replace("\\", "/"),
        str(url_base / filename).replace("\\", "/"),
    )

def _normalize_local_asset_ref(ref: str) -> List[str]:
    """Generate sane local-path variants from a markdown/image reference."""
    raw = ref.strip()
    variants: List[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if not value:
            return
        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1].strip()
        if value and value not in variants:
            variants.append(value)

    add(raw)
    add(raw.replace('\\', '/'))
    add(urllib.parse.unquote(raw))
    add(urllib.parse.unquote(raw).replace('\\', '/'))

    expanded: List[str] = []
    for value in variants:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme and parsed.scheme.lower() == 'file':
            add(urllib.request.url2pathname(parsed.path))
            continue
        path_only = value.split('#', 1)[0].split('?', 1)[0].strip()
        if path_only != value:
            expanded.append(path_only)
            expanded.append(urllib.parse.unquote(path_only))
            expanded.append(path_only.replace('\\', '/'))
            expanded.append(urllib.parse.unquote(path_only).replace('\\', '/'))
    for value in expanded:
        add(value)

    final: List[str] = []
    seen: set[str] = set()
    for value in variants:
        norm = value.replace('\\', '/')
        if norm not in seen:
            final.append(norm)
            seen.add(norm)
    return final


def _tail_match_score(candidate: Path, wanted_parts: Sequence[str]) -> int:
    candidate_parts = [part.lower() for part in candidate.parts]
    wanted = [part.lower() for part in wanted_parts if part not in ('', '.', '..')]
    score = 0
    while score < len(wanted) and score < len(candidate_parts):
        if candidate_parts[-1 - score] != wanted[-1 - score]:
            break
        score += 1
    return score


def _recursive_suffix_search(ref_variants: Sequence[str], lookup_roots: Sequence[Path]) -> Tuple[Optional[Path], Optional[Path]]:
    """Fallback search for moved assets: find best basename/tail match under roots."""
    best: Optional[Tuple[int, int, str, Path, Path]] = None
    seen_candidates: set[str] = set()

    for root in lookup_roots:
        for variant in ref_variants:
            p = Path(variant.lstrip('/'))
            basename = p.name
            if not basename:
                continue
            wanted_parts = list(p.parts)
            try:
                matches = root.rglob(basename)
            except Exception:
                continue
            for match in matches:
                try:
                    resolved = match.resolve()
                except FileNotFoundError:
                    resolved = match.absolute()
                key = str(resolved)
                if key in seen_candidates or not resolved.is_file():
                    continue
                seen_candidates.add(key)
                score = _tail_match_score(resolved, wanted_parts)
                rel_len = len(resolved.relative_to(root).parts) if resolved.is_relative_to(root) else len(resolved.parts)
                ranking = (score, -rel_len, str(resolved))
                if best is None or ranking > (best[0], best[1], best[2]):
                    best = (score, -rel_len, str(resolved), resolved, root)

    if best is None:
        return None, None
    # Require at least a basename match, and prefer some directory-tail agreement when possible.
    return best[3], best[4]


def _resolve_local_asset(ref: str, lookup_roots: Sequence[Path]) -> Tuple[Optional[Path], Optional[Path]]:
    variants = _normalize_local_asset_ref(ref)
    candidates: List[Tuple[Path, Optional[Path]]] = []

    for variant in variants:
        ref_path = Path(variant)
        stripped = variant.lstrip('/')
        # Treat leading-slash paths as project-root-relative first, absolute filesystem paths second.
        for root in lookup_roots:
            candidates.append((root / stripped, root))
            if not ref_path.is_absolute():
                candidates.append((root / variant, root))
        if ref_path.is_absolute():
            candidates.append((ref_path, None))

    seen: set[str] = set()
    for candidate, root in candidates:
        try:
            resolved = candidate.resolve()
        except FileNotFoundError:
            resolved = candidate.absolute()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists() and resolved.is_file():
            return resolved, root

    return _recursive_suffix_search(variants, lookup_roots)


def _compat_asset_destinations(config: AssetConfig, kind: str, dest_rel: str) -> List[Path]:
    return [config.project_root / dest_rel]


def _write_compat_copies(config: AssetConfig, kind: str, dest_rel: str, source_file: Path) -> None:
    for target in _compat_asset_destinations(config, kind, dest_rel):
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_file.resolve() == target.resolve():
            continue
        shutil.copy2(source_file, target)


def _ensure_placeholder_image(config: AssetConfig) -> str:
    fs_rel  = f"{config.course_assets_rel}/unresolved-image.svg"
    url_rel = f"{config.course_assets_url}/unresolved-image.svg"
    for target in _compat_asset_destinations(config, "image", fs_rel):
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(UNRESOLVED_IMAGE_SVG, encoding="utf-8")
    return url_rel


def _download_remote_asset(url: str, dest_path: Path, timeout: int, max_bytes: int) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "md2adapt/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest_path.open("wb") as f:
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise AssetError(f"remote asset exceeds byte limit ({max_bytes} bytes): {url}")
            f.write(chunk)


def _migrate_asset_ref(ref: str, kind: str, config: AssetConfig, report: AssetMigrationReport) -> str:
    if _is_special_nonfile_ref(ref):
        return ref

    if _is_remote_ref(ref):
        # External web links are never downloaded — only media assets are vendored.
        if kind == "link":
            return ref
        if not config.download_remote_assets:
            report.skipped_remote.append(ref)
            return ref
        fs_rel, url_rel = _relative_dest(kind, ref, None, None, config.course_assets_rel, config.course_assets_url)
        dest_path = config.project_root / fs_rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _download_remote_asset(ref, dest_path, config.remote_timeout, config.max_remote_bytes)
        except (urllib.error.URLError, OSError, AssetError) as exc:
            report.unresolved.append(f"{ref} ({exc})")
            if config.allow_missing_assets:
                if kind == "image":
                    return _ensure_placeholder_image(config)
                return ref
            raise AssetError(f"Failed to download remote asset: {ref}: {exc}") from exc
        if not dest_path.exists():
            report.unresolved.append(f"{ref} (download produced no file at {fs_rel})")
            if config.allow_missing_assets:
                if kind == "image":
                    return _ensure_placeholder_image(config)
                return ref
            raise AssetError(f"Downloaded asset missing after write: {ref} -> {fs_rel}")
        _write_compat_copies(config, kind, fs_rel, dest_path)
        report.downloaded.append({"source": ref, "dest": fs_rel})
        return url_rel

    local_path, root = _resolve_local_asset(ref, config.lookup_roots)
    if local_path is None:
        report.unresolved.append(ref)
        if config.allow_missing_assets:
            if kind == "image":
                return _ensure_placeholder_image(config)
            return ref
        raise AssetError(f"Local asset not found: {ref}")

    fs_rel, url_rel = _relative_dest(kind, ref, local_path, root, config.course_assets_rel, config.course_assets_url)
    dest_path = config.project_root / fs_rel
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_path, dest_path)
    if not dest_path.exists():
        report.unresolved.append(f"{local_path} (copy produced no file at {fs_rel})")
        if config.allow_missing_assets:
            if kind == "image":
                return _ensure_placeholder_image(config)
            return ref
        raise AssetError(f"Copied asset missing after write: {local_path} -> {fs_rel}")
    _write_compat_copies(config, kind, fs_rel, dest_path)
    report.copied.append({"source": str(local_path), "dest": fs_rel})
    return url_rel


def _find_matching(text: str, start: int, open_char: str, close_char: str) -> int:
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_inline_destination(text: str, start: int) -> Optional[Tuple[str, str, int]]:
    if start >= len(text) or text[start] != "(":
        return None
    i = start + 1
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        return None

    if text[i] == "<":
        j = i + 1
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == ">":
                break
            j += 1
        if j >= len(text):
            return None
        dest = text[i + 1 : j]
        k = j + 1
    else:
        k = i
        depth = 0
        dest_chars: List[str] = []
        while k < len(text):
            ch = text[k]
            if ch == "\\" and k + 1 < len(text):
                dest_chars.append(text[k : k + 2])
                k += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ch.isspace() and depth == 0:
                break
            dest_chars.append(ch)
            k += 1
        dest = "".join(dest_chars)

    while k < len(text) and text[k].isspace():
        k += 1

    title = ""
    if k < len(text) and text[k] in ('"', "'", "("):
        opener = text[k]
        closer = ")" if opener == "(" else opener
        j = k + 1
        title_chars: List[str] = []
        while j < len(text):
            ch = text[j]
            if ch == "\\" and j + 1 < len(text):
                title_chars.append(text[j : j + 2])
                j += 2
                continue
            if ch == closer:
                break
            title_chars.append(ch)
            j += 1
        if j >= len(text):
            return None
        title = opener + "".join(title_chars) + closer
        k = j + 1
        while k < len(text) and text[k].isspace():
            k += 1

    if k >= len(text) or text[k] != ")":
        return None
    return dest, title, k + 1


def _rewrite_inline_markdown_links(line: str, config: AssetConfig, report: AssetMigrationReport) -> str:
    out: List[str] = []
    i = 0
    in_code = False
    while i < len(line):
        ch = line[i]
        if ch == "`":
            in_code = not in_code
            out.append(ch)
            i += 1
            continue
        if in_code:
            out.append(ch)
            i += 1
            continue
        is_image = ch == "!" and i + 1 < len(line) and line[i + 1] == "["
        if ch == "[" or is_image:
            label_start = i + 1 if not is_image else i + 2
            if label_start >= len(line) or line[label_start - 1] != "[":
                out.append(ch)
                i += 1
                continue
            label_end = _find_matching(line, label_start - 1, "[", "]")
            if label_end == -1:
                out.append(ch)
                i += 1
                continue
            after_label = label_end + 1
            if after_label < len(line) and line[after_label] == "(":
                parsed = _parse_inline_destination(line, after_label)
                if parsed is None:
                    out.append(ch)
                    i += 1
                    continue
                dest, title, end = parsed
                kind = "image" if is_image else "link"
                new_dest = _migrate_asset_ref(dest, kind, config, report)
                prefix = "!" if is_image else ""
                title_part = f" {title}" if title else ""
                out.append(prefix + "[" + line[label_start:label_end] + "](" + new_dest + title_part + ")")
                i = end
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _rewrite_reference_definition(line: str, config: AssetConfig, report: AssetMigrationReport, ref_kinds: Dict[str, str]) -> str:
    m = REFERENCE_DEF_RE.match(line)
    if not m:
        return line
    label = m.group(1)
    dest = m.group(2) or m.group(3) or ""
    title = m.group(4) or ""
    kind = ref_kinds.get(label.strip().lower(), "link")
    new_dest = _migrate_asset_ref(dest, kind, config, report)
    title_part = f" {title}" if title else ""
    if re.search(r"\s", new_dest):
        new_dest_rendered = f"<{new_dest}>"
    else:
        new_dest_rendered = new_dest
    return f"[{label}]: {new_dest_rendered}{title_part}"


def _rewrite_html_attrs(line: str, config: AssetConfig, report: AssetMigrationReport) -> str:
    def repl_img(m: re.Match[str]) -> str:
        before, quote, src = m.group(1), m.group(2), m.group(3)
        return f"{before}{quote}{_migrate_asset_ref(src, 'image', config, report)}{quote}"

    def repl_a(m: re.Match[str]) -> str:
        before, quote, href = m.group(1), m.group(2), m.group(3)
        return f"{before}{quote}{_migrate_asset_ref(href, 'link', config, report)}{quote}"

    line = re.sub(r'(<img\b[^>]*?\bsrc=)(["\'])(.*?)(\2)', repl_img, line, flags=re.IGNORECASE)
    line = re.sub(r'(<a\b[^>]*?\bhref=)(["\'])(.*?)(\2)', repl_a, line, flags=re.IGNORECASE)
    return line


def _collect_reference_kinds(markdown: str) -> Dict[str, str]:
    refs: Dict[str, str] = {}
    in_fence = False
    fence_marker = ""
    for line in markdown.splitlines():
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        for m in re.finditer(r'!\[[^\]]*\]\[([^\]]+)\]', line):
            refs[m.group(1).strip().lower()] = "image"
        for m in re.finditer(r'!\[[^\]]*\]\[\]', line):
            pass
        for m in re.finditer(r'\[[^\]]+\]\[([^\]]+)\]', line):
            refs.setdefault(m.group(1).strip().lower(), "link")
    return refs


def _expand_reference_links(markdown: str) -> str:
    defs: Dict[str, Tuple[str, str]] = {}
    lines = markdown.splitlines()
    kept_lines: List[str] = []
    in_fence = False
    fence_marker = ""
    for line in lines:
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            kept_lines.append(line)
            continue
        if in_fence:
            kept_lines.append(line)
            continue
        m = REFERENCE_DEF_RE.match(line)
        if m:
            label = m.group(1).strip().lower()
            dest = m.group(2) or m.group(3) or ""
            title = m.group(4) or ""
            defs[label] = (dest, title)
            continue
        kept_lines.append(line)

    def replace_line(line: str) -> str:
        if "`" in line:
            return line
        def repl_image(m: re.Match[str]) -> str:
            alt = m.group(1)
            ref = (m.group(2) or alt).strip().lower()
            if ref not in defs:
                return m.group(0)
            dest, title = defs[ref]
            title_part = f" {title}" if title else ""
            return f"![{alt}]({dest}{title_part})"

        def repl_link(m: re.Match[str]) -> str:
            text = m.group(1)
            ref = (m.group(2) or text).strip().lower()
            if ref not in defs:
                return m.group(0)
            dest, title = defs[ref]
            title_part = f" {title}" if title else ""
            return f"[{text}]({dest}{title_part})"

        line = re.sub(r'!\[([^\]]*)\]\[([^\]]*)\]', repl_image, line)
        line = re.sub(r'\[([^\]]+)\]\[([^\]]*)\]', repl_link, line)
        return line

    return "\n".join(replace_line(line) for line in kept_lines)


def swap_asset_links(
    markdown: str,
    out_base: Path,
    md_dir: Path,
    *,
    lang: str,
    asset_roots: Sequence[Path] = (),
    download_remote_assets: bool = False,
    allow_missing_assets: bool = True,
    remote_timeout: int = 15,
    max_remote_bytes: int = 25_000_000,
) -> Tuple[str, AssetMigrationReport]:
    out_base_resolved = out_base.resolve()

    def _project_root_from_out(path: Path) -> Tuple[Path, str]:
        """Return (project_root, out_lang) derived purely from the out_base path.

        Looks for the pattern …/src/course/<lang> or …/course/<lang> and returns
        the root above `src/` and the language segment found in the path.
        Falls back to the grandparent of out_base and the last path component as lang.
        """
        parts = list(path.parts)
        for i in range(len(parts) - 1):
            if parts[i] == "src" and i + 2 < len(parts) and parts[i + 1] == "course":
                lang_seg = parts[i + 2]
                prefix = Path(*parts[:i]) if i > 0 else (Path("/") if path.is_absolute() else Path("."))
                return prefix.resolve(), lang_seg
        for i in range(len(parts) - 1):
            if parts[i] == "course" and i + 1 < len(parts):
                lang_seg = parts[i + 1]
                prefix = Path(*parts[:i]) if i > 0 else (Path("/") if path.is_absolute() else Path("."))
                return prefix.resolve(), lang_seg
        # Last resort: grandparent is project root, last component is lang.
        out_lang = path.name or lang
        if len(path.parents) >= 2:
            return path.parents[1].resolve(), out_lang
        return Path.cwd().resolve(), out_lang

    project_root, out_lang = _project_root_from_out(out_base_resolved)
    # Use the lang derived from out_base for both filesystem path and URL so they
    # always agree with where Grunt writes the build output — regardless of the
    # `language:` field in the markdown frontmatter.
    course_assets_rel = f"src/course/{out_lang}/assets"   # filesystem path (Grunt source tree)
    course_assets_url = f"course/{out_lang}/assets"       # URL path served from build/

    config = AssetConfig(
        project_root=project_root,
        course_assets_rel=course_assets_rel,
        course_assets_url=course_assets_url,
        lookup_roots=_normalize_lookup_roots(md_dir.resolve(), [Path(p).resolve() for p in asset_roots]),
        download_remote_assets=download_remote_assets,
        allow_missing_assets=allow_missing_assets,
        remote_timeout=remote_timeout,
        max_remote_bytes=max_remote_bytes,
    )
    report = AssetMigrationReport(copied=[], downloaded=[], skipped_remote=[], unresolved=[])

    ref_kinds = _collect_reference_kinds(markdown)
    out_lines: List[str] = []
    in_fence = False
    fence_marker = ""
    for line in markdown.splitlines():
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        updated = _rewrite_reference_definition(line, config, report, ref_kinds)
        updated = _rewrite_inline_markdown_links(updated, config, report)
        updated = _rewrite_html_attrs(updated, config, report)
        out_lines.append(updated)

    if report.unresolved and not allow_missing_assets:
        joined = "\n - ".join(report.unresolved)
        raise AssetError(f"Asset migration failed for:\n - {joined}")
    return "\n".join(out_lines), report


# -------- Builder orchestrator --------
def build_from_markdown(md: str, lang: str, menu_title: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    hero_image = None
    ids = IdSpace()
    sections = parse_headings(md)
    total_lines = len(md.splitlines())

    h1s = [s for s in sections if s.level == 1]
    course_title = h1s[0].title if h1s else "Course"
    course = course_template(course_title, lang, 1, 1)

    menu_id = ids.new()
    menu = menu_template(menu_id, menu_title or "Menu")

    h2_has_block_tags = any(s.level == 2 and split_marker(s.title)[0] == "block" for s in sections)
    has_block_h2 = _should_use_h2_blocks(md, sections, total_lines)

    if has_block_h2:
        meta = _extract_meta_titles(md)
        synthetic_page_title = meta.get("pageTitle", course_title)
        pages_heads = [Section(level=2, title=synthetic_page_title, start=0, end=total_lines)]
    else:
        pages_heads = [s for s in sections if s.level == 2]
        if not pages_heads:
            pages_heads = h1s[1:]
        if not pages_heads:
            pages_heads = [Section(level=2, title=course_title, start=0, end=total_lines)]

    pages: List[Dict[str, Any]] = []
    articles: List[Dict[str, Any]] = []
    blocks: List[Dict[str, Any]] = []
    components: List[Dict[str, Any]] = []
    tracking = 1
    hero_emitted = False

    for p_sec in pages_heads:
        page_id = ids.new()
        pages.append(page_template(page_id, menu_id, p_sec.title))

        if has_block_h2:
            meta = _extract_meta_titles(md)
            article_title = meta.get("articleTitle", "").strip()
            if article_title == p_sec.title:
                article_title = ""
            a_heads = [Section(level=3, title=article_title, start=p_sec.start, end=p_sec.end)]
        else:
            a_heads = [s for s in sections if s.level == 3 and p_sec.start <= s.start < (p_sec.end or total_lines)]
            if not a_heads:
                a_heads = [Section(level=3, title=p_sec.title, start=p_sec.start, end=p_sec.end)]

        for a_sec in a_heads:
            article_id = ids.new()
            article_text_body = get_section_intro(md, a_sec, sections, total_lines)
            articles.append(article_template(article_id, page_id, a_sec.title, body=article_text_body))

            if not hero_emitted:
                hero_markdown = build_hero_markdown(p_sec.title, hero_image)
                if hero_markdown:
                    hero_block_id = ids.new()
                    blocks.append(block_template(hero_block_id, article_id, tracking, "", body=""))
                    tracking += 1
                    hero_comp_id = ids.new()
                    hero_component = text_component(hero_comp_id, hero_block_id, "", hero_markdown)
                    hero_component["_classes"] = "md-hero-component"
                    components.append(hero_component)
                    hero_emitted = True

            if has_block_h2:
                b_heads = find_h2_block_sections(sections, total_lines) if h2_has_block_tags else _get_all_h2_sections(sections, total_lines)
                b_chunks = [get_section_text(md, s, sections, total_lines) for s in b_heads]
            else:
                a_end = a_sec.end if a_sec.end is not None else total_lines
                b_heads = [s for s in sections if s.level == 4 and a_sec.start <= s.start < a_end]
                if not b_heads:
                    article_body = get_section_text(md, a_sec, sections, total_lines)
                    chunks = re.split(r"(?m)^\s*(?:-{3,}|\*{3,})\s*$", article_body)
                    if len(chunks) <= 1:
                        b_heads = [Section(level=4, title=a_sec.title, start=a_sec.start, end=a_sec.end)]
                        b_chunks = [get_section_text(md, a_sec, sections, total_lines)]
                    else:
                        b_heads = []
                        b_chunks = []
                        cursor = 0
                        for i, chunk in enumerate(chunks, 1):
                            b_heads.append(Section(level=4, title=f"{a_sec.title} – Section {i}", start=a_sec.start + cursor, end=None))
                            b_chunks.append(chunk.strip())
                            cursor += len(chunk.splitlines()) + 1
                else:
                    b_chunks = [get_section_text(md, s, sections, total_lines) for s in b_heads]

            for b_sec, chunk in zip(b_heads, b_chunks):
                _, b_title = split_marker(b_sec.title)
                sub_heads: List[Section] = []
                block_text_body = ""
                if has_block_h2:
                    block_end = b_sec.end if b_sec.end is not None else total_lines
                    sub_heads = [s for s in sections if s.level == 3 and b_sec.start <= s.start < block_end]
                    if sub_heads:
                        block_text_body = get_section_intro(md, b_sec, sections, total_lines)

                block_id = ids.new()
                blocks.append(block_template(block_id, article_id, tracking, b_title, body=block_text_body))
                tracking += 1

                if has_block_h2 and sub_heads:
                    for sub in sub_heads:
                        comp_id = ids.new()
                        _, c_title = split_marker(sub.title)
                        sub_chunk = get_section_text(md, sub, sections, total_lines)
                        components.append(_dispatch_component(comp_id, block_id, c_title or "", sub_chunk))
                else:
                    comp_id = ids.new()
                    components.append(_dispatch_component(comp_id, block_id, "", chunk if chunk.strip() else ""))

    return [menu] + pages, articles, blocks, components, course


# -------- Validation --------
def validate_graph(content_objects: List[Dict[str, Any]], articles: List[Dict[str, Any]], blocks: List[Dict[str, Any]], components: List[Dict[str, Any]]) -> None:
    objects = [{"_id": "course", "_type": "course"}] + content_objects + articles + blocks + components
    ids = {o["_id"] for o in objects}
    bad_hex = [i for i in ids if i != "course" and not HEX24_RE.match(i)]
    if bad_hex:
        raise ValueError(f"Non-hex _ids (should be 24 hex): {', '.join(bad_hex)}")

    by_id = {o["_id"]: o for o in objects}
    parent_type_rules = {
        "menu": {"course"},
        "page": {"menu"},
        "article": {"page"},
        "block": {"article"},
        "component": {"block"},
    }
    problems: List[str] = []
    for obj in content_objects + articles + blocks + components:
        pid = obj.get("_parentId")
        if not pid:
            continue
        if pid not in ids:
            problems.append(f"{obj['_type']} {obj['_id']} references missing parent {pid}")
            continue
        expected = parent_type_rules.get(obj["_type"], set())
        actual_type = by_id[pid]["_type"]
        if expected and actual_type not in expected:
            problems.append(f"{obj['_type']} {obj['_id']} has invalid parent type {actual_type} (expected one of {sorted(expected)})")
    if problems:
        raise ValueError("Invalid object graph:\n - " + "\n - ".join(problems))

    tracking_ids = [b.get("_trackingId") for b in blocks]
    if tracking_ids != sorted(tracking_ids) or len(set(tracking_ids)) != len(tracking_ids):
        raise ValueError("Block _trackingId values must be unique and monotonic.")


def write_jsons(out_dir: Path, content_objects: List[Dict[str, Any]], articles: List[Dict[str, Any]], blocks: List[Dict[str, Any]], components: List[Dict[str, Any]], course: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    def dump(name: str, data: Any) -> None:
        (out_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    dump("contentObjects.json", content_objects)
    dump("articles.json", articles)
    dump("blocks.json", blocks)
    dump("components.json", components)
    dump("course.json", course)


def write_asset_manifest(out_dir: Path, report: AssetMigrationReport) -> None:
    payload = {
        "copied": report.copied,
        "downloaded": report.downloaded,
        "skipped_remote": report.skipped_remote,
        "unresolved": report.unresolved,
    }
    (out_dir / "asset-manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Convert Markdown into Adapt JSON (menu/page/article/block/components + course.json).")
    ap.add_argument("input_md", help="Path to Markdown file")
    ap.add_argument("--out", default="course/en", help="Output folder (e.g., course/en)")
    ap.add_argument("--asset-dest", default=None, help="Override the folder used as the asset destination (src/course/<lang>). Use this when --out points to a temp directory but assets should land in the real course output folder.")
    ap.add_argument("--lang", default="en", help="Default language code for course.json")
    ap.add_argument("--menu", default="Menu", help="Menu title")
    ap.add_argument("--no-swap-images", action="store_true", help="Disable automatic migration of local/remote asset links in markdown")
    ap.add_argument("--asset-root", action="append", default=[], help="Additional root directory to search for local assets; fallback recursive search also uses these roots (repeatable)")
    ap.add_argument("--download-remote-assets", action="store_true", help="Vendor remote http/https assets into src/course/<lang>/assets")
    ap.add_argument("--strict-assets", action="store_true", help="Fail when an asset cannot be resolved instead of keeping the original reference")
    ap.add_argument("--remote-timeout", type=int, default=15, help="Timeout in seconds for remote asset downloads")
    ap.add_argument("--max-remote-bytes", type=int, default=25_000_000, help="Maximum size for a downloaded remote asset")
    ap.add_argument("--no-asset-manifest", action="store_true", help="Do not write asset-manifest.json next to the output JSON files")
    args = ap.parse_args(argv)

    md_path = Path(args.input_md)
    if not md_path.exists():
        print(f"ERROR: Markdown file not found: {md_path}", file=sys.stderr)
        return 2

    md = md_path.read_text(encoding="utf-8")
    md = _expand_reference_links(md)
    meta = _extract_meta_titles(md)
    md = strip_frontmatter(md)
    eff_lang = meta.get("language", args.lang)
    eff_menu = meta.get("parentMenuTitle", args.menu)

    if not args.no_swap_images:
        try:
            asset_out = Path(args.asset_dest) if args.asset_dest else Path(args.out)
            md, report = swap_asset_links(
                md,
                asset_out,
                md_path.parent,
                lang=eff_lang,
                asset_roots=[Path(p) for p in args.asset_root],
                download_remote_assets=args.download_remote_assets,
                allow_missing_assets=not args.strict_assets,
                remote_timeout=args.remote_timeout,
                max_remote_bytes=args.max_remote_bytes,
            )
            if report.copied or report.downloaded or report.skipped_remote or report.unresolved:
                print(
                    "Asset migration: copied=%d downloaded=%d skipped_remote=%d unresolved=%d"
                    % (len(report.copied), len(report.downloaded), len(report.skipped_remote), len(report.unresolved)),
                    file=sys.stderr,
                )
            if not args.no_asset_manifest:
                Path(args.out).mkdir(parents=True, exist_ok=True)
                write_asset_manifest(Path(args.out), report)
        except AssetError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3

    content_objects, articles, blocks, components, course = build_from_markdown(md, eff_lang, eff_menu)
    validate_graph(content_objects, articles, blocks, components)
    out_dir = Path(args.out)
    write_jsons(out_dir, content_objects, articles, blocks, components, course)

    print(
        f"Wrote: {out_dir/'contentObjects.json'}, {out_dir/'articles.json'}, {out_dir/'blocks.json'}, {out_dir/'components.json'}, {out_dir/'course.json'}"
    )
    print(
        "Objects: contentObjects=%d (menu+pages), articles=%d, blocks=%d, components=%d"
        % (len(content_objects), len(articles), len(blocks), len(components))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
