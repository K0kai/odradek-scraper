"""Avaliação manual da triagem (chama a IA de verdade; NÃO roda no `unittest discover`).

Uso (a partir de backend/):
    ../.venv/Scripts/python.exe checks/eval_triage.py [--limit 40] [--thresholds 55,60,65,70,75,80] [--dry-run]

Conjunto rotulado, montado do banco local:
  * positivos: vagas que você avançou (status applied/prepared);
  * negativos: vagas que a triagem aprovou (should_apply=1) e que você depois ignorou.
Os rótulos são enviesados: só existem para vagas que a triagem já deixou passar. Logo o
*recall* aqui mede quantas vagas que você avançou a v2 manteria, e a *precisão* mede
quantas aprovações a v2 manteria entre as que você aceitou ou rejeitou. Vagas que a IA
rejeitou e que você nunca viu não têm rótulo. Use os números para calibrar SCORING_V2 e
`minimum_match_score`, não como verdade absoluta.

A coluna v1 vem das decisões já gravadas (scoring_version vazio); a v2 é reexecutada agora.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from data.repository import connect, settings  # noqa: E402

POSITIVE = ("applied", "prepared")


def labelled_jobs(limit: int) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT j.*, d.match_score AS v1_score, d.should_apply AS v1_apply, d.scoring_version AS v1_version
               FROM jobs j
               JOIN ai_decisions d ON d.id = (SELECT MAX(x.id) FROM ai_decisions x WHERE x.job_id = j.id)
               WHERE j.status IN ('applied','prepared') OR (j.status = 'ignored' AND d.should_apply = 1)
               ORDER BY j.id DESC"""
        ).fetchall()
    jobs = [dict(row) for row in rows]
    positives = [j for j in jobs if j["status"] in POSITIVE][: limit // 2 or 1]
    negatives = [j for j in jobs if j["status"] not in POSITIVE][: limit - len(positives)]
    return positives + negatives


def confusion(rows: list[dict], key) -> dict[str, int]:
    tp = sum(1 for r in rows if r["label"] and key(r))
    fp = sum(1 for r in rows if not r["label"] and key(r))
    fn = sum(1 for r in rows if r["label"] and not key(r))
    tn = sum(1 for r in rows if not r["label"] and not key(r))
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def ratio(num: int, den: int) -> str:
    return f"{num / den:.0%}" if den else "n/a"


def report(name: str, rows: list[dict], key) -> None:
    c = confusion(rows, key)
    print(f"{name:<28} TP={c['tp']:<3} FP={c['fp']:<3} FN={c['fn']:<3} TN={c['tn']:<3} "
          f"precisão={ratio(c['tp'], c['tp'] + c['fp'])} recall={ratio(c['tp'], c['tp'] + c['fn'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=40, help="máximo de vagas avaliadas (metade positivas, se houver)")
    parser.add_argument("--thresholds", default="55,60,65,70,75,80", help="notas mínimas para a curva da v2")
    parser.add_argument("--dry-run", action="store_true", help="só lista as vagas rotuladas, sem chamar a IA")
    args = parser.parse_args()

    try:
        jobs = labelled_jobs(max(2, args.limit))
    except Exception as exc:  # banco ainda sem as colunas da v2: o app as cria no boot
        print(f"Não foi possível ler as decisões ({exc}). Abra o app uma vez para migrar o banco e tente de novo.")
        return 1
    pos = sum(1 for j in jobs if j["status"] in POSITIVE)
    print(f"{len(jobs)} vagas rotuladas: {pos} positivas, {len(jobs) - pos} negativas.")
    if args.dry_run or not jobs:
        for j in jobs:
            print(f"  #{j['id']} [{j['status']}] {j['title'][:60]} (v1={j['v1_score']})")
        return 0

    cfg = settings()
    rows: list[dict] = []
    tracks: Counter[str] = Counter()
    for job in jobs:
        track = app.job_track(job, cfg)
        try:
            result = app.ai_assess_job(job, cfg, track=track)
        except Exception as exc:  # uma vaga com erro não derruba a avaliação inteira
            print(f"  #{job['id']} ignorada na avaliação: {exc}")
            continue
        assessment = result["assessment"]
        reqs = assessment["requirements"] + assessment["responsibilities"]
        inflated = sum(1 for r in reqs if r.get("realism") == "inflated")
        rows.append({
            "id": job["id"], "label": job["status"] in POSITIVE, "track": track,
            "v1_apply": bool(job["v1_apply"]), "v1_score": job["v1_score"] or 0,
            "v2_apply": bool(result["should_apply"]), "v2_score": result["match_score"],
            "inflated": inflated, "bridges": len(result["bridges"]),
            "blocks": assessment["decision"]["blocks"],
        })
        print(f"  #{job['id']:<6} {'+' if rows[-1]['label'] else '-'}  v1={rows[-1]['v1_score']:<3} v2={rows[-1]['v2_score']:<3} "
              f"aprova={'sim' if rows[-1]['v2_apply'] else 'não'} inflados={inflated} pontes={rows[-1]['bridges']} "
              f"{'bloqueios: ' + '; '.join(rows[-1]['blocks']) if rows[-1]['blocks'] else ''}")
        tracks[track] += 1

    if not rows:
        print("Nenhuma vaga avaliada.")
        return 1
    print("\n== Comparação (as aprovações da v1 vêm das decisões gravadas) ==")
    report("v1 (decisão gravada)", rows, lambda r: r["v1_apply"])
    report("v2 (decisão completa)", rows, lambda r: r["v2_apply"])
    print("\n== Curva da v2 por nota mínima (ignora bloqueios e cético; só a nota) ==")
    for threshold in (int(t) for t in args.thresholds.split(",") if t.strip()):
        report(f"nota >= {threshold}", rows, lambda r, t=threshold: r["v2_score"] >= t and not r["blocks"])
    approved = [r for r in rows if r["v2_apply"]]
    relied = sum(1 for r in approved if r["inflated"])
    print(f"\nTrilhas: {dict(tracks)} | aprovações que dependeram de requisito desconsiderado: {relied}/{len(approved)}")
    print(f"Aprovações com ponte transferível: {sum(1 for r in approved if r['bridges'])}/{len(approved)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
