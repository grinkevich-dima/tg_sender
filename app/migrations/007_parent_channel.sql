-- группа обсуждения без username открывается только через свой канал — запоминаем его
ALTER TABLE found_chats ADD COLUMN parent_username text;
ALTER TABLE found_chats ADD COLUMN parent_title text;
