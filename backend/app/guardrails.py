"""
Guardrails: decide which paragraphs / sentences must be left 100% untouched.

Two independent layers:

1. STRUCTURAL guardrail — once we detect the document has entered a
   "References" / "Bibliography" / "Works Cited" section (by heading text),
   every paragraph after that heading is frozen until another top-level
   heading appears that is clearly not a references-type heading.

2. PER-SENTENCE guardrail — even inside body text, individual sentences
   that look like:
     - in-text citations:  (Smith, 2020)   [1]   [12, 13]   (see Fig. 3)
     - DOI / URL / arXiv IDs
     - scientific/mathematical content: equations, chemical formulas,
       units of measure, statistical notation (p < 0.05, r^2 = 0.8, etc.)
   are protected and skipped — the humanizer moves on to the next sentence
   without altering them, per the user's requirement:
   "if there is a reference, never touch it and move to next phrase, and
   scientific elements need not to be changed".

Nothing here uses an LLM — it is pure pattern-matching so it is fast,
deterministic, and auditable.
"""
import re
from dataclasses import dataclass

REFERENCE_HEADING_RE = re.compile(
    r"^\s*(references|bibliography|works cited|literature cited|citations)\s*$",
    re.IGNORECASE,
)

# A heading is "some other section" if it's short, title-cased/numbered,
# and doesn't itself look like a reference-section title.
GENERIC_HEADING_RE = re.compile(r"^\s*(\d+[\.\)]?\s*)?[A-Z][A-Za-z0-9 \-:]{2,60}\s*$")

CITATION_PATTERNS = [
    re.compile(r"\[\s*\d+(\s*[-,]\s*\d+)*\s*\]"),                      # [1], [1,2], [1-3]
    re.compile(r"\(\s*[A-Z][A-Za-z\.\-]+(\s*(&|and)\s*[A-Z][A-Za-z\.\-]+)?,?\s*(et al\.)?,?\s*\d{4}[a-z]?\s*\)"),  # (Smith, 2020) / (Smith & Doe, 2020) / (Smith et al., 2020)
    re.compile(r"\bet al\.\s*\(?\d{0,4}\)?"),
    re.compile(r"\bdoi:\s*\S+", re.IGNORECASE),
    re.compile(r"https?://\S+"),
    re.compile(r"\barXiv:\s*\d{4}\.\d{4,5}", re.IGNORECASE),
    re.compile(r"\(\s*(see\s+)?(fig(ure)?|table|eq(uation)?|section|sec|ch(apter)?)\.?\s*\d+[a-z]?\s*\)", re.IGNORECASE),
]

SCIENTIFIC_PATTERNS = [
    re.compile(r"[=<>≤≥±∑∏∫√]"),                              # math operators
    re.compile(r"\b\d+(\.\d+)?\s*(%|kg|km|cm|mm|mg|ml|mol|hz|khz|mhz|ghz|°c|°f|kj|kcal|nm|µm|ppm|pa|bar|atm)\b", re.IGNORECASE),
    re.compile(r"\bp\s*[<>=]\s*0?\.\d+"),                       # p < 0.05
    re.compile(r"\br\^?2\s*=\s*0?\.\d+", re.IGNORECASE),        # r^2 = 0.8
    re.compile(r"[A-Z][a-z]?\d(\+|\-)?(\s|$)"),                 # chemical formula fragments e.g. H2O, CO2
    re.compile(r"\\[a-zA-Z]+\{"),                               # LaTeX-ish
    re.compile(r"\b[a-zA-Z]\s*=\s*[-\d.]+"),                    # variable assignment, e.g. x = 3.14
]


@dataclass
class SentenceVerdict:
    text: str
    protected: bool
    reason: str | None = None


def is_reference_heading(paragraph_text: str) -> bool:
    return bool(REFERENCE_HEADING_RE.match(paragraph_text.strip()))


def looks_like_new_generic_heading(paragraph_text: str, is_heading_style: bool) -> bool:
    """True if this paragraph likely starts a new (non-reference) section."""
    if not is_heading_style:
        return False
    text = paragraph_text.strip()
    if not text:
        return False
    if is_reference_heading(text):
        return False
    return bool(GENERIC_HEADING_RE.match(text))


def sentence_split(text: str) -> list[str]:
    """Lightweight sentence splitter that keeps punctuation attached."""
    if not text.strip():
        return [text]
    # Split on sentence-ending punctuation followed by whitespace + capital/quote,
    # but avoid splitting on common abbreviations / decimal numbers.
    pieces = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])", text)
    return [p for p in pieces if p != ""]


def classify_sentence(sentence: str) -> SentenceVerdict:
    for pat in CITATION_PATTERNS:
        if pat.search(sentence):
            return SentenceVerdict(sentence, True, "citation/reference")
    for pat in SCIENTIFIC_PATTERNS:
        if pat.search(sentence):
            return SentenceVerdict(sentence, True, "scientific/technical")
    return SentenceVerdict(sentence, False, None)


def classify_paragraph_sentences(text: str) -> list[SentenceVerdict]:
    """Split a paragraph into sentences and classify each one."""
    return [classify_sentence(s) for s in sentence_split(text)]


PLACEHOLDER_TEMPLATE = "⟦LOCK{i}⟧"
PLACEHOLDER_RE = re.compile(r"⟦LOCK(\d+)⟧")


def mask_protected_spans(text: str) -> tuple[str, dict[str, str]]:
    """Replace every protected sentence with an opaque placeholder token
    so the LLM can restructure the paragraph FREELY around fixed anchors,
    instead of rewriting sentence-by-sentence in place.

    Rewriting sentence-by-sentence is exactly the "preserved deep
    structure beneath surface-level changes" signature modern detectors
    (e.g. Turnitin's 2025 bypass-detector) key on: same sentence
    boundaries/order = same skeleton. Masking lets the model reorder,
    merge, and split freely, then we swap the placeholders back for the
    verbatim protected text afterward.

    Returns (masked_text, {placeholder: original_text}).
    """
    verdicts = classify_paragraph_sentences(text)
    mapping: dict[str, str] = {}
    pieces = []
    counter = 0
    for v in verdicts:
        if v.protected:
            token = PLACEHOLDER_TEMPLATE.format(i=counter)
            mapping[token] = v.text
            pieces.append(token)
            counter += 1
        else:
            pieces.append(v.text)
    return " ".join(pieces), mapping


def unmask_protected_spans(text: str, mapping: dict[str, str]) -> tuple[str, bool]:
    """Restore placeholders to their original protected text. Returns
    (result, ok) where ok=False if the model dropped/duplicated/altered a
    placeholder — signalling the caller to fall back to the original
    paragraph rather than risk losing a citation."""
    found_tokens = set(PLACEHOLDER_RE.findall(text))
    expected_tokens = {PLACEHOLDER_RE.match(t).group(1) for t in mapping}  # type: ignore[union-attr]
    if found_tokens != expected_tokens:
        return text, False
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text, True


NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?(?![\w])")


def extract_key_numbers(text: str) -> set[str]:
    """Pull every standalone numeric figure out of a text (counts,
    percentages, sample sizes, years, etc.) so we can verify none of them
    got silently dropped or altered by a paraphrase.

    This is deliberately narrower and more literal than an embedding
    similarity score: a paraphrase can legitimately score low on cosine
    similarity while preserving every fact perfectly (heavy restructuring
    looks like semantic drift to an embedding model even when nothing
    factual changed) — and can score high while quietly swapping a
    number. Checking the literal figures survive is a much more direct
    guarantee of "same meaning" for exactly the kind of numeric claims
    that matter in reports and papers.
    """
    return set(NUMBER_RE.findall(text))


def missing_numbers(original: str, rewritten: str) -> set[str]:
    """Numbers present in the original (outside of already-locked
    protected spans) that vanished from the rewrite."""
    return extract_key_numbers(original) - extract_key_numbers(rewritten)
