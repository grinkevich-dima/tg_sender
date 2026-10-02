-- Стоп-лист после полного удаления человека (только Telegram ID — чтобы при повторном импорте ему не писали)
CREATE TABLE do_not_contact (
    tg_id    bigint PRIMARY KEY,
    added_at timestamptz NOT NULL DEFAULT now()
);

-- Часовой пояс аккаунта (рабочие часы и дневной лимит); NULL — общий TZ_NAME
ALTER TABLE tg_accounts ADD COLUMN tz text;
