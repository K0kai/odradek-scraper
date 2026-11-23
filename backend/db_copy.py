"""Copia o banco inteiro entre SQLite e PostgreSQL (Neon, Supabase...). O destino é substituído.

Exemplos (rode de dentro de backend/):
  python db_copy.py --from "$NEON_URL" --to "$SUPABASE_URL"        # migrar para a Supabase
  python db_copy.py --from "$STANDBY_URL" --to "$SUPABASE_URL"     # restaurar a partir do standby
  python db_copy.py --from jobs.db --to "$SUPABASE_URL"            # subir o banco local
  python db_copy.py --from "$SUPABASE_URL" --to backup.db          # backup em SQLite
  python db_copy.py --from "$NEON_URL" --to "$SUPABASE_URL" --dry-run   # só conta as linhas

As URLs vêm do argumento; passe-as por variável de ambiente (como acima) para não ficarem no histórico do shell.
Use a URL do pooler (porta 6543 na Supabase) ou a direta; o script abre uma conexão só.
"""
from __future__ import annotations

import argparse
import sys

from data.dbcopy import copy_database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", required=True, help="URL postgres:// ou arquivo SQLite de origem")
    parser.add_argument("--to", dest="target", required=True, help="URL postgres:// ou arquivo SQLite de destino")
    parser.add_argument("--dry-run", action="store_true", help="só conta as linhas da origem, sem gravar")
    parser.add_argument("--force", action="store_true", help="permite substituir o banco ativo (DATABASE_URL)")
    args = parser.parse_args()
    counts = copy_database(args.source, args.target, dry_run=args.dry_run, force=args.force, log=print)
    print(("Contagem (nada foi gravado): " if args.dry_run else "Cópia concluída: ") + f"{sum(counts.values())} linha(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Falha: {exc.__class__.__name__}: {str(exc)[:300]}", file=sys.stderr)  # sem imprimir a URL
        raise SystemExit(1)
