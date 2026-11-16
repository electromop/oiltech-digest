-- Регистрация на экране входа (решение владельца 16.11). Выполнить на живой базе ДО выката кода ядра:
-- новый код пишет и читает users.self_registered. Колонка с постоянным значением по умолчанию добавляется
-- без переписывания таблицы; у всех нынешних учёток FALSE (их заводил администратор).
-- Откат: ALTER TABLE users DROP COLUMN IF EXISTS self_registered; (только после отката кода).
ALTER TABLE users ADD COLUMN IF NOT EXISTS self_registered BOOLEAN NOT NULL DEFAULT FALSE;
