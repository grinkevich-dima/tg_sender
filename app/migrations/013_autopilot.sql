-- Автоответы ИИ в кампаниях: включение по кампании, перехват человеком, очередь ответов с человеческой задержкой

ALTER TABLE campaigns ADD COLUMN ai_autoreply boolean NOT NULL DEFAULT false;

ALTER TABLE leads ADD COLUMN ai_paused boolean NOT NULL DEFAULT false;   -- автопилот для этого человека выключен
ALTER TABLE leads ADD COLUMN ai_handoff text;                             -- нужен человек: причина
ALTER TABLE leads ADD COLUMN ai_handoff_at timestamptz;

-- очередь автоответов: одна запись на «паузу перед ответом»; новые сообщения клиента её переносят
CREATE TABLE ai_reply_jobs (
    id          bigserial PRIMARY KEY,
    lead_id     bigint NOT NULL REFERENCES leads ON DELETE CASCADE,
    account_id  int NOT NULL REFERENCES tg_accounts,
    campaign_id int REFERENCES campaigns ON DELETE SET NULL,
    profile_id  int REFERENCES ai_profiles ON DELETE SET NULL,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sending', 'sent', 'handoff', 'cancelled', 'failed')),
    due_at      timestamptz NOT NULL,
    reason      text,                        -- почему передан человеку / отменён / ошибка
    text        text,                        -- что ИИ отправил
    created_at  timestamptz NOT NULL DEFAULT now(),
    done_at     timestamptz
);
CREATE UNIQUE INDEX ux_ai_jobs_pending ON ai_reply_jobs(lead_id) WHERE status IN ('pending', 'sending');
CREATE INDEX ix_ai_jobs_due ON ai_reply_jobs(status, due_at);
