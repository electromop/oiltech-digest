import { apiFetch } from "./client";
import type { Article, ArticlePatch } from "./types";

// Каталог грузится одним запросом и фильтруется на клиенте. 5000 строк со всеми
// summary/score_items — это мегабайты JSON на каждый вход, что особенно бьёт при
// доступе через VPN/из-за рубежа. Берём топ по score: для редакторской работы
// нижние по релевантности сигналы в каталоге практически не нужны.
export const DEFAULT_ARTICLE_LIMIT = 2000;

export function listArticles(limit = DEFAULT_ARTICLE_LIMIT) {
  return apiFetch<Article[]>(`/api/articles?limit=${limit}`);
}

export function updateArticle(articleId: number, payload: ArticlePatch) {
  return apiFetch<{ ok: boolean }>(`/api/articles/${articleId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}
