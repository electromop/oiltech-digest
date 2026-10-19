-- Допуск по коду (#113) — схема ДО выката кода, который её читает.
--
-- Только добавления, повторный прогон безопасен. Учётки, которые есть сейчас, получают допуск
-- один раз — в момент добавления колонки: повторный прогон (и init-db) не вернёт допуск тем, с
-- кого его сняли (сброс по списку — задача «Одна роль», #114).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/admission/admission-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'admitted_at') THEN
    ALTER TABLE users ADD COLUMN admitted_at TIMESTAMPTZ;
    UPDATE users SET admitted_at = now();
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS admission_codes (
  user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  code_salt      TEXT NOT NULL,
  code_hash      TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ NOT NULL,
  used_at        TIMESTAMPTZ,
  attempts       INTEGER NOT NULL DEFAULT 0,
  notify_job_id  BIGINT,
  sent_at        TIMESTAMPTZ,
  send_error     TEXT
);
COMMIT;
