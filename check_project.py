from __future__ import annotations

import compileall
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
EXPECTED_TABLES = {
    "users",
    "user_roles",
    "buyer_profiles",
    "daily_reports",
    "statistics_snapshots",
    "sync_runs",
    "sync_errors",
}
EXPECTED_ENUMS = {
    "role_code": ("BOSS", "MANAGER", "TEAMLEAD", "BUYER", "ADMIN"),
    "report_status": ("SUBMITTED", "MISSING", "LATE", "REPLACED"),
    "sync_status": ("RUNNING", "SUCCESS", "PARTIAL", "FAILED"),
    "sync_kind": ("DAILY_REPORTS", "STATISTICS", "BUYERS", "MANUAL"),
    "snapshot_kind": ("TUESDAY", "THURSDAY", "SATURDAY", "MONTH_END", "MANUAL"),
}


def fail(message: str) -> None:
    raise RuntimeError(message)


def check_python_files() -> None:
    ok = compileall.compile_dir(
        PROJECT_ROOT,
        quiet=1,
        rx=None,
        maxlevels=10,
    )
    if not ok:
        fail("Есть Python-файлы с синтаксическими ошибками")
    print("✅ Python-файлы компилируются")


def check_models() -> None:
    from db.base import Base
    import db.models  # noqa: F401

    tables = set(Base.metadata.tables)
    if tables != EXPECTED_TABLES:
        fail(
            "Неверный набор таблиц. "
            f"Ожидалось {sorted(EXPECTED_TABLES)}, получено {sorted(tables)}"
        )

    found_enums: dict[str, tuple[str, ...]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            enum_name = getattr(column.type, "name", None)
            enum_values = getattr(column.type, "enums", None)
            if enum_name and enum_values:
                found_enums[enum_name] = tuple(enum_values)

    if found_enums != EXPECTED_ENUMS:
        fail(f"ENUM в моделях не совпадают с ожидаемыми: {found_enums}")

    print("✅ Модели содержат 7 таблиц и согласованные ENUM")


def check_migration_sql() -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        "postgresql+asyncpg://user:password@localhost:5432/reports_bot"
    )

    with tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", suffix=".sql", delete=False
    ) as output:
        sql_path = Path(output.name)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            fail(
                "Alembic не смог сгенерировать SQL миграции:\n"
                + result.stdout
                + result.stderr
            )

        sql_path.write_text(result.stdout, encoding="utf-8")
        sql = result.stdout

        for enum_name in EXPECTED_ENUMS:
            count = sql.count(f"CREATE TYPE {enum_name} AS ENUM")
            if count != 1:
                fail(
                    f"ENUM {enum_name} создаётся {count} раз вместо одного раза"
                )

        forbidden = (
            "ck_buyer_profiles_ck_buyer_profiles_",
            "ck_statistics_snapshots_ck_statistics_snapshots_",
        )
        for fragment in forbidden:
            if fragment in sql:
                fail(f"Имя CHECK constraint задвоено: {fragment}")

        print("✅ Alembic создаёт каждый ENUM ровно один раз")
    finally:
        sql_path.unlink(missing_ok=True)


def main() -> int:
    try:
        check_python_files()
        check_models()
        check_migration_sql()
    except Exception as error:
        print(f"❌ Проверка проекта не пройдена: {error}")
        return 1

    print("\n✅ Локальная проверка проекта полностью пройдена")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
