"""修改管理员口令：直接改写 state.db 的 salt + password_hash，并注销所有旧会话。

背景：`ADMIN_USERNAME` / `ADMIN_PASSWORD` 只在**首次创建数据库**时生效，之后在
Compose 或 .env 里改密码不会改变库里的口令（服务启动时只会记一条提示）。本脚本
补上这件事，改完立即生效，旧会话全部失效。

用法（仓库根目录或容器内均可）：

    python scripts/set_password.py                          # 交互输入两次
    python scripts/set_password.py --data /vol1/.../data     # 指定数据目录
    python scripts/set_password.py '新的至少12位密码'         # 命令行传入（会进 shell 历史）

数据目录默认取 `STRIMKEEP_DATA_DIR`，其次 `/data`（容器内默认值）。
"""
import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from strimkeep.store import Store, password_hash  # noqa: E402

MIN_LENGTH = 12


def ask_password():
    first = getpass.getpass('新密码（至少 %d 位）：' % MIN_LENGTH)
    if len(first) < MIN_LENGTH:
        raise SystemExit('密码至少 %d 位。' % MIN_LENGTH)
    if first != getpass.getpass('再输入一次：'):
        raise SystemExit('两次输入不一致。')
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description='修改 StrimKeep 管理员口令')
    parser.add_argument('password', nargs='?', help='新密码；省略则交互输入')
    parser.add_argument('--data', default=os.environ.get('STRIMKEEP_DATA_DIR') or '/data',
                        help='包含 data/state.db 的数据目录（默认 STRIMKEEP_DATA_DIR 或 /data）')
    args = parser.parse_args(argv)

    root = Path(args.data)
    database = root / 'data/state.db'
    if not database.is_file():
        raise SystemExit('找不到 %s；请用 --data 指定包含 data/state.db 的目录。' % database)

    password = args.password or ask_password()
    if len(password) < MIN_LENGTH:
        raise SystemExit('密码至少 %d 位。' % MIN_LENGTH)

    store = Store(root, '', '')
    try:
        if not store.meta('username'):
            raise SystemExit('数据库里还没有管理员账号，请先启动一次服务完成初始化。')
        salt = secrets.token_bytes(16)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO meta VALUES('salt',?)", (salt.hex(),))
            store.db.execute("INSERT OR REPLACE INTO meta VALUES('password_hash',?)",
                             (password_hash(password, salt),))
            removed = store.db.execute('DELETE FROM sessions').rowcount
        store.event('system', '管理员口令已通过 scripts/set_password.py 修改；旧会话已注销。')
        print('口令已更新，已注销 %d 个旧会话。下次登录请使用新密码。' % max(0, removed))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
