"""Offline: perfil canônico do currículo (seções, ancoragem de citações, meses calculados)."""
from datetime import date
import unittest

from candidate import resume_pipeline as pipeline
from candidate.resume_profile import (
    NormalizedText, compact_profile_text, compute_derived, evidence_index, ground_profile,
    legacy_analysis, merge_profiles, quote_in_text, split_sections, union_months,
)

RESUME = """JOÃO SILVA
Desenvolvedor Python

EXPERIÊNCIA PROFISSIONAL
Assistente Administrativo — Acme Ltda (01/2019 - 12/2021)
- Organização de documentos e controle de contratos
- Suporte a usuários internos e planilhas Excel

Desenvolvedor Backend — Beta SA (2022 - atual)
- APIs em Python e FastAPI

FORMAÇÃO
Análise e Desenvolvimento de Sistemas
"""

TODAY = date(2026, 11, 1)


def raw_profile():
    return {
        "headline": "Dev com base administrativa",
        "roles": [
            {"title": "Assistente Administrativo", "employer": "Acme Ltda", "start": "2019-01", "end": "2021-12",
             "quote": "Assistente Administrativo — Acme Ltda (01/2019 - 12/2021)",
             "responsibilities": [
                 {"text": "suporte interno", "quote": "Suporte a usuários internos e planilhas Excel"},
                 {"text": "inventada", "quote": "Liderou time de 40 engenheiros"},
             ], "skills_used": ["excel"]},
            {"title": "Desenvolvedor Backend", "employer": "Beta SA", "start": "2022-01", "end": "present",
             "quote": "Desenvolvedor Backend — Beta SA (2022 - atual)",
             "responsibilities": [{"text": "APIs", "quote": "APIs em Python e FastAPI"}], "skills_used": ["python", "fastapi"]},
        ],
        "skills": [
            {"name": "Python", "canonical": "python", "category": "language", "quotes": ["APIs em Python e FastAPI"]},
            {"name": "Rust", "canonical": "rust", "category": "language", "quotes": ["Rust avançado"]},
        ],
        "transferable_skills": [
            {"capability": "suporte a usuários", "from_roles": ["Assistente Administrativo"], "quotes": ["Suporte a usuários internos"]},
            {"capability": "gestão de pessoas", "from_roles": [], "quotes": ["Gerenciou equipe inteira"]},
        ],
    }


class ProfileTests(unittest.TestCase):
    def test_sections_split_on_known_headings(self):
        kinds = [s.kind for s in split_sections(RESUME)]
        self.assertEqual(kinds, ["header", "experience", "education"])

    def test_flattened_text_is_a_single_section(self):
        self.assertEqual(len(split_sections("Texto corrido sem quebras de linha nenhuma EXPERIÊNCIA algo")), 1)

    def test_quote_matching_ignores_accents_and_spacing_but_not_inventions(self):
        source = NormalizedText.of(RESUME)
        self.assertTrue(quote_in_text("suporte a USUARIOS   internos", source))
        self.assertFalse(quote_in_text("Liderou time de 40 engenheiros", source))

    def test_ungrounded_facts_are_dropped_and_counted(self):
        profile = ground_profile(raw_profile(), RESUME, today=TODAY)
        self.assertEqual([r["id"] for r in profile["roles"]], ["R1", "R2"])
        self.assertEqual(len(profile["roles"][0]["responsibilities"]), 1)
        self.assertEqual([s["canonical"] for s in profile["skills"]], ["python"])
        self.assertEqual([t["capability"] for t in profile["transferable_skills"]], ["suporte a usuários"])
        self.assertEqual(profile["coverage"]["dropped"], {"responsibilities": 1, "skills": 1, "transferable_skills": 1})

    def test_months_are_computed_from_dates_not_by_the_model(self):
        profile = ground_profile(raw_profile(), RESUME, today=TODAY)
        derived = profile["derived"]
        self.assertEqual(derived["role_months"], {"R1": 36, "R2": 59})
        self.assertEqual(derived["total_months"], 95)
        python = next(v for v in derived["skills"].values() if v["canonical"] == "python")
        self.assertEqual((python["months"], python["last_used"]), (59, "2026-11"))

    def test_overlapping_intervals_are_not_double_counted(self):
        self.assertEqual(union_months([(0, 12), (6, 18), (30, 36)]), 24)

    def test_bad_dates_are_blanked_not_guessed(self):
        raw = raw_profile()
        raw["roles"][0]["start"] = "ontem"
        profile = ground_profile(raw, RESUME, today=TODAY)
        self.assertEqual(profile["roles"][0]["start"], "")
        self.assertNotIn("R1", profile["derived"]["role_months"])

    def test_evidence_index_and_compact_text_carry_stable_ids(self):
        profile = ground_profile(raw_profile(), RESUME, today=TODAY)
        index = evidence_index(profile)
        self.assertIn("R1.1", index)
        self.assertIn("T1", index)
        text = compact_profile_text(profile)
        self.assertIn("[R2] Desenvolvedor Backend", text)
        self.assertIn("[S1] Python: 4y11m", text)

    def test_merge_does_not_duplicate_same_role_or_skill(self):
        pt = ground_profile(raw_profile(), RESUME, today=TODAY)
        en = ground_profile(raw_profile(), RESUME, today=TODAY)
        merged = merge_profiles([pt, en], today=TODAY)
        self.assertEqual(len(merged["roles"]), 2)
        self.assertEqual(len(merged["skills"]), 1)
        self.assertEqual(merged["derived"]["total_months"], 95)

    def test_legacy_analysis_keeps_dict_experience_and_computed_years(self):
        legacy = legacy_analysis(ground_profile(raw_profile(), RESUME, today=TODAY), "pt")
        self.assertTrue(legacy["experience"][1]["is_current"])
        self.assertEqual(legacy["current_employer"], "Beta SA")
        self.assertIn("7.9 anos", legacy["years_of_experience"])

    def test_analyze_resume_text_uses_grounded_profile(self):
        analysis = pipeline.analyze_resume_text(
            RESUME, "pt", provider="x", model="y", api_key="k", call_json=lambda *a: raw_profile()
        )
        profile = analysis.pop("_profile")
        self.assertEqual(profile["coverage"]["ungrounded"], 3)
        self.assertIn("Experiência profissional", analysis["summary"])
        self.assertLess(analysis["summary"].index("Experiência profissional"), analysis["summary"].index("Competências técnicas"))

    def test_analysis_with_nothing_verifiable_fails(self):
        with self.assertRaises(RuntimeError):
            pipeline.analyze_resume_text(
                "texto qualquer do currículo sem nada relevante para citar aqui", "pt",
                provider="x", model="y", api_key="k", call_json=lambda *a: raw_profile(),
            )

    def test_pdf_page_cleaning_keeps_lines_and_drops_repeated_headers(self):
        pages = ["Pagina de Topo\nEXPERIÊNCIA\nAssis-\ntente  X", "Pagina de Topo\nlinha dois", "Pagina de Topo\nlinha tres"]
        text = pipeline._clean_pdf_pages(pages)
        self.assertNotIn("Pagina de Topo", text)
        self.assertIn("Assistente X", text)
        self.assertIn("EXPERIÊNCIA\n", text)


if __name__ == "__main__":
    unittest.main()
