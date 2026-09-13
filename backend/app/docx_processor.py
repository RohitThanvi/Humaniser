"""
In-place DOCX humanizer.

Strategy
--------
1. Walk the document body in true reading order (paragraphs AND tables,
   including nested tables inside table cells) using the standard
   python-docx `iter_block_items` recipe.
2. For every paragraph:
     - If it's a heading that starts a References/Bibliography section,
       flip a flag and leave the heading (and everything after it, until
       a genuine new section heading appears) completely untouched.
     - If the paragraph contains an image / drawing / chart object,
       leave it completely untouched (this guarantees images never move
       and are never corrupted).
     - Otherwise split the paragraph's plain text into sentences and run
       each sentence through the guardrails classifier. Sentences that
       match a citation/reference/scientific pattern are frozen;
       everything else is queued for LLM humanization.
3. All queued sentences across the *entire* document are humanized
   concurrently in one batch (bounded by LLM_CONCURRENCY), which is far
   faster than one request per paragraph.
4. Rewritten sentences are stitched back together in original order and
   written into the paragraph's FIRST run only; the paragraph's other
   runs are emptied (not deleted) so that:
     - paragraph-level formatting (alignment, spacing, list numbering)
       is 100% preserved,
     - the first run's character formatting (font, size, bold, color)
       is preserved and applied to the whole rewritten sentence,
     - no runs are removed, so nothing shifts position in the XML tree
       and inline images anchored elsewhere are unaffected,
     - tables keep their exact structure — we only ever touch the text
       inside existing cell paragraphs, never the table grid itself.

This is intentionally conservative: when in doubt (image, complex
run structure we cannot safely re-flow, or LLM failure) we leave the
original text exactly as it was rather than risk corrupting the
document.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from app import guardrails
from app.humanizer import (
    HumanizationFailed,
    HumanizerClient,
    NO_OP_RETRY_SUFFIX,
    RETRY_VARIATION_SUFFIX,
    build_fact_retry_prompt,
)

logger = logging.getLogger("docx_processor")


def iter_block_items(parent):
    """Yield each paragraph and table child, in document order, from a
    Document, _Cell, or table cell. Standard python-docx recipe, extended
    to recurse into nested tables.
    """
    if isinstance(parent, DocumentObject):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError("iter_block_items: unsupported parent type")

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def paragraph_has_image(paragraph: Paragraph) -> bool:
    """True if any run in the paragraph contains a drawing / picture /
    chart / OLE object — anything we must never re-flow."""
    xml = paragraph._p.xml
    return any(
        tag in xml
        for tag in (
            "<w:drawing", "<w:pict", "<w:object", "<a:graphic", "<pic:pic",
        )
    )


def paragraph_style_is_heading(paragraph: Paragraph) -> bool:
    try:
        name = (paragraph.style.name or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return name.startswith("heading") or name in ("title", "subtitle")


@dataclass
class ParagraphJob:
    """One paragraph queued for LLM restructuring, with its protected
    sentences masked out behind opaque lock tokens (see
    guardrails.mask_protected_spans) so the model can freely reorder /
    split / merge sentences instead of rewriting them one-for-one —
    the one-for-one pattern is exactly the leftover structural
    fingerprint modern AI detectors key on."""
    paragraph: Paragraph
    masked_text: str
    mapping: dict[str, str]
    original_text: str
    rewritten_text: str | None = None
    fell_back: bool = False


@dataclass
class ProcessStats:
    paragraphs_seen: int = 0
    paragraphs_rewritten: int = 0
    paragraphs_skipped_image: int = 0
    paragraphs_skipped_reference: int = 0
    paragraphs_fallback_unmask_failed: int = 0
    paragraphs_llm_failed: int = 0
    paragraphs_retried: int = 0
    paragraphs_noop_first_pass: int = 0
    sentences_total: int = 0
    sentences_protected: int = 0
    sentences_humanized: int = 0
    burstiness_before: float = 0.0
    burstiness_after: float = 0.0
    errors: list[str] = field(default_factory=list)


def _sentence_length_stdev(all_text: list[str]) -> float:
    """Cheap proxy for 'burstiness': standard deviation of sentence
    word-counts across the document. Higher = more human-like variation
    in sentence length; a near-zero value is the classic AI tell."""
    import statistics

    lengths = [
        len(s.split())
        for text in all_text
        for s in guardrails.sentence_split(text)
        if s.strip()
    ]
    if len(lengths) < 2:
        return 0.0
    return round(statistics.pstdev(lengths), 2)


def apply_paragraph_text(paragraph: Paragraph, new_text: str) -> None:
    """Write new_text into the paragraph while preserving paragraph-level
    formatting and the first run's character formatting. Other runs are
    text-cleared rather than removed to keep the XML structure (and any
    anchored objects) stable."""
    runs = paragraph.runs
    if not runs:
        # No runs (rare, e.g. empty paragraph) — nothing to safely edit.
        return
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""


async def humanize_docx(input_path: str, output_path: str) -> ProcessStats:
    stats = ProcessStats()
    document = Document(input_path)
    client = HumanizerClient()

    jobs: list[ParagraphJob] = []
    all_original_texts: list[str] = []
    in_reference_section = False

    def process_paragraph(paragraph: Paragraph):
        nonlocal in_reference_section
        stats.paragraphs_seen += 1
        text = paragraph.text
        is_heading = paragraph_style_is_heading(paragraph)

        if is_heading and guardrails.is_reference_heading(text):
            in_reference_section = True
            return  # heading itself left untouched

        if in_reference_section:
            if guardrails.looks_like_new_generic_heading(text, is_heading):
                in_reference_section = False
                # fall through: this new heading paragraph is still just
                # a heading, treat it like a normal short paragraph below
            else:
                stats.paragraphs_skipped_reference += 1
                return

        if paragraph_has_image(paragraph):
            stats.paragraphs_skipped_image += 1
            return

        if not text.strip():
            return

        if is_heading:
            # Headings are usually short labels ("1. Introduction") —
            # safest to leave structural headings untouched entirely,
            # since rewriting them risks losing document navigation/TOC
            # consistency. Body text is where humanization adds value.
            return

        all_original_texts.append(text)
        verdicts = guardrails.classify_paragraph_sentences(text)
        stats.sentences_total += len(verdicts)
        stats.sentences_protected += sum(1 for v in verdicts if v.protected)

        # Mask protected sentences (citations/scientific content) behind
        # opaque lock tokens so the LLM can restructure the REST of the
        # paragraph freely (reorder/split/merge sentences) rather than
        # rewriting sentence-for-sentence — see guardrails.py docstring
        # for why that distinction matters against modern detectors.
        masked_text, mapping = guardrails.mask_protected_spans(text)
        jobs.append(ParagraphJob(paragraph, masked_text, mapping, text))

    def walk(parent):
        for block in iter_block_items(parent):
            if isinstance(block, Paragraph):
                process_paragraph(block)
            elif isinstance(block, Table):
                for row in block.rows:
                    for cell in row.cells:
                        walk(cell)

    walk(document)

    stats.burstiness_before = _sentence_length_stdev(all_original_texts)

    # First pass: restructure every paragraph concurrently, rotating a
    # style seed across the document (see humanizer.STYLE_SEEDS) so the
    # whole file doesn't share one uniform stylistic fingerprint, and
    # jittering temperature per call for extra variation.
    from app.humanizer import STYLE_SEEDS

    async def first_pass(job: ParagraphJob, idx: int) -> str:
        seed = STYLE_SEEDS[idx % len(STYLE_SEEDS)]
        return await client.humanize(job.masked_text, style_seed=seed)

    if jobs:
        try:
            results = await asyncio.gather(
                *(first_pass(j, i) for i, j in enumerate(jobs)),
                return_exceptions=True,
            )
        except Exception as exc:  # noqa: BLE001
            # Should be unreachable now that gather uses return_exceptions,
            # but keep a hard fail-safe so a truly unexpected error still
            # degrades to "keep originals" rather than a 500.
            logger.exception("Batch humanization failed")
            stats.errors.append(str(exc))
            results = [HumanizationFailed(j.masked_text, str(exc)) for j in jobs]

        for job, raw_result in zip(jobs, results):
            if isinstance(raw_result, HumanizationFailed):
                # The LLM call genuinely failed after every retry — keep
                # the original text, but count and report this as a real
                # failure rather than a silent "successful" no-op rewrite.
                job.rewritten_text = job.original_text
                job.fell_back = True
                stats.paragraphs_llm_failed += 1
                stats.errors.append(f"Paragraph humanization failed: {raw_result.reason}")
                continue
            if isinstance(raw_result, Exception):
                job.rewritten_text = job.original_text
                job.fell_back = True
                stats.paragraphs_llm_failed += 1
                stats.errors.append(f"Paragraph humanization failed: {raw_result}")
                continue
            unmasked, ok = guardrails.unmask_protected_spans(raw_result, job.mapping)
            if not ok:
                # Model dropped/duplicated a lock token — never risk a
                # lost citation or altered scientific content. Fall back
                # to the untouched original paragraph. We deliberately do
                # NOT ask the model to "reconcile against the original"
                # here (the old similarity-threshold approach) because a
                # corrective pass shown the original tends to regress
                # toward its exact phrasing/structure, undoing the very
                # restructuring that makes text pass as human.
                job.rewritten_text = job.original_text
                job.fell_back = True
                stats.paragraphs_fallback_unmask_failed += 1
                continue
            job.rewritten_text = unmasked
            stats.sentences_humanized += 1

        # Second pass, targeted only at paragraphs that need it — never a
        # blanket revert:
        #  (a) fact check: any number from the original missing in the
        #      rewrite gets ONE retry with the missing figures named
        #      explicitly, asking for a fresh restructure, not a revert.
        #  (b) burstiness check: a paragraph with 2+ sentences whose
        #      rewritten sentence-length variance came back flat (still
        #      reads uniform/AI-shaped) gets ONE retry with a stronger
        #      variation instruction.
        retry_jobs: list[tuple[ParagraphJob, str]] = []
        for job in jobs:
            if job.fell_back or job.rewritten_text is None:
                continue
            is_noop = job.rewritten_text.strip() == job.original_text.strip()
            # Only force a retry on genuinely substantive paragraphs —
            # a 2-3 word caption or label may legitimately have nothing
            # to restructure, and forcing a retry there just burns an
            # API call for no benefit.
            if is_noop and len(job.original_text.split()) > 6:
                stats.paragraphs_noop_first_pass += 1
                retry_jobs.append((job, NO_OP_RETRY_SUFFIX))
                continue
            missing = guardrails.missing_numbers(job.original_text, job.rewritten_text)
            if missing:
                retry_jobs.append((job, build_fact_retry_prompt(sorted(missing))))
                continue
            sentence_count = len(guardrails.sentence_split(job.original_text))
            if sentence_count >= 2:
                rewritten_variance = _sentence_length_stdev([job.rewritten_text])
                if rewritten_variance < 1.5:
                    retry_jobs.append((job, RETRY_VARIATION_SUFFIX))

        if retry_jobs:
            async def retry_pass(job: ParagraphJob, idx: int, instruction: str) -> str:
                seed = STYLE_SEEDS[idx % len(STYLE_SEEDS)]
                return await client.humanize(job.masked_text, style_seed=seed, extra_instruction=instruction)

            retry_results = await asyncio.gather(
                *(retry_pass(job, i, instr) for i, (job, instr) in enumerate(retry_jobs)),
                return_exceptions=True,
            )
            for (job, instruction), raw_result in zip(retry_jobs, retry_results):
                if isinstance(raw_result, Exception):
                    # Retry failed outright — keep the first-pass result,
                    # already validated above; just note it happened.
                    reason = (
                        raw_result.reason
                        if isinstance(raw_result, HumanizationFailed)
                        else str(raw_result)
                    )
                    stats.errors.append(f"Retry pass failed for a paragraph: {reason}")
                    continue
                unmasked, ok = guardrails.unmask_protected_spans(raw_result, job.mapping)
                if not ok:
                    continue  # keep the first-pass result, already validated above
                still_missing = guardrails.missing_numbers(job.original_text, unmasked)
                if instruction != RETRY_VARIATION_SUFFIX and still_missing:
                    # Still dropping a required figure after a targeted
                    # retry — don't keep trying indefinitely; prefer the
                    # untouched original over risking a wrong fact.
                    job.rewritten_text = job.original_text
                    job.fell_back = True
                    stats.paragraphs_fallback_unmask_failed += 1
                    continue
                job.rewritten_text = unmasked
                stats.paragraphs_retried += 1

    rewritten_texts: list[str] = []
    for job in jobs:
        new_text = (job.rewritten_text or job.original_text).strip()
        if new_text and new_text != job.original_text:
            apply_paragraph_text(job.paragraph, new_text)
            stats.paragraphs_rewritten += 1
            rewritten_texts.append(new_text)
        else:
            rewritten_texts.append(job.original_text)

    stats.burstiness_after = _sentence_length_stdev(rewritten_texts)

    document.save(output_path)
    return stats
