-- Наполнение базы знаний ИИ: предложения на проверку (импорт, переписки, мастер) и вопросы без ответа в базе

CREATE TABLE ai_suggestions (
    id          bigserial PRIMARY KEY,
    profile_id  int NOT NULL REFERENCES ai_profiles ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('card', 'example', 'instruction')),
    title       text NOT NULL DEFAULT '',   -- карточка: заголовок; пример: вопрос клиента
    body        text NOT NULL,              -- карточка: факты; пример: наш ответ; инструкция: текст
    source      text NOT NULL,              -- откуда: «файл прайс.pdf», «сайт …», «переписки», «мастер»
    created_by  int REFERENCES users ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ai_suggestions_profile ON ai_suggestions(profile_id, id);

-- вопросы, на которых ИИ не хватило фактов; profile_id NULL — без профиля (общая база)
CREATE TABLE ai_gaps (
    id          bigserial PRIMARY KEY,
    profile_id  int REFERENCES ai_profiles ON DELETE CASCADE,
    question    text NOT NULL,
    reason      text,
    source      text NOT NULL CHECK (source IN ('autopilot', 'draft', 'sandbox')),
    lead_id     bigint REFERENCES leads ON DELETE SET NULL,
    hits        int NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_ai_gaps ON ai_gaps(COALESCE(profile_id, 0), md5(lower(question)));

ALTER TABLE ai_examples DROP CONSTRAINT ai_examples_source_check;
ALTER TABLE ai_examples ADD CONSTRAINT ai_examples_source_check CHECK (source IN ('manual', 'inbox', 'sandbox', 'dialogs'));
