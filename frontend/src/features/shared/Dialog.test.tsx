import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Dialog } from "./Dialog";

// Подокно закрывается по затемнению, только если на нём и нажали, и отпустили (ревью 24.10): выделение текста в поле,
// отпущенное за краем подокна, раньше закрывало его вместе с несохранённой правкой.
describe("подокно", () => {
  function renderDialog(onClose = vi.fn()) {
    render(
      <Dialog open title="Править материал" onClose={onClose}>
        <label>
          Заголовок
          <input defaultValue="Пилот роботизированной буровой" />
        </label>
      </Dialog>,
    );
    return onClose;
  }

  it("выделение текста, отпущенное на затемнении, подокно не закрывает", () => {
    const onClose = renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Править материал" });

    fireEvent.pointerDown(screen.getByRole("textbox", { name: "Заголовок" }));
    fireEvent.click(dialog);

    expect(onClose).not.toHaveBeenCalled();
  });

  it("нажатие и отпускание на затемнении закрывают подокно, Escape тоже", () => {
    const onClose = renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Править материал" });

    fireEvent.pointerDown(dialog);
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
