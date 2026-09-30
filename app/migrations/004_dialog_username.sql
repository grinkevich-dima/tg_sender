-- username группы/канала: ссылка t.me/<username> для публичных чатов
ALTER TABLE tg_dialogs ADD COLUMN username text;
