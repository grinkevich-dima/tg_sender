"""Кампании из xlsx: разбор ссылок и файла, фильтры, очередь, поиск получателей, чаты."""
import io
import os
import types
import zipfile
from pathlib import Path

import pytest

from app import campaigns, db
from app.tg import tgm
from app.xlsx import read_xlsx
from tests.conftest import FakeClient, drain, make_account, make_lead, make_user, run

SAMPLE = os.environ.get("SAMPLE_XLSX", "")
needs_sample = pytest.mark.skipif(not SAMPLE or not Path(SAMPLE).exists(), reason="нет файла-образца (SAMPLE_XLSX)")


def test_parse_link():
    assert campaigns.parse_link("https://web.telegram.org/a/#-1001234567890_29438") == \
        {"peer_id": -1001234567890, "topic_id": 29438, "username": None}
    assert campaigns.parse_link("https://web.telegram.org/a/#152949444")["peer_id"] == 152949444
    assert campaigns.parse_link("https://t.me/Marconi_kasper")["username"] == "Marconi_kasper"
    assert campaigns.parse_link("https://t.me/c/1234567890/5")["peer_id"] == -1001234567890
    assert campaigns.status_group("Не отправлено. Прямая ссылка") == "Не отправлено"
    assert campaigns.status_group("Отправлено 28.09.2026 · партия") == "Отправлено 28.09.2026"


def _xlsx(rows: list[list[str]], with_refs=True) -> bytes:
    """Мини-xlsx с inline-строками; with_refs=False — без атрибутов r (так пишут некоторые программы)."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'

    def cell(ci, ri, v):
        ref = f' r="{chr(65 + ci)}{ri}"' if with_refs else ""
        return f'<c{ref} t="inlineStr"><is><t>{v}</t></is></c>'
    def row(ri, r):
        ref = f' r="{ri}"' if with_refs else ""
        return f"<row{ref}>" + "".join(cell(ci, ri, v) for ci, v in enumerate(r)) + "</row>"
    body = "".join(row(ri, r) for ri, r in enumerate(rows, 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="Единый список" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")
    return buf.getvalue()


HEADER = ["Исх. №", "Тип", "Имя", "Название", "Ссылка", "Ссылка на тему", "История личного общения", "На ты / вы",
          "Статус", "Обращение (черновик)"]


def test_xlsx_without_cell_refs():
    assert read_xlsx(_xlsx([["Тип", "Ссылка"], ["Чат", "5"]], with_refs=False)) == \
        {"Единый список": [["Тип", "Ссылка"], ["Чат", "5"]]}


def test_import_filter_enqueue_and_send():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    rows = [HEADER,
            ["1", "Чат", "", "Форум", "https://web.telegram.org/a/#-1001234567890", "https://web.telegram.org/a/#-1001234567890_55", "", "Чат", "Не отправлено", "Пост в тему"],
            ["2", "Человек", "Аня", "Аня", "https://web.telegram.org/a/#111", "", "Диалог есть", "ты", "Не отправлено", "Привет, Аня"],
            ["3", "Человек", "Боб", "Боб", "https://web.telegram.org/a/#222", "", "Диалога нет", "вы", "Не отправлено", "Здравствуйте, Боб"],
            ["4", "Человек", "Вера", "Вера", "https://web.telegram.org/a/#333", "", "Диалог есть", "Не писать", "Не отправлено", "Вера"],
            ["5", "Человек", "Гена", "Гена", "https://web.telegram.org/a/#444", "", "Диалог есть", "ты", "Отправлено 28.09.2026", "Гена"],
            ["6", "Человек", "Аня", "Аня (дубль)", "https://web.telegram.org/a/#111", "", "Диалог есть", "ты", "Не отправлено", "Ещё раз Аня"],
            ["7", "Человек", "Без ссылки", "", "", "", "", "ты", "Не отправлено", "текст"]]
    cid, n, warns = campaigns.import_xlsx(_xlsx(rows), "list.xlsx", u["id"], [a])
    assert n == 6
    assert any("повторяющихся" in w for w in warns) and any("без распознанной ссылки" in w for w in warns)
    f = campaigns.default_filter(cid)
    queued, skipped = campaigns.enqueue(cid, f)
    assert (queued, skipped) == (3, 0)          # чат, Аня, Боб; «Не писать», «Отправлено», без ссылки — нет
    order = [r["title"] for r in db.q("""SELECT l.title FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id
                                         WHERE cl.campaign_id=%s AND cl.state='queued' ORDER BY cl.order_idx""", (cid,))]
    assert order == ["Форум", "Аня", "Боб"]     # чат → с диалогом → без диалога
    db.ex("UPDATE campaigns SET status='running' WHERE id=%s", (cid,))
    drain(a, 4)
    assert [(e, r) for e, _, r in client.sent] == [(-1001234567890, 55), (111, None), (222, None)]


def test_prepare_checks_dialogs_and_marks_mismatch():
    u = make_user()
    a = make_account(u["id"])
    rows = [HEADER,
            ["1", "Человек", "Аня", "Аня", "https://web.telegram.org/a/#111", "", "Диалог есть", "ты", "", "a"],
            ["2", "Человек", "Боб", "Боб", "https://web.telegram.org/a/#222", "", "Диалога нет", "вы", "", "b"],
            ["3", "Человек", "Вера", "Вера", "https://web.telegram.org/a/#333", "", "Диалога нет", "вы", "", "c"]]
    cid, _, _ = campaigns.import_xlsx(_xlsx(rows), "l.xlsx", u["id"], [a])

    class D:
        def __init__(self, uid):
            self.is_user, self.is_group, self.is_channel = True, False, False
            self.id, self.name, self.message = uid, f"u{uid}", object()
            self.entity = types.SimpleNamespace(id=uid)

    class C(FakeClient):
        async def iter_dialogs(self, limit=None):
            for uid in (222, 333):        # с Аней переписки нет, с Бобом и Верой — есть
                yield D(uid)

        async def iter_participants(self, *a, **k):
            if False:
                yield None
    tgm.get(a).client = C()
    run(tgm.prepare_campaign(cid))
    st = tgm.prepare_state[cid]
    assert st["step"] == "готово" and st["total"] == 3
    w, p = campaigns.filter_sql(cid, {"mismatch": True})
    assert db.val(f"SELECT COUNT(*) FROM {campaigns.FROM_SQL} WHERE {w}", p) == 3
    campaigns.enqueue(cid, {"state": ["new"], "use_real": True})
    first = db.one("""SELECT l.tg_id FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id WHERE cl.campaign_id=%s
                      ORDER BY cl.order_idx LIMIT 1""", (cid,))
    assert first["tg_id"] in (222, 333)   # по данным Telegram «с диалогом» идут первыми


def test_resolve_chat_alt_id_and_title():
    u = make_user()
    a = make_account(u["id"])
    wrong_prefix = make_lead(kind="chat", title="Test1", tg_id=-1004000000001)
    by_title = make_lead(kind="chat", title="Мой чат", tg_id=-777)
    db.ex("INSERT INTO tg_dialogs(account_id, peer_id, title, kind) VALUES (%s, -1009999, 'мой чат', 'group')", (a,))

    class C(FakeClient):
        async def get_input_entity(self, pid):
            if pid in (-4000000001, -1009999):
                return pid
            raise ValueError("no")
    acc = tgm.get(a)
    acc.client = C()
    lead = db.one("SELECT * FROM leads WHERE id=%s", (wrong_prefix,))
    assert run(acc.resolve(lead)) == -4000000001          # «-100» лишний → обычная группа
    assert db.val("SELECT tg_id FROM leads WHERE id=%s", (wrong_prefix,)) == -4000000001
    assert run(acc.resolve(db.one("SELECT * FROM leads WHERE id=%s", (by_title,)))) == -1009999   # по названию
    db.ex("UPDATE leads SET tg_id=-1, title='нет такого' WHERE id=%s", (by_title,))
    with pytest.raises(ValueError, match="чат не найден"):
        run(acc.resolve(db.one("SELECT * FROM leads WHERE id=%s", (by_title,))))


@needs_sample
def test_real_file():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client, warmup_start=4, warmup_step=0)
    cid, n, warns = campaigns.import_xlsx(Path(SAMPLE).read_bytes(), "sample.xlsx", u["id"], [a])
    print(n, warns)
    assert n == 155
    queued, _ = campaigns.enqueue(cid, campaigns.default_filter(cid))
    assert queued == 105
    db.ex("UPDATE campaigns SET status='running' WHERE id=%s", (cid,))
    drain(a, 6)
    assert len(client.sent) == 4                      # лимит 4 в день
    assert all(r is None or r > 1 for _, _, r in client.sent)   # тема «General» (_1) → без reply_to
