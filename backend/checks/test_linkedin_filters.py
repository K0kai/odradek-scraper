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


class LegacyPromptMigrationTests(AppDbCase):
    LEGACY = "You design multiple LinkedIn Jobs search filters.\nMULTIPLE FILTERS (mandatory):\n- 4 to 8"

    def test_saved_multi_filter_prompts_are_reset_once_and_later_custom_prompts_survive(self):
        for key in ("linkedin_filter_prompt", "transferable_prompt"):
            repository.set_setting(key, self.LEGACY)
        with repository.connect() as db:
            db.execute("DELETE FROM settings WHERE key='filter_prompts_v3_migrated'")
        repository.initialize()
        cfg = repository.settings()
        self.assertEqual((cfg["linkedin_filter_prompt"], cfg["transferable_prompt"]), ("", ""))
        repository.set_setting("linkedin_filter_prompt", "my own single-filter prompt")
        repository.initialize()
        self.assertEqual(repository.settings()["linkedin_filter_prompt"], "my own single-filter prompt")


class LegacySelectionPromptTests(AppDbCase):
    LEGACY = ("You are a fair, practical job-fit evaluator.\n- Reject only for a genuine hard blocker.\n"
              "- Base the score and recommendation on the optimism calibration supplied below.")

    def migrate(self):
        with repository.connect() as db:
            db.execute("DELETE FROM settings WHERE key='selection_prompt_v2_migrated'")
        repository.initialize()

    def test_legacy_saved_prompt_is_reset_once_and_later_custom_prompts_survive(self):
        repository.set_setting("job_selection_prompt", self.LEGACY)
        self.migrate()
        self.assertEqual(repository.settings()["job_selection_prompt"], "")
        repository.set_setting("job_selection_prompt", self.LEGACY)
        repository.initialize()  # já migrado: não apaga de novo
        self.assertEqual(repository.settings()["job_selection_prompt"], self.LEGACY)

    def test_custom_prompt_without_the_legacy_line_is_kept(self):
        repository.set_setting("job_selection_prompt", "Prefiro vagas de backend com Python.")
        self.migrate()
        self.assertEqual(repository.settings()["job_selection_prompt"], "Prefiro vagas de backend com Python.")

    def test_default_prompt_covers_entry_level_evidence_and_silence_is_not_a_restriction(self):
        self.assertIn("internships and entry-level", app.DEFAULT_JOB_SELECTION_PROMPT)
        self.assertIn("Silence in the posting", app.DEFAULT_JOB_SELECTION_PROMPT)


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

    def test_phrases_never_name_a_language_framework_or_tool_even_with_long_use(self):
        raw = raw_filter([kw("Backend Developer Python", "R2"), kw("Python Developer", "R2"),
                          kw("Software Engineer", "R2"), kw("Desenvolvedor de Software", "R2.1")])
        out = self.verify(raw)
        self.assertEqual(out["keywords"], ["Software Engineer", "Desenvolvedor de Software"])  # Python: 59 meses, mas é stack

    def test_secondary_phrases_also_exclude_named_technologies(self):
        out = self.verify(raw_filter([kw("Python Support Analyst", "T1"), kw("IT Support Analyst", "T1")]), slot="secondary")
        self.assertEqual(out["keywords"], ["IT Support Analyst"])

    def test_default_main_prompt_asks_for_one_stack_agnostic_role(self):
        self.assertIn("NO PROGRAMMING LANGUAGE, FRAMEWORK OR TOOL", app.DEFAULT_LINKEDIN_FILTER_PROMPT)
        self.assertIn("architecture", app.DEFAULT_LINKEDIN_FILTER_PROMPT)
        self.assertNotIn("stack qualifier", app.DEFAULT_LINKEDIN_FILTER_PROMPT)

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


class IndiaExclusionTests(unittest.TestCase):
    def test_location_in_india_is_excluded_by_country_city_or_state(self):
        for location in ("India", "Bengaluru, Karnataka, India", "Hyderabad, Telangana", "Greater Pune Area", "Noida"):
            self.assertTrue(app.excluded_region_reason({"location": location, "title": "Dev", "description": ""}), location)

    def test_explicit_india_only_postings_are_excluded_even_with_a_vague_location(self):
        job = {"location": "Remote", "title": "Intern", "description": "<p>100% Remote: Participate from anywhere in India.</p>"}
        self.assertTrue(app.excluded_region_reason(job))
        self.assertTrue(app.excluded_region_reason({"location": "Remote", "title": "Dev", "description": "Stipend: ₹10,000"}))

    def test_other_places_and_lookalikes_are_not_excluded(self):
        for location in ("Brazil", "Belo Horizonte, Minas Gerais, Brazil", "Indiana, United States", "Worldwide", "Remote"):
            job = {"location": location, "title": "Dev", "description": "Work with the Indiana team and Indian cuisine lovers."}
            self.assertEqual(app.excluded_region_reason(job), "", location)

    def test_multi_country_location_lists_are_not_india_only_jobs(self):
        job = {"location": "USA, UK, India, Australia, Ireland, New Zealand, Philippines, Pakistan, Singapore, Mexico",
               "title": "AI Code Trainer", "description": ""}
        self.assertEqual(app.excluded_region_reason(job), "")

    def test_batch_filter_drops_only_indian_jobs_and_counts_them(self):
        batch = [{"location": "India", "title": "a", "description": ""}, {"location": "Brazil", "title": "b", "description": ""}]
        kept, dropped = app.drop_excluded_region_jobs(batch)
        self.assertEqual([j["title"] for j in kept], ["b"])
        self.assertEqual(dropped, 1)


def hours_ago(hours):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


class PostedWindowTests(unittest.TestCase):
    ACTOR = {"id": "li", "label": "LinkedIn", "enabled": True, "input_mode": "linkedin_search", "count": 25}
    SCOPE = [{"location": "Brazil", "f_wt": "2"}, {"location": "Worldwide", "f_wt": "2"}]

    def test_phases_always_start_at_24h_and_widen_up_to_the_slider_value(self):
        self.assertEqual([app.linkedin_recency_phases(n) for n in (1, 3, 7, 10, 30)],
                         [[1], [1, 3], [1, 7], [1, 7, 10], [1, 7, 30]])
        self.assertEqual(app.linkedin_recency_phases(99), [1, 7, 30])  # nunca passa de 30 dias
        self.assertEqual(app.linkedin_recency_phases(0), [1])

    def test_setting_is_clamped_and_falls_back_to_the_old_seven_days(self):
        read = lambda value: app.linkedin_max_age_days({"linkedin_max_age_days": value})
        self.assertEqual([read("1"), read("30"), read("45"), read("0"), read("abc"), read("")], [1, 30, 30, 1, 7, 7])
        self.assertEqual(app.linkedin_max_age_days({}), 7)

    def test_urls_go_from_the_newest_window_to_the_widest_and_ask_for_most_recent_first(self):
        urls = app.build_linkedin_search_urls(make_filter(), self.SCOPE, 30)
        windows = [next(part for part in url.split("&") if part.startswith("f_TPR=")) for url in urls]
        self.assertEqual(windows, ["f_TPR=r86400"] * 2 + ["f_TPR=r604800"] * 2 + ["f_TPR=r2592000"] * 2)
        self.assertTrue(all("sortBy=DD" in url for url in urls))

    def test_one_day_slider_searches_only_the_last_24_hours(self):
        urls = app.build_linkedin_search_urls(make_filter(), self.SCOPE, 1)
        self.assertTrue(urls and all("f_TPR=r86400" in url for url in urls))

    def test_plan_runs_newest_window_first_whatever_the_slider(self):
        for days in (1, 5, 14, 30):
            planned = app.plan_apify_runs([self.ACTOR], make_filter(), scope=self.SCOPE, max_age_days=days)
            labels = [actor["label"] for actor, _, _ in planned]
            self.assertIn("24h", labels[0], days)
            phases = [label.split("(")[1].split(",")[0] for label in labels]
            order = [int(x[:-1]) if x.endswith("d") else 1 for x in phases]
            self.assertEqual(order, sorted(order), days)

    def test_collection_age_ceiling_follows_the_slider(self):
        posted = hours_ago(24 * 10)
        self.assertFalse(app._linkedin_within_max_age(posted, max_days=7))
        self.assertTrue(app._linkedin_within_max_age(posted, max_days=30))
        self.assertTrue(app._linkedin_within_max_age(hours_ago(2), max_days=1))
        self.assertFalse(app._linkedin_within_max_age(hours_ago(30), max_days=1))


class PreferredCountriesTests(unittest.TestCase):
    CFG = {"candidate_city": "Belo Horizonte", "candidate_country": "Brazil"}

    def scope(self, preferred):
        return [(s["location"], s["f_wt"]) for s in app.linkedin_search_scope({**self.CFG, "linkedin_preferred_countries": preferred})]

    def test_no_preferred_countries_keeps_the_original_scope_order(self):
        self.assertEqual(self.scope(""), [("Worldwide", "2"), ("Brazil", "2"), ("Belo Horizonte", "")])

    def test_preferred_countries_are_searched_before_worldwide(self):
        self.assertEqual(self.scope("Portugal, Canada"),
                         [("Portugal", "2"), ("Canada", "2"), ("Worldwide", "2"), ("Brazil", "2"), ("Belo Horizonte", "")])

    def test_home_country_as_preferred_brings_the_home_city_right_after_it_without_duplicates(self):
        self.assertEqual(self.scope("Brasil, Portugal"),
                         [("Brasil", "2"), ("Portugal", "2"), ("Belo Horizonte", ""), ("Worldwide", "2")])

    def test_list_is_trimmed_deduplicated_and_capped_at_five(self):
        cfg = {"linkedin_preferred_countries": " Brazil ; brazil\nPortugal, Spain, Canada, Germany, Italy, France "}
        self.assertEqual(app.linkedin_preferred_countries(cfg), ["Brazil", "Portugal", "Spain", "Canada", "Germany"])

    def test_location_matching_understands_portuguese_and_english_names(self):
        self.assertTrue(app.location_in_countries("Belo Horizonte, Minas Gerais, Brazil", ["Brasil"]))
        self.assertTrue(app.location_in_countries("Lisboa, Portugal", ["portugal"]))
        self.assertFalse(app.location_in_countries("Bengaluru, Karnataka, India", ["Brasil", "Portugal"]))
        self.assertFalse(app.location_in_countries("", ["Brasil"]))

    def test_order_is_preferred_country_first_then_most_recent_then_unknown_date(self):
        jobs = [
            {"id": 1, "location": "India", "posted_at": hours_ago(1)},
            {"id": 2, "location": "Brazil", "posted_at": hours_ago(100)},
            {"id": 3, "location": "Brazil", "posted_at": hours_ago(5)},
            {"id": 4, "location": "Brazil", "posted_at": None},
        ]
        ranked = sorted(jobs, key=lambda job: app.recency_country_key(job, ["Brazil"]))
        self.assertEqual([j["id"] for j in ranked], [3, 2, 4, 1])

    def test_without_preferred_countries_only_recency_orders(self):
        jobs = [{"id": 1, "location": "India", "posted_at": hours_ago(1)}, {"id": 2, "location": "Brazil", "posted_at": hours_ago(9)}]
        ranked = sorted(jobs, key=lambda job: app.recency_country_key(job, []))
        self.assertEqual([j["id"] for j in ranked], [1, 2])


class SearchSettingsValidationTests(unittest.TestCase):
    def validate(self, **form):
        from web import panel_settings
        panel_settings._validate_search_form(form)
        return form

    def test_slider_accepts_1_to_30_and_rejects_the_rest(self):
        self.assertEqual(self.validate(linkedin_max_age_days="30")["linkedin_max_age_days"], "30")
        for bad in ("0", "31", "-1", "abc", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.validate(linkedin_max_age_days=bad)

    def test_preferred_countries_are_normalized_and_validated(self):
        self.assertEqual(self.validate(linkedin_preferred_countries=" Brazil ;Portugal\n")["linkedin_preferred_countries"], "Brazil, Portugal")
        self.assertEqual(self.validate(linkedin_preferred_countries="")["linkedin_preferred_countries"], "")
        for bad in ("A, B, C, D, E, F", "Brazil 123", "<script>"):
            with self.assertRaises(ValueError, msg=bad):
                self.validate(linkedin_preferred_countries=bad)


class SettingsRoundTripAndQueueOrderTests(AppDbCase):
    def test_slider_and_preferred_countries_are_saved_and_read_back(self):
        from web import panel_settings
        values = panel_settings.settings_api_payload("search")["values"]
        self.assertEqual((values["linkedin_max_age_days"], values["linkedin_preferred_countries"]), ("7", ""))
        panel_settings.save_settings_api_group("search", {"linkedin_max_age_days": "21", "linkedin_preferred_countries": "Brazil; Portugal"})
        values = panel_settings.settings_api_payload("search")["values"]
        self.assertEqual((values["linkedin_max_age_days"], values["linkedin_preferred_countries"]), ("21", "Brazil, Portugal"))
        self.assertEqual(app.linkedin_max_age_days(app.settings()), 21)
        self.assertEqual(app.linkedin_preferred_countries(app.settings()), ["Brazil", "Portugal"])

    def test_triage_queue_takes_preferred_countries_first_then_the_most_recent(self):
        for job_id, location, hours in ((1, "India", 1), (2, "Brazil", 100), (3, "Brazil", 5), (4, "Portugal", 50)):
            self.seed_job(id=job_id, location=location, fingerprint=f"fp{job_id}", url=f"https://example.com/{job_id}",
                          posted_at=hours_ago(hours), status="new")
        app.set_setting("linkedin_preferred_countries", "Brazil")
        queued = []
        with patch.object(app.queue, "enqueue", side_effect=lambda kind, payload, **kw: queued.append(payload["job_id"])):
            app.enqueue_new_jobs_for_triage(limit=3)
        self.assertEqual(queued, [3, 2, 1])  # Brasil mais recente, Brasil antigo, e só então o resto mais recente


if __name__ == "__main__":
    unittest.main()
