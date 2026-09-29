import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FeedbackEntry } from "../../api/types";
import { FeedbackPanel } from "./FeedbackPanel";

const WRONG_BLOCK = "Не тот блок — это технологический сигнал";

// Словарь как у сервера: для источника он сам убирает причины про сигнал.
const REASONS = [
  { value: "off_topic", label: "Не соответствует тематике" },
  { value: "good", label: "Ценный сигнал" },
  { value: "wrong_block", label: WRONG_BLOCK },
  { value: "other", label: "Другое" },
];

function json(payload: unknown) {
  return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
}

function savedEntry(body: Record<string, unknown>): FeedbackEntry {
  return {
    id: 1,
    user_id: 5,
    article_id: (body.article_id as number) ?? null,
    source_id: (body.source_id as number) ?? null,
    reason: (body.reason as string) ?? null,
    usefulness: null,
    translation: null,
    source_quality: null,
    comment: null,
    created_at: "2026-09-29T09:00:00Z",
    updated_at: "2026-09-29T09:00:00Z",
  };
}

describe("ОС: «Не тот блок» в ленте бизнес-сигналов (встреча 21.09, решение 5)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/feedback/reasons")) {
        const forSource = url.includes("target=source");
        return Promise.resolve(json(forSource ? REASONS.filter((r) => r.value !== "wrong_block") : REASONS));
      }
      if (url === "/api/feedback" && init?.method === "POST") {
        return Promise.resolve(json({ ok: true, entry: savedEntry(JSON.parse(String(init.body))) }));
      }
      if (url.startsWith("/api/feedback")) return Promise.resolve(json({ ok: true, entry: null }));
      return Promise.resolve(json({ ok: true }));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("в карточке сигнала чип есть и сохраняется как причина", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    render(<FeedbackPanel articleId={42} onSaved={onSaved} />);

    const chip = await screen.findByRole("button", { name: WRONG_BLOCK });
    await user.click(chip);

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({ article_id: 42, reason: "wrong_block" });
    expect(chip).toHaveClass("active");
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toContain("/api/feedback/reasons?target=article");
  });

  it("повторный клик по чипу снимает причину явным null", async () => {
    // Сервер снимает причину только по явному "reason": null, а не присланное поле оставляет
    // как было. Если ключ потеряется (например, undefined вместо null), чип снова не снять.
    const user = userEvent.setup();
    render(<FeedbackPanel articleId={42} />);

    const chip = await screen.findByRole("button", { name: WRONG_BLOCK });
    await user.click(chip);
    await waitFor(() => expect(chip).toHaveClass("active"));
    await waitFor(() => expect(chip).toBeEnabled());
    await user.click(chip);
    await waitFor(() => expect(chip).not.toHaveClass("active"));

    const posts = fetchMock.mock.calls
      .filter(([, init]) => (init as RequestInit | undefined)?.method === "POST")
      .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
    expect(posts).toEqual([
      { article_id: 42, reason: "wrong_block" },
      { article_id: 42, reason: null },
    ]);
  });

  it("в ОС по источнику чипа нет: панель просит набор для источника", async () => {
    render(<FeedbackPanel sourceId={7} withTranslation={false} />);

    await screen.findByRole("button", { name: "Ценный сигнал" });
    expect(screen.queryByRole("button", { name: WRONG_BLOCK })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toContain("/api/feedback/reasons?target=source");
  });
});
