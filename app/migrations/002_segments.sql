-- Сегменты: именованные списки лидов для кампаний (наполняются из CSV, Telegram, по тегу)
CREATE TABLE segments (
    id          serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '',
    created_by  int REFERENCES users,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE segment_leads (
    segment_id int NOT NULL REFERENCES segments ON DELETE CASCADE,
    lead_id    bigint NOT NULL REFERENCES leads ON DELETE CASCADE,
    source     text,                        -- csv | telegram | tag | manual
    added_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (segment_id, lead_id)
);
CREATE INDEX ix_segment_leads_lead ON segment_leads(lead_id);
