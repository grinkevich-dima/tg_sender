"""Подстановка переменных и спинтакс.

  {first_name}, {name}, {username}, {phone}, {last_name} + любые колонки из CSV
  {Привет|Добрый день|Здравствуйте} — случайный вариант (спинтакс)
"""
import random
import re

SPIN_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")
VAR_RE = re.compile(r"\{(\w+)\}")


def render(body: str, variables: dict, rng: random.Random | None = None) -> str:
    rng = rng or random
    # 1) переменные → временные метки (чтобы {Привет, {first_name}|Hi} работал)
    slots: list[str] = []

    def _slot(m):
        slots.append(str(variables.get(m.group(1), "")))
        return f"\x00{len(slots) - 1}\x00"

    text = VAR_RE.sub(_slot, body)
    # 2) спинтакс изнутри наружу (поддерживает вложенность)
    for _ in range(20):
        new = SPIN_RE.sub(lambda m: rng.choice(m.group(1).split("|")), text)
        if new == text:
            break
        text = new
    # 3) метки → значения
    text = re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], text)
    # убираем двойные пробелы, если переменная оказалась пустой
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s*,\s*([!?.])", r"\1", text)   # «Привет, !» → «Привет!»
    text = re.sub(r" +([,.!?])", r"\1", text)
    return text.strip()


def variables_in(body: str) -> list[str]:
    no_spin = SPIN_RE.sub("", body)
    return sorted(set(VAR_RE.findall(no_spin)))
