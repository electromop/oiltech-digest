import type { SourceDiagnostics, SourceHealth, SourcePatch } from "../../api/types";

export function healthLabel(verdict?: SourceHealth["verdict"]) {
  return (
    {
      ok: "ОК",
      stale: "застой",
      no_articles: "0 статей",
      disabled: "выкл",
    }[verdict || "disabled"] || "—"
  );
}

export function healthClass(verdict?: SourceHealth["verdict"]) {
  if (verdict === "ok") return "ok";
  if (verdict === "stale") return "warn";
  if (verdict === "no_articles") return "bad";
  return "muted";
}

export function normalizePatch(patch: SourcePatch): SourcePatch {
  const payload: Partial<Record<keyof SourcePatch, SourcePatch[keyof SourcePatch]>> = {};
  Object.entries(patch).forEach(([key, value]) => {
    if (value === "") {
      payload[key as keyof SourcePatch] = null;
      return;
    }
    payload[key as keyof SourcePatch] = value as SourcePatch[keyof SourcePatch];
  });
  return payload as SourcePatch;
}

export function diagnosticText(diagnostic: SourceDiagnostics) {
  const probe = diagnostic.listing_probe || diagnostic.preview_probe || diagnostic.rss_probe || {};
  const counts = [
    diagnostic.candidate_count != null ? `кандидатов: ${diagnostic.candidate_count}` : "",
    diagnostic.post_count != null ? `постов: ${diagnostic.post_count}` : "",
    diagnostic.entry_count != null ? `RSS entries: ${diagnostic.entry_count}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const checks = (diagnostic.article_checks || [])
    .slice(0, 3)
    .map((item, index) => `${index + 1}. ${item.verdict || "—"} · ${item.text_chars || 0} симв. · ${item.candidate_url || ""}`)
    .join("\n");

  const items = (diagnostic.candidates || diagnostic.posts || diagnostic.entries || [])
    .slice(0, 3)
    .map((item, index) => `${index + 1}. ${item.title || item.url || ""}`)
    .join("\n");

  return [
    `verdict: ${diagnostic.verdict || "—"}`,
    probe.status ? `HTTP: ${probe.status} · ${probe.bytes || 0} bytes${probe.proxy ? ` · proxy ${probe.proxy}` : ""}` : "",
    counts,
    checks || items,
  ]
    .filter(Boolean)
    .join("\n");
}
