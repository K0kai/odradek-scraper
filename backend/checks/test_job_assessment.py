"""Offline: triagem v2 — citações verificadas, requisitos inflados, transferíveis, bloqueios e cético."""
import copy
from datetime import date
import unittest

from candidate.resume_profile import NormalizedText, ground_profile
from checks.test_resume_profile import RESUME, TODAY, raw_profile
from jobs import job_assessment as ja

JOB_HTML = """<h2>Estágio em Suporte de TI</h2>
<p>Responsabilidades:</p>
<ul><li>Atendimento a usuarios internos</li><li>Elaboracao de planilhas e relatorios</li></ul>
<p>Requisitos obrigatorios:</p>
<ul><li>Experiencia minima de 5 anos em suporte</li><li>Conhecimento em Excel</li><li>Ingles fluente</li></ul>
<p>Modalidade: remoto</p>"""

CFG = {"candidate_city": "Belo Horizonte", "job_selection_optimism": "75", "minimum_match_score": "70",
       "transferable_minimum_score": "60"}
JOB = {"title": "Estágio em Suporte de TI", "company": "Acme", "location": "Remoto", "description": JOB_HTML}


def profile():
    prof = ground_profile(raw_profile(), RESUME, today=TODAY)
    prof["languages"] = [{"id": "L1", "language": "Português", "level": "nativo", "quote": "x"}]
    return prof


def match(rid, status="met", evidence=("R1.1",), quote="Suporte a usuários internos e planilhas Excel", bridge=""):
    return {"requirement_id": rid, "status": status, "evidence_ids": list(evidence), "resume_quote": quote, "bridge": bridge}


def base_raw():
    return {
        "requirements": [
            {"id": "Q1", "text": "5 anos de suporte", "kind": "must", "category": "experience_years",
             "job_quote": "Experiencia minima de 5 anos em suporte", "realism": "inflated",
             "realism_basis_quote": "Estágio em Suporte de TI", "realism_reason": "estágio pedindo 5 anos"},
            {"id": "Q2", "text": "Excel", "kind": "must", "category": "skill", "job_quote": "Conhecimento em Excel",
             "realism": "core", "realism_basis_quote": "", "realism_reason": ""},
        ],
        "role_core": {"level_label": "Estágio", "level_quote": "Estágio em Suporte de TI", "responsibilities": [
            {"id": "J1", "text": "Atendimento a usuários internos", "job_quote": "Atendimento a usuarios internos"},
            {"id": "J2", "text": "Planilhas e relatórios", "job_quote": "Elaboracao de planilhas e relatorios"},
        ]},
        "matches": [match("Q1", "unmet", ()), match("Q2", "met", ("S1",), "APIs em Python e FastAPI"),
                    match("J1", "transferable", ("T1",), "Suporte a usuários internos", "suporte interno → atendimento N1"),
                    match("J2", "met")],
        "stated": {"min_years": 5, "min_years_quote": "Experiencia minima de 5 anos em suporte", "workplace": "remote",
                   "workplace_quote": "Modalidade: remoto", "job_country": "", "job_country_quote": "",
                   "job_city": "", "job_city_quote": "", "foreign_eligibility": "unstated", "eligibility_quote": "",
                   "authorization": "unclear", "authorization_quote": "", "required_languages": []},
        "role_family_fit": "adjacent", "fit_reason": "", "cover_letter_required": False,
    }


class Runner:
    def __init__(self, raw, skeptic=None):
        self.raw, self.skeptic, self.calls = raw, skeptic or {"objections": []}, 0

    def __call__(self, prompt, schema, **kwargs):
        self.calls += 1
        return self.raw if schema is ja.ASSESS_SCHEMA else self.skeptic


def run(raw, *, cfg=None, job=None, skeptic=None, track="core", prof=None):
    runner = Runner(raw, skeptic)
    result = ja.assess_job(
        job or JOB, {**CFG, **(cfg or {})}, profile=prof or profile(), resume_norm=NormalizedText.of(RESUME),
        call_json=runner, language="pt", preferences="", track=track,
    )
    return result, runner


def req(result, rid):
    return next(r for r in result["assessment"]["requirements"] if r["id"] == rid)


class AssessmentTests(unittest.TestCase):
    def test_inflated_internship_years_neither_block_nor_count(self):
        result, _ = run(base_raw())
        self.assertEqual(req(result, "Q1")["realism"], "inflated")
        self.assertEqual(result["assessment"]["decision"]["blocks"], [])
        self.assertNotIn("Q1", result["assessment"]["decision"]["must_ids"])
        self.assertIn("ignorados (inflados)", result["reason"])

    def test_inflated_without_verifiable_basis_quote_reverts_to_core_and_counts(self):
        raw = base_raw()
        raw["requirements"][0]["realism_basis_quote"] = "texto que não está na vaga"
        result, _ = run(raw)
        self.assertEqual(req(result, "Q1")["realism"], "core")
        self.assertIn("Q1", result["assessment"]["decision"]["must_ids"])
        self.assertEqual(result["assessment"]["dropped"]["realism_reverted"], 1)
        self.assertFalse(result["should_apply"])  # Q1 core e não atendido derruba a cobertura

    def test_requirement_without_job_quote_is_discarded(self):
        raw = base_raw()
        raw["requirements"].append({"id": "Q3", "text": "Kubernetes", "kind": "must", "category": "skill",
                                    "job_quote": "Kubernetes avançado em produção", "realism": "core",
                                    "realism_basis_quote": "", "realism_reason": ""})
        result, _ = run(raw)
        self.assertEqual([r["id"] for r in result["assessment"]["requirements"]], ["Q1", "Q2"])
        self.assertEqual(result["assessment"]["dropped"]["requirements"], 1)

    def test_met_with_invented_resume_quote_becomes_unmet(self):
        raw = base_raw()
        raw["matches"][1] = match("Q2", "met", ("S1",), "Especialista certificado em Excel avançado")
        result, _ = run(raw)
        self.assertEqual(req(result, "Q2")["status"], "unmet")
        self.assertFalse(result["should_apply"])

    def test_met_without_valid_evidence_id_becomes_unmet(self):
        raw = base_raw()
        raw["matches"][1] = match("Q2", "met", ("S99",), "APIs em Python e FastAPI")
        result, _ = run(raw)
        self.assertEqual(req(result, "Q2")["status"], "unmet")

    def test_transferable_gets_partial_credit_and_a_bridge(self):
        result, _ = run(base_raw())
        self.assertEqual(result["assessment"]["responsibilities"][0]["status"], "transferable")
        self.assertEqual(len(result["bridges"]), 1)
        self.assertIn("suporte interno", result["bridges"][0])

    def test_transferable_without_bridge_is_only_partial(self):
        raw = base_raw()
        raw["matches"][2]["bridge"] = ""
        result, _ = run(raw)
        self.assertEqual(result["assessment"]["responsibilities"][0]["status"], "partial")

    def test_score_is_deterministic_with_no_optimism_floor(self):
        result, _ = run(base_raw())
        decision = result["assessment"]["decision"]
        # must_cov: só Q2 (core) e... Q2 era "met" com citação de outra evidência → conferir a fórmula, não o valor fixo
        expected = round(100 * (0.70 * decision["must_cov"] + 0.15 * decision["nice_cov"] + 0.15 * decision["fit_credit"]))
        self.assertEqual(result["match_score"], expected)
        again, _ = run(base_raw())
        self.assertEqual(again["match_score"], result["match_score"])

    def test_low_coverage_is_rejected_even_if_the_model_is_enthusiastic(self):
        raw = base_raw()
        for item in raw["matches"]:
            item["status"], item["evidence_ids"] = "unmet", []
        result, runner = run(raw)
        self.assertFalse(result["should_apply"])
        self.assertLess(result["match_score"], 50)
        self.assertEqual(runner.calls, 1)  # cético só roda para aprovadas

    def test_core_years_requirement_blocks_when_candidate_lacks_experience(self):
        raw = base_raw()
        raw["requirements"][0].update(realism="core", realism_basis_quote="", realism_reason="")
        raw["stated"]["min_years"] = 15
        raw["stated"]["min_years_quote"] = "Experiencia minima de 5 anos em suporte"
        result, _ = run(raw)
        self.assertTrue(any("anos comprovados" in b for b in result["assessment"]["decision"]["blocks"]))

    def test_language_requirement_is_never_inflated_and_blocks_when_missing(self):
        raw = base_raw()
        raw["requirements"].append({"id": "Q3", "text": "Inglês fluente", "kind": "must", "category": "language",
                                    "job_quote": "Ingles fluente", "realism": "inflated",
                                    "realism_basis_quote": "Estágio em Suporte de TI", "realism_reason": "x"})
        raw["stated"]["required_languages"] = [{"language": "Inglês", "quote": "Ingles fluente"}]
        raw["matches"].append(match("Q3", "unmet", ()))
        result, _ = run(raw)
        self.assertEqual(req(result, "Q3")["realism"], "core")
        self.assertTrue(any("idioma exigido" in b for b in result["assessment"]["decision"]["blocks"]))

    GEO_DESC = JOB_HTML + ("<p>Trabalho presencial. Hybrid in Berlin. Open to candidates worldwide: "
                           "work from anywhere. Located in Germany.</p>")
    GEO_WORDS = ("exterior", "presencial", "híbrida", "configure sua cidade", "restrita", "sem informar")

    def geo(self, stated, *, location="Remoto", cfg=None):
        raw = base_raw()
        raw["stated"].update(stated)
        result, _ = run(raw, job={**JOB, "location": location, "description": self.GEO_DESC}, cfg=cfg)
        blocks = result["assessment"]["decision"]["blocks"]
        return [b for b in blocks if any(word in b for word in self.GEO_WORDS)], blocks

    FOREIGN = {"job_country": "Germany", "job_country_quote": "Located in Germany"}

    def test_foreign_remote_job_needs_a_verified_open_to_foreigners_guarantee(self):
        open_ = {**self.FOREIGN, "foreign_eligibility": "open_to_candidate", "eligibility_quote": "work from anywhere"}
        self.assertEqual(self.geo(open_)[0], [])
        silent = self.geo(self.FOREIGN)[0]
        self.assertTrue(any("sem garantia" in b for b in silent))
        invented = self.geo({**open_, "eligibility_quote": "everyone is welcome to apply"})[0]
        self.assertTrue(any("sem garantia" in b for b in invented))  # citação inventada cai para "não informado"

    def test_foreign_hybrid_or_onsite_job_is_blocked_even_when_open_to_foreigners(self):
        stated = {**self.FOREIGN, "workplace": "hybrid", "workplace_quote": "Hybrid in Berlin",
                  "foreign_eligibility": "open_to_candidate", "eligibility_quote": "work from anywhere"}
        self.assertTrue(any("exterior não remota" in b for b in self.geo(stated)[0]))

    def test_domestic_remote_job_passes_anywhere_in_the_country(self):
        stated = {"job_country": "Brasil", "job_country_quote": "São Paulo, Brasil"}
        self.assertEqual(self.geo(stated, location="São Paulo, Brasil")[0], [])

    def test_onsite_and_hybrid_only_pass_in_the_home_city(self):
        onsite = {"workplace": "onsite", "workplace_quote": "Trabalho presencial"}
        self.assertEqual(self.geo(onsite, location="Belo Horizonte, MG")[0], [])
        elsewhere = self.geo(onsite, location="São Paulo, Brasil")[0]
        self.assertTrue(any("presencial fora de Belo Horizonte" in b for b in elsewhere))
        hybrid = {"workplace": "hybrid", "workplace_quote": "Hybrid in Berlin"}
        self.assertTrue(any("híbrida fora de" in b for b in self.geo(hybrid, location="São Paulo, Brasil")[0]))

    def test_unverified_workplace_quote_never_counts_as_remote_or_onsite(self):
        stated = {"workplace": "onsite", "workplace_quote": "100% in the office"}
        result, _ = run({**base_raw(), "stated": {**base_raw()["stated"], **stated}})
        self.assertEqual(result["assessment"]["stated"]["workplace"], "unspecified")

    def test_missing_home_city_asks_to_configure_the_profile(self):
        onsite = {"workplace": "onsite", "workplace_quote": "Trabalho presencial", "job_country": "Brasil",
                  "job_country_quote": "São Paulo, Brasil"}
        blocks = self.geo(onsite, location="São Paulo, Brasil", cfg={"candidate_city": ""})[0]
        self.assertTrue(any("configure sua cidade" in b for b in blocks))

    def test_explicit_restriction_blocks_only_with_a_real_quote(self):
        restricted = {"foreign_eligibility": "restricted", "eligibility_quote": "Trabalho presencial"}
        self.assertTrue(any("restrita" in b for b in self.geo(restricted)[0]))
        fake = {"foreign_eligibility": "restricted", "eligibility_quote": "must live in the US"}
        self.assertEqual(self.geo(fake)[0], [])

    def test_geo_rule_can_be_turned_off_but_work_authorization_still_blocks(self):
        off = {"geo_eligibility_enabled": "0"}
        onsite_foreign = {**self.FOREIGN, "workplace": "onsite", "workplace_quote": "Trabalho presencial"}
        self.assertEqual(self.geo(onsite_foreign, cfg=off)[0], [])
        auth = {"authorization": "candidate_lacks", "authorization_quote": "Conhecimento em Excel"}
        _, blocks = self.geo({**onsite_foreign, **auth}, cfg=off)
        self.assertTrue(any("autorização de trabalho" in b for b in blocks))

    def test_different_field_without_transfer_path_blocks_but_with_strong_path_does_not(self):
        raw = base_raw()
        raw["role_family_fit"] = "different"
        weak = copy.deepcopy(raw)
        for item in weak["matches"]:
            if item["requirement_id"] in ("J1", "J2"):
                item["status"], item["evidence_ids"] = "unmet", []
        blocked, _ = run(weak, track="transferable")
        self.assertTrue(any("área diferente" in b for b in blocked["assessment"]["decision"]["blocks"]))
        strong, _ = run(raw, track="transferable")
        self.assertFalse(any("área diferente" in b for b in strong["assessment"]["decision"]["blocks"]))
        self.assertEqual(strong["assessment"]["decision"]["transfer_path"], "strong")
        self.assertEqual(strong["track"], "transferable")

    def test_transferable_track_uses_its_own_lower_minimum_score(self):
        core, _ = run(base_raw(), cfg={"minimum_match_score": "99", "transferable_minimum_score": "10"})
        transferable, _ = run(base_raw(), cfg={"minimum_match_score": "99", "transferable_minimum_score": "10"}, track="transferable")
        self.assertFalse(core["should_apply"])
        self.assertTrue(transferable["should_apply"])

    def test_skeptic_verified_blocker_rejects_an_approved_job(self):
        skeptic = {"objections": [{"kind": "new_issue", "requirement_id": "", "severity": "blocker",
                                   "job_quote": "Modalidade: remoto", "reason": "x"}]}
        result, runner = run(base_raw(), skeptic=skeptic)
        self.assertEqual(runner.calls, 2)
        self.assertFalse(result["should_apply"])

    def test_skeptic_objection_with_fake_quote_is_ignored(self):
        skeptic = {"objections": [{"kind": "new_issue", "requirement_id": "", "severity": "blocker",
                                   "job_quote": "citação inventada pelo cético", "reason": "x"}]}
        result, _ = run(base_raw(), skeptic=skeptic)
        self.assertTrue(result["should_apply"])
        self.assertEqual(result["assessment"]["objections"], [])

    def test_skeptic_cannot_use_an_inflated_requirement_as_a_blocker(self):
        skeptic = {"objections": [{"kind": "requirement_unmet", "requirement_id": "Q1", "severity": "blocker",
                                   "job_quote": "Experiencia minima de 5 anos em suporte", "reason": "x"}]}
        result, _ = run(base_raw(), skeptic=skeptic)
        self.assertTrue(result["should_apply"])

    def test_skeptic_can_prove_an_inflated_requirement_is_actually_core(self):
        skeptic = {"objections": [{"kind": "inflated_is_core", "requirement_id": "Q1", "severity": "minor",
                                   "job_quote": "Experiencia minima de 5 anos em suporte", "reason": "x"}]}
        result, _ = run(base_raw(), skeptic=skeptic)
        self.assertEqual(req(result, "Q1")["realism"], "core")
        self.assertFalse(result["should_apply"])  # agora exige 5 anos que o candidato não tem em suporte

    def test_kitchen_sink_of_unrelated_musts_is_marked_inflated(self):
        raw = base_raw()
        extra = ["Terraform", "Kafka", "Cassandra", "Haskell", "Erlang", "Scala", "Hadoop", "Spark", "Airflow", "Snowflake"]
        for i, name in enumerate(extra, start=10):
            raw["requirements"].append({"id": f"Q{i}", "text": name, "kind": "must", "category": "skill",
                                        "job_quote": "Conhecimento em Excel", "realism": "core",
                                        "realism_basis_quote": "", "realism_reason": ""})
            raw["matches"].append(match(f"Q{i}", "unmet", ()))
        result, _ = run(raw)
        flagged = [r for r in result["assessment"]["requirements"] if r["id"] in {f"Q{i}" for i in range(10, 20)}]
        self.assertTrue(all(r["realism"] == "inflated" for r in flagged))
        self.assertTrue(result["assessment"]["inflated_posting"])
        self.assertTrue(result["should_apply"])  # decisão passa a depender das responsabilidades

    def test_optimism_maps_to_a_predictable_threshold(self):
        self.assertEqual(ja.optimism_threshold(0), 1.0)
        self.assertAlmostEqual(ja.optimism_threshold(50), 0.80)
        self.assertAlmostEqual(ja.optimism_threshold(75), 0.70)
        self.assertAlmostEqual(ja.optimism_threshold(100), 0.55)
        self.assertAlmostEqual(ja.optimism_threshold(62.5), 0.75)
        self.assertAlmostEqual(ja.optimism_threshold("lixo"), 0.70)

    def test_clean_job_html_keeps_block_breaks_and_unescapes(self):
        text = ja.clean_job_html("<p>Dev &amp; Ops</p><ul><li>Python</li><li>SQL</li></ul>")
        self.assertEqual(text.split("\n"), ["Dev & Ops", "- Python", "- SQL"])


if __name__ == "__main__":
    unittest.main()
