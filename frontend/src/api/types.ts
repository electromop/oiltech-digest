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
  // NULL = активен. Отдельно от enabled: выключение ленту не чистило.
  archived_at: string | null;
};

export type SourceHealth = {
  id: number;
  // archived, источник в архиве: сбор выключен и статьи скрыты из ленты. Отдельно от
  // disabled, иначе плитки экрана (с архивом) расходились со списком (без архива).
  verdict: "ok" | "stale" | "no_articles" | "disabled" | "archived";
  articles: number | null;
  // Материалы за 30 дней по дате сбора. Необязательное: старый сервер его не отдаёт.
  articles_30d?: number | null;
  last_article_at: string | null;
};
export type SignalEvidence = {
  id: number;
  signal_id: number;
  article_id: number | null;
  source_url: string;
  title: string;
  title_ru: string | null;
  publisher: string | null;
  published_at: string | null;
  evidence_type: string;
  extracted_fact: string | null;
  summary_ru: string | null;
  strength: number;
  raw_payload_json: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
};

export type Signal = {
  id: number;
  signal_key: string;
  title: string;
  title_ru: string | null;
  theme: string;
  summary: string | null;
  thesis: string | null;
  transferability: string | null;
  // «Решение платформы»; у «другого» после «Отбора» его нет (#132).
  maturity: "reject" | "watch" | "shortlist" | "proven" | string | null;
  confidence: number;
  score: number;
  // Словесная оценка по порогам ленты 80/65/40 (pipeline.score_label): как у бизнес-сигналов. У оценённого вторым
  // вызовом «Карточки» это подпись ступени, без баллов подписи нет (#132).
  score_label?: string | null;
  why_now: string | null;
  why_not_noise: string | null;
  companies_json: string[] | null;
  industries_json: string[] | null;
  evidence_count: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  user_status?: "watch" | "digest" | "archive" | "noise" | "duplicate" | string;
  selected_for_digest?: boolean;
  // Месяц выпуска карточки (ГГГГ-ММ): месяц поступления на радар по Москве, и закрыт ли он
  // (окно месяца, решение владельца 29.09): в закрытом месяце «В дайджест» и «Убрать» не
  // работают, сервер отвечает 409. Считает сервер: своей формулы у экрана нет.
  digest_month?: string;
  digest_locked?: boolean;
  user_comment?: string | null;
  user_status_updated_at?: string | null;
  feedback_count?: number;
  merged_count?: number;
  // Тема: одна из 13 тематик заказчика; false: свободный текст первой партии радара (13.09).
  theme_is_topic?: boolean;
  // Правки людей из ОС поверх текста модели: исходные: для справки.
  original_title_ru?: string | null;
  original_summary?: string | null;
  // Ревью пачки: насколько сигнал интересен на фоне соседей по прогону и почему.
  interest_score?: number | null;
  why_interesting?: string | null;
  // Поля судьи (замечания эксперта заказчика 29.09). event_date: дата самого события (ГГГГ-ММ-ДД), а не
  // поступления на радар; null: у карточки до правки или судья даты не нашёл.
  event_date?: string | null;
  signal_category?: "technology" | "business" | "other" | string | null;
  mixed_events?: boolean | null;
  mixed_events_reason?: string | null;
  // Применимость для нефтесервиса (судья): direct: событие в нефтегазе, transferable: перенос из
  // другой отрасли, none: связи нет (на радар не выходит). oilfield_application: где применить.
  oilfield_relevance?: "direct" | "transferable" | "none" | string | null;
  oilfield_application?: string | null;
  // Балл по профилю «Технологический радар»: подпункты ссылаются на критерии снимка (имя и вес
  // на момент оценки). Нет профиля: балл общий, судьи.
  score_profile?: string | null;
  score_items_json?: SignalScoreItem[] | null;
  criteria_snapshot?: Array<{ id: number; name: string; weight: number }> | null;
  // Почему карточка не на радаре: в выборке «Отсеянные и скрытые».
  hidden_reason?: string | null;
  // Почему такое решение (судья) и ход решения по шагам: судья, ревью пачки, тематика, отсев.
  verdict_reason?: string | null;
  decision_log?: Array<{ stage: string; text: string }> | null;
  // Происхождение (#95): radar: находка поиска, feed: статья ленты, принятая отбором.
  origin?: "radar" | "feed" | string;
  origin_article_id?: number | null;
  // Карточка сигнала (#96, агент «Карточка сигнала»): значения справочников или «не определено»;
  // null: агент поле ещё не заполнял. technology_maturity, зрелость технологии, а maturity выше:
  // решение радара (наблюдать / кандидат / подтверждено).
  event_type?: string | null;
  region?: string | null;
  technology_class?: string | null;
  technology_maturity?: string | null;
  // Компании-участники: только с цитатой из текста публикации (проверено кодом); страна участника (#137): имя
  // справочника стран, null: агент её не знает (у карточек до #137 поля нет).
  participants_json?: Array<{ name: string; quote: string; verified?: boolean; country?: string | null }> | null;
  // Страны события (#137): до трёх имён справочника стран; null: агент поле ещё не заполнял.
  event_countries?: string[] | null;
  // Откуда дата события: «из текста», «по дате публикации» или «не определено»; null: дата судьи радара.
  event_date_source?: string | null;
  // Точность даты события (#132): day, month или year; у месяца и года event_date: первый день периода, и как
  // день он не показывается.
  event_date_precision?: "day" | "month" | "year" | string | null;
  // Кто поставил значение поля карточки: agent, radar_judge или human.
  card_set_by?: Record<string, string> | null;
  card_recipe?: Record<string, unknown> | null;
  // Агент «Отбор» (#130): отрасль, решение и код причины отказа; «спорное»: почему; сбой без решения.
  industry?: "oilfield_services" | "oil_gas" | "other_industry" | string | null;
  otbor_decision?: "accept" | "reject" | string | null;
  reject_reason?: string | null;
  otbor_disputed?: string | null;
  otbor_failure?: "technical" | "invalid_answer" | string | null;
  otbor_failure_reason?: string | null;
  // Короткая причина публикации в разделе отклонённого «кто убрал → причина» (только админу).
  section_reason?: string | null;
  // Второй вызов «Карточки» (#132): ступень балла (её подпись: score_label), баллы «пересчитываются» после
  // «не тот блок», сколько раз «Карточка» возвращала публикацию в «Отбор» (старое событие).
  score_band?: "strong" | "useful" | "watch" | "weak" | string | null;
  scores_pending?: boolean;
  // Балла нет: после «Отбора» без ступени, сбой «Отбора» или отсеяно поиском; «0» тогда не балл.
  unscored?: boolean;
  scores_recipe?: Record<string, unknown> | null;
  otbor_returns?: number;
  // Блоки ОС: каждый человек добавляет свой (оценка, причина, комментарий), чужие не перезаписываются;
  // на отбор и показ они не влияют.
  feedback_blocks?: SignalFeedbackBlock[];
  evidence?: SignalEvidence[];
  // Поля материала выпуска на сигнале (#162, ADR 0006): «Гипотеза для Компании» (кто поставил: card_set_by.hypothesis,
  // почта человека и когда), подпись выпуска (пусто: по типу события).
  hypothesis?: string | null;
  hypothesis_set_at?: string | null;
  hypothesis_author?: string | null;
};

// Правка полей материала выпуска (#162): прислано только меняемое; пустая подпись и гипотеза стирают значение.
export type IssueMaterialPatch = {
  title?: string;
  summary?: string;
  label?: string;
  hypothesis?: string;
  picture?: "own" | "placeholder";
};

export type SignalFeedbackBlock = {
  id: number | null;
  // Почта учётки автора: другого имени у неё нет.
  author: string | null;
  verdict: string | null;
  reason: string | null;
  comment: string | null;
  created_at: string | null;
};

export type SignalScoreItem = {
  criterion_id: number;
  // У «Свежести» без даты события в тексте балла нет: в итог она не входит (excluded: почему).
  final_score: number | null;
  ai_score?: number | null;
  rationale?: string;
  excluded?: string | null;
};

export type SignalPatch = {
  status?: "watch" | "digest" | "archive" | "noise" | "duplicate";
  selected_for_digest?: boolean;
  analyst_comment?: string | null;
};

export type SignalFeedbackPayload = {
  article_id?: number | null;
  signal_id?: number | null;
  signal_evidence_id?: number | null;
  source_url?: string | null;
  signal_title?: string | null;
  source?: string | null;
  comment: string;
  verdict?:
    | "strong_signal"
    | "approved"
    | "watch_later"
    | "background_material"
    | "reject"
    | "wrong_domain"
    | "merge_duplicate"
    // «Не тот блок»: бизнес-сигнал в технологическом радаре (встреча с заказчиком 21.09).
    | "wrong_block"
    // «Оценка завышена» (03.10): находка годная, балл выше, чем она стоит: учит судью.
    | "overrated"
    // Прежняя шкала: с экрана убрана, но в старых записях встречается.
    | "needs_better_source"
    | "bad_translation"
    | "too_generic"
    | null;
  reason?: string | null;
  corrected_title?: string | null;
  corrected_thesis?: string | null;
  duplicate_of_signal_id?: number | null;
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
// Окно месяца ленты (ADR 0001, п. 6): открытые месяцы «ГГГГ-ММ»; month: запрошенный
// месяц; read_only это архив прошлого месяца, только просмотр.
export type FeedWindowInfo = {
  months: string[];
  month: string | null;
  read_only: boolean;
  rollover_day: number;
};

// Прошлый месяц для переключателя «Архив»: статей в месяце и сколько из них выбрано
// «в дайджест» текущим пользователем (по нему конструктор выпуска строит архив выпусков).
export type ArchiveMonth = {
  month: string;
  articles: number;
  digest: number;
};

export type FeedWindowPayload = FeedWindowInfo & {
  archive: ArchiveMonth[];
  // Пока открыт и прошлый месяц (до дня смены окна): сколько материалов уже отмечено на следующий выпуск и с какого
  // дня экран «Дайджест» его соберёт (LOW-5 ревью PR #155).
  next_issue?: { month: string; materials: number; opens_on: string } | null;
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

// Тревоги сторожа полос (lanes.py): застой, очередь без живого воркера, истёкшие аренды.
export type LaneAlert = {
  queue: string | null;
  kind: "stale" | "no_consumer" | "unknown_queue" | "expired_leases" | "contract_mismatch" | string;
  count: number;
  minutes?: number;
  consumer?: string;
  message: string;
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
  alerts?: LaneAlert[];
  // Контракт версий РФ↔NL (contract.py): номер ядра и что сообщил каждый контейнер NL.
  contract?: number;
  consumers?: ExternalConsumer[];
};

export type ExternalConsumer = {
  consumer: string;
  queues: string[];
  build: string | null;
  contract: number | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  mismatch?: boolean;
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
  // Сигнал радара в выпуске, не статья: у него signal_id, а article_id пуст.
  item_type?: "article" | "signal";
  article_id?: number | null;
  signal_id?: number | null;
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
  // Плашка «Технология / Бизнес» (23.10): на экране сборки и в самом выпуске.
  kind?: "technology" | "business" | null;
  kind_label?: string | null;
};

// Раздел выпуска: бизнес-сигналы и технологические сигналы (digest_materials.SECTIONS).
export type DigestSectionKey = "business" | "technology";

// Материал PDF выпуска в формате эксперта (#161, #162): поля его сигнала, у статьи ленты её сигнала ленты.
export type DigestMaterial = {
  item_type: "article" | "signal";
  article_id: number | null;
  signal_id: number | null;
  // Сигнал, которому принадлежат поля материала: правка идёт ему; пусто: у статьи нет сигнала ленты, править нечего.
  material_signal_id: number | null;
  section: DigestSectionKey;
  rubric: string;
  label: string;
  label_default: string;
  title: string;
  summary: string;
  hypothesis: string | null;
  // Кто поставил гипотезу: human или agent (агент гипотезы, #163).
  hypothesis_set_by: string | null;
  hypothesis_author: string | null;
  hypothesis_set_at: string | null;
  relevance: string;
  url: string;
  image_own: boolean;
  picture: "own" | "placeholder";
  // Чего не хватает до выпуска: гипотезы, баллов по критериям, связи с нефтесервисом, своей картинки.
  missing: Array<"hypothesis" | "criteria" | "relevance" | "picture">;
};

export type DigestSection = {
  key: DigestSectionKey;
  number: string;
  title: string;
  materials: DigestMaterial[];
};

// «Главное в выпуске»: строка раздела на обложке; edited: написал человек, default: перечень рубрик раздела.
export type DigestHighlight = {
  key: DigestSectionKey;
  label: string;
  text: string;
  default: string;
  edited: boolean;
  edited_by: string | null;
  edited_at: string | null;
};

export type DigestContent = {
  month: string | null;
  title: string;
  sections?: DigestSection[];
  cover?: { highlights_title: string; highlights: DigestHighlight[] };
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

// Профили скоринга (сессия G, ADR 0002): свой набор критериев у каждой вкладки экрана.
export type ScoringProfile = "business" | "tech_radar";

export type ScoringCriterion = {
  id: number | null;
  name: string;
  description: string | null;
  weight: number;
  keywords_json: string[];
  keywords_en_json: string[];
  sort_order: number;
  enabled?: boolean;
  profile?: ScoringProfile;
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
  // Только на клиенте: стабильный ключ ещё не сохранённого тега (у него нет id, а
  // позиция в списке сдвигается при удалении). Перед сохранением вырезается.
  client_key?: string;
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

// Пароль верный, но учётка ждёт допуска (#113): сессии нет, нужен код от подтверждающих.
export type AdmissionRequiredResponse = {
  ok: false;
  admission_required: true;
  detail: string;
};

export type LoginResponse = AuthResponse | AdmissionRequiredResponse;

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
  // feed: лента (суть, теги, скоринг…), radar: технологический радар (стадии radar_*).
  area?: "feed" | "radar" | string;
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
  // Из cost_usd, технологический радар (стадии radar_*): у него нет статей, и в «₽ за статью»
  // ленты он не входит.
  radar_cost_usd?: number;
  // Курс ЦБ РФ на последний день месяца (у текущего: на сегодня); «допущение»: ЦБ недоступен.
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
  // С какого балла «сильный сигнал» и «топ» (с 08.11 ступени балла сигнала, db/analytics.score_thresholds): по ним
  // подписи плиток и графиков. Нет поля (сервер до 08.11): прежние 60 и 70.
  score_thresholds?: { strong: number; top: number };
  // Только администратору: стоимость: коммерческая сторона.
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
// но тип здесь unknown НАМЕРЕННО: карточка не должна падать, если в поле окажется
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

// Обратная связь человека: оценки 1-5 + быстрая причина + комментарий.
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

// Материалы коллег из Telegram (бот «Материалы коллег», colleague_materials.py).
export type ColleagueMaterial = {
  id: number;
  kind: "link" | "file" | "event";
  kind_label: string;
  title: string;
  description: string;
  url: string | null;
  companies: string[];
  verdict: string;
  verdict_label: string;
  verdict_comment: string | null;
  file_name: string | null;
  file_size: number | null;
  has_file: boolean;
  author: string;
  author_username: string | null;
  signal_id: number | null;
  status: string;
  status_label: string;
  created_at: string | null;
};

export type ColleagueMaterialsResult = {
  ok: boolean;
  materials: ColleagueMaterial[];
  verdicts: Array<{ value: string; label: string }>;
};
