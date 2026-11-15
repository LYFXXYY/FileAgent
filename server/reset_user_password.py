"""开发期账号密码重置 —— 忘了密码时用，不动任何业务数据。

只重写 users.password_hash，该用户名下的任务、日志、提供商、计量全部保留。
密码校验直接复用 schemas.auth.RegisterRequest，规则和注册接口完全一致
（6~72 字符），不会出现「脚本能设但注册接口拒绝」的漂移。

用法（cwd 为 server）：
    uv run python reset_user_password.py                          # 列出所有账号
    uv run python reset_user_password.py <用户名> <新密码>          # 重置已有账号
    uv run python reset_user_password.py --create <用户名> <新密码> # 账号不存在时新建
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

if hasattr(sys.stdout, "reconfigure"):
    # Windows 控制台默认 cp936，中文用户名和提示会炸
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select

import database
from models.user import User
from schemas.auth import RegisterRequest
from services.auth import hash_password

USAGE = (
    "用法：\n"
    "  uv run python reset_user_password.py                        列出所有账号\n"
    "  uv run python reset_user_password.py <用户名> <新密码>        重置密码\n"
    "  uv run python reset_user_password.py --create <用户名> <新密码> 新建账号"
)


def list_users(db) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id)))


def validate(username: str, password: str) -> None:
    """借注册接口的 Field 规则把关，避免设出一个前端登不进来的密码"""
    RegisterRequest(username=username, password=password)


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    create = bool(argv) and argv[0] == "--create"
    args = argv[1:] if create else argv

    with database.SessionLocal() as db:
        if not args:
            users = list_users(db)
            if not users:
                print("users 表为空，先用 --create 建一个账号")
                return 1
            print(f"共 {len(users)} 个账号：")
            for u in users:
                print(f"  id={u.id:<4} username={u.username}")
            print("\n重置：uv run python reset_user_password.py <用户名> <新密码>")
            return 0

        if len(args) != 2:
            print(USAGE)
            return 1

        username, password = args
        try:
            validate(username, password)
        except ValidationError as e:
            print(f"密码或用户名不合规则：{e.errors()[0]['msg']}")
            return 1

        user = db.scalars(select(User).where(User.username == username)).first()

        if user is None:
            if not create:
                print(f"账号 {username!r} 不存在。要新建就加 --create，或用下面的列表选一个：")
                for u in list_users(db):
                    print(f"  id={u.id:<4} username={u.username}")
                return 1
            user = User(username=username, password_hash=hash_password(password))
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"[created] 新账号 id={user.id} username={user.username}")
            return 0

        # 只动 password_hash，其余字段一概不碰
        user.password_hash = hash_password(password)
        db.commit()
        print(f"[done] 已重置 id={user.id} username={user.username} 的密码")
        print("      该账号下的任务、日志、提供商、计量行全部原样保留")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
