import { useEffect, useRef, useState } from "react";

type PasswordFieldProps = {
  id: string;
  label: string;
  name: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  autoComplete: "current-password" | "new-password";
  error?: string;
};

// Поле пароля с кнопкой «Показать пароль» (как PasswordInput в Carbon): кнопка-иконка у правого края
// поля показывает введённое и снова прячет. Вид поля тот же, что у остальных полей формы (.field).
// Ошибка: текст под полем, поле ссылается на него через aria-describedby.
export function PasswordField(props: PasswordFieldProps) {
  const [visible, setVisible] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const errorId = `${props.id}-error`;
  const toggleLabel = visible ? "Скрыть пароль" : "Показать пароль";

  // При отправке формы пароль снова прячется: открытым текстом браузер мог бы запомнить его в истории полей.
  useEffect(() => {
    const form = inputRef.current?.form;
    if (!form) return;
    const hide = () => setVisible(false);
    form.addEventListener("submit", hide);
    return () => form.removeEventListener("submit", hide);
  }, []);

  return (
    <div className="field">
      <label htmlFor={props.id}>
        <span>{props.label}</span>
      </label>
      <div className="passwordControl">
        <input
          ref={inputRef}
          id={props.id}
          type={visible ? "text" : "password"}
          name={props.name}
          value={props.value}
          onChange={(event) => props.onChange(event.target.value)}
          placeholder={props.placeholder}
          autoComplete={props.autoComplete}
          // Показанный пароль не уходит в проверку орфографии и не правится клавиатурой телефона.
          spellCheck={false}
          autoCapitalize="none"
          autoCorrect="off"
          aria-invalid={props.error ? true : undefined}
          aria-describedby={props.error ? errorId : undefined}
        />
        <button
          type="button"
          className="passwordToggle"
          aria-label={toggleLabel}
          title={toggleLabel}
          aria-controls={props.id}
          onClick={() => setVisible((shown) => !shown)}
        >
          <EyeIcon crossed={visible} />
        </button>
      </div>
      {props.error ? (
        <span id={errorId} className="fieldError">
          {props.error}
        </span>
      ) : null}
    </div>
  );
}

// Глаз: пароль спрятан, кнопка его покажет; перечёркнутый глаз: пароль виден, кнопка его спрячет.
function EyeIcon(props: { crossed: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden="true">
      <path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8s-2.4 4.5-6.5 4.5S1.5 8 1.5 8Z" strokeLinejoin="round" />
      <circle cx="8" cy="8" r="2" />
      {props.crossed ? <path d="M2.5 2.5l11 11" strokeLinecap="round" /> : null}
    </svg>
  );
}
