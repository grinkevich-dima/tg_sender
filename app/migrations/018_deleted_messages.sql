-- Сообщение удалили в Telegram (мы или клиент): не стираем, а помечаем — в инбоксе видно, ИИ его не учитывает
ALTER TABLE messages ADD COLUMN deleted_at timestamptz;
