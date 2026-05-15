"""Pytest tests for the Bulletin pipeline.

Run with: pytest tests/test_bulletin.py -v

Tests use mock data and monkeypatching — no Anthropic API calls are made.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from poc.bulletin.schema import (
    BulletinDraft,
    BulletinVerified,
    FactcheckReport,
    ValidationError,
    validate_draft,
    validate_verified,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_ARTICLES = [
    {
        "source": "iLaks",
        "tier": 1,
        "title": "Cermaq varsler ny slakteri i Steigen",
        "url": "https://ilaks.no/cermaq-slakteri-steigen",
        "published_at": "2026-05-13T08:00:00+00:00",
        "body": "Cermaq planlegger å investere 500 millioner kroner i nytt slakteri i Steigen kommune.",
        "region": "norge",
        "scope": "cermaq",
        "tone": "positiv",
    },
    {
        "source": "SalmonBusiness",
        "tier": 1,
        "title": "Norwegian salmon prices fall 8%",
        "url": "https://salmonbusiness.com/prices-fall",
        "published_at": "2026-05-12T10:00:00+00:00",
        "body": "Spot prices for Norwegian salmon (3-6 kg) fell to NOK 78 per kilo this week.",
        "region": "global",
        "scope": "industry",
        "tone": "negativ",
    },
    {
        "source": "E24",
        "tier": 1,
        "title": "Lakselus-problem øker i Troms",
        "url": "https://e24.no/lakseoppdrett/lakselus-troms",
        "published_at": "2026-05-14T07:30:00+00:00",
        "body": "Mattilsynet rapporterer økt lakselusnivå ved flere anlegg i Troms.",
        "region": "norge",
        "scope": "industry",
        "tone": "negativ",
    },
    # Article with claim NOT in draft — used for factcheck test
    {
        "source": "Reuters",
        "tier": 1,
        "title": "Mitsubishi reports record aquaculture profits",
        "url": "https://reuters.com/mitsubishi-aquaculture",
        "published_at": "2026-05-11T12:00:00+00:00",
        "body": "Mitsubishi Corporation reported record profits of $180M from its aquaculture segment.",
        "region": "global",
        "scope": "industry",
        "tone": "positiv",
    },
]

MOCK_MARKET_DATA = {
    "rates": {"USD": 10.52, "EUR": 11.21},
    "stocks": {"MOWI": {"price": 214.20, "change_pct": 3.8}},
    "salmon_prices": {"nasdaq": {"price_nok_kg": 78.0}},
}

MOCK_CALENDAR = [
    {"title": "Mowi Q1 2026 kvartalsrapport", "event_date": "2026-05-20T06:00:00+00:00",
     "event_type": "q-report", "company": "Mowi", "description": ""},
]

VALID_DRAFT_DICT = {
    "utgave_nummer": 24,
    "publisert_dato": "2026-05-16",
    "uke_nummer": 20,
    "editor_note": "En uke preget av fallende laksepriser og økt regulatorisk press i Norge.",
    "hovedsak": {
        "tag_region": "Norge",
        "tag_kategori": "Regulering",
        "headline": "Lakselus-alarm i Troms truer MTB-kvoter",
        "deck": "Mattilsynet varsler tiltak mot seks Cermaq-anlegg i Troms etter rekordhøye lusenivåer.",
        "body_paragraphs": [
            "Mattilsynet har varslet pålegg mot seks oppdrettsanlegg i Troms etter at "
            "lakselusnivåene oversteg grenseverdiene tre uker på rad. (E24)",
            "Cermaq bekrefter at selskapet er i dialog med Mattilsynet og implementerer "
            "tiltak, inkludert brønnbåtbehandling. (iLaks)",
            "Bransjens organisasjon Sjømat Norge mener situasjonen er håndterbar, men "
            "NGO-stemmer er ikke representert i denne ukens dekning.",
        ],
        "pullquote": {
            "text": "Situasjonen er alvorlig, men vi har verktøyene til å håndtere det",
            "attribution": "Kari Olsen · kommunikasjonsdirektør · Cermaq",
        },
        "sidebar": {
            "label": "Bakgrunn",
            "title": "Lakselus-regulering i Norge",
            "paragraphs": [
                "Norge deler kystlinjen inn i produksjonsområder. Overskrides lusenivåene "
                "i et område, kan selskapene miste retten til å vokse.",
            ],
        },
        "sources": [
            {"ordinal": "i", "publication": "E24",
             "title": "Lakselus-problem øker i Troms", "url": "https://e24.no/lakseoppdrett/lakselus-troms"},
            {"ordinal": "ii", "publication": "iLaks",
             "title": "Cermaq varsler ny slakteri i Steigen", "url": "https://ilaks.no/cermaq-slakteri-steigen"},
        ],
    },
    "sekundaere_hovedsaker": [
        {
            "tag_region": "Globalt",
            "headline": "Laksepris faller 8 % — markedet reagerer",
            "deck": "Spotpriser for norsk laks (3–6 kg) falt til NOK 78 per kilo denne uken.",
            "body_paragraphs": [
                "Spotprisen for norsk atlantisk laks falt 8 prosent til NOK 78 per kilo "
                "fra forrige ukes NOK 84,7. (SalmonBusiness)",
                "Analytikere hos Pareto peker på svekket etterspørsel fra Frankrike og "
                "USA som viktigste årsaker.",
            ],
            "sources_compact": "SalmonBusiness · Pareto",
        }
    ],
    "marked": {
        "tickers": [
            {"label": "Spot 3-6 kg", "value": "78.00", "unit": "NOK",
             "change_value": "-6.70", "change_pct": "-7.9%", "direction": "down"},
        ],
        "analysis_headline": "Lakseprisfall presser marginer",
        "analysis_paragraphs": ["Fallende spotpriser kombinert med økte fôrkostnader "
                                 "kan redusere EBIT-marginer i Q2 2026."],
        "factoids": [{"big_number": "-8%", "description": "Spotpris NOK 78/kg vs NOK 84,7 forrige uke"}],
    },
    "finans": {
        "rows": [
            {"company": "Mowi", "ticker": "MOWI", "price": "214.20", "currency": "NOK",
             "week_pct": "+3.8%", "ytd_pct": "+12.4%", "direction_week": "up",
             "direction_ytd": "up", "note": "Mowi opp tross prisfall — investorer tror på kostnadseffektivitet"},
        ],
        "takeaway": "Mowi steg 3,8 % tross prisfall, noe som indikerer at markedet priser inn "
                    "kostnadsfordeler fremfor spotpris-eksponering.",
    },
    "cermaq": [
        {"type": "tredjepart", "headline": "Cermaq-anlegg under tilsyn i Troms",
         "body": "Mattilsynet gjennomfører tilsyn ved seks Cermaq-anlegg etter rekordhøye lusenivåer.",
         "meta": "E24 · 14. mai 2026"},
    ],
    "kalender": [
        {"date_label": "Ons 20.05", "title": "Mowi Q1 2026 kvartalsrapport",
         "detail": "Rapportering for første kvartal 2026."},
    ],
}


# ── Schema validation tests ───────────────────────────────────────────────────

class TestValidateDraft:
    def test_valid_draft_parses_correctly(self):
        draft = validate_draft(VALID_DRAFT_DICT)
        assert isinstance(draft, BulletinDraft)
        assert draft.utgave_nummer == 24
        assert draft.publisert_dato == "2026-05-16"
        assert draft.hovedsak.headline == "Lakselus-alarm i Troms truer MTB-kvoter"
        assert len(draft.sekundaere_hovedsaker) == 1
        assert len(draft.marked.tickers) == 1
        assert len(draft.finans.rows) == 1
        assert draft.hovedsak.pullquote is not None
        assert draft.hovedsak.sidebar is not None

    def test_missing_required_field_raises(self):
        bad = {**VALID_DRAFT_DICT}
        del bad["editor_note"]
        with pytest.raises(ValidationError, match="editor_note"):
            validate_draft(bad)

    def test_headline_too_long_raises(self):
        bad = {**VALID_DRAFT_DICT, "hovedsak": {**VALID_DRAFT_DICT["hovedsak"]}}
        bad["hovedsak"] = {**VALID_DRAFT_DICT["hovedsak"]}
        bad["hovedsak"]["headline"] = "A" * 61
        with pytest.raises(ValidationError, match="headline too long"):
            validate_draft(bad)

    def test_empty_body_paragraphs_raises(self):
        bad = {**VALID_DRAFT_DICT, "hovedsak": {**VALID_DRAFT_DICT["hovedsak"], "body_paragraphs": []}}
        with pytest.raises(ValidationError, match="body_paragraphs"):
            validate_draft(bad)

    def test_optional_pullquote_can_be_absent(self):
        without_pq = {**VALID_DRAFT_DICT,
                      "hovedsak": {**VALID_DRAFT_DICT["hovedsak"], "pullquote": None}}
        draft = validate_draft(without_pq)
        assert draft.hovedsak.pullquote is None

    def test_empty_cermaq_array_is_valid(self):
        with_empty = {**VALID_DRAFT_DICT, "cermaq": []}
        draft = validate_draft(with_empty)
        assert draft.cermaq == []


class TestValidateVerified:
    def test_valid_verified_includes_factcheck_report(self):
        data = {
            **VALID_DRAFT_DICT,
            "_factcheck_report": {
                "claims_verified": 12,
                "claims_corrected": 1,
                "claims_removed": 0,
                "issues": [
                    {"location": "marked.tickers[0].value",
                     "issue": "Price rounded differently",
                     "action": "corrected"},
                ],
            },
        }
        verified = validate_verified(data)
        assert isinstance(verified, BulletinVerified)
        assert verified._factcheck_report is not None
        assert verified._factcheck_report.claims_verified == 12
        assert len(verified._factcheck_report.issues) == 1

    def test_verified_without_factcheck_report_still_valid(self):
        verified = validate_verified(VALID_DRAFT_DICT)
        assert verified._factcheck_report is None


# ── Generator mock test ───────────────────────────────────────────────────────

class TestGenerator:
    def test_generator_returns_draft_on_valid_response(self, tmp_path, monkeypatch):
        """Generator parses a valid JSON response into BulletinDraft."""
        from poc.bulletin import generator

        # Point prompt path to a temp file
        prompt_file = tmp_path / "generator.md"
        prompt_file.write_text("Test system prompt", encoding="utf-8")
        monkeypatch.setattr(generator, "_PROMPT_PATH", prompt_file)

        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [
            MagicMock(type="text", text=json.dumps(VALID_DRAFT_DICT)),
        ]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("poc.bulletin.generator.anthropic.Anthropic", return_value=mock_client):
            draft = generator.run_generator(
                weekly_feed=MOCK_ARTICLES,
                market_data=MOCK_MARKET_DATA,
                calendar_data=MOCK_CALENDAR,
                utgave_nummer=24,
                api_key="test-key",
            )

        assert isinstance(draft, BulletinDraft)
        assert draft.utgave_nummer == 24
        assert draft.hovedsak.headline == "Lakselus-alarm i Troms truer MTB-kvoter"

    def test_generator_handles_tool_use_then_end_turn(self, tmp_path, monkeypatch):
        """Generator correctly handles tool_use round-trip before end_turn."""
        from poc.bulletin import generator

        prompt_file = tmp_path / "generator.md"
        prompt_file.write_text("System prompt", encoding="utf-8")
        monkeypatch.setattr(generator, "_PROMPT_PATH", prompt_file)

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "fetch_weekly_feed"
        tool_block.id = "tu_001"
        tool_block.input = {}

        turn1 = MagicMock()
        turn1.stop_reason = "tool_use"
        turn1.content = [tool_block]

        turn2 = MagicMock()
        turn2.stop_reason = "end_turn"
        turn2.content = [MagicMock(type="text", text=json.dumps(VALID_DRAFT_DICT))]

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [turn1, turn2]

        with patch("poc.bulletin.generator.anthropic.Anthropic", return_value=mock_client):
            draft = generator.run_generator(
                weekly_feed=MOCK_ARTICLES,
                market_data=MOCK_MARKET_DATA,
                calendar_data=MOCK_CALENDAR,
                utgave_nummer=24,
                api_key="test-key",
            )

        assert isinstance(draft, BulletinDraft)
        assert mock_client.messages.create.call_count == 2


# ── Factcheck mock test ───────────────────────────────────────────────────────

class TestFactcheck:
    def test_factcheck_corrects_wrong_claim(self, tmp_path, monkeypatch):
        """Factcheck returns verified JSON with corrections noted in report."""
        from poc.bulletin import factcheck

        prompt_file = tmp_path / "factcheck.md"
        prompt_file.write_text("Factcheck system prompt", encoding="utf-8")
        monkeypatch.setattr(factcheck, "_PROMPT_PATH", prompt_file)

        # Simulate factcheck correcting one claim
        corrected_data = {
            **VALID_DRAFT_DICT,
            "_factcheck_report": {
                "claims_verified": 10,
                "claims_corrected": 1,
                "claims_removed": 1,
                # The claim "Mitsubishi $180M" was NOT in source_pool — removed
                "issues": [
                    {
                        "location": "cermaq[0].body",
                        "issue": "Claim about $180M not found in source_pool",
                        "action": "removed",
                    }
                ],
            },
        }

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text=json.dumps(corrected_data))]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        draft = validate_draft(VALID_DRAFT_DICT)

        with patch("poc.bulletin.factcheck.anthropic.Anthropic", return_value=mock_client):
            verified = factcheck.run_factcheck(
                draft=draft,
                source_pool=MOCK_ARTICLES,
                api_key="test-key",
            )

        assert isinstance(verified, BulletinVerified)
        fc = verified._factcheck_report
        assert fc is not None
        assert fc.claims_corrected == 1
        assert fc.claims_removed == 1
        assert len(fc.issues) == 1
        assert fc.issues[0].action == "removed"


# ── Pipeline integration mock test ────────────────────────────────────────────

class TestPipeline:
    def test_pipeline_persists_to_index(self, tmp_path, monkeypatch):
        """End-to-end pipeline mock: generate → factcheck → index.json written."""
        from poc.bulletin import pipeline

        # Redirect data dir to tmp_path
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "_INDEX_FILE", tmp_path / "index.json")

        # Mock data fetchers
        monkeypatch.setattr(pipeline, "fetch_weekly_feed", lambda *a, **kw: MOCK_ARTICLES)
        monkeypatch.setattr(pipeline, "fetch_market_data", lambda: MOCK_MARKET_DATA)
        monkeypatch.setattr(pipeline, "fetch_calendar_next_week", lambda: MOCK_CALENDAR)

        # Mock generator and factcheck
        draft = validate_draft(VALID_DRAFT_DICT)
        verified_data = {
            **VALID_DRAFT_DICT,
            "_factcheck_report": {
                "claims_verified": 11, "claims_corrected": 0, "claims_removed": 0, "issues": []
            },
        }
        verified = validate_verified(verified_data)

        with (
            patch("poc.bulletin.pipeline.run_generator", return_value=draft),
            patch("poc.bulletin.pipeline.run_factcheck", return_value=verified),
        ):
            result = pipeline.generate_weekly_bulletin(
                week_start=date(2026, 5, 11),
                api_key="test-key",
            )

        assert isinstance(result, BulletinVerified)

        # Index file must exist
        index_file = tmp_path / "index.json"
        assert index_file.exists()
        index = json.loads(index_file.read_text())
        assert len(index) >= 1
        assert index[0]["nr"] == result.utgave_nummer
        assert index[0]["html_url"] == f"/bulletin/{result.utgave_nummer}"

        # Bulletin JSON file must exist
        bulletin_files = list(tmp_path.glob("bulletin-*.json"))
        assert len(bulletin_files) == 1
        content = json.loads(bulletin_files[0].read_text())
        assert content["utgave_nummer"] == result.utgave_nummer
