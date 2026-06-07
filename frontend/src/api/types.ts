export type User = {
  id: number;
  email: string;
};

export type Source = {
  id: number;
  name: string;
  enabled: boolean;
  url: string | null;
  rss_url: string | null;
  parse_strategy: string | null;
  source_type: string | null;
  update_frequency: string | null;
  listing_url: string | null;
  listing_strategy: string | null;
  listing_selector: string | null;
  article_link_selector: string | null;
  article_date_selector: string | null;
  last_seen_article_url: string | null;
  last_seen_published_at: string | null;
};

export type SourceHealth = {
  id: number;
  verdict: "ok" | "stale" | "no_articles" | "disabled";
  articles: number | null;
  last_article_at: string | null;
};

export type SourceDiagnostics = {
  verdict?: string;
  candidate_count?: number;
  post_count?: number;
  entry_count?: number;
  listing_probe?: ProbePayload;
  preview_probe?: ProbePayload;
  rss_probe?: ProbePayload;
  article_checks?: DiagnosticArticleCheck[];
  candidates?: DiagnosticListItem[];
  posts?: DiagnosticListItem[];
  entries?: DiagnosticListItem[];
};

export type ProbePayload = {
  status?: number;
  bytes?: number;
  proxy?: string;
};

export type DiagnosticArticleCheck = {
  verdict?: string;
  text_chars?: number;
  candidate_url?: string;
};

export type DiagnosticListItem = {
  title?: string;
  url?: string;
};

export type SourcePatch = Partial<
  Pick<
    Source,
    | "enabled"
    | "url"
    | "rss_url"
    | "parse_strategy"
    | "update_frequency"
    | "listing_url"
    | "listing_strategy"
    | "listing_selector"
    | "article_link_selector"
    | "article_date_selector"
  >
>;

export type Article = {
  id: number;
  title: string;
  url: string;
  source: string;
  tag: string;
  summary: string;
  score: number;
  rating: string;
  status: "new" | "review" | "digest" | "archive";
  language: string | null;
  date: string | null;
  collected: string | null;
  raw_text_chars: number;
  text_truncated: boolean;
  relevant: boolean | null;
  relevance_reason: string | null;
  digest: boolean;
  future_date?: boolean;
  published_at?: string | null;
  score_explanation?: string | null;
  tag_rationale?: string | null;
  score_items?: ScoreItem[];
};

export type ScoreItem = {
  name: string;
  final_score: number;
  rationale?: string | null;
};

export type DashboardStats = {
  total_articles: number;
  with_summary: number;
  processed_articles: number;
  selected_for_digest: number;
  avg_score: number;
  sources: number;
};

export type BackgroundJob = {
  id: number;
  kind: string;
  queue: string;
  status: "queued" | "running" | "ok" | "failed";
  progress: number;
  attempts: number;
  max_attempts: number;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
  run_after: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type ArticlePatch = {
  status?: "new" | "review" | "digest" | "archive";
  selected_for_digest?: boolean;
  analyst_comment?: string | null;
};

export type DigestContentItem = {
  article_id?: number;
  category: string;
  title: string;
  summary: string;
  url: string;
  tag?: string;
  score?: number | null;
  score_label?: string | null;
};

export type DigestContent = {
  month: string;
  title: string;
  news: DigestContentItem[];
};

export type MonthlyDigestDraft = {
  id: number;
  month: string;
  title: string;
  status: string;
  items: Array<{
    article_id: number;
    title: string;
    summary: string;
    url: string;
    tag: string | null;
    score: number | null;
  }>;
};

export type ScoringCriterion = {
  id: number | null;
  name: string;
  description: string | null;
  weight: number;
  keywords_json: string[];
  keywords_en_json: string[];
  sort_order: number;
  enabled?: boolean;
};

export type Tag = {
  id: number | null;
  parent_name: string | null;
  name: string;
  name_en?: string | null;
  description: string | null;
  keywords_json: string[];
  keywords_en_json: string[];
  enabled: boolean;
  sort_order: number;
};

export type CreateSourcePayload = {
  name: string;
  rss_url: string;
  url?: string | null;
  priority?: number;
  category?: string | null;
  update_frequency?: string | null;
};

export type ScrapeResponse = {
  ok: boolean;
  stats: {
    added: number;
    attempted: number;
  };
};

export type AuthResponse = {
  ok: boolean;
  user: User;
};
