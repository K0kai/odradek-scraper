"""Offline: evidência do currículo no Qpid — por seção/cargo, sem resumo de IA, relevantes primeiro."""
import json
import types
import unittest

from fastapi import FastAPI

from candidate.resume_profile import ground_profile, resume_excerpts
from checks.test_linkedin_filters import AppDbCase
from checks.test_resume_profile import RESUME, TODAY, raw_profile
from data import repository
from web.qpid_applications import register


class ExcerptTests(unittest.TestCase):
    def test_excerpts_never_cross_section_boundaries_or_split_lines(self):
        excerpts = resume_excerpts(RESUME, ground_profile(raw_profile(), RESUME, today=TODAY), max_chars=120)
        sections = {e["section"] for e in excerpts}
        self.assertEqual(sections, {"header", "experience", "education"})
        for excerpt in excerpts:
            self.assertLessEqual(len(excerpt["text"]), 120)
            for line in excerpt["text"].splitlines():
                self.assertIn(line, RESUME)  # linhas inteiras do PDF, nunca fragmentos no meio
        self.assertFalse(any("FORMAÇÃO" in e["text"] and "Beta SA" in e["text"] for e in excerpts))

    def test_each_role_is_its_own_excerpt_when_the_profile_knows_the_roles(self):
        excerpts = resume_excerpts(RESUME, ground_profile(raw_profile(), RESUME, today=TODAY))
        roles = [e for e in excerpts if e["section"] == "experience"]
        self.assertEqual(len(roles), 2)
        self.assertIn("Acme", roles[0]["text"])
        self.assertNotIn("Beta SA", roles[0]["text"])
        self.assertIn("Beta SA", roles[1]["label"])  # rótulo vem do próprio texto do PDF

    def test_flattened_legacy_text_still_chunks_by_words(self):
        excerpts = resume_excerpts("palavra " * 400, None, max_chars=900)
        self.assertGreater(len(excerpts), 1)
        self.assertTrue(all(len(e["text"]) <= 900 for e in excerpts))


class QpidContextTests(AppDbCase):
    def context(self):
        api = FastAPI()
        register(api, types.SimpleNamespace(connect=repository.connect, settings=lambda: {}))
        endpoint = next(r.endpoint for r in api.routes if getattr(r, "path", "") == "/api/qpid/applications/{job_id}")
        return endpoint(1, "pt")

    def evidence(self):
        return [r for r in self.context()["rules"] if r.get("sourceOnly")]

    def test_excerpts_carry_section_and_language_and_stay_source_only(self):
        self.seed_resume()
        self.seed_job()
        evidence = self.evidence()
        self.assertGreaterEqual(len(evidence), 3)
        self.assertTrue(all(e["sourceLanguage"] == "pt" and e["source"] == "uploaded-resume" for e in evidence))
        self.assertTrue(all("matched_requirements" not in e for e in evidence))  # sem triagem ainda

    def test_excerpts_backing_the_jobs_verified_matches_come_first(self):
        self.seed_resume()
        self.seed_job()
        audit = {"requirements": [], "responsibilities": [
            {"text": "Desenvolver APIs", "status": "met", "resume_quote": "APIs em Python e FastAPI"},
            {"text": "Inventada", "status": "unmet", "resume_quote": "Experiência: Acme"},
        ]}
        with repository.connect() as db:
            db.execute(
                "INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,assessment_json) "
                "VALUES(1,'now',80,1,0,'r',?)", (json.dumps(audit),))
        evidence = self.evidence()
        self.assertIn("Beta SA", evidence[0]["value"])
        self.assertEqual(evidence[0]["matched_requirements"], ["Desenvolver APIs"])
        self.assertEqual(evidence[0]["id"], "resume:pt:excerpt:0")
        self.assertTrue(all("matched_requirements" not in e for e in evidence[1:]))

    def test_generated_summary_and_profile_text_never_leak_into_evidence(self):
        self.seed_resume()
        self.seed_job()
        with repository.connect() as db:
            db.execute("UPDATE resumes SET analysis_summary='DO NOT USE GENERATED SUMMARY'")
        blob = json.dumps(self.evidence(), ensure_ascii=False)
        self.assertNotIn("DO NOT USE GENERATED SUMMARY", blob)
        self.assertNotIn("Dev com base administrativa", blob)  # headline gerada pela IA


if __name__ == "__main__":
    unittest.main()
