import { afterEach, describe, expect, it, vi } from "vitest";
import { apiDownload, readableErrorMessage } from "./client";

describe("текст ошибки API", () => {
  it("отказ сервера показывается текстом detail, а не JSON-строкой", () => {
    expect(readableErrorMessage('{"detail":"Статья относится к архиву за август 2026."}')).toBe(
      "Статья относится к архиву за август 2026.",
    );
  });

  it("список ошибок валидации и не-JSON остаются как есть", () => {
    expect(readableErrorMessage('{"detail":[{"loc":["body"],"msg":"field required"}]}')).toContain('"detail"');
    expect(readableErrorMessage("Internal Server Error")).toBe("Internal Server Error");
  });

  describe("при скачивании файла", () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it("отказ сервера тоже текстом detail: экран предпросмотра PDF показывал сырой JSON", async () => {
      vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"PDF сейчас собирается. Повторите через минуту."}', { status: 503 })));

      await expect(apiDownload("/api/digest-preview.pdf")).rejects.toMatchObject({
        status: 503,
        message: "PDF сейчас собирается. Повторите через минуту.",
      });
    });
  });
});
