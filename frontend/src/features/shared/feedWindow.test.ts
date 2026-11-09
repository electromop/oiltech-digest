import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { monthLabel, useFeedWindow } from "./feedWindow";

describe("useFeedWindow", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("перезапрашивает окно при возврате на вкладку — 5-е число не застанет старое окно", async () => {
    const payload = { months: ["2026-09"], month: null, read_only: false, rollover_day: 5, archive: [] };
    const fetchMock = vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useFeedWindow());
    await waitFor(() => expect(result.current?.months).toEqual(["2026-09"]));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});

describe("подписи окна месяца", () => {
  it("месяц по-русски, незнакомый формат — как есть", () => {
    expect(monthLabel("2026-08")).toBe("август 2026");
    expect(monthLabel("2027-01")).toBe("январь 2027");
    expect(monthLabel("2026-13")).toBe("2026-13");
    expect(monthLabel("август")).toBe("август");
  });

});
