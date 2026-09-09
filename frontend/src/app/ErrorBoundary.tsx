import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = {
  children: ReactNode;
  title?: string;
};

type State = {
  message: string | null;
};

/**
 * Граница ошибки отрисовки.
 *
 * Во фронтенде её не было нигде: любое исключение внутри render гасило ВСЁ приложение
 * в белый экран, без единой подсказки пользователю. Карточка документа — самое опасное
 * место для этого: она рисует данные, собранные моделью (сводка, заявления, факты),
 * и форма этих данных не гарантирована схемой.
 *
 * Сбрасывать состояние умеет только пересоздание: родитель ставит на границу
 * key={documentId}, поэтому открытие другого документа монтирует чистую границу.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { message: null };

  static getDerivedStateFromError(error: unknown): State {
    return { message: error instanceof Error ? error.message : String(error) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("Сбой отрисовки", error, info?.componentStack);
  }

  render() {
    if (this.state.message !== null) {
      return (
        <div className="emptyState">
          <strong>{this.props.title ?? "Не удалось отобразить раздел"}</strong>
          <span>{this.state.message}</span>
        </div>
      );
    }
    return this.props.children;
  }
}
