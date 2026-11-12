"""Offline: auditoria compacta no card e rendimento por filtro."""
import json
import unittest

from checks.test_linkedin_filters import AppDbCase
from data import repository
from web import panel_queries


class PanelQueryTests(AppDbCase):
    def add_job(self, job_id, filter_id, *, approved=None, status="new", seen="2999-01-01T00:00:00+00:00", audit=""):
        self.seed_job(id=job_id, fingerprint=f"fp{job_id}", url=f"https://example.com/{job_id}",
                      search_filter_id=filter_id, status=status, first_seen_at=seen)
        if approved is not None:
            with repository.connect() as db:
                db.execute(
                    "INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,assessment_json) "
                    "VALUES(?,?,?,?,0,'r',?)", (job_id, "now", 80 if approved else 20, int(approved), audit))

    def test_filter_metrics_count_collected_triaged_approved_and_user_ignored(self):
        self.add_job(1, "li-a", approved=True)
        self.add_job(2, "li-a", approved=False, status="ignored")
        self.add_job(3, "li-a", approved=True, status="ignored")
        self.add_job(4, "li-a")
        self.add_job(5, "", approved=True)  # sem filtro: fora da métrica
        self.add_job(6, "li-b", approved=True, seen="2000-01-01T00:00:00+00:00")
        metrics = panel_queries.filter_metrics_api_payload()["metrics"]
        self.assertEqual(set(metrics), {"li-a", "li-b"})
        a = metrics["li-a"]
        self.assertEqual((a["collected"], a["last_7d"], a["triaged"], a["approved"], a["ignored_by_user"]), (4, 4, 3, 2, 1))
        self.assertAlmostEqual(a["approval_rate"], 2 / 3)
        self.assertFalse(a["low_precision"])
        self.assertEqual((metrics["li-b"]["collected"], metrics["li-b"]["last_7d"], metrics["li-b"]["last_30d"]), (1, 0, 0))

    def test_low_precision_warning_needs_enough_triaged_jobs(self):
        for i in range(1, 23):
            self.add_job(i, "li-x", approved=(i == 1))
        self.assertTrue(panel_queries.filter_metrics_api_payload()["metrics"]["li-x"]["low_precision"])

    def test_job_payload_carries_compact_audit_and_dashboard_hides_it(self):
        audit = {"track": "transferable", "inflated_posting": True,
                 "decision": {"must_cov": 0.8, "threshold": 0.7, "blocks": ["x"], "track": "transferable"},
                 "requirements": [{"id": "Q1", "text": "Excel", "kind": "must", "status": "met", "realism": "core",
                                   "resume_quote": "planilhas", "job_quote": "Excel", "secret": "nope"}],
                 "responsibilities": [], "objections": [{"severity": "minor", "reason": "r", "job_quote": "q"}]}
        self.add_job(1, "li-a", approved=True, status="worth", audit=json.dumps(audit))
        item = panel_queries.jobs_api_payload(status="worth")["items"][0]
        self.assertEqual(item["assessment"]["track"], "transferable")
        self.assertEqual(item["assessment"]["requirements"][0]["status"], "met")
        self.assertNotIn("secret", item["assessment"]["requirements"][0])
        self.assertNotIn("assessment_json", item)
        self.assertIsNone(panel_queries.dashboard_api_payload()["jobs"][0]["assessment"])

    def test_jobs_without_audit_have_no_assessment(self):
        self.add_job(1, "", approved=True, status="worth")
        self.assertIsNone(panel_queries.jobs_api_payload(status="worth")["items"][0]["assessment"])


class TriageViewTests(AppDbCase):
    add_job = PanelQueryTests.add_job  # mesmo helper de vagas/decisões, sem reexecutar os testes da outra classe

    def add_decision(self, job_id, score, approved):
        with repository.connect() as db:
            db.execute("INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason) "
                       "VALUES(?,?,?,?,0,'r')", (job_id, "now", score, int(approved)))

    def ids(self, tab):
        return [item["id"] for item in panel_queries.triage_api_payload(tab=tab)["items"]]

    def test_discarded_tab_lists_ai_rejections_best_score_first(self):
        self.add_job(1, "", approved=False, status="ignored")   # score 20
        self.add_job(2, "", approved=False, status="ignored")
        self.add_decision(2, 85, False)                           # decisão mais recente: 85
        self.assertEqual(self.ids("discarded"), [2, 1])

    def test_discarded_tab_skips_jobs_ignored_by_hand_or_without_a_decision(self):
        self.add_job(1, "", approved=True, status="ignored")    # a IA aprovou e o usuário ignorou
        self.add_job(2, "", status="ignored")                    # sem decisão (ex.: região excluída)
        self.add_job(3, "", approved=False, status="worth")      # não está ignorada
        self.add_job(4, "", approved=False, status="ignored")
        self.assertEqual(self.ids("discarded"), [4])

    def test_latest_decision_rules_a_job_that_the_ai_later_approved(self):
        self.add_job(1, "", approved=False, status="ignored")
        self.add_decision(1, 90, True)
        self.assertEqual(self.ids("discarded"), [])

    def test_marking_reviewed_removes_from_the_tab_and_only_touches_ignored_jobs(self):
        self.add_job(1, "", approved=False, status="ignored")
        self.add_job(2, "", approved=False, status="ignored")
        self.add_job(3, "", approved=True, status="worth")
        self.assertEqual(panel_queries.mark_jobs_reviewed([1, 3, 999]), 1)  # a #3 não é ignorada: intocada
        self.assertEqual(self.ids("discarded"), [2])
        self.assertEqual(panel_queries.mark_jobs_reviewed([1]), 0)  # já conferida
        with repository.connect() as db:
            self.assertEqual(db.execute("SELECT reviewed_at FROM jobs WHERE id=3").fetchone()["reviewed_at"], "")

    def test_pending_tab_lists_new_and_review_jobs_newest_first_with_counts(self):
        self.add_job(1, "", status="new", seen="2026-01-01T00:00:00+00:00")
        self.add_job(2, "", status="review", seen="2026-02-01T00:00:00+00:00")
        self.add_job(3, "", approved=False, status="ignored")
        self.add_job(4, "", approved=True, status="worth")
        payload = panel_queries.triage_api_payload(tab="pending")
        self.assertEqual([item["id"] for item in payload["items"]], [2, 1])
        self.assertEqual(payload["counts"], {"discarded": 1, "pending": 2})

    def test_filters_match_minimum_period_and_sort_like_the_review_page(self):
        self.add_job(1, "", approved=False, status="ignored", seen="2026-01-10T12:00:00+00:00")   # nota 20
        self.add_job(2, "", approved=False, status="ignored", seen="2026-03-10T12:00:00+00:00")
        self.add_decision(2, 85, False)
        self.add_job(3, "", approved=False, status="ignored", seen="2026-02-10T12:00:00+00:00")
        self.add_decision(3, 60, False)
        ids = lambda **kw: [i["id"] for i in panel_queries.triage_api_payload(tab="discarded", **kw)["items"]]
        self.assertEqual(ids(), [2, 3, 1])                                   # padrão: maior nota primeiro
        self.assertEqual(ids(min_match=50), [2, 3])
        self.assertEqual(ids(date_from="2026-02-01"), [2, 3])
        self.assertEqual(ids(date_from="2026-02-01", date_to="2026-02-28"), [3])
        self.assertEqual(ids(date_from="2026-02-28", date_to="2026-02-01"), [3])  # intervalo invertido é corrigido
        self.assertEqual(ids(sort="seen_asc"), [1, 3, 2])
        self.assertEqual(ids(sort="match_asc"), [1, 3, 2])

    def test_filtered_total_and_pages_follow_filters_but_tab_badges_do_not(self):
        for i in range(1, 8):
            self.add_job(i, "", approved=False, status="ignored")
            self.add_decision(i, 90 if i <= 6 else 10, False)
        payload = panel_queries.triage_api_payload(tab="discarded", min_match=50, page_size=5)
        self.assertEqual((payload["total"], payload["pages"], len(payload["items"])), (6, 2, 5))
        self.assertEqual(payload["counts"]["discarded"], 7)  # selo da aba: sem filtros
        self.assertEqual(payload["filters"]["min_match"], 50)

    def test_pending_tab_ignores_match_minimum_but_honors_the_period(self):
        self.add_job(1, "", status="new", seen="2026-01-10T12:00:00+00:00")
        self.add_job(2, "", status="review", seen="2026-03-10T12:00:00+00:00")
        pending = lambda **kw: [i["id"] for i in panel_queries.triage_api_payload(tab="pending", **kw)["items"]]
        self.assertEqual(pending(min_match=90), [2, 1])           # ainda sem nota: o match mínimo não esconde nada
        self.assertEqual(pending(date_from="2026-03-01"), [2])
        self.assertEqual(pending(sort="seen_asc"), [1, 2])

    def add_titled(self, job_id, title, company="Acme", location="Remoto", status="ignored", score=50):
        self.seed_job(id=job_id, fingerprint=f"fp{job_id}", url=f"https://example.com/{job_id}", title=title,
                      company=company, location=location, status=status)
        if status == "ignored":
            self.add_decision(job_id, score, False)

    def test_text_search_matches_title_company_or_location_case_insensitively(self):
        self.add_titled(1, "Analista de Processos", "Banco Azul", "Belo Horizonte, Brazil", score=70)
        self.add_titled(2, "Full Stack Developer", "Processos Digitais SA", "Remote", score=60)
        self.add_titled(3, "Engenheiro de Dados", "Acme", "Lisboa, Portugal", score=50)
        search = lambda q, **kw: [i["id"] for i in panel_queries.triage_api_payload(tab="discarded", q=q, **kw)["items"]]
        self.assertEqual(search("PROCESSOS"), [1, 2])      # título ou empresa
        self.assertEqual(search("lisboa"), [3])            # local
        self.assertEqual(search("belo brazil"), [1])       # várias palavras: todas precisam aparecer
        self.assertEqual(search("analista dados"), [])     # ...e nenhuma vaga tem as duas
        self.assertEqual(search("  "), [1, 2, 3])          # busca vazia não filtra
        self.assertEqual(search("processos", min_match=65), [1])  # combina com os outros filtros

    def test_text_search_treats_percent_and_underscore_as_plain_text(self):
        self.add_titled(1, "Dev 100% remoto")
        self.add_titled(2, "Dev presencial")
        self.add_titled(3, "back_end Engineer")
        search = lambda q: [i["id"] for i in panel_queries.triage_api_payload(tab="discarded", q=q)["items"]]
        self.assertEqual(search("100%"), [1])
        self.assertEqual(search("%"), [1])    # sem escape, "%" casaria com tudo
        self.assertEqual(search("back_end"), [3])
        self.assertEqual(search("_"), [3])

    def test_text_search_also_works_on_the_pending_tab_and_is_echoed_back(self):
        self.add_titled(1, "Analista de Suporte", status="new")
        self.add_titled(2, "Engenheiro", status="review")
        payload = panel_queries.triage_api_payload(tab="pending", q="Suporte  ANALISTA")
        self.assertEqual([i["id"] for i in payload["items"]], [1])
        self.assertEqual(payload["filters"]["q"], "suporte analista")
        self.assertEqual(payload["counts"]["pending"], 2)  # selo da aba segue sem filtros

    def test_unknown_tab_falls_back_to_discarded_and_pages_are_clamped(self):
        for i in range(1, 8):
            self.add_job(i, "", approved=False, status="ignored")
        payload = panel_queries.triage_api_payload(tab="nada", page=99, page_size=5)
        self.assertEqual((payload["tab"], payload["page"], payload["pages"], len(payload["items"])), ("discarded", 2, 2, 2))


class DiscardOldIndiaJobsTests(AppDbCase):
    MARKER = "india_jobs_discarded_v1"

    def seed(self, job_id, location, status, *, approved=None):
        self.seed_job(id=job_id, fingerprint=f"fp{job_id}", url=f"https://example.com/{job_id}", location=location, status=status)
        if approved is not None:
            with repository.connect() as db:
                db.execute("INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason) "
                           "VALUES(?,?,?,?,0,'r')", (job_id, "now", 90, int(approved)))

    def migrate(self):
        with repository.connect() as db:
            db.execute("DELETE FROM settings WHERE key=?", (self.MARKER,))  # initialize() já marcou o banco vazio
            repository.migrate_discard_excluded_region_jobs(db)

    def row(self, job_id):
        with repository.connect() as db:
            return dict(db.execute("SELECT status, reviewed_at, notes FROM jobs WHERE id=?", (job_id,)).fetchone())

    def test_old_india_jobs_leave_every_list_but_applied_and_saved_ones_stay(self):
        self.seed(1, "India", "ignored", approved=False)       # descartada pela IA
        self.seed(2, "Bengaluru, Karnataka, India", "prepared")
        self.seed(3, "India", "new")
        self.seed(4, "India", "worth")
        self.seed(5, "India", "applied")
        self.seed(6, "India", "saved")
        self.migrate()
        self.assertEqual(self.row(1)["status"], "ignored")
        self.assertNotEqual(self.row(1)["reviewed_at"], "")
        for job_id in (2, 3, 4):
            row = self.row(job_id)
            self.assertEqual(row["status"], "ignored")
            self.assertNotEqual(row["reviewed_at"], "")
            self.assertIn("Região excluída", row["notes"])
        self.assertEqual((self.row(5)["status"], self.row(5)["reviewed_at"]), ("applied", ""))
        self.assertEqual((self.row(6)["status"], self.row(6)["reviewed_at"]), ("saved", ""))

    def test_other_countries_are_untouched_and_the_tab_no_longer_lists_india(self):
        self.seed(1, "India", "ignored", approved=False)
        self.seed(2, "Brazil", "ignored", approved=False)
        self.seed(3, "Brazil", "prepared")
        self.migrate()
        self.assertEqual([item["id"] for item in panel_queries.triage_api_payload(tab="discarded")["items"]], [2])
        self.assertEqual((self.row(3)["status"], self.row(3)["reviewed_at"]), ("prepared", ""))
        self.assertEqual(self.row(2)["reviewed_at"], "")

    def test_runs_only_once_so_a_job_moved_back_to_review_later_stays(self):
        self.seed(1, "India", "new")
        self.migrate()
        with repository.connect() as db:
            db.execute("UPDATE jobs SET status='worth' WHERE id=1")
            repository.migrate_discard_excluded_region_jobs(db)  # marcador já gravado: não faz nada
        self.assertEqual(self.row(1)["status"], "worth")


if __name__ == "__main__":
    unittest.main()
