"""Isola a suíte do jobs.db real.

`log_event` grava em panel_log.DB_PATH (o banco do painel). Sem isto, qualquer teste que dispare um log escrevia
no event_logs do jobs.db de verdade. Aqui o caminho padrão aponta para um arquivo temporário sem tabelas: os eventos
são descartados em silêncio. Testes que conferem logs usam um banco próprio (AppDbCase aponta panel_log para ele).
"""
import atexit
import shutil
import tempfile
from pathlib import Path

from web import panel_log

_scratch = tempfile.mkdtemp(prefix="odradek-checks-")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
panel_log.DB_PATH = str(Path(_scratch) / "discarded-logs.db")
