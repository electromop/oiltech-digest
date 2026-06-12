import type { Source, SourceDiagnostics, SourceHealth, SourcePatch } from "../../api/types";

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

type TriageTone = "ok" | "warn" | "bad" | "muted";

export type SourceTriage = {
  tone: TriageTone;
  key: string;
  label: string;
  title: string;
  action: string;
};

const diagnosticVerdictLabels: Record<string, string> = {
  ok: "ОК",
  unsupported_strategy: "стратегия не поддержана",
  missing_listing_url: "нет listing URL",
  listing_fetch_failed: "listing не открылся",
  listing_render_failed: "listing не отрендерился",
  no_candidates: "ссылки не извлеклись",
  no_insertable_articles: "статьи не вставляются",
  article_fetch_failed: "статья не открылась",
  article_render_failed: "статья не отрендерилась",
  article_not_insertable: "статья не вставляется",
  missing_or_invalid_channel_url: "неверный telegram URL",
  preview_fetch_failed: "telegram preview недоступен",
  no_posts: "посты не найдены",
  missing_rss_url: "нет RSS URL",
  rss_fetch_failed: "RSS недоступна",
  no_entries: "RSS пустая",
  playwright_unavailable: "playwright недоступен",
};

export function diagnosticVerdictLabel(verdict?: string) {
  if (!verdict) return "—";
  return diagnosticVerdictLabels[verdict] || verdict;
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

function defaultActionForStrategy(strategy?: string | null) {
  if (strategy === "request") return "Запустите диагностику и проверьте страницу новостей, ссылки и шаблон статьи.";
  if (strategy === "playwright") return "Проверьте рендер listing/article и при необходимости прокси для hard-WAF.";
  if (strategy === "rss") return "Проверьте RSS URL и наличие свежих entries.";
  if (strategy === "telegram") return "Проверьте URL канала и доступность preview.";
  return "Запустите диагностику и проверьте настройки источника.";
}

function diagnosticTriage(source: Source, verdict?: string): SourceTriage | null {
  if (!verdict || verdict === "ok") return null;
  switch (verdict) {
    case "missing_listing_url":
      return {
        tone: "bad",
        key: "config",
        label: "настройка",
        title: "Не задана страница новостей.",
        action: "Укажите listing URL и сохраните источник.",
      };
    case "listing_fetch_failed":
    case "listing_render_failed":
    case "preview_fetch_failed":
    case "rss_fetch_failed":
      return {
        tone: "bad",
        key: "access",
        label: "доступ",
        title: "Источник не открывается на этапе диагностики.",
        action: defaultActionForStrategy(source.parse_strategy),
      };
    case "no_candidates":
      return {
        tone: "warn",
        key: "extraction",
        label: "извлечение",
        title: "Страница открывается, но ссылки на материалы не извлекаются.",
        action: "Проверьте listing URL, структуру страницы и при необходимости переведите источник на Playwright.",
      };
    case "no_insertable_articles":
      return {
        tone: "warn",
        key: "content",
        label: "контент",
        title: "Материалы находятся, но не проходят до вставки в базу.",
        action: "Проверьте шаблон статьи, шум и длину текста в диагностике.",
      };
    case "missing_or_invalid_channel_url":
      return {
        tone: "bad",
        key: "telegram",
        label: "telegram",
        title: "У источника некорректный URL Telegram-канала.",
        action: "Исправьте URL канала и повторите диагностику.",
      };
    case "no_posts":
      return {
        tone: "warn",
        key: "telegram",
        label: "telegram",
        title: "Preview канала открывается, но посты не находятся.",
        action: "Проверьте разметку preview и актуальность канала.",
      };
    case "missing_rss_url":
      return {
        tone: "bad",
        key: "rss",
        label: "rss",
        title: "У RSS-источника не задан RSS URL.",
        action: "Укажите RSS URL или переключите источник на request/playwright.",
      };
    case "no_entries":
      return {
        tone: "warn",
        key: "rss",
        label: "rss",
        title: "RSS открывается, но лента пустая.",
        action: "Проверьте актуальность RSS URL и наличие новых записей в фиде.",
      };
    case "playwright_unavailable":
      return {
        tone: "bad",
        key: "infra",
        label: "infra",
        title: "Playwright недоступен в текущем окружении.",
        action: "Проверьте playwright-worker и установку браузера в контейнере.",
      };
    default:
      return {
        tone: "warn",
        key: "diagnostic",
        label: "диагностика",
        title: `Источник вернул статус: ${diagnosticVerdictLabel(verdict)}.`,
        action: defaultActionForStrategy(source.parse_strategy),
      };
  }
}

export function getSourceTriage(
  source: Source,
  health?: SourceHealth,
  diagnostic?: SourceDiagnostics,
): SourceTriage {
  if (!source.enabled) {
    return {
      tone: "muted",
      key: "disabled",
      label: "выключен",
      title: "Источник выключен и не участвует в сборе.",
      action: "Включите источник, если его нужно вернуть в мониторинг.",
    };
  }

  const fromDiagnostic = diagnosticTriage(source, diagnostic?.verdict);
  if (fromDiagnostic) return fromDiagnostic;

  if (health?.verdict === "no_articles") {
    return {
      tone: "bad",
      key: "no_articles",
      label: "0 статей",
      title: "В базе еще нет ни одной статьи из этого источника.",
      action: defaultActionForStrategy(source.parse_strategy),
    };
  }
  if (health?.verdict === "stale") {
    return {
      tone: "warn",
      key: "stale",
      label: "застой",
      title: "Источник давно не приносил новых материалов.",
      action: "Запустите диагностику и проверьте свежий listing/RSS перед форс-парсингом.",
    };
  }

  return {
    tone: "ok",
    key: "ok",
    label: "рабочий",
    title: "Источник выглядит рабочим по текущим данным.",
    action: "Ничего не требуется, только периодический контроль обновлений.",
  };
}

export const TRIAGE_FILTER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "Все проблемы" },
  { value: "no_articles", label: "0 статей" },
  { value: "stale", label: "Застой" },
  { value: "access", label: "Нет доступа" },
  { value: "extraction", label: "Не извлекаются ссылки" },
  { value: "content", label: "Не вставляется контент" },
  { value: "rss", label: "Проблема RSS" },
  { value: "telegram", label: "Проблема Telegram" },
  { value: "config", label: "Нужна настройка" },
  { value: "infra", label: "Проблема infra" },
  { value: "ok", label: "Рабочие" },
  { value: "disabled", label: "Выключенные" },
];

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
