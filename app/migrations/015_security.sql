-- Неудачные попытки входа: защита от перебора паролей
CREATE TABLE login_failures (
    id    bigserial PRIMARY KEY,
    login text NOT NULL,
    ip    text NOT NULL,
    at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_login_failures ON login_failures(at);
