"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useState } from "react";
import { downloadUrlFor, humanizeDocument, type HumanizeStats } from "@/lib/api";

type Status = "idle" | "uploading" | "done" | "error";

export default function Uploader() {
  const { getToken } = useAuth();
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [stats, setStats] = useState<HumanizeStats | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const reset = () => {
    setStatus("idle");
    setStats(null);
    setDownloadUrl(null);
    setError(null);
  };

  const onFile = (f: File | null) => {
    reset();
    if (f && !f.name.toLowerCase().endsWith(".docx")) {
      setError("Please upload a .docx file.");
      return;
    }
    setFile(f);
  };

  const onDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0] ?? null;
    onFile(f);
  }, []);

  const submit = async () => {
    if (!file) return;
    setStatus("uploading");
    setError(null);
    try {
      const result = await humanizeDocument(file, () => getToken());
      setStats(result.stats);
      setDownloadUrl(downloadUrlFor(result.job_id));
      setStatus("done");
    } catch (e: any) {
      setError(e.message || "Something went wrong.");
      setStatus("error");
    }
  };

  return (
    <div className="space-y-6">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        className={`flex flex-col items-center justify-center rounded-2xl border-2 border-dashed p-12 text-center transition ${
          dragOver ? "border-brand-500 bg-brand-50" : "border-slate-300 bg-white"
        }`}
      >
        <p className="text-sm text-slate-500">Drag & drop a .docx file here, or</p>
        <label className="mt-3 cursor-pointer rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700">
          Choose file
          <input
            type="file"
            accept=".docx"
            className="hidden"
            onChange={(e) => onFile(e.target.files?.[0] ?? null)}
          />
        </label>
        {file && <p className="mt-4 text-sm font-medium text-slate-700">{file.name}</p>}
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="flex items-center gap-3">
        <button
          disabled={!file || status === "uploading"}
          onClick={submit}
          className="rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40 hover:bg-brand-700"
        >
          {status === "uploading" ? "Humanizing…" : "Humanize document"}
        </button>
        {file && (
          <button
            onClick={() => {
              setFile(null);
              reset();
            }}
            className="text-sm text-slate-500 hover:text-slate-700"
          >
            Clear
          </button>
        )}
      </div>

      {status === "done" && stats && downloadUrl && (
        <div className="rounded-2xl border border-slate-200 bg-white p-6">
          <h3 className="text-base font-semibold text-slate-900">Done ✅</h3>
          <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm text-slate-600 sm:grid-cols-3">
            <Stat label="Paragraphs rewritten" value={stats.paragraphs_rewritten} />
            <Stat label="Paragraphs seen" value={stats.paragraphs_seen} />
            <Stat label="Images preserved" value={stats.paragraphs_skipped_image} />
            <Stat label="Reference paragraphs frozen" value={stats.paragraphs_skipped_reference} />
            <Stat label="Sentences humanized" value={stats.sentences_humanized} />
            <Stat label="Citations/science protected" value={stats.sentences_protected} />
            <Stat label="Paragraphs auto-retried" value={stats.paragraphs_retried} />
          </dl>
          <div className="mt-4 rounded-lg bg-slate-50 px-4 py-3 text-sm text-slate-600">
            <span className="font-medium text-slate-800">Sentence-length variation (burstiness):</span>{" "}
            {stats.burstiness_before.toFixed(2)} → {stats.burstiness_after.toFixed(2)}
            <span className="ml-1 text-slate-400">
              (higher = more natural rhythm, less uniform AI-style pacing)
            </span>
          </div>
          <a
            href={downloadUrl}
            className="mt-6 inline-block rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-semibold text-white hover:bg-slate-700"
          >
            Download humanized.docx
          </a>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="text-lg font-semibold text-slate-900">{value}</dd>
    </div>
  );
}
