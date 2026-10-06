"""AgentCore Platform v1.0"""

# Caller-request contract for the markdown & clearance plan generator.
#
# One module validates everything a caller can send, so there is exactly one
# answer to "what is accepted?" — the pre_process node calls into here and
# nothing downstream re-parses raw request data.
#
# Two request channels reach this module:
#   * the free-text field of a request, which may be a plain note or a JSON
#     payload carrying the whole SKU list;
#   * the structured invocation parameters carried by the framework, which may
#     carry the same SKU list as real fields rather than as a string.
#
# Rules that hold for every field:
#   * numbers are parsed by a finite + bounded parser. NaN and the infinities
#     survive float() and every comparison against them is False, so an
#     unchecked non-finite figure would be carried into a discount calculation
#     and rendered as "nan" with nothing in the log to say why;
#   * every string that reaches the rendered plan is restricted to an inert
#     alphabet — the plan is Markdown, and a label that cannot contain a
#     newline, a pipe or a backtick cannot forge a table row or a heading;
#   * a value that fails any check REFUSES the request, naming the field but
#     never repeating the value;
#   * absent optional data is not an error — the plan is simply sparser.

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ── Bounds ────────────────────────────────────────────────────────────────────
MAX_INPUT_CHARS = 200_000
MAX_SKUS = 500
MAX_SKU_KEYS = 24
MAX_NOTE_CHARS = 400

# Physical bounds for each caller figure. A ceiling is not decoration: the plan
# multiplies these together, and an unbounded price or shelf age produces a
# schedule no merchandiser can act on.
DAYS_ON_SHELF_MAX = 3_650
PRICE_MAX = 1e9
PERCENT_MAX = 100.0
HORIZON_WEEKS_MIN = 1
HORIZON_WEEKS_MAX = 12

# ── Inert alphabets ───────────────────────────────────────────────────────────
# Product codes, categories and channel names are rendered into the plan as
# table cells, so they are restricted rather than escaped.
_LABEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# Codes arrive in merchandiser spelling ("SKU A-001"); the normalisation below
# is the documented route from that to the inert alphabet. Only horizontal
# space and the three punctuation separators are normalised: a NEWLINE in a
# product code is not a spelling variant, it is a structural character, and
# collapsing it would accept a code no export ever produced.
_LABEL_SEPARATORS_RE = re.compile(r"[ \t\-/.]+")

# Field names are caller-controlled too. One is repeated back in a refusal only
# when it is short, inert, and carries no disallowed pattern of its own.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")

_WHITESPACE_RE = re.compile(r"\s+")
# Zero-width and bidi controls: invisible in a rendered plan, so they can hide a
# directive from a human reviewer while a model still reads it.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
# Control characters have no meaning in plan prose and break renderers.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Simple markup tags. Used ONLY to build a screening candidate — stored text is
# never rewritten by it. A tag inserted mid-word hides a directive from a
# pattern while a renderer puts the word back together.
_MARKUP_RE = re.compile(r"</?[A-Za-z][^<>]{0,64}>")

# ── Shopper personal-data shapes ──────────────────────────────────────────────
# ONE definition, used by both directions of the personal-data guarantee: the
# inbound strip that rewrites these shapes out of caller text, and the outbound
# gate that refuses to release anything still matching them. Two lists would
# drift, and the drift would always favour the leak.
#
# Retail order and returns data is where shopper identity leaks into a
# merchandising feed: an e-mail address in a note field, a loyalty or payment
# card number pasted into a product code.
# The card pattern is deliberately NARROWER than "any long digit run". Retail
# article numbers ARE long digit runs — EAN-13 is 13 digits, ITF-14 is 14 — and
# a gate that refused those would refuse the agent's own subject matter, which
# is the more damaging of the two failure directions. A payment card number is
# 16-19 contiguous digits, or written in the 4-4-4-N groups a card is printed
# in; neither shape is a retail article number.
PERSONAL_DATA_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("card_number", re.compile(r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{1,7}\b|\b\d{16,19}\b")),
    ("international_phone_number", re.compile(r"\+\d{1,3}[-\s]\d{1,4}[-\s]\d{2,4}[-\s]\d{3,4}\b")),
    ("phone_number", re.compile(r"\b0\d{1,4}[-\s]\d{2,4}[-\s]\d{4}\b")),
)
REDACTION_STUB = "[REDACTED]"

# Field names whose VALUE is shopper personal data whatever its shape. A name
# has no pattern, so the key is the only thing that can identify it.
PERSONAL_DATA_KEYS = frozenset(
    {
        "customer_name",
        "customer_id",
        "buyer_name",
        "shopper_name",
        "member_id",
        "loyalty_id",
        "loyalty_number",
        "card_number",
        "employee_id",
        "staff_id",
        "name",
        "full_name",
        "address",
        "home_address",
        "phone",
        "phone_number",
        "tel",
        "email",
        "e_mail",
        "date_of_birth",
        "dob",
    }
)


def strip_direct_identifiers(text: str) -> str:
    """Replace direct-identifier shapes in free text with a fixed stub.

    Applied to every free-text field before it is stored, whichever channel it
    arrived on: the framework's own masking covers the request's free-text field
    only, so a note arriving on the structured channel would otherwise skip it.
    """
    for _name, pattern in PERSONAL_DATA_PATTERNS:
        text = pattern.sub(REDACTION_STUB, text)
    return text


def find_personal_data(text: str) -> Optional[str]:
    """Name the first shopper personal-data shape present in a string, or None."""
    for name, pattern in PERSONAL_DATA_PATTERNS:
        if pattern.search(text):
            return name
    return None


# ── Disallowed-instruction screen ─────────────────────────────────────────────
# Chat-template control tokens are screened as a class. They are how a payload
# forges a turn boundary, they carry no meaning in merchandising data, and a
# screen written around directive phrases alone does not see them at all.
_CONTROL_TOKEN_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("chat_template_token", re.compile(r"<\|[^<>|]{0,64}\|>")),
    ("instruction_token", re.compile(r"\[/?INST\]", re.IGNORECASE)),
    ("system_token", re.compile(r"<</?SYS>>", re.IGNORECASE)),
)

# Instruction-shaped phrases. Every pattern requires a verb AND its object, so
# the surrounding prose has to actually be an instruction: a buying note that
# merely mentions rules, a system or a promotion does not match. Merchandising
# notes quote supplier terms verbatim, and a screen that fires on those refuses
# genuine work — the more damaging of the two failures.
_DIRECTIVE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "override_directive",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\s+"
            r"(?:all\s+|any\s+|the\s+|your\s+|these\s+|those\s+)*"
            r"(?:previous|prior|above|earlier|preceding|system|initial|safety)\s+"
            r"(?:instruction|rule|prompt|direction|guardrail|guideline)s?",
            re.IGNORECASE,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\b(?:you\s+are\s+now|pretend\s+to\s+be|act\s+as|behave\s+as|roleplay\s+as)\s+"
            r"(?:a|an|the)\s+"
            r"(?:system|assistant|language\s+model|ai\s+model|unrestricted|jailbroken|"
            r"admin(?:istrator)?|developer\s+mode|dan\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_disclosure",
        re.compile(
            r"\b(?:reveal|print|repeat|show|output|display|disclose)\s+(?:me\s+)?"
            r"(?:your|the)\s+"
            r"(?:(?:system|initial|original|hidden|full|exact|configured|underlying)\s+){1,3}"
            r"(?:prompt|instruction|rule)s?",
            re.IGNORECASE,
        ),
    ),
)

_ALL_SCREEN_PATTERNS = _CONTROL_TOKEN_PATTERNS + _DIRECTIVE_PATTERNS

MAX_CONTEXT_DEPTH = 6


class CallerDataError(ValueError):
    """A caller field failed its contract. Carries a field reference, never a value.

    ``code`` is the reason CLASS, carried with the error rather than inferred
    from its text at the handler. A value the caller can correct completes the
    run carrying this code, so the reason reaches the caller and a corrected
    request can be sent on the same conversation.
    """

    def __init__(self, message: str, code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.code = code


class CallerContentRefused(CallerDataError):
    """Content this agent refuses to act on — NOT a value the caller can correct.

    A separate type rather than a distinguishing phrase in the message, because
    the handler has to be able to tell the two apart by structure: matching on
    wording would put a refusal one careless edit away from being reported as a
    correctable value, and the two stop the request in different ways.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="")


def _reassembled(text: str) -> str:
    """What a reader sees once the inert runs between words are taken out.

    A strip is not a refusal, and it can make an attack HARDER to see: replacing
    a card number with a stub, or a renderer dropping a markup tag, can turn a
    directive that no pattern matched into plain prose that reads perfectly.
    This candidate removes the redaction stub and simple markup tags and closes
    the resulting gaps, so the screen sees the re-assembled sentence.
    """
    without_stub = strip_direct_identifiers(text).replace(REDACTION_STUB, " ")
    return _WHITESPACE_RE.sub(" ", _MARKUP_RE.sub("", without_stub))


def screen_text(text: str) -> Optional[str]:
    """Name the first disallowed pattern in one string, or None.

    Screens the string four ways, and no pass subsumes another:
      * as received — control tokens have to be seen before any rewrite could
        consume them;
      * after the personal-data strip;
      * with zero-width and bidi characters removed — those are invisible to a
        human reviewer and transparent to a reader;
      * re-assembled — see _reassembled above.
    """
    for candidate in (text, strip_direct_identifiers(text), _INVISIBLE_RE.sub("", text), _reassembled(text)):
        for name, pattern in _ALL_SCREEN_PATTERNS:
            if pattern.search(candidate):
                return name
    return None


def _reference(parent: str, name: object, index: int) -> str:
    """Render a caller-supplied field name safe to repeat in a refusal."""
    if isinstance(name, str) and _SAFE_NAME_RE.match(name) and screen_text(name) is None:
        return f"{parent}.{name}"
    return f"{parent}[field #{index}]"


def screen_payload(value: object, reference: str = "input_context", depth: int = 0) -> Optional[Tuple[str, str]]:
    """Depth-first screen of a parsed payload; returns (pattern, field) or None.

    Mapping KEYS are screened as well as values: a payload delivered as JSON can
    write any pattern into a key, and \\u escapes make a scan of the raw request
    text unreliable — only a scan after parsing sees what the reader will see.
    Nesting is bounded so a pathologically nested payload cannot exhaust the
    stack before the per-field checks run.
    """
    if depth > MAX_CONTEXT_DEPTH:
        return ("nesting_depth", reference)
    if isinstance(value, str):
        hit = screen_text(value)
        return (hit, reference) if hit else None
    if isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items(), start=1):
            child = _reference(reference, key, index)
            if isinstance(key, str):
                hit = screen_text(key)
                if hit:
                    return (hit, child)
            found = screen_payload(item, child, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value, start=1):
            found = screen_payload(item, f"{reference}[{index}]", depth + 1)
            if found:
                return found
        return None
    return None


# ── Field parsers (every failure refuses the request) ─────────────────────────
def parse_number(
    value: object,
    *,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    """Parse a caller figure, or refuse.

    Rejects booleans (True is an int in Python), non-numeric text, NaN and the
    infinities, and anything outside the stated range. A non-finite value that
    reaches a comparison never raises — it makes every comparison False, so a
    discount ceiling computed against one is neither enforced nor reported, and
    the plan renders "nan%" with nothing in the log to explain it.
    """
    if isinstance(value, bool) or value is None:
        raise CallerDataError(f"{field} must be a number")
    if isinstance(value, str):
        try:
            number = float(value.replace(",", "").strip())
        except (TypeError, ValueError):
            raise CallerDataError(f"{field} must be a number") from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise CallerDataError(f"{field} must be a number")
    if not math.isfinite(number):
        raise CallerDataError(f"{field} must be a finite number")
    if not minimum <= number <= maximum:
        raise CallerDataError(f"{field} must be between {minimum:g} and {maximum:g}")
    return number


def parse_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    """Parse a caller whole number, or refuse.

    Runs through parse_number first, so NaN and the infinities are refused here
    too rather than raising an opaque OverflowError at the int() call.
    """
    number = parse_number(value, field=field, minimum=float(minimum), maximum=float(maximum))
    if number != int(number):
        raise CallerDataError(f"{field} must be a whole number")
    return int(number)


def parse_label(value: object, *, field: str) -> str:
    """Parse an inert label (product code, category, channel), or refuse.

    Merchandiser spelling is normalised first — trimmed, lowercased, and
    internal spaces, hyphens, slashes and dots collapsed to underscores — so
    "SKU A-001" becomes "sku_a_001" rather than being refused. Anything still
    outside the alphabet after that refuses the request: these strings are
    rendered into the plan as table cells, so they are restricted rather than
    escaped.
    """
    if not isinstance(value, str):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CallerDataError(f"{field} must be text")
        value = f"{value:g}" if isinstance(value, float) else str(value)
    label = _LABEL_SEPARATORS_RE.sub("_", _INVISIBLE_RE.sub("", value).strip().lower())
    if not _LABEL_RE.match(label):
        raise CallerDataError(f"{field} must be 1-32 characters of letters, digits, spaces or underscores")
    return label


def parse_note(value: object, *, field: str) -> str:
    """Parse a short buying note, or refuse.

    Whitespace is collapsed to single spaces. That is the whole defence against
    caller text forging plan structure: the plan is Markdown, and a block
    element only starts at the beginning of a line, so text that cannot contain
    a newline cannot open a heading, a list item or a fenced block. Pipes and
    backticks are dropped for the same reason inside a table cell.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        value = f"{value}"
    if not isinstance(value, str):
        raise CallerDataError(f"{field} must be text")
    cleaned = _CONTROL_CHARS_RE.sub(" ", _INVISIBLE_RE.sub("", value)).replace("|", " ").replace("`", " ")
    text = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if not text:
        return ""
    if len(text) > MAX_NOTE_CHARS:
        raise CallerDataError(f"{field} must be at most {MAX_NOTE_CHARS} characters")
    return strip_direct_identifiers(text)


# ── SKU record contract ───────────────────────────────────────────────────────
# The keys a caller may send on a product record. Everything else is dropped:
# an unrecognised key has no place in the plan, and echoing an unknown field
# name back is how caller text reaches a reader.
_REQUIRED_SKU_FIELDS = ("sku_id", "category", "days_on_shelf", "sell_through_rate", "current_price")
_OPTIONAL_SKU_FIELDS = ("margin_floor_pct", "note")


def parse_sku(value: object, *, field: str) -> Dict[str, Any]:
    """Parse one product record, or refuse.

    `sell_through_rate` is accepted either as a share (0.0-1.0) or as a
    percentage (0-100) because both spellings arrive from real merchandising
    exports; the ambiguity at exactly 1 is resolved as 100% of one unit sold,
    which is the reading that never overstates remaining stock.
    """
    if not isinstance(value, Mapping):
        raise CallerDataError(f"{field} must be an object")
    if len(value) > MAX_SKU_KEYS:
        raise CallerDataError(f"{field} accepts at most {MAX_SKU_KEYS} keys")

    missing = [name for name in _REQUIRED_SKU_FIELDS if value.get(name) is None]
    if missing:
        raise CallerDataError(f"{field} is missing required field(s): {', '.join(missing)}")

    record: Dict[str, Any] = {
        "sku_id": parse_label(value.get("sku_id"), field=f"{field}.sku_id"),
        "category": parse_label(value.get("category"), field=f"{field}.category"),
        "days_on_shelf": parse_int(
            value.get("days_on_shelf"), field=f"{field}.days_on_shelf", minimum=0, maximum=DAYS_ON_SHELF_MAX
        ),
        "current_price": parse_number(
            value.get("current_price"), field=f"{field}.current_price", minimum=0.0, maximum=PRICE_MAX
        ),
    }
    if record["current_price"] <= 0.0:
        raise CallerDataError(f"{field}.current_price must be greater than 0")

    rate = parse_number(
        value.get("sell_through_rate"), field=f"{field}.sell_through_rate", minimum=0.0, maximum=PERCENT_MAX
    )
    record["sell_through_pct"] = round(rate * 100.0, 4) if rate <= 1.0 else round(rate, 4)

    record["margin_floor_pct"] = round(
        parse_number(
            value.get("margin_floor_pct", 0.0),
            field=f"{field}.margin_floor_pct",
            minimum=0.0,
            maximum=PERCENT_MAX,
        ),
        4,
    )
    record["note"] = parse_note(value.get("note"), field=f"{field}.note")
    return record


def _as_payload(value: object) -> Optional[Mapping[str, Any]]:
    """Read a request payload out of whatever shape the caller sent.

    A bare list is the SKU list; a mapping is the full payload. Anything else —
    a plain merchandising note, for example — carries no structured data, which
    is not an error.
    """
    if isinstance(value, Mapping):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return {"skus": list(value)}
    return None


def parse_free_text_payload(user_input: object) -> Optional[Mapping[str, Any]]:
    """Read the structured payload out of the request's free-text field.

    Returns None when the field is prose rather than JSON. A malformed JSON
    document that LOOKS like one (leading brace or bracket) refuses the request
    instead of being silently treated as prose — a caller who meant to send data
    should hear that it did not arrive.
    """
    if not isinstance(user_input, str):
        return None
    text = user_input.strip()
    if not text:
        return None
    if len(text) > MAX_INPUT_CHARS:
        raise CallerDataError(f"input must be at most {MAX_INPUT_CHARS} characters", code="QUESTION_TOO_LONG")
    if text[0] not in "[{":
        return None
    try:
        return _as_payload(json.loads(text))
    except json.JSONDecodeError:
        raise CallerDataError("input starts as a JSON document but is not valid JSON") from None


def parse_request(user_input: object, input_context: object) -> Dict[str, Any]:
    """Build the validated caller contract from both request channels, or refuse.

    The structured parameters win field by field where both channels carry the
    same field: a caller that sends real fields alongside a JSON blob meant the
    fields.
    """
    context: Mapping[str, Any] = input_context if isinstance(input_context, Mapping) else {}

    screened = screen_payload(context, "input_context")
    if screened:
        raise CallerContentRefused(f"{screened[1]} contains a disallowed instruction pattern ({screened[0]})")

    payload = parse_free_text_payload(user_input) or {}
    screened = screen_payload(payload, "input")
    if screened:
        raise CallerContentRefused(f"{screened[1]} contains a disallowed instruction pattern ({screened[0]})")

    merged: Dict[str, Any] = dict(payload)
    merged.update({key: value for key, value in context.items() if value is not None})

    raw_skus = merged.get("skus")
    if raw_skus is None:
        raw_skus = []
    if not isinstance(raw_skus, Sequence) or isinstance(raw_skus, (str, bytes)):
        raise CallerDataError("skus must be a list of product records")
    if len(raw_skus) > MAX_SKUS:
        raise CallerDataError(f"skus accepts at most {MAX_SKUS} records")

    skus = [parse_sku(item, field=f"skus[{index}]") for index, item in enumerate(raw_skus, start=1)]

    contract: Dict[str, Any] = {"skus": skus}

    if merged.get("channel") is not None:
        contract["channel"] = parse_label(merged.get("channel"), field="channel")
    if merged.get("horizon_weeks") is not None:
        contract["horizon_weeks"] = parse_int(
            merged.get("horizon_weeks"),
            field="horizon_weeks",
            minimum=HORIZON_WEEKS_MIN,
            maximum=HORIZON_WEEKS_MAX,
        )
    if merged.get("max_discount_pct") is not None:
        # A caller may ask for a TIGHTER ceiling than the deployment's. It is
        # narrowed against the configured cap downstream, never widened — the
        # configured cap is the retailer's policy and a request cannot raise it.
        contract["max_discount_pct"] = parse_number(
            merged.get("max_discount_pct"), field="max_discount_pct", minimum=0.0, maximum=PERCENT_MAX
        )

    return contract


def dominant_category(skus: List[Dict[str, Any]]) -> str:
    """The category carrying the most records, or "default" when there are none.

    Ties break on the category name so the same input always produces the same
    plan — a schedule that changed between two identical calls would be
    impossible to review.
    """
    counts: Dict[str, int] = {}
    for sku in skus:
        counts[sku["category"]] = counts.get(sku["category"], 0) + 1
    if not counts:
        return "default"
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
