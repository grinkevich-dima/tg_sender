-- Свои группы (из них можно добавлять участников в сегменты)
ALTER TABLE tg_dialogs ADD COLUMN is_admin boolean NOT NULL DEFAULT false;
ALTER TABLE tg_dialogs ADD COLUMN members int;
