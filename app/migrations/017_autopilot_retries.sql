-- Автоответ: перезапуски, когда клиент дописал (E1), повторы при сбоях ИИ и Telegram (E2)
ALTER TABLE ai_reply_jobs ADD COLUMN restarts int NOT NULL DEFAULT 0;      -- сколько раз выбросили черновик: клиент дописал
ALTER TABLE ai_reply_jobs ADD COLUMN ai_attempts int NOT NULL DEFAULT 0;   -- неудачных обращений к ИИ подряд
ALTER TABLE ai_reply_jobs ADD COLUMN fail_since timestamptz;               -- с какого момента не удаётся отправить
