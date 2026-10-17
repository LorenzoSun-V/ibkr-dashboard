"""管理给记账软件用的只读密钥。每把密钥只能读指定的 IBKR 账户。

    python -m app.keys add NAME ACCOUNT [ACCOUNT ...]   # 签发，密钥只显示这一次
    python -m app.keys list
    python -m app.keys revoke NAME

数据库里只存密钥的 SHA-256。密钥丢了就 revoke 后重新 add。
"""
import argparse
import hashlib
import json
import secrets
import sys
from datetime import datetime

from . import db


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def add(conn, name: str, accounts: list[str]) -> str:
    if conn.execute("SELECT 1 FROM api_keys WHERE name = ? AND revoked_at IS NULL", (name,)).fetchone():
        raise ValueError(f"{name} 已有有效的密钥，先 revoke 再重新签发")
    key = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_keys (name, key_hash, accounts, created_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET key_hash = excluded.key_hash, accounts = excluded.accounts,"
        " created_at = excluded.created_at, revoked_at = NULL",
        (name, _hash(key), json.dumps(sorted(set(accounts))), datetime.now().isoformat(timespec="seconds")))
    return key


def revoke(conn, name: str) -> bool:
    cur = conn.execute("UPDATE api_keys SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                       (datetime.now().isoformat(timespec="seconds"), name))
    return cur.rowcount > 0


def accounts_for(conn, key: str | None) -> list[str] | None:
    """密钥能读的账户；密钥不存在或已撤销时返回 None。"""
    if not key:
        return None
    row = conn.execute("SELECT accounts FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (_hash(key),)).fetchone()
    return json.loads(row["accounts"]) if row else None


def main():
    ap = argparse.ArgumentParser(description="管理记账软件用的只读密钥")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="签发密钥")
    a.add_argument("name")
    a.add_argument("accounts", nargs="+", help="可读的 IBKR 完整账号，如 U1234567")
    sub.add_parser("list", help="列出密钥")
    r = sub.add_parser("revoke", help="撤销密钥")
    r.add_argument("name")
    args = ap.parse_args()
    with db.connect() as conn:
        if args.cmd == "add":
            try:
                key = add(conn, args.name, args.accounts)
            except ValueError as e:
                sys.exit(str(e))
            print(f"已签发 {args.name}，可读 {', '.join(sorted(set(args.accounts)))}。密钥只显示这一次：\n{key}")
        elif args.cmd == "list":
            for row in conn.execute("SELECT * FROM api_keys ORDER BY name"):
                state = f"已撤销 {row['revoked_at']}" if row["revoked_at"] else "有效"
                print(f"{row['name']}\t{', '.join(json.loads(row['accounts']))}\t{row['created_at']}\t{state}")
        elif not revoke(conn, args.name):
            sys.exit(f"{args.name} 没有有效的密钥")
        else:
            print(f"已撤销 {args.name}")


if __name__ == "__main__":
    main()
