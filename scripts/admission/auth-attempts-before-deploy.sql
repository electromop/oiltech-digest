-- Защита входа и регистрации (решение владельца 16.11). Выполнить на живой базе ДО выката кода ядра: новый код пишет и
-- читает auth_attempts при каждом входе, вводе кода и регистрации, без таблицы они отвечают 500. Новая таблица, старые
-- таблицы не трогаются, замков на них нет.
-- Откат: DROP TABLE IF EXISTS auth_attempts; (только после отката кода).
CREATE TABLE IF NOT EXISTS auth_attempts (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        TEXT NOT NULL,
  client_ip   TEXT,
  email       TEXT NOT NULL,
  outcome     TEXT NOT NULL DEFAULT 'pending',
  forgiven    BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip ON auth_attempts (client_ip, created_at);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_email ON auth_attempts (email, created_at);
