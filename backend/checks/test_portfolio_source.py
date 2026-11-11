"""Offline: portfólio público (schema 2) como segunda fonte do perfil, sem IA e sem rede."""
import copy
import unittest

from candidate import portfolio_source as ps
from candidate.resume_profile import (
    NormalizedText, PROFILE_VERSION, compact_profile_text, compute_derived, evidence_index,
    ground_profile, merge_profiles, merged_profile, quote_in_text,
)
from checks.test_job_assessment import RESUME, TODAY, raw_profile
from checks.test_linkedin_filters import AppDbCase
from data import repository
from jobs import job_assessment as ja

ODRADEK = ("September 2026 - present · Python, FastAPI, React. Painel full stack de vagas com triagem por IA "
           "e filtros de busca por cargo.")


def record(rid, kind, title, text, facts, language="pt"):
    return {"id": f"{language}:{rid}", "kind": kind, "title": title, "language": language,
            "url": f"https://site.example/{language}", "text": text, "facts": facts}


def make_payload(version="v1"):
    records = []
    for lang in ("pt", "en"):
        records += [
            record("profile", "profile", "Perfil", "Desenvolvedor full stack com Python e React.", {"residence": "Belo Horizonte, Minas Gerais, Brasil"}, lang),
            record("skills:0", "skills", "Linguagens", "Python, TypeScript", {"kind": "language", "items": ["Python", "TypeScript"]}, lang),
            record("skills:1", "skills", "Frameworks", "React, FastAPI", {"kind": "framework", "items": ["React", "FastAPI"]}, lang),
            record("skills:2", "skills", "Idiomas", "English (fluent), Portuguese (native)",
                   {"kind": "spoken", "items": ["English (fluent)", "Portuguese (native)"]}, lang),
            record("experience:0", "experience", "Beta SA",
                   "Desenvolvedor Backend. 2022-01 / atual. Python, FastAPI. APIs de integração com filas.",
                   {"company": "Beta SA", "role": "Desenvolvedor Backend", "start": "2022-01", "end": None,
                    "stack": ["Python", "FastAPI"], "bullets": ["APIs de integração com filas"]}, lang),
            record("project:odradek", "project", "Odradek Scraper", ODRADEK,
                   {"slug": "odradek", "title": "Odradek Scraper", "start": "2026-09", "end": None,
                    "stack": ["Python", "FastAPI", "React"]}, lang),
            record("project:odradek:bullet:0", "project", "Odradek Scraper", "Triagem por IA com citações verificadas.",
                   {"slug": "odradek"}, lang),
            record("project:odradek:story", "project", "Odradek Scraper", "Narrativa técnica longa do projeto.", {"slug": "odradek"}, lang),
            record("project:qpid", "project", "Qpid", "Outubro 2026 - atual · React, Electron. Aplicativo desktop.",
                   {"slug": "qpid", "title": "Qpid", "start": "2026-10", "end": None, "stack": ["React", "Electron"]}, lang),
            record("education:0", "education", "Newton Paiva", "Tecnólogo em ADS. 2024-01 a 2027-12.",
                   {"school": "Newton Paiva", "credential": "ADS", "start": "2024-01", "end": "2027-12"}, lang),
        ]
    return {"schema": 1, "version": version, "records": records}


def portfolio_profile(language="pt"):
    return ps.build_portfolio_profile(ps.validate_payload(make_payload()), language)


def skill(profile, name):
    return next(s for s in profile["skills"] if s["name"] == name)


class BuildProfileTests(unittest.TestCase):
    def test_roles_projects_skills_and_languages(self):
        profile = portfolio_profile()
        self.assertEqual(profile["version"], PROFILE_VERSION)
        role = profile["roles"][0]
        self.assertEqual((role["id"], role["title"], role["employer"], role["start"], role["end"]),
                         ("R1", "Desenvolvedor Backend", "Beta SA", "2022-01", "present"))
        self.assertEqual(role["responsibilities"][0]["id"], "R1.1")
        self.assertEqual([p["title"] for p in profile["projects"]], ["Odradek Scraper", "Qpid"])  # bullets não viram projeto
        self.assertEqual(profile["projects"][0]["end"], "present")
        self.assertEqual(skill(profile, "Python")["category"], "language")
        self.assertEqual(skill(profile, "React")["category"], "framework")
        self.assertEqual(skill(profile, "Electron")["category"], "other")  # só aparece em stack de projeto
        self.assertEqual([(l["language"], l["level"]) for l in profile["languages"]],
                         [("English", "fluent"), ("Portuguese", "native")])
        self.assertEqual(profile["location_notes"], "Belo Horizonte, Minas Gerais, Brasil")
        self.assertEqual(profile["education"][0]["start"], "2024-01")
        for key in ("roles", "projects", "education"):
            self.assertTrue(all(item["source"] == "portfolio" and item["url"] for item in profile[key]))

    def test_every_quote_is_found_in_the_portfolio_text(self):
        payload = ps.validate_payload(make_payload())
        profile = ps.build_portfolio_profile(payload, "pt")
        source = NormalizedText.of(ps.portfolio_text(payload))
        quotes = [profile["roles"][0]["quote"], *(r["quote"] for r in profile["roles"][0]["responsibilities"]),
                  *(p["quote"] for p in profile["projects"]), *(e["quote"] for e in profile["education"]),
                  *(q for s in profile["skills"] for q in s["quotes"])]
        self.assertTrue(quotes)
        for quote in quotes:
            self.assertTrue(quote_in_text(quote, source), quote)

    def test_old_schema_and_malformed_payloads_are_rejected(self):
        for bad in ({"schema": 1, "version": "x", "records": [{"id": "pt:a", "kind": "k", "language": "pt", "text": "sem facts"}]}, [], {"version": "x", "records": [1]}, {"schema": 2, "version": "x", "records": []},
                    {"schema": 2, "version": "x", "records": [{"id": "pt:a", "kind": "k", "language": "pt", "text": "t"}]}):
            with self.assertRaises(ps.PortfolioError):
                ps.validate_payload(bad)


class DerivedAndMergeTests(unittest.TestCase):
    def test_projects_give_skill_evidence_but_never_professional_months(self):
        profile = portfolio_profile()
        derived = compute_derived(profile, today=TODAY)
        without_projects = copy.deepcopy(profile)
        without_projects["projects"] = []
        self.assertEqual(derived["total_months"], compute_derived(without_projects, today=TODAY)["total_months"])
        react = derived["skills"][skill(profile, "React")["id"]]
        self.assertEqual(react["months"], 0)  # React só em projeto pessoal
        self.assertEqual(react["project_months"], 3)  # 2026-09..2026-11 e 2026-10..2026-11 sem dupla contagem
        self.assertEqual(len(react["project_ids"]), 2)
        self.assertEqual(react["project_last_used"], "2026-11")
        python = derived["skills"][skill(profile, "Python")["id"]]
        self.assertGreater(python["months"], 0)
        self.assertEqual(python["project_months"], 3)

    def test_merge_with_pdf_dedupes_roles_skills_and_projects(self):
        pdf = ground_profile(raw_profile(), RESUME, today=TODAY)
        pdf["projects"] = [{"id": "P1", "text": "Odradek Scraper: painel de vagas", "quote": "Odradek Scraper"}]
        merged = merge_profiles([pdf, portfolio_profile()], today=TODAY)
        self.assertEqual([r["title"] for r in merged["roles"]], ["Assistente Administrativo", "Desenvolvedor Backend"])
        self.assertEqual(sum(1 for s in merged["skills"] if s["canonical"] == "python"), 1)
        titles = [p.get("title") for p in merged["projects"]]
        self.assertEqual(len(merged["projects"]), 2)  # Odradek enriquecido + Qpid
        enriched = next(p for p in merged["projects"] if p.get("title") == "Odradek Scraper")
        self.assertEqual(enriched["text"], "Odradek Scraper: painel de vagas")  # texto do PDF preservado
        self.assertEqual(enriched["start"], "2026-09")
        self.assertIn("Qpid", titles)
        self.assertEqual(merged["derived"]["total_months"], pdf["derived"]["total_months"])

    def test_same_job_with_different_employer_spelling_is_one_role(self):
        pdf = ground_profile(raw_profile(), RESUME, today=TODAY)
        pdf["roles"][1]["employer"] = "Beta"
        merged = merge_profiles([pdf, portfolio_profile()], today=TODAY)
        self.assertEqual(len(merged["roles"]), 2)

    def test_triage_text_labels_personal_project_usage(self):
        merged = merge_profiles([ground_profile(raw_profile(), RESUME, today=TODAY), portfolio_profile()], today=TODAY)
        text = compact_profile_text(merged)
        self.assertRegex(text, r"React: no dated professional usage; portfolio projects 3m \(P\d,P\d\), last 2026-11")
        self.assertRegex(text, r"Odradek Scraper \(2026-09 → present; Python, FastAPI, React\)")
        entry = evidence_index(merged)[next(p["id"] for p in merged["projects"] if p.get("title") == "Qpid")]
        self.assertEqual(entry["source"], "portfolio")


class SyncTests(AppDbCase):
    def sync(self, fetch, **kwargs):
        with repository.connect() as db:
            return ps.sync_portfolio(db, "https://site.example/ai/profile.json", fetch=fetch, **kwargs)

    def stored(self, key):
        return repository.settings().get(key, "")

    def test_disabled_without_url(self):
        with repository.connect() as db:
            self.assertEqual(ps.sync_portfolio(db, "  ")["status"], "disabled")

    def test_same_version_is_unchanged_and_new_version_updates(self):
        fetch = lambda url: ps.validate_payload(make_payload("v1"))
        self.assertEqual(self.sync(fetch)["status"], "updated")
        self.assertIn("v1", self.stored("portfolio_version"))
        self.assertEqual(self.sync(fetch)["status"], "unchanged")
        self.assertEqual(self.sync(fetch, force=True)["status"], "updated")
        self.assertEqual(self.sync(lambda url: ps.validate_payload(make_payload("v2")))["status"], "updated")
        self.assertIn("v2", self.stored("portfolio_version"))

    def test_failure_keeps_last_good_snapshot(self):
        self.sync(lambda url: ps.validate_payload(make_payload("v1")))
        before = (self.stored("portfolio_version"), self.stored("portfolio_profile_json"))

        def broken(url):
            raise ps.PortfolioError("sem rede")

        result = self.sync(broken)
        self.assertEqual(result["status"], "error")
        self.assertEqual((self.stored("portfolio_version"), self.stored("portfolio_profile_json")), before)
        self.assertEqual(self.stored("portfolio_error"), "sem rede")
        old = self.sync(lambda url: ps.validate_payload({"schema": 1, "version": "x", "records": [{"id": "pt:a", "kind": "k", "language": "pt", "text": "t"}]}))
        self.assertEqual(old["status"], "error")
        self.assertIn("facts", old["message"])
        self.assertEqual(self.sync(lambda url: ps.validate_payload(make_payload("v1")))["status"], "unchanged")
        self.assertEqual(self.stored("portfolio_error"), "")

    def test_builder_version_change_rebuilds_the_snapshot(self):
        fetch = lambda url: ps.validate_payload(make_payload("v1"))
        self.sync(fetch)
        from unittest.mock import patch
        with patch.object(ps, "BUILDER_VERSION", "999"):
            self.assertEqual(self.sync(fetch)["status"], "updated")

    def test_merged_profile_works_with_portfolio_only(self):
        self.sync(lambda url: ps.validate_payload(make_payload()))
        with repository.connect() as db:
            profile, norm = merged_profile(db, prefer="pt")
            status = ps.portfolio_status(db)
        self.assertEqual(profile["roles"][0]["title"], "Desenvolvedor Backend")
        self.assertGreater(profile["derived"]["total_months"], 0)
        self.assertTrue(quote_in_text("Triagem por IA com citações verificadas", norm))
        self.assertEqual(status["counts"]["projects"], 2)

    def test_assessment_accepts_a_resume_quote_that_exists_only_in_the_portfolio(self):
        pdf = self.seed_resume("pt")
        self.sync(lambda url: ps.validate_payload(make_payload()))
        with repository.connect() as db:
            profile, merged_norm = merged_profile(db, prefer="pt")
        react = skill(profile, "React")
        job_text = "Requisitos obrigatorios: Conhecimento em React"
        raw = {"requirements": [{"id": "Q1", "text": "React", "kind": "must", "category": "skill",
                                 "job_quote": "Conhecimento em React", "realism": "core"}],
               "matches": [{"requirement_id": "Q1", "status": "met", "evidence_ids": [react["id"]],
                            "resume_quote": "Painel full stack de vagas com triagem por IA", "bridge": ""}]}
        job_norm = NormalizedText.of(job_text)
        only_pdf = ja._verify(raw, job_norm, NormalizedText.of(RESUME), profile)
        self.assertEqual(only_pdf["requirements"][0]["status"], "unmet")
        with_portfolio = ja._verify(raw, job_norm, merged_norm, profile)
        self.assertEqual(with_portfolio["requirements"][0]["status"], "met")
        self.assertTrue(pdf["roles"])


if __name__ == "__main__":
    unittest.main()
