"""Инбокс и воронка: переписка с лидами, этапы, автоматическая смена этапа."""
from . import db


def stages() -> list[dict]:
    return db.q("SELECT * FROM funnel_stages ORDER BY position, id")


def auto_stage(lead_id: int, event: str) -> None:
    """Сам двигает лида по воронке: отправка → «написали», ответ → «ответил».
    Только вперёд с начала воронки: этап, который менеджер поставил руками, не трогаем."""
    target = db.one("SELECT id, position FROM funnel_stages WHERE auto=%s", (event,))
    if not target:
        return
    db.ex("""UPDATE leads l SET stage_id=%s, stage_changed_at=now() WHERE l.id=%s AND (
                 l.stage_id IS NULL OR
                 EXISTS (SELECT 1 FROM funnel_stages s WHERE s.id=l.stage_id AND s.auto IS NOT NULL AND s.position < %s))""",
          (target["id"], lead_id, target["position"]))


def set_stage(lead_id: int, stage_id: int | None) -> None:
    db.ex("UPDATE leads SET stage_id=%s, stage_changed_at=now() WHERE id=%s", (stage_id, lead_id))


def record(account_id: int, lead_id: int, direction: str, text: str | None, tg_message_id: int | None,
           source: str, campaign_lead_id: int | None = None, sender_user_id: int | None = None,
           step: int | None = None) -> bool:
    """Сохраняет сообщение переписки; одно сообщение Telegram — одна строка. True — новая строка.

    Своё исходящее может прийти дважды: событием Telegram («вручную в Telegram») и из панели (кампания/инбокс).
    Запись из панели точнее, поэтому она уточняет уже сохранённую событием строку, а событие не перетирает панель."""
    if source == "telegram":
        conflict = "DO NOTHING"
    else:
        conflict = """DO UPDATE SET source=excluded.source, step=COALESCE(excluded.step, messages.step),
                      campaign_lead_id=COALESCE(excluded.campaign_lead_id, messages.campaign_lead_id),
                      sender_user_id=COALESCE(excluded.sender_user_id, messages.sender_user_id)"""
    return db.val(f"""INSERT INTO messages(account_id, lead_id, campaign_lead_id, direction, tg_message_id, text,
                                           source, sender_user_id, step)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (account_id, lead_id, direction, tg_message_id) WHERE tg_message_id IS NOT NULL {conflict}
                      RETURNING (xmax = 0)""",
                  (account_id, lead_id, campaign_lead_id, direction, tg_message_id, text, source, sender_user_id,
                   step)) is True


def unread_count(account_ids: list[int] | None) -> int:
    """Сколько диалогов с непрочитанными ответами (None — все аккаунты)."""
    acc = "" if account_ids is None else "AND l.owner_account_id = ANY(%s)"
    params = [] if account_ids is None else [account_ids]
    return db.val(f"""SELECT COUNT(*) FROM leads l WHERE EXISTS (SELECT 1 FROM messages m WHERE m.lead_id=l.id
                      AND m.direction='in' AND m.created_at > COALESCE(l.inbox_read_at, '-infinity')) {acc}""", params) or 0


def funnel(account_ids: list[int] | None = None, segment_id: int | None = None) -> list[dict]:
    """Сколько лидов на каждом этапе."""
    where, params = ["true"], []
    if account_ids is not None:
        where.append("l.owner_account_id = ANY(%s)")
        params.append(account_ids)
    if segment_id:
        where.append("l.id IN (SELECT lead_id FROM segment_leads WHERE segment_id=%s)")
        params.append(segment_id)
    return db.q(f"""SELECT s.*, COUNT(l.id) n FROM funnel_stages s LEFT JOIN leads l ON l.stage_id=s.id AND {' AND '.join(where)}
                    GROUP BY s.id ORDER BY s.position, s.id""", params)
