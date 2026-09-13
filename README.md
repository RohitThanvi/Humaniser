# AI Text Humanizer — In-Place Document Edition

Upload a `.docx`, get back the same document with the body text rewritten
to read naturally — **images, tables, and layout stay exactly where they
are**. References/citations and scientific/technical notation are
automatically detected and frozen so the facts and sourcing never change,
and the humanizer is instructed to preserve the original meaning.

This is an evolution of the AI/ML API + Next.js + Clerk tutorial by
Ibrohim Abdivokhidov, extended from a plain text box into a real
**document pipeline** with a FastAPI backend, structural guardrails, and
in-place `.docx` editing.

## How it preserves layout

The backend never regenerates the document. It opens your original
`.docx` with `python-docx`, walks it in true reading order (including
tables and nested tables), and only ever rewrites the *text inside
existing runs* of paragraphs that are plain body text:

- **Images / charts / OLE objects** → paragraph is left 100% untouched.
- **Tables** → structure (rows/cols/merges) is never modified, only cell
  text is rewritten.
- **Headings** → left untouched (keeps TOC / navigation consistent).
- **References / Bibliography section** → once that heading is seen,
  every paragraph after it is frozen until a new, non-reference heading
  appears.
- **In-text citations** (`(Smith, 2020)`, `[1]`, DOIs, arXiv IDs, "see
  Fig. 3", etc.) → detected per-sentence and skipped; the humanizer
  rewrites the rest of the paragraph and moves to the next sentence.
- **Scientific/technical content** (equations, units, `p < 0.05`,
  chemical formulas, LaTeX-like notation) → detected per-sentence and
  skipped.
- Rewritten text is written into the paragraph's first run only (other
  runs are text-cleared, not deleted), so paragraph alignment, spacing,
  list numbering, and the position of every other element in the XML
  tree is preserved exactly.

## Architecture

```
ai-humanizer/
├── backend/                 FastAPI service
│   ├── app/
│   │   ├── main.py          Upload / process / download endpoints
│   │   ├── docx_processor.py  Core in-place docx rewriting engine
│   │   ├── guardrails.py    Reference & scientific-content detection
│   │   ├── humanizer.py     AI/ML API client (OpenAI-compatible)
│   │   ├── auth.py          Clerk JWT verification
│   │   └── config.py
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                 Next.js 14 (App Router) + Tailwind + Clerk
│   ├── app/
│   ├── components/Uploader.tsx
│   └── lib/api.ts
└── docker-compose.yml
```

## 1. Backend setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set AIML_API_KEY (get one at https://aimlapi.com)
uvicorn app.main:app --reload --port 8000
```

Test it:

```bash
curl -X POST http://localhost:8000/api/humanize \
  -F "file=@/path/to/your.docx"
```

### Guardrail tuning

`app/guardrails.py` is pure regex/pattern matching (no LLM calls), so it's
fast and fully auditable. Add more citation styles or unit patterns to
`CITATION_PATTERNS` / `SCIENTIFIC_PATTERNS` as needed for your domain.

## 2. Frontend setup

```bash
cd frontend
npm install
cp .env.local.example .env.local
# add your Clerk keys from https://dashboard.clerk.com
# set NEXT_PUBLIC_API_BASE_URL to your backend URL
npm run dev
```

Open http://localhost:3000, sign in, drag in a `.docx`, and download the
humanized result.

To run without auth during local development, leave
`CLERK_AUTH_ENABLED=false` in `backend/.env` — the API will accept
requests without a token (the frontend UI still shows Clerk sign-in, but
you can call the API directly with curl for testing).

## 3. Docker (full stack locally)

```bash
docker compose up --build
```

Backend on `:8000`, frontend on `:3000`.

## 4. Deploying

**Frontend → Vercel**

```bash
cd frontend
vercel
```

Set these in the Vercel project's Environment Variables:
- `NEXT_PUBLIC_API_BASE_URL` → your deployed backend URL
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- `CLERK_SECRET_KEY`

**Backend → any container host** (Render, Fly.io, Railway, an EC2 box,
etc.) using `backend/Dockerfile`. Set the environment variables from
`backend/.env.example`, in particular:
- `AIML_API_KEY`
- `FRONTEND_ORIGIN` → your deployed Vercel URL (for CORS)
- `CLERK_AUTH_ENABLED=true`, `CLERK_JWKS_URL`, `CLERK_ISSUER` (from your
  Clerk instance's `.well-known/jwks.json` and issuer URL) once you want
  to enforce auth on the API too.

## Rate-limit (429) handling

The `HumanizerClient.humanize()` retry loop treats HTTP 429 differently
from other failures:
- It reads the provider's suggested wait time from a `Retry-After`
  header when present, or parses it out of the JSON error body (Groq's
  429s say `"...try again in 0.7s"` in the message text) — and waits
  exactly that long rather than guessing.
- If no wait time is given, it backs off exponentially (`2^attempt`
  seconds, capped at 60s) with a small random jitter so many
  concurrently-rate-limited paragraph calls don't all retry in lockstep
  and immediately re-trip the limit.
- `LLM_MAX_RETRIES` (default 5) caps the attempts; if a paragraph is
  still rate-limited after that many tries, it fails safe and is left
  as the original text rather than the whole job crashing.
- If you're on a free-tier key with tight per-minute limits, lower
  `LLM_CONCURRENCY` in `.env` (e.g. to 2) so fewer paragraphs are ever
  in flight at once — this reduces how often you hit 429s in the first
  place, on top of the retry logic handling the ones that still occur.

## Using Groq instead of AI/ML API

Groq is OpenAI-compatible, so no code changes are needed — just point
the same env vars at it:

```bash
AIML_API_KEY=your_groq_api_key_here
AIML_BASE_URL=https://api.groq.com/openai/v1
AIML_MODEL=openai/gpt-oss-120b
```

Other current Groq model IDs: `openai/gpt-oss-20b`,
`llama-3.3-70b-versatile`, `qwen/qwen3-32b` — check
https://console.groq.com/docs/models for the current catalogue, since
Groq rotates which open-weight models it hosts.

## Lesson from a real 50%-detected failure

An earlier version of this pipeline (Groq + sentence-transformers, chunk
→ humanize → cosine-similarity gate → "verifier" correction) came back
50% AI-detected on a 100% AI-generated document. Root cause: the
correction path was similarity-*gated reversion* — whenever a rewrite's
embedding similarity to the original dropped below a threshold (which
happens on genuinely good, heavily-restructured paraphrases just as
often as on bad ones), a second, deterministic (temperature 0) call was
asked to "restore 100% accuracy" against the original. With no
instruction to preserve burstiness, that corrective pass naturally
minimizes edit distance from the original it's shown — regressing
exactly the well-restructured half back toward the original's uniform,
AI-shaped sentence boundaries and phrasing. Two paths, two different
structural outcomes, a clean ~50/50 split.

Fixes applied here as a direct result:
- **No revert-to-original correction path, ever.** If something needs
  fixing, the retry asks for a *fresh restructure* with the problem
  named explicitly — never "reconcile against the original."
- **Fact-integrity check instead of embedding similarity** for the
  trigger: literal numbers/figures are extracted from the original and
  must reappear verbatim in the rewrite (`guardrails.missing_numbers`).
  This catches actual factual drift without penalizing legitimate heavy
  paraphrase, which is exactly what embedding similarity conflates.
- **A dedicated burstiness retry**: any paragraph whose rewritten
  sentence-length variance comes back suspiciously flat gets one retry
  with an explicit variation instruction (`RETRY_VARIATION_SUFFIX`) —
  never a blanket similarity-based correction.
- **Citation protection is structural (masking), not prompt-only** — the
  old script only *told* the model not to touch `[1]`-style citations;
  a model can still ignore that instruction. Masking makes it
  impossible to alter what's locked, and unmask failure triggers a
  paragraph-level fallback (see `paragraphs_fallback_unmask_failed` in
  the stats).
- **Rotating style seeds + temperature jitter** per paragraph
  (`humanizer.STYLE_SEEDS`) instead of one fixed persona/temperature for
  the whole document, so the finished document doesn't read as one
  constant model voice end to end.
- **Full docx reconstruction**, not `print()`'d plain text — the old
  script discarded all formatting, images, and tables entirely; this
  one never touches the container document at all outside of text runs.

The `paragraphs_retried` and `paragraphs_fallback_unmask_failed` fields
in the API response exist specifically so you can see *which* paragraphs
needed a second pass or got frozen, instead of discovering a 50/50 split
after the fact with no way to localize it.

## Detector-aware humanization (2026 update)

The original tutorial (and most "humanizer" tools) rewrite text
sentence-by-sentence — swap a word here, reorder a clause there. That
keeps the sentence *boundaries and order* identical to the AI draft,
which is exactly the leftover fingerprint modern detectors now key on:
Turnitin's August 2025 "bypasser detector" explicitly looks for
"unnatural synonym substitution patterns and preserved deep structure
beneath surface-level changes." Detection has also moved past simple
perplexity/burstiness scoring on individual sentences toward
passage-level signals: sentence-length variance, syntactic tree depth,
discourse coherence, and paragraph-level structural signatures.

So this version restructures at the **paragraph level** instead:

1. Every paragraph's citations/scientific spans are masked behind opaque
   `⟦LOCK0⟧`-style tokens (`guardrails.mask_protected_spans`).
2. The *whole paragraph* (with locks in place) is sent to the LLM with
   instructions to re-plan it — split, merge, or reorder sentences,
   vary sentence length deliberately (burstiness), and avoid a
   blocklist of overused AI-tell phrases ("delve into", "moreover",
   "it is important to note", etc.) — rather than translate it
   sentence-for-sentence.
3. Lock tokens are swapped back for the original citation/scientific
   text afterward. If the model drops, duplicates, or alters a lock
   token, that paragraph falls back to its untouched original rather
   than risk a corrupted citation — see `paragraphs_fallback_unmask_failed`
   in the response stats.
4. The API returns a simple **burstiness score** (standard deviation of
   sentence word-count across the document) before vs. after, so you
   can see the rhythm actually became less uniform, not just reworded.

This doesn't claim a 99% "bypass rate" the way some marketing pages do
— those numbers are measured against specific detector snapshots that
change monthly, and are not something to build guarantees on. What
this pipeline does guarantee is: meaning preserved, citations/science
untouched, and a genuine structural rewrite rather than a synonym
shuffle.

### A note on use

Passing off AI-restructured writing as fully your own can violate
academic integrity policies or publication guidelines — those rules
usually turn on *disclosure and originality of thought*, not on
whether a detector's regex happens to fire. If you're using this for
coursework or peer review, check your institution's AI-use policy
first; this tool doesn't know your context and can't make that call
for you.

## Meaning-preservation guarantee

The humanizer's system prompt (`app/humanizer.py`) hard-constrains the
model to: keep the exact same facts/numbers/names, not summarize or
expand, stay within ±20% length, and never touch anything that looks
like a citation/reference/unit/formula (belt-and-braces on top of the
regex guardrail layer). If a rewrite ever fails or times out, the
original sentence is kept verbatim rather than dropped or guessed at —
processing never corrupts content.

## Extending to other formats

The same guardrail module can back a PDF pipeline (e.g., extract text
runs positionally with `pdfplumber`/`PyMuPDF`, humanize non-protected
spans, then redraw). `docx_processor.py` is intentionally structured so
`iter_block_items` + `guardrails.classify_paragraph_sentences` +
`humanizer.HumanizerClient` can be reused by a future `pdf_processor.py`.
#   H u m a n i s e r  
 