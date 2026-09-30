"""Сегменты: именованные списки лидов, из которых собираются кампании."""
from . import db


def create(name: str, description: str, user_id: int) -> int:
    return db.ex("INSERT INTO segments(name, description, created_by) VALUES (%s, %s, %s) RETURNING id",
                 (name.strip(), description.strip(), user_id))


def get(segment_id: int) -> dict | None:
    return db.one("SELECT s.*, u.name AS author FROM segments s LEFT JOIN users u ON u.id=s.created_by WHERE s.id=%s",
                  (segment_id,))


def listing() -> list[dict]:
    """Сегменты со счётчиками: всего, можно писать (не отписаны), отписаны, уже писали."""
    return db.q("""SELECT s.*, u.name AS author,
                   COUNT(sl.lead_id) total,
                   COUNT(*) FILTER (WHERE l.opted_out_at IS NULL AND l.kind='person') ready,
                   COUNT(*) FILTER (WHERE l.opted_out_at IS NOT NULL) opted_out,
                   COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM messages m WHERE m.lead_id=l.id AND m.direction='out')) contacted
                   FROM segments s LEFT JOIN users u ON u.id=s.created_by
                   LEFT JOIN segment_leads sl ON sl.segment_id=s.id LEFT JOIN leads l ON l.id=sl.lead_id
                   GROUP BY s.id, u.name ORDER BY s.id DESC""")


def add_by_tag(segment_id: int, tag: str) -> int:
    return db.changed("""INSERT INTO segment_leads(segment_id, lead_id, source)
                         SELECT %s, id, 'tag' FROM leads WHERE %s = ANY(tags) ON CONFLICT DO NOTHING""",
                      (segment_id, tag))


def remove(segment_id: int, lead_id: int) -> None:
    db.ex("DELETE FROM segment_leads WHERE segment_id=%s AND lead_id=%s", (segment_id, lead_id))


def size(segment_id: int) -> int:
    return db.val("SELECT COUNT(*) FROM segment_leads WHERE segment_id=%s", (segment_id,)) or 0
