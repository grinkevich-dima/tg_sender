-- Дожимы: условия шагов цепочки, текущий шаг и время следующего у лида кампании, шаг у сообщения
ALTER TABLE campaign_steps ADD COLUMN condition text NOT NULL DEFAULT 'no_reply'
    CHECK (condition IN ('no_reply', 'read_no_reply', 'unread'));

ALTER TABLE campaign_leads ADD COLUMN step int NOT NULL DEFAULT 1;          -- последний отправленный шаг
ALTER TABLE campaign_leads ADD COLUMN next_step_at timestamptz;             -- когда отправлять следующий
ALTER TABLE campaign_leads ADD COLUMN chain_note text;                      -- почему цепочка остановлена / шаг пропущен
CREATE INDEX ix_cl_next_step ON campaign_leads(account_id, next_step_at) WHERE next_step_at IS NOT NULL;

ALTER TABLE messages ADD COLUMN step int;
UPDATE messages SET step = 1 WHERE campaign_lead_id IS NOT NULL;
