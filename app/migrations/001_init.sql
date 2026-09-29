-- Команда, аккаунты Telegram, лиды, кампании, переписка.

CREATE TABLE settings (key text PRIMARY KEY, value text NOT NULL);

CREATE TABLE users (
    id            serial PRIMARY KEY,
    login         text NOT NULL UNIQUE,
    name          text NOT NULL,
    password_hash text NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'manager')),
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Личный аккаунт Telegram менеджера. Лимиты и прогрев — у каждого аккаунта свои.
CREATE TABLE tg_accounts (
    id                serial PRIMARY KEY,
    user_id           int NOT NULL REFERENCES users,
    label             text NOT NULL DEFAULT '',
    phone             text,
    tg_user_id        bigint UNIQUE,
    username          text,
    first_name        text,
    last_name         text,
    status            text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'active', 'paused', 'logged_out')),
    warmup_enabled    boolean NOT NULL DEFAULT true,
    warmup_start      int NOT NULL DEFAULT 10 CHECK (warmup_start >= 0),
    warmup_step       int NOT NULL DEFAULT 5 CHECK (warmup_step >= 0),
    daily_max         int NOT NULL DEFAULT 50 CHECK (daily_max >= 0),
    delay_min         int NOT NULL DEFAULT 45 CHECK (delay_min >= 0),
    delay_max         int NOT NULL DEFAULT 150 CHECK (delay_max >= 0),
    work_start        time NOT NULL DEFAULT '10:00',
    work_end          time NOT NULL DEFAULT '20:00',
    warmup_start_date date,                 -- ставится при первой отправке
    paused_until      timestamptz,          -- FloodWait / PEER_FLOOD
    pause_reason      text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- Лид: человек или чат. Закреплён за одним аккаунтом — вся переписка с ним идёт от этого аккаунта.
CREATE TABLE leads (
    id               bigserial PRIMARY KEY,
    kind             text NOT NULL DEFAULT 'person' CHECK (kind IN ('person', 'chat', 'bot', 'other')),
    tg_id            bigint,                -- marked id: человек > 0, группа/канал < 0
    username         text,
    phone            text,
    first_name       text NOT NULL DEFAULT '',
    last_name        text NOT NULL DEFAULT '',
    title            text,                  -- название чата / имя в Telegram из файла
    extra            jsonb NOT NULL DEFAULT '{}',   -- доп. колонки импорта → переменные шаблона
    tags             text[] NOT NULL DEFAULT '{}',
    owner_account_id int REFERENCES tg_accounts,
    opted_out_at     timestamptz,           -- отписка действует на всю команду
    opt_out_reason   text,
    source           text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_leads_tg_id ON leads(tg_id) WHERE tg_id IS NOT NULL;
CREATE UNIQUE INDEX ux_leads_username ON leads(lower(username)) WHERE username IS NOT NULL;
CREATE UNIQUE INDEX ux_leads_phone ON leads(phone) WHERE phone IS NOT NULL;
CREATE INDEX ix_leads_tags ON leads USING gin(tags);
CREATE INDEX ix_leads_owner ON leads(owner_account_id);

CREATE TABLE templates (
    id         serial PRIMARY KEY,
    name       text NOT NULL,
    body       text NOT NULL,
    created_by int REFERENCES users,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE campaigns (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    source      text NOT NULL DEFAULT 'template' CHECK (source IN ('template', 'xlsx')),
    filename    text,
    status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'running', 'paused', 'done')),
    goal        text,
    created_by  int REFERENCES users,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz
);

-- С каких аккаунтов идёт кампания (новые лиды распределяются между ними)
CREATE TABLE campaign_accounts (
    campaign_id int NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    account_id  int NOT NULL REFERENCES tg_accounts,
    PRIMARY KEY (campaign_id, account_id)
);

-- Шаги цепочки. Сейчас используется шаг 1; дожимы — следующий этап.
CREATE TABLE campaign_steps (
    id          serial PRIMARY KEY,
    campaign_id int NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    position    int NOT NULL,
    body        text NOT NULL,
    delay_days  int NOT NULL DEFAULT 0,
    UNIQUE (campaign_id, position)
);

-- Лид в кампании: состояние отправки, свой текст из файла, тема форума, поля из xlsx для фильтров
CREATE TABLE campaign_leads (
    id            bigserial PRIMARY KEY,
    campaign_id   int NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    lead_id       bigint NOT NULL REFERENCES leads ON DELETE CASCADE,
    account_id    int REFERENCES tg_accounts,
    state         text NOT NULL DEFAULT 'new'
                  CHECK (state IN ('new', 'queued', 'sending', 'sent', 'read', 'replied', 'failed', 'skipped')),
    order_idx     int,
    custom_text   text,                     -- готовый текст строки xlsx (иначе — шаблон шага)
    topic_id      int,
    topic_title   text,
    row_no        text,
    dialog        text,                     -- «История общения» из файла
    real_dialog   text,                     -- то же по данным Telegram
    address       text,                     -- ты / вы / Не писать
    src_status    text,
    src_group     text,
    meta          jsonb NOT NULL DEFAULT '{}',   -- папка, план, примечание, № текста, ссылка
    error         text,
    tg_message_id bigint,
    sent_at       timestamptz,
    sent_day      date,
    read_at       timestamptz,
    replied_at    timestamptz
);
CREATE UNIQUE INDEX ux_cl_lead_topic ON campaign_leads(campaign_id, lead_id, COALESCE(topic_id, 0));
CREATE INDEX ix_cl_queue ON campaign_leads(account_id, state);
CREATE INDEX ix_cl_campaign ON campaign_leads(campaign_id, state);
CREATE INDEX ix_cl_lead ON campaign_leads(lead_id);

-- Вся переписка с лидами: исходящие из кампаний и входящие ответы (для инбокса)
CREATE TABLE messages (
    id               bigserial PRIMARY KEY,
    account_id       int NOT NULL REFERENCES tg_accounts,
    lead_id          bigint NOT NULL REFERENCES leads ON DELETE CASCADE,
    campaign_lead_id bigint REFERENCES campaign_leads ON DELETE SET NULL,
    direction        text NOT NULL CHECK (direction IN ('in', 'out')),
    tg_message_id    bigint,
    text             text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_messages_lead ON messages(lead_id, created_at);

-- Диалоги аккаунта (снимок при «Найти получателей»): поиск чатов по названию, сверка «есть ли переписка»
CREATE TABLE tg_dialogs (
    account_id  int NOT NULL REFERENCES tg_accounts ON DELETE CASCADE,
    peer_id     bigint NOT NULL,
    title       text,
    kind        text,                       -- user | group | channel
    has_private boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, peer_id)
);

CREATE TABLE event_log (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    level      text NOT NULL,
    text       text NOT NULL,
    account_id int
);
