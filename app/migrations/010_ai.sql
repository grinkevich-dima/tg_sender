-- ИИ: профили (инструкция + база знаний + примеры) для кампаний, черновики, разбор входящих

CREATE TABLE ai_profiles (
    id          serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    instruction text NOT NULL DEFAULT '',          -- слой 1: кто пишет, тон, что можно и нельзя (дополняет общую)
    created_by  int REFERENCES users,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- слой 2: база знаний; profile_id NULL — общие карточки команды
CREATE TABLE ai_cards (
    id         serial PRIMARY KEY,
    profile_id int REFERENCES ai_profiles ON DELETE CASCADE,
    title      text NOT NULL,
    body       text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- слой 3: примеры хороших ответов (вопрос клиента → наш ответ)
CREATE TABLE ai_examples (
    id         serial PRIMARY KEY,
    profile_id int REFERENCES ai_profiles ON DELETE CASCADE,
    question   text NOT NULL,
    answer     text NOT NULL,
    source     text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'inbox')),
    lead_id    bigint REFERENCES leads ON DELETE SET NULL,
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- черновики из инбокса: что предложил ИИ и что в итоге отправили (обратная связь)
CREATE TABLE ai_drafts (
    id          bigserial PRIMARY KEY,
    lead_id     bigint NOT NULL REFERENCES leads ON DELETE CASCADE,
    profile_id  int REFERENCES ai_profiles ON DELETE SET NULL,
    user_id     int REFERENCES users,
    question    text,
    draft       text NOT NULL,
    final       text,
    similarity  real,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz
);

ALTER TABLE campaigns ADD COLUMN ai_profile_id int REFERENCES ai_profiles ON DELETE SET NULL;

-- разбор входящего: interest | question | later | refusal | stop
ALTER TABLE messages ADD COLUMN ai_label text;
ALTER TABLE messages ADD COLUMN ai_note text;
