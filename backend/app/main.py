import logging
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.config import get_settings
from app.docx_processor import humanize_docx
from app.auth import verify_clerk_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

settings = get_settings()

app = FastAPI(
    title="AI Text Humanizer API",
    description=(
        "Uploads a .docx, humanizes body text in place while preserving "
        "images, tables, layout, references/citations, and scientific "
        "notation, and returns the humanized .docx."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS_DIR = Path(tempfile.gettempdir()) / "ai-humanizer-jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)


async def optional_auth(request: Request):
    """If Clerk auth is enabled via env, require and verify a bearer
    token. Otherwise this is a no-op (useful for local dev)."""
    if not settings.CLERK_AUTH_ENABLED:
        return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth_header.removeprefix("Bearer ").strip()
    return await verify_clerk_token(token)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/humanize")
async def humanize_endpoint(
    file: UploadFile = File(...),
    _user=Depends(optional_auth),
):
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are supported")

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > settings.MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f}MB). Max is {settings.MAX_FILE_SIZE_MB}MB.",
        )

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    input_path = job_dir / "input.docx"
    output_path = job_dir / "humanized.docx"
    input_path.write_bytes(contents)

    try:
        stats = await humanize_docx(str(input_path), str(output_path))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Humanization failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}") from exc

    return JSONResponse(
        {
            "job_id": job_id,
            "download_url": f"/api/download/{job_id}",
            "stats": {
                "paragraphs_seen": stats.paragraphs_seen,
                "paragraphs_rewritten": stats.paragraphs_rewritten,
                "paragraphs_skipped_image": stats.paragraphs_skipped_image,
                "paragraphs_skipped_reference": stats.paragraphs_skipped_reference,
                "paragraphs_fallback_unmask_failed": stats.paragraphs_fallback_unmask_failed,
                "paragraphs_retried": stats.paragraphs_retried,
                "sentences_total": stats.sentences_total,
                "sentences_protected": stats.sentences_protected,
                "sentences_humanized": stats.sentences_humanized,
                "burstiness_before": stats.burstiness_before,
                "burstiness_after": stats.burstiness_after,
                "errors": stats.errors,
            },
        }
    )


@app.get("/api/download/{job_id}")
async def download_endpoint(job_id: str, _user=Depends(optional_auth)):
    safe_id = "".join(c for c in job_id if c.isalnum())
    output_path = JOBS_DIR / safe_id / "humanized.docx"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return FileResponse(
        path=str(output_path),
        filename="humanized.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
