import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UsersPage } from "./UsersPage";

describe("UsersPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("показывает допуск и отмечает учётки, которые завели сами на экране входа", async () => {
    const users = [
      { id: 1, email: "admin@example.com", role: "admin", created_at: "2026-10-01T09:00:00Z",
        admitted_at: "2026-10-19T08:52:00Z", self_registered: false },
      // 22:30 UTC 15.11 по Москве уже 16.11.
      { id: 25, email: "stranger@example.com", role: "user", created_at: "2026-11-15T22:30:00Z", admitted_at: null,
        self_registered: true },
      { id: 26, email: "colleague@example.com", role: "user", created_at: "2026-11-16T07:00:00Z", admitted_at: null,
        self_registered: false },
    ];
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ users }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }))));

    render(<UsersPage onUnauthorized={() => undefined} showToast={() => undefined} currentUserId={1} />);

    expect(await screen.findByDisplayValue("есть с 19.10.2026")).toBeInTheDocument();
    expect(screen.getAllByDisplayValue("ждёт кода")).toHaveLength(2);
    expect(screen.getByText("Учётку завели сами на экране входа 16.11.2026.")).toBeInTheDocument();
    expect(screen.getAllByText(/Учётку завели сами/)).toHaveLength(1);
  });
});
