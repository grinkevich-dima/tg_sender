"""Собирает образец списка для кампании из xlsx: app/static/xlsx-sample.xlsx.

Все люди, чаты, ID и username вымышлены. Файл — шаблон для пользователей (скачивается со страницы «Кампании»)
и данные для теста tests/test_xlsx.py::test_sample_file. Показывает все возможности формата (docs/xlsx-format.md):
строки над шапкой, форум и тема, тема General, тема из другого чата, обычная группа, ID / t.me / @username,
текст с листа «Тексты» с [Имя], статусы и «Не писать», повтор и строка без ссылки.

Запуск: python scripts/make_xlsx_sample.py
"""
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "xlsx-sample.xlsx"
W = "https://web.telegram.org/a/#"

HEADER = ["Исх. №", "Тип", "Имя", "Фамилия", "Название / имя в Telegram", "Папка / источник", "Ссылка",
          "Ссылка на тему", "Куда", "План действия", "Примечание", "Текст №", "Обращение (черновик)", "Статус",
          "На ты / вы", "История личного общения"]
ROWS = [
    ["1", "Чат", "", "", "Клуб предпринимателей", "Вебинар", f"{W}-1001111111111", f"{W}-1001111111111_55", "Знакомства",
     "пост в тему", "форум", "", "Коллеги, всем привет! Я Дмитрий, на прошлой неделе мы проводили вебинар о сайтах для "
     "бизнеса. Если кому-то нужна запись — напишите, пришлю.", "Не отправлено", "Чат", ""],
    ["2", "Чат", "", "", "Маркетинг без воды", "Семинар", f"{W}-1002222222222", f"{W}-1002222222222_1", "General",
     "пост в общий поток", "тема _1 — General", "", "Добрый день! Делимся конспектом семинара по продвижению в Telegram.",
     "Не отправлено", "Чат", ""],
    ["3", "Группа", "", "", "Выпускники курса", "Курс", f"{W}-4012345678", "", "", "", "обычная группа, без -100", "",
     "Друзья, напоминаю: в четверг встреча выпускников.", "Не отправлено", "Чат", ""],
    ["4", "Человек", "Анна", "Иванова", "Анна Иванова", "Вебинар", f"{W}100000001", "", "", "написать лично", "", "",
     "Анна, привет! Спасибо, что была на вебинаре. Как тебе тема с лендингами — актуально сейчас?", "Не отправлено",
     "ты", "Диалог есть"],
    ["5", "Человек", "Борис", "Петров", "Борис", "Семинар", "https://t.me/boris_example", "", "", "", "ссылка t.me", "",
     "Борис, здравствуйте! Вы были на нашем семинаре — подскажите, удалось применить что-то из разобранного?",
     "Не отправлено", "вы", "Диалога нет"],
    ["6", "Человек", "Вера", "", "Вера", "Вебинар", "@vera_example", "", "", "", "текст — с листа «Тексты», №2", "2", "",
     "Не отправлено", "вы", "Диалога нет"],
    ["7", "Человек", "Глеб", "", "Глеб", "Вебинар", f"{W}100000002", "", "", "", "уже писали", "",
     "Глеб, привет!", "Отправлено 28.09.2026", "ты", "Диалог есть"],
    ["8", "Человек", "Дарья", "", "Дарья", "Семинар", f"{W}100000003", "", "", "", "просила не писать", "",
     "Дарья, добрый день!", "Не отправлено", "Не писать", "Диалог есть"],
    ["9", "Человек", "Егор", "", "Егор", "Курс", f"{W}100000004", "", "", "", "", "",
     "Егор, привет!", "Только личные обращения", "ты", "Диалог есть"],
    ["10", "Чат", "", "", "Чужой форум", "Вебинар", f"{W}-1003333333333", f"{W}-1004444444444_7", "Новости",
     "", "ссылка на тему из другого чата — тема не применится", "", "Всем привет! Анонс нового вебинара.",
     "Не отправлено", "Чат", ""],
    ["11", "Человек", "Анна", "Иванова", "Анна Иванова (повтор)", "Вебинар", f"{W}100000001", "", "", "", "повтор строки 4", "",
     "Анна, ещё раз привет!", "Не отправлено", "ты", "Диалог есть"],
    ["12", "Человек", "Жанна", "", "Жанна", "Вебинар", "", "", "", "", "нет ссылки — отправить не получится", "",
     "Жанна, добрый день!", "Не отправлено", "вы", ""],
]
TEXTS = [["№", "Тема", "Текст"],
         ["1", "Знакомство", "[Имя], привет! Спасибо, что пришли на вебинар."],
         ["2", "После вебинара", "[Имя], добрый день! Вы были на нашем вебинаре о сайтах — остались ли вопросы? "
                                 "Могу прислать запись и презентацию."]]


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet(rows: list[list[str]], bold_row: int | None, widths: list[int]) -> str:
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    out = []
    for ri, row in enumerate(rows, 1):
        style = ' s="1"' if ri == bold_row else ""
        cells = "".join(f'<c r="{_col(ci)}{ri}" t="inlineStr"{style}><is><t xml:space="preserve">{escape(v)}</t></is></c>'
                        for ci, v in enumerate(row) if v != "")
        out.append(f'<row r="{ri}">{cells}</row>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<cols>{cols}</cols><sheetData>{"".join(out)}</sheetData></worksheet>')


def build() -> bytes:
    import io
    main = [["Список обращений — образец (все данные вымышлены)"],
            ["Строки над шапкой панель пропускает. Описание колонок: docs/xlsx-format.md"], HEADER] + ROWS
    widths = [7, 9, 10, 11, 26, 12, 40, 40, 12, 18, 30, 8, 60, 22, 11, 16]
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          '<sheet name="Единый список" sheetId="1" r:id="rId1"/><sheet name="Тексты 1–10" sheetId="2" r:id="rId2"/>'
          '</sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
               '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
              '</styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in [("[Content_Types].xml", ct), ("_rels/.rels", rels), ("xl/workbook.xml", wb),
                           ("xl/_rels/workbook.xml.rels", wb_rels), ("xl/styles.xml", styles),
                           ("xl/worksheets/sheet1.xml", _sheet(main, 3, widths)),
                           ("xl/worksheets/sheet2.xml", _sheet(TEXTS, 1, [5, 18, 90]))]:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))     # одинаковый файл при каждой сборке
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


if __name__ == "__main__":
    OUT.write_bytes(build())
    print(f"Записано: {OUT}")
