"""
LLM-backed sentence/paragraph humanizer using the AI/ML API
(https://aimlapi.com) — an OpenAI-compatible chat-completions endpoint.

Design goals:
  - Preserve meaning exactly (no facts added/removed/changed).
  - Never touch anything the guardrails layer has already marked protected
    (that filtering happens *before* this module is called).
  - Return plain rewritten text with no extra commentary, quotes, or
    markdown — this text is spliced back into the original docx run.
"""
import asyncio
import logging
import random
import re

import httpx

from app.config import get_settings

logger = logging.getLogger("humanizer")


class RateLimitError(Exception):
    """Raised on HTTP 429 from the provider. Carries the provider's
    suggested wait time (from a Retry-After header) when available, so
    the retry loop can wait exactly as long as asked instead of guessing."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(f"Rate limited (429), retry_after={retry_after}")
        self.retry_after = retry_after


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """Extract a wait time from a 429 response. Groq and most
    OpenAI-compatible providers send a standard `Retry-After` header
    (seconds); some send provider-specific headers or put the wait time
    in the JSON error body instead — check both."""
    header_val = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    if header_val:
        try:
            return float(header_val)
        except ValueError:
            pass
    try:
        body = resp.json()
        msg = str(body.get("error", {}).get("message", ""))
        m = re.search(r"try again in ([\d.]+)s", msg, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return None

AI_TELL_PHRASES = [
    "delve into", "it is important to note", "it's important to note",
    "in today's world", "in conclusion", "moreover", "furthermore",
    "boasts", "a testament to", "plays a pivotal role", "in the realm of",
    "navigate the complexities", "unlock the potential", "landscape of",
    "at the end of the day", "when it comes to", "shed light on",
    "underscore", "leverage", "robust", "seamless", "seamlessly",
    "in summary", "overall, it is clear", "this highlights",
]

HARD_RULES = """
HARD RULES — never break these:
1. Preserve the EXACT original meaning, every fact, number, name, and
   claim. This is a restructure, not a summary or an argument — do not
   add opinions, hedges, or new claims.
2. Do not translate — keep the same language as the input.
3. The input may contain opaque lock tokens shaped exactly like
   ⟦LOCK0⟧, ⟦LOCK1⟧, etc. These represent citations, references, or
   scientific/technical content that must not be altered, reworded, or
   removed. Copy every lock token through to your output byte-for-byte,
   exactly once each, in whatever position makes sense in your
   restructured version (you MAY move a lock token to a different
   position in the paragraph if that produces more natural phrasing,
   as long as it stays in the same sentence-level context). Never
   invent new lock tokens, never merge two into one, never drop one.
4. Keep roughly the same length (+/- 25%). Do not pad or over-compress.
5. Every standalone number, percentage, or figure in the input (e.g.
   "42", "15%", "1995") MUST reappear, unchanged, somewhere in your
   output — restructure the sentence around it, never drop or round it.
6. Return ONLY the rewritten paragraph text. No quotes, no preface, no
   notes, no markdown formatting, no explanation of what you changed.
"""

# Multiple style seeds, rotated across the document, so the whole file
# doesn't share one uniform "voice fingerprint" end to end. Some
# ensemble-style detectors weight consistency of authorial style across
# a whole document as a secondary signal on top of per-paragraph
# perplexity/burstiness, so a document that reads like several human
# passes rather than one constant model temperament is more robust than
# a single fixed persona applied uniformly throughout.
STYLE_SEEDS = [
    "Lean toward slightly terser, more direct phrasing in this pass — "
    "say things plainly, trim throat-clearing.",
    "Lean toward a slightly more discursive, layered phrasing in this "
    "pass — a few longer sentences with a subordinate clause are fine.",
    "Lean toward a brisk, matter-of-fact phrasing in this pass — favor "
    "active voice and concrete verbs over abstract nouns.",
]

BASE_INSTRUCTIONS = f"""You are a careful text restructurer whose job is to make
AI-drafted prose read as if a specific human wrote it in one sitting —
not to swap synonyms.

WHY STRUCTURE, NOT WORDS: modern detectors (Turnitin's 2025+ bypass
detector, Originality.ai, GPTZero) no longer just score individual
sentences. They score the passage's *shape*: sentence-length variance
(burstiness), how uniform paragraph rhythm is, syntactic tree depth,
and whether the deep structure survived a light reword. Word-for-word
synonym substitution keeps the original sentence boundaries and order
intact — that is exactly the leftover fingerprint they now key on. So:

WHAT TO DO:
1. Re-plan the paragraph, don't just reword it. You may split one
   sentence into two, or fuse two short ones into one, or reorder
   clauses — whatever a real person drafting this idea from scratch
   would naturally do.
2. Deliberately vary sentence length within the paragraph: mix short,
   punchy sentences with longer, more layered ones. Avoid producing two
   consecutive sentences of similar length or the same
   subject-verb-object shape.
3. Avoid these overused AI-tell words/phrases entirely, do not use any
   of them even as synonyms of themselves: {", ".join(AI_TELL_PHRASES)}.
4. Use plainer, more concrete word choices over abstract/formal ones
   where it doesn't change meaning. Contractions are fine if the
   register of the text allows it.
5. Do not use a rule-of-three list structure ("X, Y, and Z") in every
   sentence — that repetition is itself a detectable pattern.
"""


def build_system_prompt(style_seed: str | None = None) -> str:
    seed_block = f"\nSTYLE FOR THIS PASS: {style_seed}\n" if style_seed else ""
    return BASE_INSTRUCTIONS + seed_block + HARD_RULES


RETRY_VARIATION_SUFFIX = """
ADDITIONAL INSTRUCTION FOR THIS RETRY: your previous rewrite of this
same paragraph came back with almost no variation in sentence length —
it still reads like a uniform, AI-generated block. Try a noticeably
different structure this time: for example, open with a short sentence
(under 8 words), then follow with a longer, more detailed one. Do not
reuse your previous phrasing.
"""


def build_fact_retry_prompt(missing_numbers: list[str]) -> str:
    joined = ", ".join(missing_numbers)
    return f"""
ADDITIONAL INSTRUCTION FOR THIS RETRY: your previous rewrite of this
paragraph dropped or altered the following exact figure(s) from the
original: {joined}. Restructure the paragraph again — you do not need
to revert to the original wording or sentence order — but make sure
every one of these exact figures appears verbatim somewhere in your
new output.
"""


class HumanizerClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._sem = asyncio.Semaphore(self.settings.LLM_CONCURRENCY)

    async def humanize(
        self,
        text: str,
        style_seed: str | None = None,
        temperature: float | None = None,
        extra_instruction: str | None = None,
    ) -> str:
        """Humanize a single paragraph (may contain ⟦LOCKn⟧ placeholders
        that must be preserved verbatim — see guardrails.mask_protected_spans).

        `extra_instruction` is appended for feedback-driven retries (fact
        recovery, low-burstiness retry) — retries always ask the model to
        restructure again from scratch, never to revert toward the
        original phrasing, so a correction pass can't quietly re-flatten
        an already-humanized paragraph back to AI-shaped prose.
        """
        if not text or not text.strip():
            return text

        system_prompt = build_system_prompt(style_seed)
        if extra_instruction:
            system_prompt += "\n" + extra_instruction
        temp = temperature if temperature is not None else random.uniform(0.65, 0.9)

        async with self._sem:
            for attempt in range(1, self.settings.LLM_MAX_RETRIES + 1):
                try:
                    return await self._call_api(text, system_prompt, temp)
                except RateLimitError as exc:
                    # 429 — the provider is telling us to slow down, not
                    # that the request is bad. Retry more patiently than a
                    # generic failure: honor Retry-After when the provider
                    # sends one (Groq does), otherwise back off
                    # exponentially with jitter so many concurrent
                    # paragraph calls don't all retry in lockstep and
                    # immediately trip the limit again.
                    if attempt == self.settings.LLM_MAX_RETRIES:
                        logger.error(
                            "Rate limited %s times on this paragraph, giving up — "
                            "returning original text untouched.",
                            attempt,
                        )
                        return text
                    wait = exc.retry_after
                    if wait is None:
                        wait = min(60.0, (2 ** attempt)) + random.uniform(0, 1.0)
                    logger.warning(
                        "Rate limited (429) on attempt %s/%s — waiting %.1fs before retry.",
                        attempt, self.settings.LLM_MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Humanize attempt %s failed: %s", attempt, exc)
                    if attempt == self.settings.LLM_MAX_RETRIES:
                        logger.error("Giving up, returning original text untouched.")
                        return text
                    await asyncio.sleep(1.5 * attempt + random.uniform(0, 0.5))
        return text

    async def _call_api(self, text: str, system_prompt: str, temperature: float) -> str:
        settings = self.settings
        if not settings.AIML_API_KEY:
            # No key configured — fail safe by not altering the document.
            raise RuntimeError("AIML_API_KEY is not configured")

        headers = {
            "Authorization": f"Bearer {settings.AIML_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.AIML_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": temperature,
            "max_tokens": max(256, int(len(text.split()) * 2.5) + 64),
        }

        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{settings.AIML_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            if resp.status_code == 429:
                raise RateLimitError(_parse_retry_after(resp))
            resp.raise_for_status()
            data = resp.json()

        rewritten = data["choices"][0]["message"]["content"].strip()

        # Strip accidental wrapping quotes the model sometimes adds.
        if len(rewritten) >= 2 and rewritten[0] == rewritten[-1] and rewritten[0] in "\"'":
            rewritten = rewritten[1:-1].strip()

        return rewritten or text


async def humanize_many(client: HumanizerClient, texts: list[str]) -> list[str]:
    """Humanize a batch of independent text chunks concurrently."""
    return await asyncio.gather(*(client.humanize(t) for t in texts))
