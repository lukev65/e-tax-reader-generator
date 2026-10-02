"""Generic CSV importer.

Reads a single broker-neutral CSV file (one row per fact: a balance, a trade,
an interest payment, a dividend, an expense, ...) and turns it into a
TaxStatement.  The format is documented in ``docs/importer_csv.md``.

Key design choices
------------------
* The CSV carries only raw broker facts in the original currency.  Exchange
  rates, A/B classification, withholding tax claims and Kursliste values are
  left to the calculation phase, exactly as for the broker importers.
* Column names and ``tipo`` values are Italian (the format was designed for
  e-tax Ticino); English aliases are accepted for both.
* Bank account country defaults to the IBAN prefix and security country to
  the ISIN prefix; the optional ``paese`` column overrides either.
* ``SALDO_INIZIALE`` is the position at the *start* of its date,
  ``SALDO_FINALE`` the position at the *end* of its date (it is stored with
  ``referenceDate = date + 1`` as eCH-0196 balance stocks require).
* Every problem is reported with its CSV line number; all problems are
  collected before raising so the user can fix the file in one go.
"""

import csv
import io
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from opensteuerauszug.importers.common import (
    PositionHints,
    SecurityNameRegistry,
    SecurityPositionData,
    augment_list_of_securities,
    build_client,
    build_security_payment,
    parse_swiss_canton,
    resolve_first_last_name,
)
from opensteuerauszug.model.ech0196 import (
    BankAccount,
    BankAccountName,
    BankAccountNumber,
    BankAccountPayment,
    BankAccountTaxValue,
    DepotNumber,
    Expense,
    Institution,
    ISINType,
    LiabilityAccount,
    LiabilityAccountPayment,
    LiabilityAccountTaxValue,
    ListOfBankAccounts,
    ListOfExpenses,
    ListOfLiabilities,
    SecurityCategory,
    SecurityStock,
    TaxStatement,
    ValorNumber,
)
from opensteuerauszug.model.position import SecurityPosition

logger = logging.getLogger(__name__)

COLUMNS = [
    "tipo",
    "data",
    "conto",
    "deposito",
    "isin",
    "valor",
    "descrizione",
    "categoria",
    "valuta",
    "quantita",
    "prezzo",
    "importo",
    "data_ex",
    "ritenuta",
    "paese",
]

# English aliases for the column headers.
_COLUMN_ALIASES = {
    "type": "tipo",
    "date": "data",
    "account": "conto",
    "iban": "conto",
    "depot": "deposito",
    "valor_number": "valor",
    "description": "descrizione",
    "name": "descrizione",
    "category": "categoria",
    "currency": "valuta",
    "quantity": "quantita",
    "quantità": "quantita",
    "price": "prezzo",
    "amount": "importo",
    "ex_date": "data_ex",
    "withholding": "ritenuta",
    "country": "paese",
}

_ROW_TYPES = {
    "CONTO": "CONTO",
    "ACCOUNT": "CONTO",
    "INTERESSE": "INTERESSE",
    "INTEREST": "INTERESSE",
    "DEBITO": "DEBITO",
    "LIABILITY": "DEBITO",
    "INTERESSE_PASSIVO": "INTERESSE_PASSIVO",
    "DEBT_INTEREST": "INTERESSE_PASSIVO",
    "SPESA": "SPESA",
    "EXPENSE": "SPESA",
    "SALDO_INIZIALE": "SALDO_INIZIALE",
    "OPENING_BALANCE": "SALDO_INIZIALE",
    "ACQUISTO": "ACQUISTO",
    "BUY": "ACQUISTO",
    "VENDITA": "VENDITA",
    "SELL": "VENDITA",
    "DIVIDENDO": "DIVIDENDO",
    "DIVIDEND": "DIVIDENDO",
    "REINVESTIMENTO": "REINVESTIMENTO",
    "REINVESTMENT": "REINVESTIMENTO",
    "GUADAGNO_CAPITALE": "GUADAGNO_CAPITALE",
    "CAPITAL_GAIN": "GUADAGNO_CAPITALE",
    "SALDO_FINALE": "SALDO_FINALE",
    "CLOSING_BALANCE": "SALDO_FINALE",
}

_REQUIRED: Dict[str, Tuple[str, ...]] = {
    "CONTO": ("data", "conto", "valuta", "importo"),
    "INTERESSE": ("data", "conto", "valuta", "importo"),
    "DEBITO": ("data", "conto", "valuta", "importo"),
    "INTERESSE_PASSIVO": ("data", "conto", "valuta", "importo"),
    "SPESA": ("data", "valuta", "importo", "descrizione"),
    "SALDO_INIZIALE": ("data", "deposito", "isin", "valuta", "quantita"),
    "ACQUISTO": ("data", "deposito", "isin", "valuta", "quantita", "prezzo"),
    "VENDITA": ("data", "deposito", "isin", "valuta", "quantita", "prezzo"),
    "DIVIDENDO": ("data", "deposito", "isin", "valuta", "importo"),
    "REINVESTIMENTO": ("data", "deposito", "isin", "valuta", "importo"),
    "GUADAGNO_CAPITALE": ("data", "deposito", "isin", "valuta", "importo"),
    "SALDO_FINALE": ("data", "deposito", "isin", "quantita"),
}

_CATEGORIES: Dict[str, SecurityCategory] = {
    "AZIONE": "SHARE",
    "SHARE": "SHARE",
    "FONDO": "FUND",
    "FUND": "FUND",
    "ETF": "FUND",
    "OBBLIGAZIONE": "BOND",
    "BOND": "BOND",
    "OPZIONE": "OPTION",
    "OPTION": "OPTION",
    "STRUTTURATO": "DEVT",
    "STRUCTURED": "DEVT",
    "ALTRO": "OTHER",
    "OTHER": "OTHER",
}

_PAYMENT_LABELS = {
    "DIVIDENDO": "Dividend",
    "REINVESTIMENTO": "Reinvestment",
    "GUADAGNO_CAPITALE": "Capital gain",
}

# eCH-0196 expenseType used when the CSV gives no numeric code in ``categoria``.
_DEFAULT_EXPENSE_TYPE = "99"
_EXPENSE_TYPE_RE = re.compile(r"^(\d{1,2})$")

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


class CsvImportError(ValueError):
    """Raised when the CSV file has one or more invalid rows."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__(
            f"{len(problems)} problem(s) in CSV file:\n" + "\n".join(f"  - {p}" for p in problems)
        )


@dataclass
class CsvRow:
    line: int
    kind: str
    values: Dict[str, str]

    def get(self, column: str) -> str:
        return self.values.get(column, "").strip()


@dataclass
class _Account:
    iban: str
    currency: Optional[str] = None
    name: Optional[str] = None
    country: Optional[str] = None
    balance: Optional[Tuple[date, Decimal]] = None
    payments: List[Tuple[date, Decimal, str]] = field(default_factory=list)


def _parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"data non valida {value!r} (usare AAAA-MM-GG o GG.MM.AAAA)")


def _parse_decimal(value: str) -> Decimal:
    cleaned = value.replace("'", "").replace("’", "").replace(" ", "")
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"numero non valido {value!r}")


def _country_from_code(code: str) -> Optional[str]:
    prefix = code[:2].upper()
    return prefix if _COUNTRY_RE.match(prefix) else None


def read_csv_rows(text: str) -> List[CsvRow]:
    """Parse CSV text into typed rows, validating headers, types and required fields."""
    if text.startswith("﻿"):
        text = text[1:]
    first_line = text.split("\n", 1)[0]
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    try:
        header = next(reader)
    except StopIteration:
        raise CsvImportError(["il file è vuoto"])
    columns = [_COLUMN_ALIASES.get(h.strip().lower(), h.strip().lower()) for h in header]
    problems: List[str] = []
    unknown = [c for c in columns if c and c not in COLUMNS]
    if unknown:
        problems.append(f"riga 1: colonne sconosciute {', '.join(unknown)}")
    if "tipo" not in columns:
        raise CsvImportError(problems + ["riga 1: manca la colonna 'tipo'"])

    rows: List[CsvRow] = []
    for line_no, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue
        values = {col: raw[i] if i < len(raw) else "" for i, col in enumerate(columns) if col}
        kind_raw = values.get("tipo", "").strip().upper()
        kind = _ROW_TYPES.get(kind_raw)
        if kind is None:
            problems.append(f"riga {line_no}: tipo sconosciuto {kind_raw!r}")
            continue
        row = CsvRow(line=line_no, kind=kind, values=values)
        missing = [c for c in _REQUIRED[kind] if not row.get(c)]
        if missing:
            problems.append(f"riga {line_no} ({kind}): mancano {', '.join(missing)}")
            continue
        problems.extend(_check_formats(row))
        rows.append(row)

    if problems:
        raise CsvImportError(problems)
    return rows


def _check_formats(row: CsvRow) -> List[str]:
    problems = []
    prefix = f"riga {row.line} ({row.kind})"
    for column in ("data", "data_ex"):
        if row.get(column):
            try:
                _parse_date(row.get(column))
            except ValueError as e:
                problems.append(f"{prefix}: {e}")
    for column in ("quantita", "prezzo", "importo", "ritenuta"):
        if row.get(column):
            try:
                _parse_decimal(row.get(column))
            except ValueError as e:
                problems.append(f"{prefix}: {e}")
    if row.get("valuta") and not _CURRENCY_RE.match(row.get("valuta").upper()):
        problems.append(f"{prefix}: valuta non valida {row.get('valuta')!r}")
    if row.get("isin") and not _ISIN_RE.match(row.get("isin").upper()):
        problems.append(f"{prefix}: ISIN non valido {row.get('isin')!r}")
    if row.get("valor"):
        if not row.get("valor").isdigit() or int(row.get("valor")) < 100:
            problems.append(f"{prefix}: numero di valore non valido {row.get('valor')!r}")
    if row.get("paese") and not _COUNTRY_RE.match(row.get("paese").upper()):
        problems.append(f"{prefix}: paese non valido {row.get('paese')!r} (codice ISO a 2 lettere)")
    if row.get("categoria") and row.kind not in ("SPESA", "CONTO", "DEBITO"):
        if row.get("categoria").upper() not in _CATEGORIES:
            problems.append(
                f"{prefix}: categoria non valida {row.get('categoria')!r} "
                "(AZIONE, FONDO, OBBLIGAZIONE, OPZIONE, STRUTTURATO, ALTRO)"
            )
    if row.kind == "SPESA" and row.get("categoria"):
        if not _EXPENSE_TYPE_RE.match(row.get("categoria")):
            problems.append(f"{prefix}: per le spese 'categoria' è il codice eCH-0196 (1-44 o 99)")
    return problems


class CsvImporter:
    """Import a generic CSV file (see ``docs/importer_csv.md``) for a tax period."""

    def __init__(
        self,
        period_from: date,
        period_to: date,
        full_name: Optional[str] = None,
        canton: Optional[str] = None,
        client_number: Optional[str] = None,
        institution_name: str = "",
    ) -> None:
        self.period_from = period_from
        self.period_to = period_to
        self.full_name = full_name
        self.canton = canton
        self.client_number = client_number
        self.institution_name = institution_name

    def import_file(self, filename: str) -> TaxStatement:
        with open(filename, encoding="utf-8-sig") as f:
            return self.import_text(f.read())

    def import_text(self, text: str) -> TaxStatement:
        rows = read_csv_rows(text)
        problems: List[str] = []

        accounts: Dict[str, _Account] = {}
        liabilities: Dict[str, _Account] = {}
        expenses: List[Expense] = []
        name_registry = SecurityNameRegistry()
        positions: Dict[SecurityPosition, SecurityPositionData] = defaultdict(
            lambda: SecurityPositionData({"stocks": [], "payments": []})
        )
        categories: Dict[SecurityPosition, SecurityCategory] = {}
        countries: Dict[SecurityPosition, str] = {}
        positions_by_key: Dict[Tuple[str, str], SecurityPosition] = {}

        for row in rows:
            d = _parse_date(row.get("data"))
            currency = row.get("valuta").upper()
            if row.kind in ("CONTO", "INTERESSE"):
                self._account_row(row, d, currency, accounts, problems)
            elif row.kind in ("DEBITO", "INTERESSE_PASSIVO"):
                self._account_row(row, d, currency, liabilities, problems)
            elif row.kind == "SPESA":
                amount = _parse_decimal(row.get("importo"))
                expenses.append(
                    Expense(
                        referenceDate=d,
                        name=row.get("descrizione")[:200],
                        iban=row.get("conto") or None,
                        depotNumber=(
                            DepotNumber(row.get("deposito")) if row.get("deposito") else None
                        ),
                        amountCurrency=currency,
                        amount=abs(amount),
                        expenseType=row.get("categoria") or _DEFAULT_EXPENSE_TYPE,
                    )
                )
            else:
                self._security_row(
                    row,
                    d,
                    currency,
                    positions,
                    positions_by_key,
                    name_registry,
                    categories,
                    countries,
                    problems,
                )

        if problems:
            raise CsvImportError(problems)

        statement = TaxStatement(
            minorVersion=22,
            periodFrom=self.period_from,
            periodTo=self.period_to,
            taxPeriod=self.period_from.year,
            listOfSecurities=None,
            listOfBankAccounts=None,
        )
        statement.institution = Institution(name=self.institution_name)
        canton = parse_swiss_canton(self.canton)
        if canton:
            statement.canton = canton
        first_name, last_name = resolve_first_last_name(full_name=self.full_name)
        client = build_client(self.client_number or "-", first_name, last_name)
        if client is not None:
            statement.client = [client]

        if positions:

            def _hints_for(sec_pos: SecurityPosition) -> PositionHints:
                return PositionHints(
                    security_category=categories.get(sec_pos, "SHARE"),
                    country=countries.get(sec_pos, "CH"),
                )

            augment_list_of_securities(
                statement,
                positions,
                name_registry=name_registry,
                hints_for=_hints_for,
                strict_consistency=False,
                run_initial_consistency_check=True,
                assume_zero_if_no_balances=True,
            )

        if accounts:
            statement.listOfBankAccounts = ListOfBankAccounts(
                bankAccount=[self._bank_account(acc) for acc in accounts.values()]
            )
        if liabilities:
            statement.listOfLiabilities = ListOfLiabilities(
                liabilityAccount=[self._liability_account(acc) for acc in liabilities.values()]
            )
        if expenses:
            statement.listOfExpenses = ListOfExpenses(
                expense=sorted(expenses, key=lambda e: e.referenceDate or self.period_to)
            )
        return statement

    # ------------------------------------------------------------------
    # Row handlers
    # ------------------------------------------------------------------

    def _account_row(
        self,
        row: CsvRow,
        d: date,
        currency: str,
        accounts: Dict[str, _Account],
        problems: List[str],
    ) -> None:
        iban = row.get("conto").replace(" ", "").upper()
        acc = accounts.setdefault(iban, _Account(iban=iban))
        if acc.currency is None:
            acc.currency = currency
        elif acc.currency != currency:
            problems.append(
                f"riga {row.line}: il conto {iban} è in {acc.currency}, non in {currency}"
            )
            return
        if row.get("descrizione"):
            acc.name = row.get("descrizione")
        if row.get("paese"):
            acc.country = row.get("paese").upper()
        amount = _parse_decimal(row.get("importo"))
        if row.kind in ("CONTO", "DEBITO"):
            if acc.balance is not None:
                problems.append(f"riga {row.line}: saldo del conto {iban} indicato due volte")
                return
            acc.balance = (d, abs(amount) if row.kind == "DEBITO" else amount)
        else:
            acc.payments.append(
                (d, abs(amount) if row.kind == "INTERESSE_PASSIVO" else amount, "Interest")
            )

    def _security_row(
        self,
        row: CsvRow,
        d: date,
        currency: str,
        positions: Dict[SecurityPosition, SecurityPositionData],
        positions_by_key: Dict[Tuple[str, str], SecurityPosition],
        name_registry: SecurityNameRegistry,
        categories: Dict[SecurityPosition, SecurityCategory],
        countries: Dict[SecurityPosition, str],
        problems: List[str],
    ) -> None:
        depot = row.get("deposito")
        isin = row.get("isin").upper()
        key = (depot, isin)
        sec_pos = positions_by_key.get(key)
        if sec_pos is None:
            sec_pos = SecurityPosition(
                depot=depot,
                isin=ISINType(isin),
                valor=ValorNumber(int(row.get("valor"))) if row.get("valor") else None,
                symbol=isin,
                description=row.get("descrizione") or isin,
            )
            positions_by_key[key] = sec_pos
            name_registry.update(sec_pos, isin, 1)
            countries[sec_pos] = _country_from_code(isin) or "CH"
        if row.get("descrizione"):
            name_registry.update(sec_pos, row.get("descrizione"), 9)
        if row.get("categoria"):
            categories[sec_pos] = _CATEGORIES[row.get("categoria").upper()]
        if row.get("paese"):
            countries[sec_pos] = row.get("paese").upper()

        data = positions[sec_pos]
        quantity = _parse_decimal(row.get("quantita")) if row.get("quantita") else None

        if row.kind in ("SALDO_INIZIALE", "SALDO_FINALE"):
            assert quantity is not None
            reference = d if row.kind == "SALDO_INIZIALE" else d + timedelta(days=1)
            data["stocks"].append(
                SecurityStock(
                    referenceDate=reference,
                    mutation=False,
                    quantity=quantity,
                    balanceCurrency=currency or "CHF",
                    quotationType="PIECE",
                )
            )
        elif row.kind in ("ACQUISTO", "VENDITA"):
            assert quantity is not None
            signed = abs(quantity) if row.kind == "ACQUISTO" else -abs(quantity)
            data["stocks"].append(
                SecurityStock(
                    referenceDate=d,
                    mutation=True,
                    quantity=signed,
                    unitPrice=_parse_decimal(row.get("prezzo")),
                    balanceCurrency=currency,
                    quotationType="PIECE",
                    name="Buy" if signed > 0 else "Sell",
                )
            )
        else:
            label = _PAYMENT_LABELS[row.kind]
            payment = build_security_payment(
                payment_date=d,
                description=label,
                currency=currency,
                amount=_parse_decimal(row.get("importo")),
                broker_label=label,
            )
            payment.quantity = quantity
            if row.get("data_ex"):
                payment.exDate = _parse_date(row.get("data_ex"))
            data["payments"].append(payment)
            if row.get("ritenuta"):
                withheld = abs(_parse_decimal(row.get("ritenuta")))
                if withheld:
                    data["payments"].append(
                        build_security_payment(
                            payment_date=d,
                            description=f"{label} withholding tax",
                            currency=currency,
                            amount=-withheld,
                            broker_label="Withholding Tax",
                            is_withholding=True,
                        )
                    )

    # ------------------------------------------------------------------
    # Model builders
    # ------------------------------------------------------------------

    def _bank_account(self, acc: _Account) -> BankAccount:
        assert acc.currency is not None
        tax_value = None
        if acc.balance is not None:
            balance_date, balance = acc.balance
            tax_value = BankAccountTaxValue(
                referenceDate=balance_date,
                balanceCurrency=acc.currency,
                balance=balance,
            )
        return BankAccount(
            iban=acc.iban if _country_from_code(acc.iban) else None,
            bankAccountNumber=BankAccountNumber(acc.iban[:32]),
            bankAccountName=BankAccountName((acc.name or acc.iban)[:40]),
            bankAccountCountry=acc.country or _country_from_code(acc.iban) or "CH",
            bankAccountCurrency=acc.currency,
            taxValue=tax_value,
            payment=[
                BankAccountPayment(
                    paymentDate=d, name=name, amountCurrency=acc.currency, amount=amt
                )
                for d, amt, name in sorted(acc.payments)
            ],
        )

    def _liability_account(self, acc: _Account) -> LiabilityAccount:
        assert acc.currency is not None
        tax_value = None
        if acc.balance is not None:
            balance_date, balance = acc.balance
            tax_value = LiabilityAccountTaxValue(
                referenceDate=balance_date,
                balanceCurrency=acc.currency,
                balance=balance,
            )
        return LiabilityAccount(
            iban=acc.iban if _country_from_code(acc.iban) else None,
            bankAccountNumber=BankAccountNumber(acc.iban[:32]),
            bankAccountName=BankAccountName((acc.name or acc.iban)[:40]),
            bankAccountCountry=acc.country or _country_from_code(acc.iban) or "CH",
            bankAccountCurrency=acc.currency,
            taxValue=tax_value,
            payment=[
                LiabilityAccountPayment(
                    paymentDate=d, name=name, amountCurrency=acc.currency, amount=amt
                )
                for d, amt, name in sorted(acc.payments)
            ],
            totalTaxValue=Decimal("0"),
            totalGrossRevenueB=Decimal("0"),
        )
