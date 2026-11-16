// Правило пароля одно на все экраны и сервер (auth.validate_password): не короче 8 символов. Повтор пароля сверяет
// только форма регистрации: сервер получает один пароль.
export const MIN_PASSWORD_LENGTH = 8;
export const PASSWORD_TOO_SHORT = `Пароль должен быть не короче ${MIN_PASSWORD_LENGTH} символов`;

export type RegistrationErrors = { password?: string; repeat?: string };

export function registrationProblems(password: string, repeat: string): RegistrationErrors {
  if (password.length < MIN_PASSWORD_LENGTH) return { password: PASSWORD_TOO_SHORT };
  if (repeat !== password) return { repeat: "Пароли не совпадают" };
  return {};
}
