"""Bulletin JSON schema — validation mirrors Zod schema from the TS spec."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Leaf types ────────────────────────────────────────────────────────────────

@dataclass
class Source:
    ordinal: str
    publication: str
    title: str
    url: str


@dataclass
class Pullquote:
    text: str
    attribution: str


@dataclass
class Sidebar:
    label: str   # Kontekst | Bakgrunn | Tidslinje
    title: str
    paragraphs: list[str]


@dataclass
class Ticker:
    label: str
    value: str
    unit: str
    change_value: str
    change_pct: str
    direction: str   # up | down | flat


@dataclass
class Factoid:
    big_number: str
    description: str


@dataclass
class FinansRow:
    company: str
    ticker: str
    price: str
    currency: str
    week_pct: str
    ytd_pct: str
    direction_week: str   # up | down | flat
    direction_ytd: str
    note: str


@dataclass
class CermaQItem:
    type: str           # tredjepart | cermaq_egen
    headline: str
    body: str
    meta: str


@dataclass
class KalenderItem:
    date_label: str
    title: str
    detail: str


# ── Composite types ───────────────────────────────────────────────────────────

@dataclass
class Hovedsak:
    tag_region: str        # Norge | Chile | Canada | Globalt
    tag_kategori: str      # Politikk | Regulering | Finans | Biologi | Marked | M&A | Annet
    headline: str
    deck: str
    body_paragraphs: list[str]
    sources: list[Source]
    pullquote: Pullquote | None = None
    sidebar: Sidebar | None = None


@dataclass
class SekundaerHovedsak:
    tag_region: str
    headline: str
    deck: str
    body_paragraphs: list[str]
    sources_compact: str


@dataclass
class Marked:
    tickers: list[Ticker]
    analysis_headline: str
    analysis_paragraphs: list[str]
    factoids: list[Factoid]


@dataclass
class Finans:
    rows: list[FinansRow]
    takeaway: str


@dataclass
class FactcheckIssue:
    location: str
    issue: str
    action: str   # corrected | removed


@dataclass
class FactcheckReport:
    claims_verified: int
    claims_corrected: int
    claims_removed: int
    issues: list[FactcheckIssue]


# ── Root ──────────────────────────────────────────────────────────────────────

@dataclass
class BulletinDraft:
    utgave_nummer: int
    publisert_dato: str       # YYYY-MM-DD
    uke_nummer: int
    editor_note: str
    hovedsak: Hovedsak
    sekundaere_hovedsaker: list[SekundaerHovedsak]
    marked: Marked
    finans: Finans
    cermaq: list[CermaQItem]
    kalender: list[KalenderItem]


@dataclass
class BulletinVerified(BulletinDraft):
    _factcheck_report: FactcheckReport | None = None


# ── Validation ────────────────────────────────────────────────────────────────

class ValidationError(Exception):
    pass


def _require(obj: dict, *keys: str, context: str = "") -> None:
    for k in keys:
        if k not in obj:
            raise ValidationError(f"Missing required field '{k}'" + (f" in {context}" if context else ""))


def validate_draft(data: dict) -> BulletinDraft:
    """Parse and validate raw dict into BulletinDraft. Raises ValidationError on failure."""
    _require(data, "utgave_nummer", "publisert_dato", "uke_nummer", "editor_note",
             "hovedsak", "sekundaere_hovedsaker", "marked", "finans", "cermaq", "kalender")

    hs_raw = data["hovedsak"]
    _require(hs_raw, "tag_region", "tag_kategori", "headline", "deck",
             "body_paragraphs", "sources", context="hovedsak")

    if not isinstance(hs_raw["body_paragraphs"], list) or len(hs_raw["body_paragraphs"]) < 1:
        raise ValidationError("hovedsak.body_paragraphs must be a non-empty list")

    pq_raw = hs_raw.get("pullquote")
    pullquote = Pullquote(**pq_raw) if pq_raw else None

    sb_raw = hs_raw.get("sidebar")
    sidebar = Sidebar(**sb_raw) if sb_raw else None

    hovedsak = Hovedsak(
        tag_region=hs_raw["tag_region"],
        tag_kategori=hs_raw["tag_kategori"],
        headline=hs_raw["headline"],
        deck=hs_raw["deck"],
        body_paragraphs=hs_raw["body_paragraphs"],
        sources=[Source(**s) for s in hs_raw["sources"]],
        pullquote=pullquote,
        sidebar=sidebar,
    )

    sekundaere = []
    for i, s in enumerate(data["sekundaere_hovedsaker"]):
        _require(s, "tag_region", "headline", "deck", "body_paragraphs", "sources_compact",
                 context=f"sekundaere_hovedsaker[{i}]")
        sekundaere.append(SekundaerHovedsak(**s))

    m_raw = data["marked"]
    _require(m_raw, "tickers", "analysis_headline", "analysis_paragraphs", "factoids",
             context="marked")
    marked = Marked(
        tickers=[Ticker(**t) for t in m_raw["tickers"]],
        analysis_headline=m_raw["analysis_headline"],
        analysis_paragraphs=m_raw["analysis_paragraphs"],
        factoids=[Factoid(**f) for f in m_raw["factoids"]],
    )

    f_raw = data["finans"]
    _require(f_raw, "rows", "takeaway", context="finans")
    finans = Finans(
        rows=[FinansRow(**r) for r in f_raw["rows"]],
        takeaway=f_raw["takeaway"],
    )

    cermaq = [CermaQItem(**c) for c in data["cermaq"]]
    kalender = [KalenderItem(**k) for k in data["kalender"]]

    if len(headline := hs_raw["headline"]) > 60:
        raise ValidationError(f"hovedsak.headline too long ({len(headline)} chars, max 60)")

    return BulletinDraft(
        utgave_nummer=int(data["utgave_nummer"]),
        publisert_dato=data["publisert_dato"],
        uke_nummer=int(data["uke_nummer"]),
        editor_note=data["editor_note"],
        hovedsak=hovedsak,
        sekundaere_hovedsaker=sekundaere,
        marked=marked,
        finans=finans,
        cermaq=cermaq,
        kalender=kalender,
    )


def validate_verified(data: dict) -> BulletinVerified:
    """Parse factchecked response — same as draft but with _factcheck_report."""
    draft = validate_draft(data)
    fc_raw = data.get("_factcheck_report")
    fc = None
    if fc_raw:
        fc = FactcheckReport(
            claims_verified=int(fc_raw.get("claims_verified", 0)),
            claims_corrected=int(fc_raw.get("claims_corrected", 0)),
            claims_removed=int(fc_raw.get("claims_removed", 0)),
            issues=[FactcheckIssue(**i) for i in fc_raw.get("issues", [])],
        )
    verified = BulletinVerified(
        utgave_nummer=draft.utgave_nummer,
        publisert_dato=draft.publisert_dato,
        uke_nummer=draft.uke_nummer,
        editor_note=draft.editor_note,
        hovedsak=draft.hovedsak,
        sekundaere_hovedsaker=draft.sekundaere_hovedsaker,
        marked=draft.marked,
        finans=draft.finans,
        cermaq=draft.cermaq,
        kalender=draft.kalender,
        _factcheck_report=fc,
    )
    return verified
