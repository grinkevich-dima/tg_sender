-- Поиск групп: темы поиска, найденные группы, какими запросами найдены
CREATE TABLE chat_searches (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    keywords    text[] NOT NULL,
    geo         text[] NOT NULL DEFAULT '{}',
    stop_words  text[] NOT NULL DEFAULT '{}',
    account_id  int REFERENCES tg_accounts,
    created_by  int REFERENCES users,
    status      text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'running', 'done', 'error')),
    step        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE found_chats (
    id              serial PRIMARY KEY,
    tg_id           bigint UNIQUE,               -- marked id (-100…); у приглашения без вступления неизвестен
    username        text,
    invite_link     text UNIQUE,                 -- для закрытых групп из ручного ввода
    title           text NOT NULL DEFAULT '',
    about           text NOT NULL DEFAULT '',
    members         int,
    last_message_at timestamptz,
    msgs_per_day    real,
    lang            text,
    match           int NOT NULL DEFAULT 0,      -- сколько ключевых слов нашлось
    stop_hit        text,                        -- какое стоп-слово нашлось
    score           int NOT NULL DEFAULT 0,      -- 0–100
    status          text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'interesting', 'joined', 'rejected')),
    via             text,                        -- search | discussion | link
    first_search_id int REFERENCES chat_searches ON DELETE SET NULL,
    checked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_found_chats_score ON found_chats(status, score DESC);

CREATE TABLE found_chat_hits (
    found_chat_id int NOT NULL REFERENCES found_chats ON DELETE CASCADE,
    search_id     int NOT NULL REFERENCES chat_searches ON DELETE CASCADE,
    query         text NOT NULL,
    PRIMARY KEY (found_chat_id, search_id, query)
);
