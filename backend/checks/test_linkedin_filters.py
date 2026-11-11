"""Offline: filtros ancorados em evidência, trilha transferível, search_filter_id e triagem ponta a ponta."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from candidate.resume_pipeline import AiUnavailableError
from candidate.resume_profile import PROFILE_VERSION, ground_profile
from checks.test_job_assessment import JOB_HTML, base_raw
from checks.test_resume_profile import RESUME, TODAY, raw_profile
from data import repository


class AppDbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"DATABASE_URL": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.settings = patch.multiple(repository, DB_PATH=root / "test.db", RESUMES_DIR=str(root / "resumes"))
        self.settings.start(); self.addCleanup(self.settings.stop)
        repository.initialize()
        key = patch.object(app, "get_ai_key", return_value="test-key")
        key.start(); self.addCleanup(key.stop)

    def seed_resume(self, language="pt", *, with_profile=True):
        profile = ground_profile(raw_profile(), RESUME, today=TODAY)
        with repository.connect() as db:
            db.execute(
                """INSERT INTO resumes(language,original_filename,stored_path,file_sha256,extracted_text,analysis_json,
                   analysis_summary,analysis_status,created_at,updated_at,profile_json,profile_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (language, "cv.pdf", "x", "sha", RESUME, "{}", "Resumo", "ok", "now", "now",
                 json.dumps(profile) if with_profile else "", PROFILE_VERSION if with_profile else ""),
            )
        return profile

    def seed_job(self, **extra):
        values = {"id": 1, "source": "t", "title": "Estágio em Suporte de TI", "company": "Acme", "location": "Remoto",
                  "description": JOB_HTML, "url": "https://example.com/1", "first_seen_at": "now",
                  "fingerprint": "fp1", "language": "pt", **extra}
        with repository.connect() as db:
            db.execute(f"INSERT INTO jobs({','.join(values)}) VALUES({','.join('?' * len(values))})", tuple(values.values()))
        return values


class MigrationTests(AppDbCase):
    def test_new_columns_and_defaults_exist(self):
        with repository.connect() as db:
            jobs = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
            decisions = {r["name"] for r in db.execute("PRAGMA table_info(ai_decisions)")}
            resumes = {r["name"] for r in db.execute("PRAGMA table_info(resumes)")}
        self.assertIn("search_filter_id", jobs)
        self.assertLessEqual({"assessment_json", "scoring_version", "provider", "model"}, decisions)
        self.assertLessEqual({"profile_json", "profile_version"}, resumes)
        cfg = repository.settings()
        self.assertEqual(cfg["linkedin_main_share"], "70")
        self.assertEqual(cfg["geo_eligibility_enabled"], "1")
        self.assertEqual(cfg["minimum_match_score"], "70")
        for gone in ("apify_linkedin_filters_json", "apify_transferable_filters_json", "transferable_search_enabled",
                     "transferable_max_filters", "apify_linkedin_filter_hash"):
            self.assertNotIn(gone, cfg)

    def test_old_default_minimum_score_is_migrated_once_and_later_choices_survive(self):
        with repository.connect() as db:
            db.execute("UPDATE settings SET value='65' WHERE key='minimum_match_score'")
            db.execute("DELETE FROM settings WHERE key='scoring_v2_migrated'")
        repository.initialize()
        self.assertEqual(repository.settings()["minimum_match_score"], "70")
        repository.set_setting("minimum_match_score", "65")
        repository.initialize()
        self.assertEqual(repository.settings()["minimum_match_score"], "65")


def kw(phrase, *ids):
    return {"phrase": phrase, "evidence_ids": list(ids)}


def raw_filter(keywords, **extra):
    return {"name": "Backend Python", "keywords": keywords, "anchor_role_ids": [], "experience_levels": [2],
            "reason": "r", "sector": "", "resilience_reason": "", **extra}


def grounded_profile():
    return ground_profile(raw_profile(), RESUME, today=TODAY)


class FilterVerificationTests(unittest.TestCase):
    """_verify_filter: o código confere o que a IA propõe contra o perfil verificado."""

    def verify(self, raw, slot="main", **kwargs):
        return app._verify_filter(raw, grounded_profile(), slot, **kwargs)

    def test_main_filter_keeps_one_role_with_at_most_one_variant(self):
        out = self.verify(raw_filter([kw("Backend Developer", "R2"), kw("Desenvolvedor Backend", "R2.1"),
                                      kw("Data Engineer", "R2")]))
        self.assertEqual(out["keywords"], ["Backend Developer", "Desenvolvedor Backend"])
        self.assertEqual(out["evidence"]["Desenvolvedor Backend"], ["R2.1"])

    def test_main_phrase_needs_a_role_not_just_a_skill_or_capability(self):
        out = self.verify(raw_filter([kw("Python", "S1"), kw("Suporte", "T1"), kw("Backend Developer", "R2")]))
        self.assertEqual(out["keywords"], ["Backend Developer"])

    def test_phrase_without_valid_evidence_is_dropped_and_nothing_left_is_an_error(self):
        with self.assertRaises(ValueError):
            self.verify(raw_filter([kw("Quantum Chef", "R99"), kw("Outro", [])]))

    def test_stack_qualifier_needs_twelve_months_or_current_use(self):
        profile = grounded_profile()
        profile["skills"].append({"id": "S9", "name": "Rust", "category": "language", "canonical": "rust", "quotes": []})
        profile["derived"]["skills"]["S9"] = {"months": 3, "role_ids": ["R1"], "last_used": "2021-12", "canonical": "rust"}
        raw = raw_filter([kw("Backend Developer Rust", "R2"), kw("Backend Developer Python", "R2")])
        out = app._verify_filter(raw, profile, "main")
        self.assertEqual(out["keywords"], ["Backend Developer Python"])  # Python: 59 meses, cargo atual

    def test_career_months_and_anchor_roles_come_from_code_not_from_the_model(self):
        out = self.verify(raw_filter([kw("Backend Developer", "R2.1")], career_months=999, anchor_role_ids=["R1", "R42"]))
        self.assertEqual([a["id"] for a in out["anchor_roles"]], ["R2", "R1"])  # R42 não existe; R2 vem da evidência
        self.assertEqual(out["career_months"], 36 + 59)
        self.assertEqual(out["anchor_roles"][0]["months"], 59)

    def test_secondary_accepts_transferable_capability_and_drops_the_mains_phrases(self):
        out = self.verify(
            raw_filter([kw("IT Support Analyst", "T1"), kw("Backend Developer", "R2")], sector="saúde",
                       resilience_reason="setor estável"),
            slot="secondary", avoid=("Backend Developer",))
        self.assertEqual(out["keywords"], ["IT Support Analyst"])
        self.assertEqual(out["sector"], "saúde")

    def test_secondary_phrase_cannot_rest_on_a_skill_alone(self):
        with self.assertRaises(ValueError):
            self.verify(raw_filter([kw("Python Specialist", "S1")]), slot="secondary")


class FilterGenerationTests(AppDbCase):
    def generate(self, slot, raw, **cfg):
        self.seed_resume()
        with patch("integrations.ai_client.call_ai_json", return_value={"filter": raw}) as call:
            result = app._generate_filter({**repository.settings(), **cfg}, slot)
        return result, call

    def test_main_filter_is_normalized_with_a_stable_main_id(self):
        result, _ = self.generate("main", raw_filter([kw("Backend Developer", "R2"), kw("Desenvolvedor Backend", "R2")]))
        self.assertTrue(result["id"].startswith("main-"))
        self.assertTrue(result["enabled"])
        self.assertEqual(result["slot"], "main")
        self.assertNotIn("locations", result)  # local e modelo vêm do Perfil, não da IA
        self.assertNotIn("workplace_types", result)

    def test_secondary_is_enabled_on_generation_and_receives_the_area_hint_and_main_filter(self):
        repository.set_setting("linkedin_main_filter_json", json.dumps(app._normalize_filter(
            {"name": "Main", "keywords": ["Backend Developer"]}, "main")))
        result, call = self.generate(
            "secondary", raw_filter([kw("IT Support Analyst", "T1")], sector="saúde", resilience_reason="r"),
            secondary_area_hint="administrativo com sistemas")
        self.assertTrue(result["id"].startswith("sec-"))
        self.assertTrue(result["enabled"])
        self.assertEqual(result["track"], "transferable")
        prompt = call.call_args.kwargs["prompt"]
        self.assertIn("administrativo com sistemas", prompt)
        self.assertIn("Backend Developer", prompt)

    def test_no_profile_gives_a_clear_error(self):
        self.seed_resume(with_profile=False)
        with self.assertRaises(ValueError):
            app._generate_filter(repository.settings(), "main")

    def test_generating_one_slot_never_touches_the_other(self):
        from web import panel_settings
        repository.set_setting("linkedin_secondary_filter_json", json.dumps(app._normalize_filter(
            {"name": "Sec", "keywords": ["IT Support"], "enabled": False}, "secondary")))
        self.seed_resume()
        with patch("integrations.ai_client.call_ai_json", return_value={"filter": raw_filter([kw("Backend Developer", "R2")])}):
            panel_settings.generate_main_filter({})
        cfg = repository.settings()
        self.assertEqual(app.get_main_filter(cfg)["keywords"], ["Backend Developer"])
        self.assertFalse(app.get_secondary_filter(cfg)["enabled"])  # intacto, inclusive o liga/desliga

    def test_job_track_follows_the_filter_slot_prefix(self):
        self.assertEqual(app.job_track({"search_filter_id": "sec-abc"}, {}), "transferable")
        self.assertEqual(app.job_track({"search_filter_id": "tr-abc"}, {}), "transferable")  # formato antigo
        self.assertEqual(app.job_track({"search_filter_id": "main-abc"}, {}), "core")
        self.assertEqual(app.job_track({}, {}), "core")


class FilterFormTests(unittest.TestCase):
    def test_form_accepts_empty_or_one_object_and_rejects_lists_and_more_than_two_phrases(self):
        self.assertEqual(app._validate_filter_form("", "main"), "")
        ok = json.loads(app._validate_filter_form(json.dumps({"name": "X", "keywords": ["Dev", "Developer"]}), "main"))
        self.assertEqual(ok["keywords"], ["Dev", "Developer"])
        for bad in ('[{"name": "X", "keywords": ["Dev"]}]', json.dumps({"name": "X", "keywords": ["A", "B", "C"]}),
                    json.dumps({"name": "", "keywords": ["A"]}), json.dumps({"name": "X", "keywords": []})):
            with self.assertRaises(ValueError):
                app._validate_filter_form(bad, "main")

    def test_main_filter_is_always_enabled_and_the_secondary_keeps_its_switch(self):
        main = app._normalize_filter({"name": "M", "keywords": ["Dev"], "enabled": False}, "main")
        sec = app._normalize_filter({"name": "S", "keywords": ["Support"], "enabled": False}, "secondary")
        self.assertTrue(main["enabled"])
        self.assertFalse(sec["enabled"])


def make_filter(slot="main", keywords=("Backend Developer",), levels=(2,), enabled=True):
    return app._normalize_filter({"name": slot, "keywords": list(keywords), "experience_levels": list(levels), "enabled": enabled}, slot)


class ScopeAndPlanTests(unittest.TestCase):
    ACTOR = {"id": "li", "label": "LinkedIn", "enabled": True, "input_mode": "linkedin_search", "count": 25}

    def scope(self, **cfg):
        return app.linkedin_search_scope({"candidate_city": "Belo Horizonte", "candidate_country": "Brazil", **cfg})

    def test_scope_with_geo_rule_is_remote_worldwide_remote_country_and_any_model_in_the_city(self):
        self.assertEqual([(s["location"], s["f_wt"]) for s in self.scope()],
                         [("Worldwide", "2"), ("Brazil", "2"), ("Belo Horizonte", "")])

    def test_scope_without_a_city_skips_the_city_search(self):
        self.assertEqual([s["location"] for s in self.scope(candidate_city="")], ["Worldwide", "Brazil"])

    def test_scope_with_geo_rule_off_never_filters_by_work_model(self):
        scope = self.scope(geo_eligibility_enabled="0")
        self.assertEqual([(s["location"], s["f_wt"]) for s in scope], [("Worldwide", ""), ("Brazil", "")])

    def test_urls_cover_keyword_x_scope_day_first_and_nothing_is_hardcoded_to_a_city(self):
        item = make_filter(keywords=("Backend Developer", "Desenvolvedor Backend"))
        urls = app.build_linkedin_search_urls(item, [{"location": "Worldwide", "f_wt": "2"}, {"location": "Curitiba", "f_wt": ""}])
        self.assertEqual(len(urls), 8)
        self.assertTrue(all("r86400" in u for u in urls[:4]) and all("r604800" in u for u in urls[4:]))
        self.assertTrue(all("Belo" not in u for u in urls))
        self.assertTrue(any("location=Curitiba" in u and "f_WT" not in u for u in urls))
        self.assertTrue(all("f_E=2" in u for u in urls))

    def test_count_split_is_main_weighted_with_a_minimum_of_one(self):
        main, sec = make_filter("main"), make_filter("secondary", ("IT Support",))
        self.assertEqual(app.split_apify_count(25, [main], 70), [25])
        self.assertEqual(app.split_apify_count(25, [main, sec], 70), [18, 7])
        self.assertEqual(app.split_apify_count(1, [main, sec], 70), [1, 1])
        self.assertEqual(app.split_apify_count(10, [main, sec], 99), [10, 1])  # fatia limitada a 95%; mínimo 1 para o secundário

    def test_plan_gives_each_filter_its_share_and_day_batches_come_first(self):
        main, sec = make_filter("main"), make_filter("secondary", ("IT Support",))
        planned = app.plan_apify_runs([self.ACTOR], main, sec, scope=self.scope(), main_share=70)
        counts = {item["id"]: actor["count"] for actor, item, _ in planned}
        self.assertEqual(counts, {main["id"]: 18, sec["id"]: 7})
        phases = ["24h" if "24h" in actor["label"] else "7d" for actor, _, _ in planned]
        self.assertEqual(phases, sorted(phases, key=lambda p: p != "24h"))

    def test_disabled_secondary_does_not_run_and_main_gets_the_whole_count(self):
        main, sec = make_filter("main"), make_filter("secondary", ("IT Support",), enabled=False)
        planned = app.plan_apify_runs([self.ACTOR], main, sec, scope=self.scope())
        self.assertEqual({item["id"] for _, item, _ in planned}, {main["id"]})
        self.assertTrue(all(actor["count"] == 25 for actor, _, _ in planned))

    def test_secondary_alone_gets_the_whole_count(self):
        planned = app.plan_apify_runs([self.ACTOR], None, make_filter("secondary", ("IT Support",)), scope=self.scope())
        self.assertTrue(planned and all(actor["count"] == 25 for actor, _, _ in planned))

    def test_no_filter_at_all_plans_no_linkedin_run(self):
        self.assertEqual(app.plan_apify_runs([self.ACTOR], None, None, scope=self.scope()), [])

    def test_run_input_without_any_filter_fails_instead_of_using_panel_keywords(self):
        with self.assertRaises(ValueError):
            app.build_apify_run_input(self.ACTOR, {"keywords": "python"})

    def test_custom_actor_count_is_not_divided(self):
        custom = {"id": "c", "enabled": True, "input_mode": "custom", "count": 9}
        planned = app.plan_apify_runs([custom], make_filter("main"), make_filter("secondary", ("IT Support",)), scope=self.scope())
        self.assertEqual(planned, [(custom, None, None)])


class CollectionPreferencesTests(unittest.TestCase):
    def test_keywords_match_whole_words_only_and_location_no_longer_filters(self):
        job = {"title": "Aqua Diver", "company": "Sea", "location": "Atlantis", "description": "<p>javascript only</p>"}
        self.assertFalse(app.matches_collection_preferences(job, ["qa"]))
        self.assertFalse(app.matches_collection_preferences(job, ["java"]))
        self.assertTrue(app.matches_collection_preferences(job, ["javascript"]))
        self.assertTrue(app.matches_collection_preferences({**job, "_linkedin_filter_id": "main-1"}, ["qa"]))


class TriageEndToEndTests(AppDbCase):
    @staticmethod
    def fake_caller(raw):
        def factory(provider, model, key):
            def call(prompt, schema, **kwargs):
                return raw if "requirements" in schema["properties"] else {"objections": []}
            return call
        return factory

    def test_no_resume_at_all_is_a_blocking_value_error(self):
        self.seed_job()
        with self.assertRaises(ValueError):
            app.ai_assess_job(dict(self.seed_job_row()), repository.settings())

    def seed_job_row(self):
        with repository.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=1").fetchone()

    def test_resume_without_profile_yet_is_retryable_not_blocking(self):
        self.seed_resume(with_profile=False)
        self.seed_job()
        with self.assertRaises(AiUnavailableError):
            app.ai_assess_job(dict(self.seed_job_row()), repository.settings())

    def test_rejected_job_is_ignored_and_audit_is_persisted(self):
        self.seed_resume()
        self.seed_job()
        raw = base_raw()
        for item in raw["matches"]:
            item["status"], item["evidence_ids"] = "unmet", []
        with patch.object(app, "_assessment_caller", self.fake_caller(raw)):
            message = app.process_auto_job(1)
        self.assertIn("descartou", message)
        with repository.connect() as db:
            job = db.execute("SELECT status, notes FROM jobs WHERE id=1").fetchone()
            decision = db.execute("SELECT * FROM ai_decisions WHERE job_id=1").fetchone()
        self.assertEqual(job["status"], "ignored")
        self.assertEqual(decision["should_apply"], 0)
        self.assertEqual(decision["scoring_version"], "v2.1")
        self.assertEqual(decision["provider"], "gemini")
        audit = json.loads(decision["assessment_json"])
        self.assertIn("requirements", audit)
        self.assertEqual(audit["decision"]["track"], "core")

    def test_resume_language_comes_from_the_job_not_the_model(self):
        self.seed_resume("en")
        self.seed_job(language="pt")
        with patch.object(app, "_assessment_caller", self.fake_caller(base_raw())):
            result = app.ai_assess_job(dict(self.seed_job_row()), repository.settings())
        self.assertEqual(result["resume_language"], "en")  # só existe o currículo EN
        self.assertTrue(result["should_apply"])
        self.assertEqual(len(result["bridges"]), 1)

    def test_approved_job_gets_a_letter_with_verified_bridges(self):
        self.seed_resume("pt")
        self.seed_job(source="linkedin", url="https://www.linkedin.com/jobs/1")
        captured = {}

        def fake_letter(provider, model, language, job, cfg, bridges=None):
            captured["bridges"] = bridges
            return "Carta."

        with patch.object(app, "_assessment_caller", self.fake_caller(base_raw())), \
                patch.object(app, "ai_generate_letter", fake_letter):
            message = app.process_auto_job(1)
        self.assertIn("vale a pena olhar", message)
        self.assertEqual(len(captured["bridges"]), 1)
        self.assertIn("suporte interno", captured["bridges"][0])
        with repository.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id=1").fetchone()["status"], "worth")


if __name__ == "__main__":
    unittest.main()
