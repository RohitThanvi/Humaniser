const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

export type HumanizeStats = {
  paragraphs_seen: number;
  paragraphs_rewritten: number;
  paragraphs_skipped_image: number;
  paragraphs_skipped_reference: number;
  paragraphs_fallback_unmask_failed: number;
  paragraphs_retried: number;
  sentences_total: number;
  sentences_protected: number;
  sentences_humanized: number;
  burstiness_before: number;
  burstiness_after: number;
  errors: string[];
};

export type HumanizeResponse = {
  job_id: string;
  download_url: string;
  stats: HumanizeStats;
};

export async function humanizeDocument(
  file: File,
  getToken: () => Promise<string | null>
): Promise<HumanizeResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const token = await getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}/api/humanize`, {
    method: "POST",
    body: formData,
    headers,
  });

  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || "Humanization failed");
  }

  return res.json();
}

export function downloadUrlFor(jobId: string): string {
  return `${API_BASE}/api/download/${jobId}`;
}
