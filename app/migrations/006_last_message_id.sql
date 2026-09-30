-- номер последнего сообщения: нужен для ссылки tg://privatepost, чтобы открыть группу без username в приложении
ALTER TABLE tg_dialogs ADD COLUMN last_message_id int;
ALTER TABLE found_chats ADD COLUMN last_message_id int;
