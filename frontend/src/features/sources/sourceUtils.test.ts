import { describe, expect, it } from "vitest";
import type { Source, SourceDiagnostics, SourceHealth } from "../../api/types";
import { diagnosticVerdictLabel, getSourceTriage } from "./sourceUtils";

const baseSource: Source = {
  id: 1,
  name: "Test source",
  enabled: true,
  url: "https://example.com",
  rss_url: null,
  parse_strategy: "request",
  source_type: "News",
  update_frequency: "ежедневно",
  listing_url: "https://example.com/news",
  listing_strategy: null,
  listing_selector: null,
  article_link_selector: null,
  article_date_selector: null,
  last_seen_article_url: null,
  last_seen_published_at: null,
};

describe("source triage", () => {
  it("prioritizes diagnostic failure over generic health state", () => {
    const health: SourceHealth = {
      id: 1,
      verdict: "stale",
      articles: 12,
      last_article_at: "2026-06-01T00:00:00Z",
    };
    const diagnostic: SourceDiagnostics = {
      verdict: "no_candidates",
      candidate_count: 0,
    };

    const triage = getSourceTriage(baseSource, health, diagnostic);

    expect(triage.tone).toBe("warn");
    expect(triage.key).toBe("extraction");
    expect(triage.label).toBe("извлечение");
    expect(triage.title).toContain("ссылки");
  });

  it("marks sources with zero articles as a hard issue when no diagnostics are loaded", () => {
    const health: SourceHealth = {
      id: 1,
      verdict: "no_articles",
      articles: 0,
      last_article_at: null,
    };

    const triage = getSourceTriage(baseSource, health);

    expect(triage.tone).toBe("bad");
    expect(triage.key).toBe("no_articles");
    expect(triage.label).toBe("0 статей");
  });

  it("returns muted triage for disabled sources", () => {
    const triage = getSourceTriage({ ...baseSource, enabled: false });

    expect(triage.tone).toBe("muted");
    expect(triage.key).toBe("disabled");
    expect(triage.label).toBe("выключен");
  });

  it("localizes diagnostic verdict labels", () => {
    expect(diagnosticVerdictLabel("playwright_unavailable")).toBe("playwright недоступен");
    expect(diagnosticVerdictLabel("unknown_problem")).toBe("unknown_problem");
  });
});
