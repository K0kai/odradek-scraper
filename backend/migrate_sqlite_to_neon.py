"""Copia o jobs.db local para o Postgres apontado por DATABASE_URL (Neon, Supabase...).

Atalho para `python db_copy.py --from jobs.db --to $DATABASE_URL`. O destino é substituído.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from data.dbcopy import copy_database
from paths import DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=DB_PATH, help="SQLite a copiar (padrão: jobs.db do projeto)")
    args = parser.parse_args()
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        parser.error("Defina DATABASE_URL com a conexão do Postgres antes de rodar este script.")
    counts = copy_database(str(args.sqlite.expanduser()), database_url, force=True, log=print)
    print(f"Migração concluída: {sum(counts.values())} linha(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Falha na migração: {exc.__class__.__name__}: {str(exc)[:300]}", file=sys.stderr)
        raise SystemExit(1)
