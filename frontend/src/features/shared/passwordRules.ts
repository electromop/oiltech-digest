// Правило пароля одно на все экраны и сервер (auth.validate_password): не короче 8 символов. Повтор пароля сверяет
// только форма регистрации: сервер получает один пароль. Слабые пароли (частые, ряды клавиш, повторы, даты, своя почта)
// отклоняет только сервер (auth.password_problem, защита входа 16.11.2026): список частых паролей живёт там, экран
// показывает подсказку заранее и текст отказа сервера.
export const MIN_PASSWORD_LENGTH = 8;
export const PASSWORD_TOO_SHORT = `Пароль должен быть не короче ${MIN_PASSWORD_LENGTH} символов`;
export const PASSWORD_HINT =
  `Не короче ${MIN_PASSWORD_LENGTH} символов. Частые пароли, ряды клавиш, даты и своя почта не подойдут.`;

export type RegistrationErrors = { password?: string; repeat?: string };

export function registrationProblems(password: string, repeat: string): RegistrationErrors {
  if (password.length < MIN_PASSWORD_LENGTH) return { password: PASSWORD_TOO_SHORT };
  if (repeat !== password) return { repeat: "Пароли не совпадают" };
  return {};
}
