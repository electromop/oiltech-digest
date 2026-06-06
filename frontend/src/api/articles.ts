import { apiFetch } from "./client";
import type { Article, ArticlePatch } from "./types";

export function listArticles(limit = 5000) {
  return apiFetch<Article[]>(`/api/articles?limit=${limit}`);
}

export function updateArticle(articleId: number, payload: ArticlePatch) {
  return apiFetch<{ ok: boolean }>(`/api/articles/${articleId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}
