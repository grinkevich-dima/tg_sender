-- Инбокс и воронка: этапы, этап лида, заметка, «прочитано», источник сообщения

CREATE TABLE funnel_stages (
    id       serial PRIMARY KEY,
    name     text NOT NULL UNIQUE,
    position int NOT NULL,
    is_goal  boolean NOT NULL DEFAULT false,     -- цель (для отчётов)
    is_lost  boolean NOT NULL DEFAULT false,     -- отказ
    auto     text UNIQUE CHECK (auto IN ('contacted', 'replied'))   -- ставится сам: при отправке / при ответе
);
INSERT INTO funnel_stages(name, position, is_goal, is_lost, auto) VALUES
    ('написали', 1, false, false, 'contacted'),
    ('ответил', 2, false, false, 'replied'),
    ('интерес', 3, false, false, NULL),
    ('записался', 4, false, false, NULL),
    ('пришёл / купил', 5, true, false, NULL),
    ('отказ', 6, false, true, NULL);

ALTER TABLE leads ADD COLUMN stage_id int REFERENCES funnel_stages ON DELETE SET NULL;
ALTER TABLE leads ADD COLUMN stage_changed_at timestamptz;
ALTER TABLE leads ADD COLUMN note text NOT NULL DEFAULT '';
ALTER TABLE leads ADD COLUMN inbox_read_at timestamptz;      -- когда диалог последний раз открывали в инбоксе
CREATE INDEX ix_leads_stage ON leads(stage_id);

-- откуда сообщение: кампания, ответ из инбокса, вручную в Telegram, входящее
ALTER TABLE messages ADD COLUMN source text;
ALTER TABLE messages ADD COLUMN sender_user_id int REFERENCES users;
UPDATE messages SET source = CASE WHEN direction = 'in' THEN 'incoming' ELSE 'campaign' END;
-- одно сообщение Telegram — одна строка (входящее/исходящее может прийти и событием, и из кампании)
CREATE UNIQUE INDEX ux_messages_tg ON messages(account_id, lead_id, direction, tg_message_id) WHERE tg_message_id IS NOT NULL;

-- этапы для уже существующей переписки
UPDATE leads l SET stage_id = (SELECT id FROM funnel_stages WHERE auto = 'replied'), stage_changed_at = now()
 WHERE EXISTS (SELECT 1 FROM messages m WHERE m.lead_id = l.id AND m.direction = 'in');
UPDATE leads l SET stage_id = (SELECT id FROM funnel_stages WHERE auto = 'contacted'), stage_changed_at = now()
 WHERE stage_id IS NULL AND EXISTS (SELECT 1 FROM messages m WHERE m.lead_id = l.id AND m.direction = 'out');
