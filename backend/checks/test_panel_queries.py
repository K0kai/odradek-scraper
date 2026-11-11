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


if __name__ == "__main__":
    unittest.main()
