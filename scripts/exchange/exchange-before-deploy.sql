-- Обмен с потребителем выпуска (#198, спека #197). Выполнить на живой базе ДО выката кода ядра, без init-db: новый код
-- читает эти таблицы во вкладке «Журнал обмена», в точке обмена и в шаге exchange-release (без них 500 и FAIL шага).
-- Только новые объекты (последовательность, две таблицы, функция и триггер), старые таблицы не трогаются, замков на них
-- нет. Тот же текст в oiltech_digest/db/schema.sql.
-- Откат (только после отката кода и только если пакетов ещё никто не забирал):
--   DROP TABLE IF EXISTS exchange_sent_signals; DROP TABLE IF EXISTS exchange_packages;
--   DROP FUNCTION IF EXISTS exchange_packages_immutable(); DROP SEQUENCE IF EXISTS exchange_package_numbers;
CREATE SEQUENCE IF NOT EXISTS exchange_package_numbers;
CREATE TABLE IF NOT EXISTS exchange_packages (
  number               BIGINT PRIMARY KEY,
  kind                 TEXT NOT NULL,
  month                TEXT,
  content_fingerprint  TEXT NOT NULL,
  envelope             BYTEA NOT NULL,
  signal_ids           BIGINT[] NOT NULL DEFAULT '{}',
  details              JSONB NOT NULL DEFAULT '{}'::jsonb,
  built_by             TEXT,
  built_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  first_taken_at       TIMESTAMPTZ,
  last_taken_at        TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exchange_packages_release_month ON exchange_packages(month)
  WHERE kind = 'release';
CREATE OR REPLACE FUNCTION exchange_packages_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'пакет обмена № % не удаляется', OLD.number;
  END IF;
  IF (NEW.number, NEW.kind, NEW.month, NEW.content_fingerprint, NEW.envelope, NEW.signal_ids, NEW.details,
      NEW.built_by, NEW.built_at)
     IS DISTINCT FROM (OLD.number, OLD.kind, OLD.month, OLD.content_fingerprint, OLD.envelope, OLD.signal_ids,
                       OLD.details, OLD.built_by, OLD.built_at) THEN
    RAISE EXCEPTION 'пакет обмена № % не меняется: меняются только отметки выдачи', OLD.number;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS exchange_packages_immutable ON exchange_packages;
CREATE TRIGGER exchange_packages_immutable BEFORE UPDATE OR DELETE ON exchange_packages
  FOR EACH ROW EXECUTE FUNCTION exchange_packages_immutable();
CREATE TABLE IF NOT EXISTS exchange_sent_signals (
  signal_id            BIGINT PRIMARY KEY,
  first_package        BIGINT NOT NULL REFERENCES exchange_packages(number),
  last_package         BIGINT NOT NULL REFERENCES exchange_packages(number),
  month                TEXT NOT NULL,
  card_fingerprint     TEXT NOT NULL,
  fingerprint_seen_at  TIMESTAMPTZ NOT NULL,
  state                TEXT NOT NULL DEFAULT 'active',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
