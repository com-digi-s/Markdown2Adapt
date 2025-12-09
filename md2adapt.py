# md2adapt.py — Generate Adapt Framework JSON (contentObjects, articles, blocks, components, course)
#
# Usage:
#   python md2adapt.py INPUT.md --out course/en --lang en --menu "Main menu"
#
# The script intentionally uses only the Python standard library.
#
# Authoring rules (tolerant defaults):
# - Course title: first H1 (#) or fallback to "Course".
# - Pages: prefer H2. If none, subsequent H1s; if none, a synthetic page.
#   SPECIAL CASE: If H2s start with "[block]" (e.g., "## [block] Intro"),
#   those H2s are treated as BLOCKS under one synthetic page+article.
# - Articles: prefer H3 per page; if none, one synthetic article per page.
#   In the "[block] at H2" case, we create one synthetic article for the page.
# - Blocks: prefer H4 within an article; otherwise split on ---/*** or one block.
#   In the "[block] at H2" case, those H2s ARE the blocks. If no explicit
#   "[block]" tags exist but H3s look like components (MCQ/slider), then
#   every H2 is treated as a block (auto-component mode).
# - Components:
#   • Default: 1 TEXT component per block (safe fallback).
#   • In "[block] at H2" or auto-component mode: each H3 inside a block becomes a component:
#       [text] -> text component
#       [mcq]  -> mcq component (options parsed from list items with [ ] / [x])
#       [slider] -> slider component (parsed from "scale: a..b", "labelStart:", "labelEnd:")
#     If no explicit marker is present, we auto-detect:
#       - MCQ if the body contains lines like "- [ ] ..." or "[x] ..."
#       - Slider if the body contains "scale: a..b" and/or "labelStart/labelEnd"
#     Unrecognized markers fall back to TEXT.
#
# Optional simple front-matter (before the first Markdown heading):
#   parentMenuTitle: "Anwenden"
#   language: "de"
#   version: "1.4"
#   pageTitle: "..."        # used in synthetic single-page mode
#   articleTitle: "..."     # used in synthetic single-article mode
#
# Output files:
#   contentObjects.json, articles.json, blocks.json, components.json, course.json
#
# Guarantees:
# - 24-char hex IDs for all objects except course (_id = "course")
# - Valid parent chains: menu → page → article → block → component
# - Monotonic unique _trackingId for blocks
#
import argparse, json, re, sys
import markdown
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

HEX24_RE = re.compile(r"^[0-9a-f]{24}$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
MARKER_RE = re.compile(r"^\[(\w+)\]\s*(.*)$", re.IGNORECASE)

def md_to_html(md_text: str) -> str:
    """
    Wandelt einen Markdown-Text in HTML um.
    :param md_text: Markdown-Quelltext als String
    :return: HTML-String
    """
    return markdown.markdown(md_text)

def split_marker(title: str):
    """
    If title starts with [something] return (marker_lower, rest_title).
    Otherwise return (None, title).
    """
    m = MARKER_RE.match(title.strip())
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return None, title.strip()

def gen_hex24(n: int) -> str:
    """Deterministic-ish 24-hex generator from an incrementing integer."""
    h = format(n, "x")[-24:]
    return h.rjust(24, "0")

class IdSpace:
    def __init__(self) -> None:
        self._next = 1
    def new(self) -> str:
        i = self._next
        self._next += 1
        return gen_hex24(i)

# -------- Minimal Markdown-to-HTML (safe default) --------
def md_to_html(md: str) -> str:
    """Very small markdown→html converter (paragraphs + unordered lists + links)."""
    lines = md.strip().splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        # unordered list
        if re.match(r"^\s*[-*+]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                item = re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip()
                items.append(f"<li>{inline_md(item)}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        # paragraph
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^\s*[-*+]\s+", lines[i]):
            para.append(lines[i].rstrip())
            i += 1
        out.append("<p>" + inline_md(" ".join(para).strip()) + "</p>")
    return "".join(out)

def inline_md(text: str) -> str:
    # very small inline markdown: **bold**, *italic*, [text](url)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)
    return text

# -------- Parse sections --------
@dataclass
class Section:
    level: int
    title: str
    start: int
    end: Optional[int] = None  # filled later

def parse_headings(md: str) -> List[Section]:
    lines = md.splitlines()
    sections: List[Section] = []
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            sections.append(Section(level, title, idx))
    # close ranges (temporary: next heading of ANY level)
    for i, s in enumerate(sections):
        s.end = sections[i+1].start if i+1 < len(sections) else len(lines)
    return sections

def slice_text(md: str, start: int, end: Optional[int]) -> str:
    if end is None:
        end = len(md.splitlines())
    lines = md.splitlines()[start:end]
    # drop the heading line itself
    if lines and HEADING_RE.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()

# -------- Adapt object builders --------
def course_template(
    title: str,
    lang: str,
    total_score: int,
    total_correct: int,
    pass_score_ratio: float = 0.6,    # z.B. 60% zum Bestehen
    is_percentage_based: bool = True  # Setzen Sie False für absolute Werte
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
            "ariaRegion": "Multiple choice question",
            "ariaCorrectAnswer": "The correct answer is {{{correctAnswer}}}",
            "ariaCorrectAnswers": "The correct answers are {{{correctAnswer}}}",
            "ariaUserAnswer": "The answer you chose was {{{userAnswer}}}",
            "ariaUserAnswers": "The answers you chose were {{{userAnswer}}}"
        },
        "_buttons": {
            "_submit": {
            "buttonText": "Antwort abgeben",
            "ariaLabel": "Antwort abgeben"
            },
            "_reset": {
            "buttonText": "Zurücksetzen",
            "ariaLabel": "Zurücksetzen"
            },
            "_showCorrectAnswer": {
            "buttonText": "Richtige Antwort anzeigen",
            "ariaLabel": "Richtige Antwort anzeigen"
            },
            "_hideCorrectAnswer": {
            "buttonText": "Meine Antwort anzeigen",
            "ariaLabel": "Meine Antwort anzeigen"
            },
            "_showFeedback": {
            "buttonText": "Feedback anzeigen",
            "ariaLabel": "Feedback anzeigen"
            },
            "remainingAttemptsText": "Verbleibende Versuche",
            "remainingAttemptText": "Letzter Versuch",
            "disabledAriaLabel": "Dieser Button ist derzeit nicht verfügbar."
        },
        "_assessment": {
            "_scoreToPass": int(pass_score_ratio * 100) if is_percentage_based else int(pass_score_ratio * total_score),
            "_correctToPass": int(pass_score_ratio * 100) if is_percentage_based else int(pass_score_ratio * total_correct),
            "_isPercentageBased": is_percentage_based
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
                "optional": "Optional"
            },
            "altFeedbackTitle": "Feedback"
            }
        }
    }

def menu_template(_id: str, title: str) -> Dict[str, Any]:
    return {
        "_type": "menu",
        "_id": _id,
        "_parentId": "course",
        "title": title,
        "displayTitle": title
    }

def page_template(_id: str, parent_id: str, title: str) -> Dict[str, Any]:
    return {
        "_type": "page",
        "_id": _id,
        "_parentId": parent_id,
        "title": title,
        "displayTitle": title
    }

def article_template(_id: str, parent_id: str, title: str, body: str, assessment_id: int) -> Dict[str, Any]:
    return {
        "_id": _id,
        "_parentId": parent_id,
        "_type": "article",
        "title": title,
        "body": md_to_html(body),
        "displayTitle": "",
        "_articleBlockSlider": {
            "_isEnabled": False,
            "_hasTabs": False
        },
        "_assessment": {
            "_isEnabled": False,                # Enable or disable this assessment per article
            "_id": assessment_id,               # Unique name for this assessment
            "_suppressMarking": False,          # Suppresses question marking until assessment complete or all attempts used
            "_scoreToPass": 60,                  # Numeric or percent score required to pass
            "_correctToPass": 60,                # Numeric or percent correctness required to pass
            "_isPercentageBased": True,         # If true, values above are percent
            "_includeInTotalScore": False,      # Should score be sent to LMS
            "_assessmentWeight": 1,             # Proportion of score contributed (1=100%)
            "_isResetOnRevisit": False,         # Reset automatically on revisit
            "_attempts": 1,                     # Number of attempts allowed (-1/0/null/None for infinite)
            "_allowResetIfPassed": False,       # Allow reset after passing (while attempts remain)
            "_scrollToOnReset": False,          # Scroll to assessment after reset
            "_banks": {
                "_isEnabled": False,            # Enable question banks (opposite of _randomisation['_isEnabled'])
                "_split": "",                   # Example: "2,1"
                "_randomisation": False         # Randomise within bank?
            },
            "_randomisation": {
                "_isEnabled": False,            # Enable question randomisation (opposite of _banks['_isEnabled'])
                "_blockCount": 0                # Number of blocks presented when randomising
            },
            "_questions": {
                "_resetType": "soft",           # "soft" or "hard" reset
                "_canShowFeedback": True,       # Allow feedback display
                "_canShowMarking": True,        # Allow marking display
                "_canShowModelAnswer": False    # Allow model answer display
            }
        }
    }

def block_template(_id: str, parent_id: str, track_id: int, title: str, body: str) -> Dict[str, Any]:
    return {
        "_id": _id,
        "_parentId": parent_id,
        "_type": "block",
        "title": title,
        "body": md_to_html(body),
        "displayTitle": title,
        "_trackingId": track_id,
        "_pageLevelProgress": {"_isEnabled": False}
    }

def component_common(_id: str, parent_id: str, comp: str, title: str) -> Dict[str, Any]:
    return {
        "_type": "component",
        "_component": comp,
        "_id": _id,
        "_parentId": parent_id,
        "title": title,
        "displayTitle": title,
        "_layout": "full",
        "_classes": "",
        "_isOptional": False,
        "_isAvailable": True,
        "_isHidden": False,
        "_isVisible": True,
        "_isResetOnRevisit": "false"
    }

def text_component(_id: str, parent_id: str, title: str, html_body: str) -> Dict[str, Any]:
    d = component_common(_id, parent_id, "text", title)
    d["body"] = md_to_html(html_body)
    return d

def get_mcq_button_object(show_feedback: bool = False):
    d = {
        "_submit": {
            "buttonText": "",
            "ariaLabel": ""
        },
        "_reset": {
            "buttonText": "Zurücksetzen",
            "ariaLabel": ""
        },
        "_showCorrectAnswer": {
            "buttonText": "",
            "ariaLabel": ""
        },
        "_hideCorrectAnswer": {
            "buttonText": "",
            "ariaLabel": ""
        },
        "remainingAttemptsText": "",
        "remainingAttemptText": ""
    }
    if show_feedback:
        d["_showFeedback"] = {
            "buttonText": "",
            "ariaLabel": ""
        }
    return d

def get_slider_button_object():
    d = {
        "_submit": {
            "buttonText": "",
            "ariaLabel": ""
        }
    }
    return d

# --- Real components: MCQ & Slider (basic schema; safe defaults) ---
def mcq_component(
    _id: str,
    parent_id: str,
    title: str,
    instruction_html: str,
    items: List[Tuple[str, bool]],
    feedback: Optional[str] = None
) -> Dict[str, Any]:
    """
    items: list of (text, is_correct)
    feedback: Optional feedback HTML string for the correct answer.
    """
    d = component_common(_id, parent_id, "mcq", title)
    d["body"] = ""
    d["instruction"] = "Bitte wähl die richtige Antwort aus!"
    defaults = {"_isPartlyCorrect": False, "feedback": ""}
    d["_items"] = [
        dict(text=t, _shouldBeSelected=ok, **defaults)
        for t, ok in items
    ]
    #d["_items"] = [{"text": t, "_score": ok, "_shouldBeSelected": False, "_isPartlyCorrect": False,"feedback": ""} for (t, ok) in items]
    d["_attempts"] = 1
    d["_selectable"] = 1
    d["_shouldDisplayAttempts"] = True
    d["_canShowModelAnswer"] = True
    d["_canShowMarking"] = True
    d["_shouldDisplayAttempts"] = False
    d["_isRandom"] = True
    d["_recordInteraction"] = True
    d["_hasItemScoring"] = False
    d["_questionWeight"] = 1
    d["_selectable"] = 1
    d["_tutor"] = {
      "_isInherited": True,
      "_type": "inline",
      "_classes": "",
      "_hasNotifyBottomButton": False,
      "_button": {
        "text": "{{_globals._extensions._tutor.hideFeedback}}",
        "ariaLabel": "{{_globals._extensions._tutor.hideFeedback}}"
      }
    }

    # Use the provided feedback, or a default if None was supplied
    if feedback is None:
        d["_canShowFeedback"] = False
    else:
        d["_canShowFeedback"] = True
        d["_feedback"] = {
            "title": "Feedback",
            "correct": "Korrekt! {}".format(feedback),
            "_incorrect": {
                "final": "Leider nicht. {}".format(feedback),
                "notFinal": ""
            },
            "_partlyCorrect": {
                "final": "Zum Teil richtig".format(feedback),
                "notFinal": ""
            }
        }
    d["_buttons"] = get_mcq_button_object(bool(feedback))
    return d

def slider_component(_id: str, parent_id: str, title: str, min_v: int, max_v: int, label_start: str, label_end: str) -> Dict[str, Any]:
    """
    Minimal slider payload compatible with many slider plugins.
    """
    d = component_common(_id, parent_id, "slider", title)
    d["body"] = ""
    # store settings in a predictable bucket; adjust to your plugin schema if needed
    d["_scaleStart"] = int(min_v)
    d["_scaleEnd"] =  int(max_v)
    d["_scaleStep"] =  1
    d["labelStart"] = label_start
    d["labelEnd"] =  label_end
    d["instruction"] = "Bitte gib eine Einschätzung ab!"
    d["_correctRange"] = {"_bottom": int(min_v), "_top": int(max_v)}
    d["_buttons"] = get_slider_button_object()
    d["_canShowFeedback"] = False
    return d


def matching_component(
    _id: str,
    parent_id: str,
    title: str,
    instruction_html: str,
    items: list,
    feedback: str = None,
    placeholder: str = "Please select an option"
) -> dict:
    d = component_common(_id, parent_id, "matching", title)
    # Required fields
    d["instruction"] = instruction_html
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
    # Items (see below for format)
    d["_items"] = items
    if feedback is not None:
        d["_canShowFeedback"] = True
        d["_feedback"] = {
            "title": "Feedback",
            "correct": "Correct! " + feedback,
            "_incorrect": {"final": "Incorrect. " + feedback, "notFinal": ""},
            "_partlyCorrect": {"final": "Partially correct. " + feedback, "notFinal": ""}
        }
    return d

# -------- Parsers for MCQ & Slider chunks --------
MCQ_OPT_RES = [
    re.compile(r"^\s*[-*+]\s*\[(x|X| )\]\s*(.+)\s*$"),   # "- [x] text"
    re.compile(r"^\s*\[(x|X| )\]\s*(.+)\s*$"),           # "[x] text"
]
INSTR_RES = [
    re.compile(r"^\s*(?:\*\*)?\s*Instruction\s*:\s*(.*)$", re.IGNORECASE),
    re.compile(r"^\s*(?:\*\*)?\s*Anweisung\s*:\s*(.*)$", re.IGNORECASE),
]

def parse_mcq_chunk(md: str) -> Tuple[str, List[Tuple[str, bool]]]:
    """
    Returns (instruction_html, items). If no items found, items=[]
    """
    lines = [ln.rstrip() for ln in md.strip().splitlines()]
    instruction_lines: List[str] = []
    items: List[Tuple[str, bool]] = []
    saw_option = False
    feedback = None

    for ln in lines:
        matched = False
        # detect options
        for rx in MCQ_OPT_RES:
            if ln.strip().lower().startswith("feedback:"):
                feedback = ln[len("Feedback:"):].strip()

            m = rx.match(ln)
            if m:
                is_x = m.group(1).lower() == "x"
                text = m.group(2).strip()
                items.append((text, is_x))
                saw_option = True
                matched = True
                break
        if matched:
            continue

        # pick up explicit Instruction: ... lines (first one wins)
        if not saw_option:
            for ri in INSTR_RES:
                mi = ri.match(ln)
                if mi:
                    instruction_lines.append(mi.group(1).strip())
                    matched = True
                    break
            if matched:
                continue

            # Otherwise, before options start: treat as part of instruction paragraph
            if ln.strip():
                instruction_lines.append(ln)

    instruction_html = md_to_html("\n".join(instruction_lines)) if instruction_lines else ""
    return instruction_html, items, feedback

SL_SCALE_RE = re.compile(r"^\s*scale\s*:\s*(\d+)\s*\.\.\s*(\d+)\s*$", re.IGNORECASE)
SL_LABEL_RE = re.compile(r'^\s*(labelStart|labelEnd)\s*:\s*"(.*)"\s*$', re.IGNORECASE)

def parse_slider_chunk(md: str) -> Tuple[int, int, str, str]:
    """
    Returns (min, max, labelStart, labelEnd), with sensible defaults.
    """
    min_v, max_v = 1, 10
    label_start, label_end = "1", "10"

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
            elif key.lower() == "labelend":
                label_end = val
    return min_v, max_v, label_start, label_end

def parse_matching_chunk(md: str) -> tuple:
    lines = md.splitlines()
    instruction_lines = []
    feedback = None
    items = []
    current_question = None
    current_options = []
    
    for ln in lines:
        stripped = ln.strip()
        # Only strip for meta line detection:
        if stripped.lower().startswith("type:") and "matching" in stripped.lower():
            continue
        if stripped.lower().startswith("instruction:"):
            instruction_lines.append(stripped[len("instruction:"):].strip())
            continue
        if stripped.lower().startswith("feedback:"):
            feedback = stripped[len("feedback:"):].strip()
            continue
        # Detect top-level question
        m_q = re.match(r"^-\s+(.+)$", ln)
    
        if m_q:
            if current_question is not None and current_options:
                items.append({"text": current_question, "_options": current_options})
            current_question = m_q.group(1).strip()
            current_options = []
            continue

        # Detect option lines (must be indented)
        m_opt = re.match(r"^[ \t]+[-*+] \[(x| )\]\s*(.+)", ln)
        if m_opt and current_question is not None:
            is_correct = m_opt.group(1).lower() == "x"
            opt_text = m_opt.group(2).strip()
            current_options.append({
                "text": opt_text,
                "_isCorrect": is_correct
            })
            continue

    # End: flush the last group
    if current_question is not None and current_options:
        items.append({"text": current_question, "_options": current_options})

    instruction_html = md_to_html("\n".join(instruction_lines)) if instruction_lines else ""
    return instruction_html, items, feedback

def _looks_like_mcq(md: str) -> bool:
    if _looks_like_matching(md):
        return False     # This block belongs to matching, not MCQ!
    for ln in md.strip().splitlines():
        for rx in MCQ_OPT_RES:
            if rx.match(ln):
                return True
    return False

def _looks_like_slider(md: str) -> bool:
    for ln in md.strip().splitlines():
        if SL_SCALE_RE.match(ln) or SL_LABEL_RE.match(ln):
            return True
    return False

def _looks_like_matching(md: str) -> bool:
    # Type: matching on its own line triggers matching parsing
    for ln in md.strip().splitlines():
        if ln.strip().lower() == "type: matching":
            return True
    return False

# -------- Tiny front-matter (optional) --------
def _extract_meta_titles(md: str) -> dict:
    """
    Minimal front-matter parser:
      - Reads KEY: "VALUE" lines that appear *before the first Markdown heading*.
      - Ignores any dashed separators (--- or --------------).
      - Recognizes arbitrary keys (e.g., pageTitle, articleTitle, parentMenuTitle, language, version).
    """
    meta = {}
    for line in md.splitlines():
        if HEADING_RE.match(line):
            break
        m = re.match(r'^\s*([A-Za-z][\w-]*):\s*"(.*)"\s*$', line.strip())
        if m:
            meta[m.group(1)] = m.group(2)
    return meta

# -------- Helpers for the H2-[block] special case --------
def find_h2_block_sections(sections: List[Section], total_lines: int) -> List[Section]:
    """
    Return H2 sections whose titles begin with [block], with end set to the next H2 (or EOF).
    This ensures H3s within a block are inside the block's [start, end) range.
    """
    result: List[Section] = []
    for i, s in enumerate(sections):
        if s.level == 2:
            marker, _ = split_marker(s.title)
            if marker == "block":
                end = total_lines
                for j in range(i + 1, len(sections)):
                    if sections[j].level == 2:  # next H2 ends this block
                        end = sections[j].start
                        break
                result.append(Section(level=2, title=s.title, start=s.start, end=end))
    return result

def _get_all_h2_sections(sections: List[Section], total_lines: int) -> List[Section]:
    """Return all H2 sections with correct [start, end) boundaries."""
    result: List[Section] = []
    h2s = [s for s in sections if s.level == 2]
    for i, s in enumerate(h2s):
        end = h2s[i+1].start if i+1 < len(h2s) else total_lines
        result.append(Section(level=2, title=s.title, start=s.start, end=end))
    return result

def get_section_body(md: str, section: Section, sections: list[Section]) -> str:
    lines = md.splitlines()
    start = section.start + 1
    # Find the next heading at same or higher level
    nexts = [s for s in sections if s.start > section.start and s.level != section.level]
    if nexts:
        end = nexts[0].start
    else:
        end = len(lines)
    return "\n".join(lines[start:end]).strip()

# -------- Builder orchestrator --------
def build_from_markdown(md: str, lang: str, menu_title: str) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict], Dict]:
    ids = IdSpace()
    sections = parse_headings(md)
    total_lines = len(md.splitlines())

    # Course title
    h1s = [s for s in sections if s.level == 1]
    course_title = h1s[0].title if h1s else "Course"
    course = course_template(course_title, lang, 1, 1)

    # Menu
    menu_id = ids.new()
    menu = menu_template(menu_id, menu_title or "Menu")

    # Detect explicit "[block]" tags at H2 AND/OR auto-component mode (MCQ/slider heuristics in H3s).
    h2_has_block_tags = any(s.level == 2 and split_marker(s.title)[0] == "block" for s in sections)
    any_component_h3 = False
    for s in sections:
        if s.level == 3:
            chunk = slice_text(md, s.start, s.end)
            if _looks_like_mcq(chunk) or _looks_like_slider(chunk):
                any_component_h3 = True
                break
    has_block_h2 = h2_has_block_tags or any_component_h3

    # Pages
    if has_block_h2:
        # single synthetic page so H2 [block] become blocks, not pages
        meta = _extract_meta_titles(md)
        synthetic_page_title = meta.get("pageTitle", course_title)
        pages_heads = [Section(level=2, title=synthetic_page_title, start=0, end=total_lines)]
    else:
        pages_heads = [s for s in sections if s.level == 2]
        if not pages_heads:
            # fallback: use H1 beyond the first as pages
            pages_heads = h1s[1:]
        if not pages_heads:
            # final fallback: create a synthetic page spanning the whole doc
            pages_heads = [Section(level=2, title=course_title, start=0, end=total_lines)]

    pages: List[Dict] = []
    articles: List[Dict] = []
    blocks: List[Dict] = []
    components: List[Dict] = []

    tracking = 1

    for p_sec in pages_heads:
        page_id = ids.new()
        pages.append(page_template(page_id, menu_id, p_sec.title))

        # Articles
        if has_block_h2:
            # One synthetic article; prefer front-matter title
            meta = _extract_meta_titles(md)
            article_title = meta.get("articleTitle", p_sec.title)
            a_heads = [Section(level=3, title=article_title, start=p_sec.start, end=p_sec.end)]
        else:
            # H3 inside this page range
            a_heads = [s for s in sections if s.level == 3 and p_sec.start <= s.start < p_sec.end]
            if not a_heads:
                a_heads = [Section(level=3, title=p_sec.title, start=p_sec.start, end=p_sec.end)]

        for a_sec in a_heads:
            article_id = ids.new()
            assesment_id=ids.new()
            # new body
            article_text_body = get_section_body(md, a_sec, sections)
            articles.append(article_template(article_id, page_id, a_sec.title, body=article_text_body, assessment_id=assesment_id))

            # Blocks
            if has_block_h2:
                # If explicit [block] tags exist, use them; otherwise, use *all* H2s as blocks.
                b_heads = find_h2_block_sections(sections, total_lines) if h2_has_block_tags else _get_all_h2_sections(sections, total_lines)
                b_chunks = [slice_text(md, s.start, s.end) for s in b_heads]
            else:
                # H4 blocks inside article; otherwise fall back to rules/single block
                b_heads = [s for s in sections if s.level == 4 and a_sec.start <= s.start < a_sec.end]
                if not b_heads:
                    # Split on horizontal rules (--- or ***) within article body
                    article_body = slice_text(md, a_sec.start, a_sec.end)
                    chunks = re.split(r"(?m)^\s*(?:-{3,}|\*{3,})\s*$", article_body)
                    if len(chunks) <= 1:
                        b_heads = [Section(level=4, title=a_sec.title, start=a_sec.start, end=a_sec.end)]
                        b_chunks = [slice_text(md, a_sec.start, a_sec.end)]
                    else:
                        b_heads = []
                        b_chunks = []
                        cursor = 0
                        for i, chunk in enumerate(chunks, 1):
                            b_heads.append(Section(level=4, title=f"{a_sec.title} – Section {i}", start=a_sec.start + cursor, end=None))
                            b_chunks.append(chunk.strip())
                            cursor += len(chunk.splitlines()) + 1
                else:
                    # collect chunks per H4
                    b_chunks = [slice_text(md, s.start, s.end) for s in b_heads]

            # Emit blocks + components
            for b_sec, chunk in zip(b_heads, b_chunks):
                block_id = ids.new()
                b_marker, b_title = split_marker(b_sec.title)
                block_text_body = get_section_body(md, b_sec, sections)
                blocks.append(block_template(block_id, article_id, tracking, b_title, body=block_text_body))
                tracking += 1

                if has_block_h2:
                    # Components = H3 inside this H2 block (bounded by corrected end)
                    sub_heads = [s for s in sections if s.level == 3 and b_sec.start <= s.start < (b_sec.end if b_sec.end is not None else total_lines)]
                    if sub_heads:
                        for sub in sub_heads:
                            comp_id = ids.new()
                            marker, c_title = split_marker(sub.title)
                            sub_chunk = slice_text(md, sub.start, sub.end)

                            # Decide component type by marker OR heuristics
                            if marker == "mcq" or _looks_like_mcq(sub_chunk):
                                instr_html, items, feedback = parse_mcq_chunk(sub_chunk)
                                if items:
                                    components.append(mcq_component(comp_id, block_id, c_title or "MCQ", instr_html, items, feedback))
                                else:
                                    # fallback to text if no options found
                                    components.append(text_component(comp_id, block_id, c_title or "MCQ", md_to_html(sub_chunk)))
                            elif marker == "slider" or _looks_like_slider(sub_chunk):
                                min_v, max_v, lstart, lend = parse_slider_chunk(sub_chunk)
                                components.append(slider_component(comp_id, block_id, c_title or "Slider", min_v, max_v, lstart, lend))
                            elif marker == "matching" or _looks_like_matching(sub_chunk):
                                instr_html, items, feedback = parse_matching_chunk(sub_chunk)
                                if items:
                                    components.append(matching_component(
                                        comp_id, block_id, c_title or "Matching", instr_html, items, feedback
                                    ))
                                else:
                                    components.append(text_component(comp_id, block_id, c_title or "Matching", md_to_html(sub_chunk)))
                            else:
                                # text or unknown -> TEXT
                                html = md_to_html(sub_chunk) if sub_chunk.strip() else "<p></p>"
                                components.append(text_component(comp_id, block_id, c_title or "", html))
                    else:
                        # Fallback: single TEXT component from entire block chunk
                        html = md_to_html(chunk) if chunk.strip() else "<p></p>"
                        comp_id = ids.new()
                        components.append(text_component(comp_id, block_id, b_title, html))
                else:
                    # Original behavior: single TEXT component per block
                    html = md_to_html(chunk) if chunk.strip() else "<p></p>"
                    comp_id = ids.new()
                    components.append(text_component(comp_id, block_id, b_title, html))

    # contentObjects array = [menu] + pages
    content_objects = [menu] + pages
    return content_objects, articles, blocks, components, course

# -------- Validation --------
def validate_graph(content_objects, articles, blocks, components) -> None:
    ids = {o["_id"] for o in content_objects + articles + blocks + components}
    # hex check
    bad_hex = [i for i in ids if i != "course" and not HEX24_RE.match(i)]
    if bad_hex:
        raise ValueError(f"Non-hex _ids (should be 24 hex): {', '.join(bad_hex)}")

    # parent existence
    missing: List[str] = []
    for arr in (articles, blocks, components, [o for o in content_objects if o.get('_type') == 'page']):
        for obj in arr:
            pid = obj.get("_parentId")
            if pid and pid not in ids:
                missing.append(pid)
    if missing:
        raise ValueError(f"Missing _ids (referenced as _parentId but not found): {', '.join(sorted(set(missing)))}")

def write_jsons(out_dir: Path, content_objects, articles, blocks, components, course):
    out_dir.mkdir(parents=True, exist_ok=True)
    def dump(name, data):
        (out_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    dump("contentObjects.json", content_objects)
    dump("articles.json", articles)
    dump("blocks.json", blocks)
    dump("components.json", components)
    dump("course.json", course)

def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert Markdown into Adapt JSON (menu/page/article/block/components + course.json).")
    ap.add_argument("input_md", help="Path to Markdown file")
    ap.add_argument("--out", default="course/en", help="Output folder (e.g., course/en)")
    ap.add_argument("--lang", default="en", help="Default language code for course.json")
    ap.add_argument("--menu", default="Menu", help="Menu title")
    args = ap.parse_args(argv)

    md_path = Path(args.input_md)
    if not md_path.exists():
        print(f"ERROR: Markdown file not found: {md_path}", file=sys.stderr)
        sys.exit(2)

    md = md_path.read_text(encoding="utf-8")
    meta = _extract_meta_titles(md)
    # Prefer front-matter if present; CLI flags remain valid fallbacks.
    eff_lang = meta.get("language", args.lang)
    eff_menu = meta.get("parentMenuTitle", args.menu)

    content_objects, articles, blocks, components, course = build_from_markdown(md, eff_lang, eff_menu)

    # validate then write
    validate_graph(content_objects, articles, blocks, components)
    out_dir = Path(args.out)
    write_jsons(out_dir, content_objects, articles, blocks, components, course)

    print(f"Wrote: {out_dir/'contentObjects.json'}, {out_dir/'articles.json'}, {out_dir/'blocks.json'}, {out_dir/'components.json'}, {out_dir/'course.json'}")
    print("Objects: contentObjects=%d (menu+pages), articles=%d, blocks=%d, components=%d" % (
        len(content_objects), len(articles), len(blocks), len(components)
    ))

if __name__ == "__main__":
    main()
