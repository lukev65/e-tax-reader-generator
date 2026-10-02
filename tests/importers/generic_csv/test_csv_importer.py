from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from opensteuerauszug.calculate.base import CalculationMode
from opensteuerauszug.calculate.minimal_tax_value import MinimalTaxValueCalculator
from opensteuerauszug.calculate.total import TotalCalculator
from opensteuerauszug.core.exchange_rate_provider import DummyExchangeRateProvider
from opensteuerauszug.importers.generic_csv.csv_importer import CsvImporter, CsvImportError

SAMPLE = Path(__file__).parents[2] / "samples" / "import" / "csv" / "datalevel_sample_2024.csv"

HEADER = "tipo;data;conto;deposito;isin;valor;descrizione;categoria;valuta;quantita;prezzo;importo;data_ex;ritenuta;paese\n"


def _importer() -> CsvImporter:
    return CsvImporter(
        period_from=date(2024, 1, 1),
        period_to=date(2024, 12, 31),
        full_name="Max Muster",
        canton="TI",
        client_number="12345678",
    )


def test_sample_csv_imports_every_account_position_and_expense():
    statement = _importer().import_file(str(SAMPLE))

    assert len(statement.listOfBankAccounts.bankAccount) == 6
    assert len(statement.listOfLiabilities.liabilityAccount) == 4
    assert len(statement.listOfExpenses.expense) == 10
    securities = [s for d in statement.listOfSecurities.depot for s in d.security]
    assert len(securities) == 26
    assert statement.canton == "TI"
    assert statement.client[0].lastName == "Muster"


def test_closing_balance_is_stored_on_the_day_after_the_period():
    csv = (
        HEADER
        + "SALDO_INIZIALE;2024-01-01;;1;CH0010645932;1064593;Givaudan;AZIONE;CHF;1;;;;;\n"
        + "ACQUISTO;2024-05-02;;1;CH0010645932;;;;CHF;2;3900;;;;\n"
        + "SALDO_FINALE;2024-12-31;;1;CH0010645932;;;;CHF;3;;;;;\n"
    )
    statement = _importer().import_text(csv)

    security = statement.listOfSecurities.depot[0].security[0]
    balances = [(s.referenceDate, s.quantity) for s in security.stock if not s.mutation]
    assert (date(2025, 1, 1), Decimal("3")) in balances
    assert security.securityName == "Givaudan"
    assert security.securityCategory == "SHARE"
    assert security.country == "CH"


def test_sell_row_is_stored_as_negative_mutation():
    csv = (
        HEADER
        + "SALDO_INIZIALE;2024-01-01;;1;US0378331005;;Apple;AZIONE;USD;10;;;;;\n"
        + "VENDITA;2024-03-01;;1;US0378331005;;;;USD;4;180;;;;\n"
    )
    statement = _importer().import_text(csv)

    security = statement.listOfSecurities.depot[0].security[0]
    mutations = [s for s in security.stock if s.mutation]
    assert mutations[0].quantity == Decimal("-4")
    assert security.country == "US"


def test_bank_account_country_comes_from_iban():
    csv = (
        HEADER
        + "CONTO;2024-12-31;DE89370400440532013000;;;;Konto;;EUR;;;1000;;;\n"
        + "INTERESSE;2024-06-30;DE89370400440532013000;;;;;;EUR;;;12.5;;;\n"
    )
    statement = _importer().import_text(csv)

    account = statement.listOfBankAccounts.bankAccount[0]
    assert account.bankAccountCountry == "DE"
    assert account.taxValue.balance == Decimal("1000")
    assert account.payment[0].amount == Decimal("12.5")


def test_swiss_date_and_number_formats_are_accepted():
    csv = HEADER + "CONTO;31.12.2024;CH9300762011623852957;;;;;;CHF;;;12'345,50;;;\n"
    statement = _importer().import_text(csv)

    tax_value = statement.listOfBankAccounts.bankAccount[0].taxValue
    assert tax_value.referenceDate == date(2024, 12, 31)
    assert tax_value.balance == Decimal("12345.50")


def test_every_invalid_row_is_reported_with_its_line_number():
    csv = (
        HEADER
        + "PIPPO;2024-01-01;;;;;;;CHF;;;1;;;\n"
        + "CONTO;2024-12-31;CH9300762011623852957;;;;;;CHF;;;;;;\n"
        + "ACQUISTO;2024-13-01;;1;XX123;;;;CHF;1;10;;;;\n"
    )
    with pytest.raises(CsvImportError) as excinfo:
        _importer().import_text(csv)

    problems = excinfo.value.problems
    assert any(p.startswith("riga 2:") and "PIPPO" in p for p in problems)
    assert any(p.startswith("riga 3 (CONTO)") and "importo" in p for p in problems)
    assert any(p.startswith("riga 4 (ACQUISTO)") and "data non valida" in p for p in problems)
    assert any(p.startswith("riga 4 (ACQUISTO)") and "ISIN non valido" in p for p in problems)


def test_account_with_two_currencies_is_rejected():
    csv = (
        HEADER
        + "CONTO;2024-12-31;CH9300762011623852957;;;;;;CHF;;;1;;;\n"
        + "INTERESSE;2024-06-30;CH9300762011623852957;;;;;;EUR;;;1;;;\n"
    )
    with pytest.raises(CsvImportError, match="è in CHF, non in EUR"):
        _importer().import_text(csv)


def test_expenses_are_converted_to_chf_and_totalled():
    csv = (
        HEADER
        + "SPESA;2024-03-01;;123;;;Custody fee;22;CHF;;;10.81;;;\n"
        + "SPESA;2024-08-01;;456;;;Custody fee;;USD;;;7.77;;;\n"
    )
    statement = _importer().import_text(csv)
    MinimalTaxValueCalculator(CalculationMode.OVERWRITE, DummyExchangeRateProvider()).calculate(
        statement
    )
    TotalCalculator(CalculationMode.OVERWRITE).calculate(statement)

    expenses = statement.listOfExpenses.expense
    assert [e.expenseType for e in expenses] == ["22", "99"]
    assert expenses[0].expenses == Decimal("10.81")
    # DummyExchangeRateProvider converts every foreign currency at 0.5
    assert expenses[1].expenses == Decimal("3.89")
    assert statement.listOfExpenses.totalExpenses == Decimal("14.70")


def test_unknown_expense_code_is_rejected():
    csv = HEADER + "SPESA;2024-03-01;;123;;;Custody fee;50;CHF;;;10.81;;;\n"
    with pytest.raises(CsvImportError, match="codice eCH-0196"):
        _importer().import_text(csv)
