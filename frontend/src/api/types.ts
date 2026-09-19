export type User = {
  id: number;
  email: string;
  role?: "admin" | "user";
  created_at?: string;
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
  network_region: "auto" | "ru" | "external";
  network_profile: "direct" | "proxy" | "browser";
  last_ru_probe_status: string | null;
  last_external_probe_status: string | null;
  external_required_reason: string | null;
  external_cooldown_until: string | null;
  last_seen_article_url: string | null;
  last_seen_published_at: string | null;
  // Архив источника: не опрашивается И его статьи не показываются в ленте.
  // NULL = активен. Отдельно от enabled — выключение ленту не чистило.
  archived_at: string | null;
};

export type SourceHealth = {
  id: number;
  verdict: "ok" | "stale" | "no_articles" | "disabled";
  articles: number | null;
  last_article_at: string | null;
};

export type QueryMemoryRow = {
  query: string;
  topic: string | null;
  score: number;
  status: string;
  found_candidates: number;
  tested_articles: number;
  relevant_articles: number;
  avg_score: number | null;
  empty_result: boolean;
  relevance_rate: number;
  last_seen_at: string | null;
  updated_at: string | null;
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
    | "network_region"
    | "network_profile"
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
  status: "new" | "digest" | "archive" | "noise" | "duplicate";
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
  // Весь объём базы (все статьи, включая отсев и вычищенные) — только под плитку «Всего».
  all_articles?: number;
  with_summary: number;
  processed_articles: number;
  // Почищено — статьи, убранные из выдачи перепроверкой релевантности (pending_deletion).
  // Даёт сходимость: всего = почищено + остаётся в работе.
  cleaned_articles?: number;
  selected_for_digest: number;
  avg_score: number;
  sources: number;
  // Счётчики по статусам — по ВСЕЙ базе (а не по загруженной странице), пер-юзерно.
  status_counts?: Record<Article["status"], number>;
};

export type BacklogTaskStatus = "new" | "in_progress" | "done" | "paused" | "rejected";

export type BacklogTask = {
  id: string;
  section: "plan" | "tech" | "inbox";
  priority: string;
  title: string;
  status: BacklogTaskStatus;
  status_label: string;
  updated: string;
  area?: string | null;
  details?: string | null;
  due_date?: string | null;
  comments?: BacklogComment[];
};

export type BacklogComment = {
  id: string;
  author: string;
  text: string;
  created_at: string;
};

export type BacklogPayload = {
  tasks: BacklogTask[];
  counts: Record<BacklogTaskStatus, number>;
  backlog_path: string;
  updated_at: string;
};

export type BackgroundJob = {
  id: number;
  kind: string;
  queue: string;
  execution_region: string;
  capability: string | null;
  agent_run_id?: number | null;
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

export type MaintenanceStatus = {
  retention: {
    stale_minutes: number;
    background_job_days: number;
    export_job_days: number;
  };
  expired_sessions: number;
  stale_running_jobs: number;
  cleanup_candidates: {
    background_jobs: number;
    export_jobs: number;
  };
  external_queues: ExternalQueueStatus;
};

export type ExternalQueueRow = {
  queue_name: string;
  queued: number;
  running: number;
  failed: number;
  ok: number;
  oldest_queued_at: string | null;
  last_heartbeat_at: string | null;
};

export type ExternalQueueStatus = {
  totals: {
    queued: number;
    running: number;
    failed: number;
    ok: number;
    oldest_queued_at: string | null;
    last_heartbeat_at: string | null;
    expired_leases: number;
  };
  queues: ExternalQueueRow[];
};

export type MaintenanceCleanupResult = {
  expired_sessions: number;
  background_jobs: number;
  background_job_days: number;
  export_jobs: number;
  export_job_days: number;
};

export type ReadinessBenchmarkCheck = {
  name: string;
  runs: number;
  rows: number;
  p50_ms: number;
  p95_ms: number;
  max_ms: number;
  status: "ok" | "warn";
};

export type ReadinessBenchmarkReport = {
  iterations: number;
  warn_ms: number;
  params: {
    articles_limit: number;
    source_limit: number;
    jobs_limit: number;
    month: string | null;
    digest_limit: number;
    min_score: number;
  };
  benchmarks: ReadinessBenchmarkCheck[];
  counts: Record<string, number>;
  warnings: string[];
};

export type ArticlePatch = {
  status?: Article["status"];
  selected_for_digest?: boolean;
  analyst_comment?: string | null;
};

export type DigestContentItem = {
  article_id?: number;
  category: string;
  title: string;
  summary: string;
  url: string;
  source?: string;
  published_at?: string | null;
  image_url?: string;
  tag?: string;
  score?: number | null;
  score_label?: string | null;
};

export type DigestContent = {
  month: string | null;
  title: string;
  issue?: {
    title?: string;
    period?: string;
    preheader?: string;
    intro?: string;
    news_title?: string;
    read_more_label?: string;
    empty_summary_text?: string;
    preview_empty_text?: string;
  };
  hero?: {
    badge?: string;
    headline?: string;
    subtitle?: string;
    image_url?: string;
  };
  news: DigestContentItem[];
  footer?: {
    contact_text?: string;
    contact_email?: string;
    note?: string;
    socials?: DigestBrandingSocial[];
  };
};

export type DigestBrandingSocial = {
  label: string;
  accent: string;
  text: string;
};

export type DigestHighlightRules = {
  analytics_source_keywords: string[];
  analytics_category_keywords: string[];
  business_category_keywords: string[];
  cards: DigestHighlightCard[];
};

export type DigestHighlightCard = {
  metric: "total" | "analytics" | "business";
  icon: "doc" | "chart" | "people";
  prefix: string;
  suffix: string;
  noun_one: string;
  noun_few: string;
  noun_many: string;
};

export type DigestBranding = {
  header: {
    brand_text: string;
    brand_suffix: string;
    department_text: string;
  };
  hero: {
    badge: string;
    headline: string;
    subtitle: string;
    image_url: string;
  };
  issue: {
    title_template: string;
    title_template_with_month: string;
    period_label_all: string;
    preheader: string;
    intro_template: string;
    intro_template_with_month: string;
    highlights_title: string;
    news_title: string;
    read_more_label: string;
    empty_summary_text: string;
    preview_empty_text: string;
  };
  footer: {
    contact_text: string;
    contact_email: string;
    note: string;
    socials: DigestBrandingSocial[];
  };
  highlights: DigestHighlightRules;
};

export type MonthlyDigestDraft = {
  id: number;
  month: string;
  title: string;
  status: string;
  items: Array<{
    article_id: number;
    sort_order?: number;
    section?: string | null;
    editor_note?: string | null;
  }>;
};

export type DigestDraftSaveResult = {
  id: number;
  month: string;
  title: string;
  status: string;
  items: number;
  content_items?: number;
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
  negative_keywords_json?: string[];
  enabled: boolean;
  sort_order: number;
};

export type CreateSourcePayload = {
  name: string;
  url: string;
  rss_url?: string;
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

export type ManualArticleImportPayload = {
  url: string;
  source_id?: number | null;
  process?: boolean;
  offline?: boolean;
};

export type ManualArticleImportResult = {
  ok: boolean;
  article: {
    id: number;
    source_id: number;
    source_name: string;
    duplicate: boolean;
    title: string;
    fetch_method: string;
    full_text_status: string | null;
    full_text_method: string | null;
    full_text_chars: number;
  };
  job?: BackgroundJob;
};

export type AuthResponse = {
  ok: boolean;
  user: User;
};

// --- Месячная статистика платформы (раздел «Статистика», admin-only) ---
export type MonthlyPlatformRow = {
  month: string;
  collected: number;
  relevant: number;
  rejected: number;
  hidden: number;
  summarized: number;
  scored: number;
  avg_score: number | null;
  digest_ready: number;
};

export type MonthlyAiCostRow = {
  month: string;
  model: string;
  runs: number;
  cost_usd: number;
};

export type MonthlyActivityRow = {
  month: string;
  user_id: number;
  email: string;
  status: string;
  marks: number;
};

export type MonthlyStats = {
  months: number;
  platform: MonthlyPlatformRow[];
  ai_cost: MonthlyAiCostRow[];
  activity: MonthlyActivityRow[];
  activity_scope: string;
};

// --- Аналитика платформы (экран «Статистика», /api/analytics/monthly) ---
export type AnalyticsCounters = {
  month: string;
  collected: number;
  en: number;
  full_text: number;
  relevant: number;
  rejected: number;
  summarized: number;
  scored: number;
  strong: number;
  top: number;
  hidden: number;
  reprints: number;
  digest_selected: number;
  sources_active: number;
  sources_relevant: number;
  sources_strong: number;
  speed_p50_hours: number | null;
  speed_p90_hours: number | null;
};

export type AnalyticsMonth = AnalyticsCounters & { digest_exports: number; complete: boolean };

export type AnalyticsCost = {
  month: string;
  calls: number;
  articles: number;
  cost_usd: number;
  // Курс ЦБ РФ на последний день месяца (у текущего — на сегодня); «допущение» — ЦБ недоступен.
  usd_rub: number;
  usd_rub_date: string | null;
  usd_rub_source: "ЦБ РФ" | "допущение" | string;
};

export type MonthlyAnalytics = {
  timezone: string;
  today: string;
  current_month: string;
  current_day: number;
  months: AnalyticsMonth[];
  previous_same_period: AnalyticsCounters & { days: number };
  themes: { month: string; tag_id: number; tag: string; relevant: number; strong: number }[];
  top_sources: { month: string; source_id: number; source: string; strong: number; relevant: number; collected: number }[];
  sources_enabled: number;
  targets: { sources: number; articles_month: number; ai_rub_month: number };
  // Только администратору: стоимость — коммерческая сторона.
  ai_cost?: AnalyticsCost[];
  ai_cost_previous_same_period?: AnalyticsCost & { days: number };
};

// --- Приём файлов: документы пользователя (экран «Материалы») ---
export type DocumentStatus = "uploaded" | "parsed" | "processing" | "ready" | "failed";

export type UploadedDocument = {
  id: number;
  filename: string;
  kind: string | null;
  size_bytes: number | null;
  status: DocumentStatus | string;
  error_message: string | null;
  anchor_unit: string | null;
  anchor_count: number | null;
  empty_anchors: number | null;
  text_chars: number | null;
  essence: string | null;
  doc_type: string | null;
  publisher: string | null;
  fact_count: number | null;
  created_at: string | null;
};

// summary_json/claims_json приходят из БД как JSON: с сервера обещан список пунктов,
// но тип здесь unknown НАМЕРЕННО — карточка не должна падать, если в поле окажется
// объект или строка, а не список (см. ErrorBoundary вокруг карточки).
export type DocumentCard = {
  doc_type: string | null;
  publisher: string | null;
  doc_date: string | null;
  // Откуда взята дата: «документ» / «имя файла» / «нет». Название файла мог поменять
  // кто угодно, поэтому в карточке это помечается (тикет #50).
  date_source?: string | null;
  language: string | null;
  essence: string | null;
  summary_json: unknown;
  claims_json: unknown;
};

export type DocumentFact = {
  value: unknown;
  unit: string | null;
  context: string | null;
  anchor: number | string | null;
  verified: boolean;
};

export type DocumentDetails = {
  ok: boolean;
  document: UploadedDocument;
  card: DocumentCard | null;
  facts: DocumentFact[];
};

export type DocumentListResult = {
  ok: boolean;
  documents: UploadedDocument[];
};

export type DocumentUploadResult = {
  ok: boolean;
  duplicate: boolean;
  document: UploadedDocument;
  job_id?: number;
};

// Обратная связь человека: оценки 1–5 + быстрая причина + комментарий.
// Поля выведены из того, как заказчик уже пишет ОС руками (чат 10.09):
// корректировка заголовка, актуальность статьи, качество источника, правки перевода.
export type FeedbackEntry = {
  id: number;
  user_id: number;
  article_id: number | null;
  source_id: number | null;
  reason: string | null;
  usefulness: number | null;
  translation: number | null;
  source_quality: number | null;
  comment: string | null;
  created_at: string;
  updated_at: string;
};

export type FeedbackReason = { value: string; label: string };

export type FeedbackSourceSummary = {
  source_id: number;
  source_name: string;
  entries: number;
  avg_usefulness: number | null;
  avg_translation: number | null;
  avg_source_quality: number | null;
  off_topic: number;
  incomplete_text: number;
  duplicate: number;
  bad_translation: number;
  good: number;
};
