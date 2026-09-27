import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SourcesPage } from "./SourcesPage";

const template = {
  enabled: true,
  url: "https://example.com",
  rss_url: null,
  parse_strategy: "rss",
  source_type: "News",
  update_frequency: "ежедневно",
  listing_url: null,
  listing_strategy: null,
  listing_selector: null,
  article_link_selector: null,
  article_date_selector: null,
  network_region: "auto",
  network_profile: "direct",
  last_ru_probe_status: null,
  last_external_probe_status: null,
  external_required_reason: null,
  external_cooldown_until: null,
  last_seen_article_url: null,
  last_seen_published_at: null,
  archived_at: null,
};

const daysAgo = (days: number) => new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();

function json(payload: unknown) {
  return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
}

function renderPage() {
  const user = userEvent.setup();
  render(<SourcesPage onUnauthorized={() => {}} showToast={() => {}} />);
  return user;
}

describe("экран «Источники»", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/?screen=sources");
    // В jsdom нет прокрутки: экран прокручивает к источнику из ссылки ?source_id=.
    Element.prototype.scrollIntoView = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/sources?limit=500") {
          return Promise.resolve(
            json([
              { ...template, id: 7, name: "World Oil", url: "https://worldoil.com" },
              { ...template, id: 8, name: "Quiet Source", parse_strategy: "request" },
              { ...template, id: 9, name: "Old Archive", enabled: false, archived_at: daysAgo(13) },
            ]),
          );
        }
        if (url === "/api/source-health?limit=500") {
          return Promise.resolve(
            json([
              { id: 7, verdict: "ok", articles: 25, articles_30d: 12, last_article_at: daysAgo(0) },
              { id: 8, verdict: "stale", articles: 4, articles_30d: 1, last_article_at: daysAgo(10) },
              { id: 9, verdict: "archived", articles: 30, articles_30d: 0, last_article_at: daysAgo(20) },
            ]),
          );
        }
        if (url.startsWith("/api/feedback/reasons")) return Promise.resolve(json([]));
        if (url.startsWith("/api/feedback")) return Promise.resolve(json({ ok: true, entry: null }));
        return Promise.resolve(json({ ok: true }));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("плитки сходятся с таблицей, архив — своя плитка", async () => {
    // 19.09 на одном экране было «133 источника» вверху и «173» в каталоге: плитки
    // считались по отчёту здоровья вместе с архивом, список — без него.
    const user = renderPage();

    const tiles = await screen.findByLabelText("Состояние источников");
    await within(tiles).findByRole("button", { name: /^2\s*Всего источников$/ });
    expect(within(tiles).getByRole("button", { name: /^1\s*Требуют внимания$/ })).toBeInTheDocument();
    expect(within(tiles).getByRole("button", { name: /^1\s*Работают штатно$/ })).toBeInTheDocument();
    expect(within(tiles).getByRole("button", { name: /^1\s*В архиве$/ })).toBeInTheDocument();
    // Старого счётчика «N источников» в шапке больше нет — ему нечем расходиться.
    expect(screen.queryByText(/^\d+ источников$/)).not.toBeInTheDocument();

    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row").slice(1)).toHaveLength(2);
    expect(within(table).getByText("Требует внимания")).toBeInTheDocument();
    expect(within(table).getByText("Нет новых материалов 10 дн.")).toBeInTheDocument();
    expect(within(table).queryByText("Old Archive")).not.toBeInTheDocument();

    await user.click(within(tiles).getByRole("button", { name: /В архиве$/ }));
    expect(within(screen.getByRole("table")).getByText("Old Archive")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).queryByText("World Oil")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Настроить: Old Archive" }));
    expect(screen.getByRole("button", { name: "Вернуть из архива" })).toBeInTheDocument();
  });

  it("ссылка на архивный источник открывает архив один раз, дальше выбор за пользователем", async () => {
    // Ревью F: эффект фокуса возвращал архив после любого клика по плиткам.
    window.history.replaceState(null, "", "/?screen=sources&source_id=9");
    const user = renderPage();

    expect(await screen.findByText("Old Archive")).toBeInTheDocument();
    const tiles = screen.getByLabelText("Состояние источников");

    await user.click(within(tiles).getByRole("button", { name: /Всего источников$/ }));

    expect(await screen.findByText("World Oil")).toBeInTheDocument();
    expect(screen.queryByText("Old Archive")).not.toBeInTheDocument();
  });
});
