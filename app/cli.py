"""Обслуживание из командной строки.

  docker compose exec tg-sender python -m app.cli set-password ЛОГИН   — задать пароль (спросит ввод)
  docker compose exec tg-sender python -m app.cli users                — список пользователей
"""
import getpass
import sys

from . import auth, db


def main(argv: list[str]) -> int:
    db.init()
    if argv[:1] == ["users"]:
        for u in db.q("SELECT login, name, role, active FROM users ORDER BY id"):
            print(f"{u['login']:20} {u['name']:25} {auth.ROLES[u['role']]:10} {'активен' if u['active'] else 'отключён'}")
        return 0
    if len(argv) == 2 and argv[0] == "set-password":
        login = argv[1].strip().lower()
        if not db.one("SELECT 1 FROM users WHERE login=%s", (login,)):
            print(f"Нет пользователя {login}")
            return 1
        password = getpass.getpass("Новый пароль: ")
        if err := auth.validate_password(password):
            print(err)
            return 1
        db.ex("UPDATE users SET password_hash=%s, active=true WHERE login=%s", (auth.hash_password(password), login))
        print("Пароль изменён, пользователь включён")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
