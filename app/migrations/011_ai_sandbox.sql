-- Тренажёр ИИ: диалоги-песочницы (в Telegram ничего не уходит)
CREATE TABLE ai_sandboxes (
    id         serial PRIMARY KEY,
    user_id    int NOT NULL REFERENCES users,
    profile_id int REFERENCES ai_profiles ON DELETE SET NULL,
    client     jsonb NOT NULL DEFAULT '{}',       -- вымышленный клиент: имя, группа, вступил, этап, заметка
    persona    text NOT NULL DEFAULT 'interested',
    persona_text text NOT NULL DEFAULT '',       -- свой сценарий клиента-робота
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ai_sandbox_messages (
    id         bigserial PRIMARY KEY,
    sandbox_id int NOT NULL REFERENCES ai_sandboxes ON DELETE CASCADE,
    role       text NOT NULL CHECK (role IN ('client', 'bot')),
    text       text NOT NULL,
    by_robot   boolean NOT NULL DEFAULT false,   -- сообщение клиента написал клиент-робот
    label      text,                             -- разбор ИИ (для сообщений клиента)
    note       text,
    debug      jsonb,                            -- что видел ИИ (для ответов бота)
    saved      boolean NOT NULL DEFAULT false,   -- сохранён как пример
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_sandbox_messages ON ai_sandbox_messages(sandbox_id, id);

-- примеры из тренажёра
ALTER TABLE ai_examples DROP CONSTRAINT ai_examples_source_check;
ALTER TABLE ai_examples ADD CONSTRAINT ai_examples_source_check CHECK (source IN ('manual', 'inbox', 'sandbox'));
