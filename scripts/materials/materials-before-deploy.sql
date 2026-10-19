-- Бот «Материалы коллег»: таблица до выката ядра с --no-schema (как в schema.sql, идемпотентно).
-- psql: docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest < scripts/materials/materials-before-deploy.sql
CREATE TABLE IF NOT EXISTS colleague_materials (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind             TEXT NOT NULL,
  title            TEXT NOT NULL,
  description      TEXT NOT NULL,
  url              TEXT,
  companies_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
  verdict          TEXT NOT NULL,
  verdict_comment  TEXT,
  file_name        TEXT,
  file_path        TEXT,
  file_size        BIGINT,
  tg_chat_id       BIGINT NOT NULL,
  tg_message_id    BIGINT NOT NULL,
  tg_user_id       BIGINT NOT NULL,
  tg_username      TEXT,
  tg_author        TEXT NOT NULL,
  signal_id        BIGINT REFERENCES signals(id) ON DELETE SET NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_colleague_materials_tg_message
  ON colleague_materials(tg_chat_id, tg_message_id);
CREATE INDEX IF NOT EXISTS idx_colleague_materials_created ON colleague_materials(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_colleague_materials_tg_user ON colleague_materials(tg_user_id, created_at DESC);
