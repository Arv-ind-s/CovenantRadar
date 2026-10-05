"""Refresh the demo's snapshot of real Indian listed-company financials.

Run with the project's Python environment:

    python scripts/fetch_indian_financials.py            # rewrite the snapshot
    python scripts/fetch_indian_financials.py --probe TATASTEEL:C24 JSWSTEEL:C24

The demo seed (`covenant_radar.demo.curated`) never touches the network; it
reads the JSON this script writes.  Every figure comes from the XBRL a
company filed with the National Stock Exchange for its quarterly results:

* the profit and loss for each of the eight most recent quarters, and
* the balance sheet, which listed companies publish only with their
  half-year (30 September) and full-year (31 March) results.

Figures are converted from rupees to ₹ crore and otherwise stored exactly as
filed.  The seed documents its own derivations (EBIT before exceptional
items, tangible net worth, holding a half-year balance sheet across the
following quarter) on every statement line it writes.

`--probe` prints the three demo covenant ratios for candidate companies
without writing anything; that is how the roster below was chosen.  Refresh
the snapshot after each results season (mid-February, mid-May, mid-August,
mid-November): a quarter is treated as overdue 60 days after its successor
ends, so a snapshot ending June 2026 stays current until late November 2026.
"""

from __future__ import annotations

# Console output is the public interface of this standalone script.
# ruff: noqa: T201
import argparse
import json
import sys
import time
import xml.etree.ElementTree as ElementTree
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH: Final[Path] = ROOT / "src/covenant_radar/demo/data/indian_companies.json"
NSE_API: Final[str] = "https://www.nseindia.com/api"
QUARTER_COUNT: Final[int] = 8
#: Seconds between requests; NSE is a shared public service.
REQUEST_PAUSE_SECONDS: Final[float] = 1.0
CRORE: Final[Decimal] = Decimal("10000000")
_HEADERS: Final[dict[str, str]] = {
    # NSE rejects requests without a browser-like agent.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
_XBRLI: Final[str] = "{http://www.xbrl.org/2003/instance}"
#: NSE publishes every timestamp in Indian Standard Time.
_IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True, slots=True)
class RosterEntry:
    symbol: str
    industry_code: str
    #: The registered name in normal case; filings sometimes upper-case it.
    legal_name: str = ""
    #: The business group, only where its membership is not in doubt.
    business_group: str | None = None
    basis: str = "consolidated"


#: The demo book, in borrower order (`B-000001` … `B-000024`).
#:
#: Chosen with `--probe` by two rules, never by the outcome a company would
#: show: the company files the standard Ind AS format with a current and
#: non-current split and carries real borrowings (lenders, brokers and
#: debt-free companies are left out), and it met the demo's standard covenant
#: package — leverage at most 3.00x, interest cover at least 1.50x, current
#: ratio at least 1.20x — on its September 2024 results, as a lender would
#: require at sanction.  Whatever band a company shows is what its filings
#: since then say.  Agriculture, aviation and financial services had no
#: candidate meeting both rules, so real estate, auto components and retail
#: carry two companies each.  `business_group` is set only for groups whose
#: membership is not in doubt.
ROSTER: Final[tuple[RosterEntry, ...]] = (
    RosterEntry("SANDUMA", "B08", "Sandur Manganese & Iron Ores Limited"),
    RosterEntry("LTFOODS", "C10", "LT Foods Limited"),
    RosterEntry("WELSPUNLIV", "C13", "Welspun Living Limited", "Welspun Group"),
    RosterEntry("SUDARSCHEM", "C20", "Sudarshan Chemical Industries Limited"),
    RosterEntry("GRANULES", "C21", "Granules India Limited"),
    RosterEntry("JSL", "C24", "Jindal Stainless Limited"),
    RosterEntry("JINDALSAW", "C25", "Jindal Saw Limited"),
    RosterEntry("CRAFTSMAN", "C29", "Craftsman Automation Limited"),
    RosterEntry("UNOMINDA", "C29", "Uno Minda Limited"),
    RosterEntry("JSWENERGY", "D35", "JSW Energy Limited", "JSW Group"),
    RosterEntry("IONEXCHANG", "E36", "Ion Exchange (India) Limited"),
    RosterEntry("NCC", "F41", "NCC Limited"),
    RosterEntry("REDINGTON", "G46", "Redington Limited"),
    RosterEntry("ARVINDFASN", "G47", "Arvind Fashions Limited", "Lalbhai Group"),
    RosterEntry("TRENT", "G47", "Trent Limited", "Tata Group"),
    RosterEntry("TCI", "H49", "Transport Corporation of India Limited", "TCI Group"),
    RosterEntry("SEAMECLTD", "H50", "SEAMEC Limited"),
    RosterEntry("INDHOTEL", "I55", "The Indian Hotels Company Limited", "Tata Group"),
    RosterEntry("HFCL", "J61", "HFCL Limited"),
    RosterEntry("COFORGE", "J62", "Coforge Limited"),
    RosterEntry("PRESTIGE", "L68", "Prestige Estates Projects Limited"),
    RosterEntry("BRIGADE", "L68", "Brigade Enterprises Limited"),
    RosterEntry("QUESS", "M70", "Quess Corp Limited"),
    RosterEntry("SANGHVIMOV", "N77", "Sanghvi Movers Limited"),
)

#: Quarterly profit and loss, read from the three-month duration context.
_PROFIT_AND_LOSS: Final[tuple[str, ...]] = (
    "RevenueFromOperations",
    "OtherIncome",
    "Expenses",
    "FinanceCosts",
    "DepreciationDepletionAndAmortisationExpense",
    "ExceptionalItemsBeforeTax",
    "ProfitBeforeTax",
    "TaxExpense",
    "ProfitLossForPeriod",
)
#: Balance sheet, read from the period-end instant context when filed.
_BALANCE_SHEET: Final[tuple[str, ...]] = (
    "CashAndCashEquivalents",
    "CurrentInvestments",
    "Inventories",
    "TradeReceivablesCurrent",
    "CurrentAssets",
    "Assets",
    "TradePayablesCurrent",
    "BorrowingsCurrent",
    "CurrentLiabilities",
    "BorrowingsNoncurrent",
    "Liabilities",
    "EquityAttributableToOwnersOfParent",
    "NonControllingInterest",
    "Equity",
    "Goodwill",
    "OtherIntangibleAssets",
)
_REQUIRED_PROFIT_AND_LOSS: Final[tuple[str, ...]] = (
    "RevenueFromOperations",
    "FinanceCosts",
    "DepreciationDepletionAndAmortisationExpense",
    "ProfitBeforeTax",
)
_REQUIRED_BALANCE_SHEET: Final[tuple[str, ...]] = (
    "CurrentAssets",
    "CurrentLiabilities",
    "EquityAttributableToOwnersOfParent",
)


class SnapshotError(RuntimeError):
    """A filing did not have the shape this script relies on."""


@dataclass(frozen=True, slots=True)
class Filing:
    period_end: date
    xbrl_url: str
    filed_at: str
    audited: str


def _nse_time(text: str, layout: str) -> datetime:
    return datetime.strptime(text, layout).replace(tzinfo=_IST)


def _quarter_ends(latest: date, count: int) -> tuple[date, ...]:
    ends = [latest]
    while len(ends) < count:
        year, month = ends[-1].year, ends[-1].month - 3
        if month <= 0:
            year, month = year - 1, month + 12
        ends.append(date(year, month, monthrange(year, month)[1]))
    return tuple(reversed(ends))


class NseClient:
    def __init__(self) -> None:
        self._client = httpx.Client(headers=_HEADERS, timeout=40, follow_redirects=True)

    def __enter__(self) -> NseClient:
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def get(self, url: str) -> httpx.Response:
        time.sleep(REQUEST_PAUSE_SECONDS)
        for attempt in range(3):
            response = self._client.get(url)
            if response.status_code == 200:
                return response
            time.sleep(3 * (attempt + 1))
        response.raise_for_status()
        return response

    def filings(self, entry: RosterEntry) -> dict[date, Filing]:
        """Every quarterly results filing NSE lists for one company and basis.

        Results up to December 2024 are listed by the original results API;
        from the March 2025 quarter SEBI's integrated filing replaced it.  A
        revised filing supersedes the original for the same quarter.
        """

        consolidated = entry.basis == "consolidated"
        found: dict[date, tuple[str, Filing]] = {}

        legacy = self.get(
            f"{NSE_API}/corporates-financial-results?index=equities"
            f"&symbol={entry.symbol}&period=Quarterly"
        ).json()
        for row in legacy if isinstance(legacy, list) else []:
            if (row.get("consolidated") == "Consolidated") != consolidated:
                continue
            if row.get("cumulative") != "Non-cumulative" or not row.get("xbrl", "").endswith(
                ".xml"
            ):
                continue
            period_end = _nse_time(row["toDate"], "%d-%b-%Y").date()
            filed_at = _nse_time(row["broadCastDate"], "%d-%b-%Y %H:%M:%S").isoformat()
            filing = Filing(period_end, row["xbrl"], filed_at, row.get("audited", ""))
            if period_end not in found or found[period_end][0] < filed_at:
                found[period_end] = (filed_at, filing)

        integrated = self.get(
            f"{NSE_API}/integrated-filing-results?index=equities&symbol={entry.symbol}"
            "&type=Integrated%20Filing-%20Financials&size=100"
        ).json()
        for row in integrated.get("data", []):
            if (row.get("consolidated") == "Consolidated") != consolidated:
                continue
            if not str(row.get("xbrl", "")).endswith(".xml"):
                continue
            period_end = _nse_time(row["qe_Date"].title(), "%d-%b-%Y").date()
            filed_at = _nse_time(row["creation_Date"], "%d-%b-%Y %H:%M:%S").isoformat()
            filing = Filing(period_end, row["xbrl"], filed_at, row.get("audited", ""))
            if period_end not in found or found[period_end][0] < filed_at:
                found[period_end] = (filed_at, filing)
        return {period_end: filing for period_end, (_, filing) in found.items()}


def _crore(text: str) -> str:
    return str((Decimal(text.strip()) / CRORE).quantize(Decimal("0.01")))


def parse_filing(content: bytes, period_end: date) -> dict[str, object]:
    """Read one quarter's figures from an NSE results XBRL instance.

    Contexts are matched by their dates, not by their ids (which differ
    between the original and the integrated filing formats), and only
    contexts without a segment or scenario count: those are the entity-wide
    totals rather than a dimensional breakdown.  The quarter is the shortest
    entity-wide duration ending on the quarter end — the results' first
    column — because a few filers mistag its start date (Welspun Corp's
    March 2025 quarter is tagged as starting in October); the year-to-date
    and full-year columns are always longer.
    """

    root = ElementTree.fromstring(content)
    durations: dict[str, set[str]] = {}
    instant_contexts: set[str] = set()
    for context in root.iter(f"{_XBRLI}context"):
        if context.find(f"{_XBRLI}entity/{_XBRLI}segment") is not None:
            continue
        if context.find(f"{_XBRLI}scenario") is not None:
            continue
        period = context.find(f"{_XBRLI}period")
        if period is None:
            continue
        start = period.findtext(f"{_XBRLI}startDate")
        end = period.findtext(f"{_XBRLI}endDate")
        instant = period.findtext(f"{_XBRLI}instant")
        if start and end == period_end.isoformat():
            durations.setdefault(start, set()).add(context.attrib["id"])
        if instant == period_end.isoformat():
            instant_contexts.add(context.attrib["id"])
    quarter_contexts = durations[max(durations)] if durations else set()
    if durations and date.fromisoformat(max(durations)) < period_end - timedelta(days=200):
        raise SnapshotError(f"{period_end}: the filing has no quarter or half-year column.")

    def facts(names: tuple[str, ...], contexts: set[str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for element in root:
            name = element.tag.rsplit("}", 1)[-1]
            if name in names and element.attrib.get("contextRef") in contexts:
                if element.text and element.text.strip():
                    values.setdefault(name, _crore(element.text))
        return values

    profit_and_loss = facts(_PROFIT_AND_LOSS, quarter_contexts)
    missing = [name for name in _REQUIRED_PROFIT_AND_LOSS if name not in profit_and_loss]
    if missing:
        raise SnapshotError(f"{period_end}: the filing has no quarterly {missing}.")
    balance_sheet = facts(_BALANCE_SHEET, instant_contexts)
    if not all(name in balance_sheet for name in _REQUIRED_BALANCE_SHEET):
        balance_sheet = {}
    names: dict[str, str] = {}
    for element in root:
        name = element.tag.rsplit("}", 1)[-1]
        if name in {"NameOfTheCompany", "ISIN"} and element.text and element.text.strip():
            # The first is the entity's own; later ones can describe a listing.
            names.setdefault(name, element.text.strip())
    return {
        "profit_and_loss": profit_and_loss,
        "balance_sheet": balance_sheet or None,
        "company_name": names.get("NameOfTheCompany", ""),
        "isin": names.get("ISIN") or None,
    }


def fetch_company(
    client: NseClient, entry: RosterEntry, quarter_ends: tuple[date, ...]
) -> dict[str, object]:
    filings = client.filings(entry)
    missing = [end.isoformat() for end in quarter_ends if end not in filings]
    if missing:
        raise SnapshotError(f"{entry.symbol}: NSE lists no {entry.basis} results for {missing}.")
    quarters: list[dict[str, object]] = []
    legal_name = ""
    isin: object = None
    for period_end in quarter_ends:
        filing = filings[period_end]
        parsed = parse_filing(client.get(filing.xbrl_url).content, period_end)
        legal_name = str(parsed["company_name"]) or legal_name
        isin = parsed["isin"] or isin
        quarters.append(
            {
                "period_end": period_end.isoformat(),
                "filing": {
                    "xbrl_url": filing.xbrl_url,
                    "filed_at": filing.filed_at,
                    "audited": filing.audited,
                },
                "profit_and_loss": parsed["profit_and_loss"],
                "balance_sheet": parsed["balance_sheet"],
            }
        )
    if quarters[0]["balance_sheet"] is None:
        raise SnapshotError(
            f"{entry.symbol}: the oldest quarter needs a filed balance sheet to start from."
        )
    return {
        "symbol": entry.symbol,
        "legal_name": entry.legal_name or legal_name,
        "filed_name": legal_name,
        "business_group": entry.business_group,
        "isin": isin,
        "industry_code": entry.industry_code,
        "basis": entry.basis,
        "source_url": f"https://www.nseindia.com/get-quotes/equity?symbol={entry.symbol}",
        "quarters": quarters,
    }


def covenant_ratios(company: dict[str, object]) -> list[tuple[str, str, str, str]]:
    """``(period_end, leverage, interest cover, current ratio)`` per quarter.

    The same derivations the demo seed applies (`demo/curated.py`), kept
    here so `--probe` shows what the queue will see before a company joins.
    """

    rows: list[tuple[str, str, str, str]] = []
    sheet: dict[str, str] = {}
    for quarter in company["quarters"]:  # type: ignore[attr-defined]
        sheet = quarter["balance_sheet"] or sheet
        pnl = quarter["profit_and_loss"]

        def d(values: dict[str, str], key: str) -> Decimal:
            return Decimal(values.get(key, "0"))

        tnw = (
            d(sheet, "EquityAttributableToOwnersOfParent")
            - d(sheet, "Goodwill")
            - d(sheet, "OtherIntangibleAssets")
        )
        debt = d(sheet, "BorrowingsCurrent") + d(sheet, "BorrowingsNoncurrent")
        finance = d(pnl, "FinanceCosts")
        ebit = (
            d(pnl, "RevenueFromOperations") + d(pnl, "OtherIncome") - d(pnl, "Expenses") + finance
        )
        leverage = f"{debt / tnw:.2f}" if tnw > 0 else "neg"
        cover = f"{ebit / finance:.2f}" if finance else "n/a"
        current_liabilities = d(sheet, "CurrentLiabilities")
        current = (
            f"{d(sheet, 'CurrentAssets') / current_liabilities:.2f}"
            if current_liabilities
            else "n/a"
        )
        rows.append((str(quarter["period_end"]), leverage, cover, current))
    return rows


def _parse_entry(text: str) -> RosterEntry:
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise argparse.ArgumentTypeError("Use SYMBOL:INDUSTRY[:standalone].")
    return RosterEntry(parts[0], parts[1], basis=parts[2] if len(parts) == 3 else "consolidated")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--probe", nargs="+", type=_parse_entry, metavar="SYMBOL:INDUSTRY")
    parser.add_argument(
        "--latest-quarter",
        type=date.fromisoformat,
        default=date(2026, 6, 30),
        help="The most recent quarter end every company has filed (default 2026-06-30).",
    )
    args = parser.parse_args()
    entries: tuple[RosterEntry, ...] = tuple(args.probe) if args.probe else ROSTER
    if not entries:
        parser.error("The roster is empty; pass --probe SYMBOL:INDUSTRY ...")
    quarter_ends = _quarter_ends(args.latest_quarter, QUARTER_COUNT)

    companies: list[dict[str, object]] = []
    with NseClient() as client:
        for entry in entries:
            try:
                company = fetch_company(client, entry, quarter_ends)
            except (httpx.HTTPError, SnapshotError, ValueError, ElementTree.ParseError) as error:
                print(f"{entry.symbol}: {error}", file=sys.stderr, flush=True)
                if not args.probe:
                    return 1
                continue
            companies.append(company)
            if args.probe:
                print(f"\n{entry.industry_code} {entry.symbol} — {company['legal_name']}")
                print("  period       lev   icr   cur")
                for row in covenant_ratios(company):
                    print(f"  {row[0]}  {row[1]:>5} {row[2]:>5} {row[3]:>5}", flush=True)

    if args.probe:
        return 0
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "schema": "indian-companies-v1",
        "retrieved_on": datetime.now(_IST).date().isoformat(),
        "source": "Quarterly results XBRL filed with the National Stock Exchange of India",
        "unit": "INR crore",
        "quarter_ends": [end.isoformat() for end in quarter_ends],
        "companies": companies,
    }
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(companies)} companies to {SNAPSHOT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
