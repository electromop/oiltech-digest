-- Обновления, квитанции и отклики потребителя выпуска (#200, спека #197). Выполнить на живой базе ДО выката кода ядра,
-- без init-db: новый код читает и пишет эти колонки и таблицы во вкладке «Журнал обмена», в точке обмена, в шаге
-- exchange-update, в разборе ОС и на витрине (строка откликов у администратора); без них 500 и FAIL шага. Новые колонки
-- двух таблиц обмена (короткий замок, таблицы маленькие) и новые таблицы; прочие таблицы не трогаются. Тот же текст в
-- oiltech_digest/db/schema.sql.
-- Откат (только после отката кода):
--   DROP TABLE IF EXISTS exchange_point_refusals; DROP TABLE IF EXISTS exchange_reactions;
--   DROP TABLE IF EXISTS exchange_reaction_packages; DROP TABLE IF EXISTS exchange_update_runs;
--   ALTER TABLE exchange_sent_signals DROP COLUMN IF EXISTS merged_into, DROP COLUMN IF EXISTS state_package,
--     DROP COLUMN IF EXISTS state_at;
--   ALTER TABLE exchange_packages DROP COLUMN IF EXISTS receipt_outcome, DROP COLUMN IF EXISTS receipt_message,
--     DROP COLUMN IF EXISTS receipt_at;
-- Квитанция потребителя на пакет: итог (accepted, already, rejected), его ответ одной строкой и когда она пришла.
-- Пишется один раз (другая квитанция на тот же пакет: 409); триггер неизменности пакета эти поля не сравнивает.
ALTER TABLE exchange_packages ADD COLUMN IF NOT EXISTS receipt_outcome TEXT;
ALTER TABLE exchange_packages ADD COLUMN IF NOT EXISTS receipt_message TEXT;
ALTER TABLE exchange_packages ADD COLUMN IF NOT EXISTS receipt_at TIMESTAMPTZ;
-- Состояние сигнала у потребителя: active (действует), removed (снят), merged (ушёл склейкой в merged_into), unmerged
-- (расклеен на платформе, у потребителя склеен с merged_into). Пакет и момент последней смены состояния.
ALTER TABLE exchange_sent_signals ADD COLUMN IF NOT EXISTS merged_into BIGINT;
ALTER TABLE exchange_sent_signals ADD COLUMN IF NOT EXISTS state_package BIGINT REFERENCES exchange_packages(number);
ALTER TABLE exchange_sent_signals ADD COLUMN IF NOT EXISTS state_at TIMESTAMPTZ;
-- Запуски обновления: шаг раз в сутки (schedule) и «Собрать обновление сейчас» (manual); пакет, если что-то изменилось,
-- и итог словами. Шаг идёт раз за сутки по Москве.
CREATE TABLE IF NOT EXISTS exchange_update_runs (
  id              BIGSERIAL PRIMARY KEY,
  trigger         TEXT NOT NULL,
  package_number  BIGINT REFERENCES exchange_packages(number),
  author          TEXT,
  note            TEXT,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_update_runs_started ON exchange_update_runs(started_at);
-- Пакеты откликов потребителя: номер его счёта, отпечаток содержимого (повтор с тем же: «уже было»), метка источника,
-- отпечаток ключа подписи, число откликов, ответ платформы, когда принят, когда пришёл последний повтор и сколько их.
CREATE TABLE IF NOT EXISTS exchange_reaction_packages (
  number               BIGINT PRIMARY KEY,
  content_fingerprint  TEXT NOT NULL,
  source               TEXT NOT NULL,
  signer               TEXT NOT NULL,
  reactions            INTEGER NOT NULL,
  answer               TEXT NOT NULL,
  received_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  repeats              INTEGER NOT NULL DEFAULT 0
);
-- Отклики: своя таблица, не журнал отметок ОС. Номер сигнала, как пришёл (счёт идёт по корню склейки при чтении), вид,
-- поставлен или снят, день по Москве, метка источника, когда принят.
CREATE TABLE IF NOT EXISTS exchange_reactions (
  id              BIGSERIAL PRIMARY KEY,
  package_number  BIGINT NOT NULL REFERENCES exchange_reaction_packages(number),
  position        INTEGER NOT NULL,
  signal_id       BIGINT NOT NULL,
  reaction        TEXT NOT NULL,
  placed          BOOLEAN NOT NULL,
  day             DATE NOT NULL,
  source          TEXT NOT NULL,
  accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (package_number, position)
);
CREATE INDEX IF NOT EXISTS idx_exchange_reactions_signal ON exchange_reactions(signal_id);
CREATE INDEX IF NOT EXISTS idx_exchange_reactions_accepted ON exchange_reactions(accepted_at);
-- Отказы точки обмена: пакет откликов или квитанция, которые точка не приняла, с причиной (не длиннее 500 знаков);
-- повтор того же отказа считается попыткой: непринятое потребитель шлёт каждым проходом. Для вкладки «Журнал обмена».
CREATE TABLE IF NOT EXISTS exchange_point_refusals (
  id        BIGSERIAL PRIMARY KEY,
  kind      TEXT NOT NULL,
  number    BIGINT NOT NULL,
  reason    TEXT NOT NULL,
  attempts  INTEGER NOT NULL DEFAULT 1,
  first_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (kind, number, reason)
);
