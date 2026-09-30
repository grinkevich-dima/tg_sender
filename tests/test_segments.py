"""Сегменты: наполнение из CSV, Telegram, по тегу; кампания по сегменту; права."""
import types

from app import campaigns, db, leads, segments
from app.tg import tgm
from tests.conftest import FakeClient, make_account, make_lead, make_user, run
from tests.test_web import browser

CSV = "username;first_name;company\n@anna_b;Анна;Салон А\n@boris;Борис;\n;;\n".encode()


def test_csv_fills_segment_and_merges_with_base():
    u = make_user()
    existing = make_lead(username="boris", first_name="Борис")
    sid = segments.create("Салоны", "", u["id"])
    assert leads.import_csv(CSV, "", segment_id=sid) == (1, 1, 1)
    members = {r["lead_id"] for r in db.q("SELECT lead_id FROM segment_leads WHERE segment_id=%s", (sid,))}
    assert existing in members and len(members) == 2
    assert leads.import_csv(CSV, "", segment_id=sid) == (0, 2, 1)     # повторная загрузка не дублирует
    assert segments.size(sid) == 2


def test_telegram_import_fills_segment_and_pins():
    u = make_user()
    a = make_account(u["id"])
    sid = segments.create("Знакомые", "", u["id"])

    class C(FakeClient):
        async def __call__(self, request):
            mk = lambda i, n: types.SimpleNamespace(id=i, username=None, phone=None, first_name=n, last_name=None,
                                                    bot=False, deleted=False)
            return types.SimpleNamespace(users=[mk(71, "Ира"), mk(72, "Олег")])
    tgm.get(a).client = C()
    assert run(tgm.get(a).import_contacts("", segment_id=sid)) == 2
    assert segments.size(sid) == 2
    assert db.val("SELECT COUNT(*) FROM leads WHERE owner_account_id=%s", (a,)) == 2


def test_add_by_tag_and_campaign_from_segment():
    u = make_user()
    a = make_account(u["id"])
    in_seg = [make_lead(tg_id=81, tags=["vip"]), make_lead(tg_id=82, tags=["vip"])]
    make_lead(tg_id=83, tags=["other"])
    make_lead(tg_id=84, tags=["vip"], opted_out_at=db.now_utc())
    sid = segments.create("VIP", "", u["id"])
    assert segments.add_by_tag(sid, "vip") == 3
    cid, queued, skipped = campaigns.create_from_template("к", "Привет", u["id"], [a], segment_id=sid)
    got = {r["lead_id"] for r in db.q("SELECT lead_id FROM campaign_leads WHERE campaign_id=%s", (cid,))}
    assert got == set(in_seg) and queued == 2            # отписавшийся в кампанию не попал
    stats = next(r for r in segments.listing() if r["id"] == sid)
    assert (stats["total"], stats["ready"], stats["opted_out"]) == (3, 2, 1)


def test_segment_pages_and_rights():
    admin = make_user("admin")
    make_user("anna", "manager")
    sid = segments.create("Админский", "", admin["id"])
    lid = make_lead(username="x1", first_name="Икс")
    db.ex("INSERT INTO segment_leads(segment_id, lead_id, source) VALUES (%s, %s, 'manual')", (sid, lid))
    anna = browser("anna")
    assert anna.get("/segments").status_code == 200 and "Админский" in anna.get("/segments").text
    assert "Икс" in anna.get(f"/segments/{sid}").text
    anna.post(f"/segments/{sid}/remove/{lid}")
    assert segments.size(sid) == 1                       # убрать лида может только автор или админ
    anna.post(f"/segments/{sid}/delete")
    assert segments.get(sid)
    anna.post("/segments/create", data={"name": "Админский"})
    assert db.val("SELECT COUNT(*) FROM segments") == 1  # название уникально
    admin_c = browser("admin")
    admin_c.post(f"/segments/{sid}/remove/{lid}")
    assert segments.size(sid) == 0
    admin_c.post(f"/segments/{sid}/delete")
    assert segments.get(sid) is None and db.one("SELECT 1 FROM leads WHERE id=%s", (lid,))


def test_segment_csv_upload_via_panel():
    u = make_user("admin")
    sid = segments.create("Из файла", "", u["id"])
    c = browser("admin")
    r = c.post(f"/segments/{sid}/import-csv", files={"file": ("l.csv", CSV)}, data={"tag": "файл"}, follow_redirects=False)
    assert r.status_code == 303 and segments.size(sid) == 2
    assert "Анна" in c.get(f"/segments/{sid}").text
    assert "Борис" in c.get(f"/leads?segment={sid}").text


# ---- свои группы ----
def _dialog(peer_id, title, *, creator=False, admin=False, group=True, members=None):
    ent = types.SimpleNamespace(id=abs(peer_id), creator=creator, admin_rights=object() if admin else None,
                                participants_count=members)
    return types.SimpleNamespace(id=peer_id, name=title, is_user=False, is_group=group, is_channel=not group,
                                 message=object(), entity=ent)


def _member(uid, first, joined=None, bot=False):
    from datetime import datetime, timezone
    return types.SimpleNamespace(id=uid, username=f"u{uid}", phone=None, first_name=first, last_name=None, bot=bot,
                                 deleted=False, is_self=False,
                                 participant=types.SimpleNamespace(date=joined or datetime(2026, 9, 20, tzinfo=timezone.utc)))


class GroupClient(FakeClient):
    members = {-1001: [_member(91, "Ира"), _member(92, "Олег"), _member(93, "Бот", bot=True)],
               -1002: [_member(94, "Чужой")]}

    async def iter_dialogs(self, limit=None):
        yield _dialog(-1001, "Вебинар: маркетинг", creator=True, members=3)
        yield _dialog(-1002, "Чужой чат")                        # просто участник
        yield _dialog(-1003, "Канал", admin=True, group=False)   # канал — не группа
        yield _dialog(-1004, "Семинар", admin=True)

    async def iter_participants(self, peer):
        for m in self.members.get(peer, []):
            yield m


def test_own_groups_found_and_members_imported():
    u = make_user()
    a = make_account(u["id"], client=GroupClient())
    run(tgm.refresh_groups(a))
    assert tgm.groups_state[a] == {"running": False, "step": "готово", "groups": 2}
    own = {r["title"] for r in db.q("SELECT title FROM tg_dialogs WHERE account_id=%s AND is_admin", (a,))}
    assert own == {"Вебинар: маркетинг", "Семинар"}

    sid = segments.create("Октябрьский вебинар", "", u["id"])
    assert run(tgm.get(a).import_group_members(-1001, sid, "вебинар")) == (2, 2, 2)   # бот не попал
    ira = db.one("SELECT * FROM leads WHERE tg_id=91")
    assert ira["extra"] == {"group": "Вебинар: маркетинг", "joined": "20.09.2026"}
    assert ira["owner_account_id"] == a and ira["tags"] == ["вебинар"]
    assert run(tgm.get(a).import_group_members(-1001, sid)) == (2, 0, 0)              # повтор — без дублей
    # переменные попадают в текст
    from app.templating import render
    assert render("{first_name}, спасибо, что пришли в «{group}»!", db.lead_vars(ira)) == \
        "Ира, спасибо, что пришли в «Вебинар: маркетинг»!"


def test_members_of_foreign_group_are_refused():
    u = make_user()
    a = make_account(u["id"], client=GroupClient())
    run(tgm.refresh_groups(a))
    sid = segments.create("x", "", u["id"])
    import pytest
    with pytest.raises(ValueError, match="создатель или админ"):
        run(tgm.get(a).import_group_members(-1002, sid))
    assert segments.size(sid) == 0


def test_import_group_via_panel_checks_account_owner():
    admin = make_user("admin")
    make_user("anna", "manager")
    a = make_account(admin["id"], client=GroupClient())
    run(tgm.refresh_groups(a))
    sid = segments.create("x", "", admin["id"])
    browser("anna").post(f"/segments/{sid}/import-group", data={"group": f"{a}:-1001"})
    assert segments.size(sid) == 0                     # чужой аккаунт менеджеру недоступен
    c = browser("admin")
    page_html = c.get(f"/segments/{sid}").text
    assert "Вебинар: маркетинг" in page_html and 'id="group-q"' in page_html
    c.post(f"/segments/{sid}/import-group", data={"group": f"{a}:-1001"})
    assert segments.size(sid) == 2
