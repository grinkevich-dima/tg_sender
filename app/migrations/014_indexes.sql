-- Быстрый подсчёт отправленного аккаунтом за день (дневной лимит считается при каждой попытке отправки)
CREATE INDEX ix_messages_account_out ON messages(account_id, created_at) WHERE direction = 'out';
