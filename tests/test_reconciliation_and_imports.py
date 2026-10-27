"""Comprehensive test suite for bank and M-Pesa statement imports, deterministic matching engine, and reconciliation sessions."""

from datetime import date, timedelta
from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from automation.models import OutboxEvent
from identity.models import User
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account, AccountingPeriod, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices
from payments.models import (
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    ReconciliationConfidence,
    ReconciliationDecisionType,
    ReconciliationSession,
    ReconciliationSessionStatus,
    StatementImportBatch,
    StatementImportSource,
    StatementImportStatus,
    StatementReconciliationStatus,
    StatementTransaction,
    StatementTransactionClassification,
)
from payments.services import (
    DeterministicReconciliationEngine,
    DuplicateStatementFileError,
    ReconciliationSafeguardError,
    create_reconciliation_session,
    execute_reconciliation_match,
    finalize_reconciliation_session,
    import_statement_file,
    mark_transaction_for_investigation,
    reopen_reconciliation_session,
)
from payments.services.statement_parsers import (
    BankCsvParser,
    MpesaStatementParser,
    Mt940StatementParser,
    OfxStatementParser,
    clean_decimal,
)


@pytest.mark.django_db
class TestStatementParsers:
    """Tests isolated statement parser adapters across all supported formats."""

    def test_clean_decimal_handles_various_accounting_formats(self):
        assert clean_decimal("1,500.50") == Decimal("1500.5000")
        assert clean_decimal("KES 120,450.00") == Decimal("120450.0000")
        assert clean_decimal("(450.75)") == Decimal("-450.7500")
        assert clean_decimal("1.234,50") == Decimal("1234.5000")
        assert clean_decimal("-") == Decimal("0.0000")
        assert clean_decimal(None) == Decimal("0.0000")

    def test_bank_csv_parser_standard_and_debit_credit_columns(self):
        csv_data = """Date,Description,Reference,Debit,Credit,Balance
2026-03-01,Opening Balance,REF001,,100000.00,100000.00
2026-03-05,Office Internet,CHQ101,15000.00,,85000.00
2026-03-10,Customer Payment - Safiri,EFT990,,50000.00,135000.00
"""
        parser = BankCsvParser()
        result = parser.parse(csv_data)

        assert len(result.transactions) == 3
        assert len(result.errors) == 0
        assert result.total_rows == 3

        t1 = result.transactions[1]
        assert t1.description == "Office Internet"
        assert t1.signed_amount == Decimal("-15000.0000")
        assert t1.debit_amount == Decimal("15000.0000")
        assert t1.credit_amount == Decimal("0.0000")
        assert t1.classification == StatementTransactionClassification.SUPPLIER_PAYMENT

        t2 = result.transactions[2]
        assert t2.signed_amount == Decimal("50000.0000")
        assert t2.credit_amount == Decimal("50000.0000")
        assert t2.classification == StatementTransactionClassification.CUSTOMER_PAYMENT

    def test_bank_csv_parser_detects_conflicting_amounts(self):
        csv_data = """Date,Description,Debit,Credit
2026-03-05,Ambiguous Row,1000.00,2000.00
"""
        parser = BankCsvParser()
        result = parser.parse(csv_data)

        assert len(result.transactions) == 0
        assert len(result.errors) == 1
        assert "Conflicting amounts" in result.errors[0]["error"]

    def test_bank_csv_parser_with_custom_column_mapping(self):
        csv_data = """Booking Date,Narrative,Doc Ref,Amount Out,Amount In,Ledger Bal
05/03/2026,Software License,SW-998,2500.00,,50000.00
"""
        mapping = {
            "date": "Booking Date",
            "description": "Narrative",
            "reference": "Doc Ref",
            "debit": "Amount Out",
            "credit": "Amount In",
            "balance": "Ledger Bal",
        }
        parser = BankCsvParser(column_mapping=mapping, date_format="%d/%m/%Y")
        result = parser.parse(csv_data)

        assert len(result.transactions) == 1
        assert result.transactions[0].signed_amount == Decimal("-2500.0000")
        assert result.transactions[0].reference == "SW-998"

    def test_mpesa_csv_parser_with_preamble_and_classifications(self):
        mpesa_data = """Safaricom M-Pesa Statement
Account Name: AVESTELLAR CORP
Short Code: 600100
Period: 01-Mar-2026 to 31-Mar-2026

Receipt No.,Completion Time,Details,Transaction Status,Paid In,Withdrawn,Balance,Other Party Info
QKH1234567,2026-03-15 14:32:10,Pay Utility to 600100 - Inv INV-2026-001,Completed,15000.00,,115000.00,254712345678 - John Doe
QKH1234568,2026-03-15 14:32:11,Paybill Charge,Completed,,110.00,114890.00,Safaricom
QKH1234569,2026-03-16 10:15:00,Business Payment to Supplier XYZ,Completed,,50000.00,64890.00,Supplier XYZ
QKH1234570,2026-03-17 11:00:00,Reversal of QKH1234567,Completed,,15000.00,49890.00,Customer Support
"""
        parser = MpesaStatementParser()
        result = parser.parse(mpesa_data)

        assert len(result.transactions) == 4
        assert len(result.errors) == 0

        # Receipt 1: Customer Collection
        r1 = result.transactions[0]
        assert r1.source_transaction_id == "QKH1234567"
        assert r1.signed_amount == Decimal("15000.0000")
        assert r1.classification == StatementTransactionClassification.CUSTOMER_PAYMENT
        assert "John Doe" in r1.description

        # Receipt 2: Paybill Charge / Fee
        r2 = result.transactions[1]
        assert r2.source_transaction_id == "QKH1234568"
        assert r2.signed_amount == Decimal("-110.0000")
        assert r2.classification == StatementTransactionClassification.BANK_FEE

        # Receipt 3: Supplier Payment
        r3 = result.transactions[2]
        assert r3.signed_amount == Decimal("-50000.0000")
        assert r3.classification == StatementTransactionClassification.SUPPLIER_PAYMENT

        # Receipt 4: Reversal
        r4 = result.transactions[3]
        assert r4.classification == StatementTransactionClassification.REVERSAL

    def test_ofx_parser_xml_and_sgml(self):
        ofx_content = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>KES</CURDEF>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT</TRNTYPE>
<DTPOSTED>20260315</DTPOSTED>
<TRNAMT>45000.00</TRNAMT>
<FITID>OFX-TRX-1001</FITID>
<NAME>Direct Deposit Client A</NAME>
<MEMO>Invoice Settlement</MEMO>
</STMTTRN>
<STMTTRN>
<TRNTYPE>FEE</TRNTYPE>
<DTPOSTED>20260315</DTPOSTED>
<TRNAMT>-350.00</TRNAMT>
<FITID>OFX-TRX-1002</FITID>
<NAME>Monthly Maintenance Fee</NAME>
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>150000.00</BALAMT>
<DTASOF>20260331</DTASOF>
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""
        parser = OfxStatementParser()
        result = parser.parse(ofx_content)

        assert len(result.transactions) == 2
        assert result.closing_balance == Decimal("150000.0000")
        assert result.transactions[0].source_transaction_id == "OFX-TRX-1001"
        assert result.transactions[0].signed_amount == Decimal("45000.0000")
        assert result.transactions[0].classification == StatementTransactionClassification.CUSTOMER_PAYMENT
        assert result.transactions[1].signed_amount == Decimal("-350.0000")
        assert result.transactions[1].classification == StatementTransactionClassification.BANK_FEE

    def test_mt940_parser_swift_statement_lines(self):
        mt940_data = """:20:START940
:25:1010-KES-001
:28C:00001
:60F:C260301KES100000,00
:61:2603150315CR25000,00NTRFNONREF//QKH998877
:86:Transfer from Client Alpha
:61:2603200320DR1500,00NCHK1002//CHQ1002
:86:Bank Service Charge
:62F:C260331KES123500,00
"""
        parser = Mt940StatementParser()
        result = parser.parse(mt940_data)

        assert len(result.transactions) == 2
        assert result.opening_balance == Decimal("100000.0000")
        assert result.closing_balance == Decimal("123500.0000")

        t1 = result.transactions[0]
        assert t1.transaction_date == date(2026, 3, 15)
        assert t1.signed_amount == Decimal("25000.0000")
        assert t1.reference == "QKH998877"
        assert t1.classification in (
            StatementTransactionClassification.TRANSFER,
            StatementTransactionClassification.CUSTOMER_PAYMENT,
        )

        t2 = result.transactions[1]
        assert t2.signed_amount == Decimal("-1500.0000")
        assert t2.classification == StatementTransactionClassification.BANK_FEE


@pytest.mark.django_db
class TestStatementImportAndReconciliation:
    """End-to-end tests for batch ingestion, deterministic matching rules, and sessions."""

    def setup_method(self):
        self.user = User.objects.create_user(email="accountant@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Avestellar Enterprise", slug="avestellar-ent", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT)
        initialize_organization_accounts(self.org, self.user)

        self.bank_account = Account.objects.get(organization=self.org, code="1010")
        self.customer = Customer.objects.create(
            organization=self.org,
            name="Kilifi Distributorship",
            email="accounts@kilifi.co.ke",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.defaults["HTTP_X_ORGANIZATION_ID"] = str(self.org.id)

    def test_statement_import_batch_deduplication(self):
        csv_payload = """Date,Description,Reference,Credit
2026-03-01,Deposit,REF-100,5000.00
"""
        # First import: succeeds
        batch1 = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="bank_march.csv",
            user=self.user,
        )
        assert batch1.imported_count == 1
        assert batch1.status == StatementImportStatus.COMPLETED

        # Second import with identical content on same account: must raise DuplicateStatementFileError
        with pytest.raises(DuplicateStatementFileError):
            import_statement_file(
                organization=self.org,
                financial_account_id=str(self.bank_account.id),
                file_content=csv_payload,
                file_name="bank_march.csv",
                user=self.user,
            )

    def test_statement_transaction_deduplication_via_source_id(self):
        # M-Pesa statement with repeated transaction across two files
        file1 = """Receipt No.,Completion Time,Details,Paid In,Withdrawn
QKH777001,2026-03-10 10:00:00,Payment from Customer A,10000.00,
QKH777002,2026-03-11 11:00:00,Payment from Customer B,20000.00,
"""
        batch1 = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=file1,
            file_name="mpesa_file1.csv",
            import_source=StatementImportSource.MPESA_CSV,
            user=self.user,
        )
        assert batch1.imported_count == 2
        assert batch1.duplicate_count == 0

        # File 2 includes QKH777002 again plus new QKH777003
        file2 = """Receipt No.,Completion Time,Details,Paid In,Withdrawn
QKH777002,2026-03-11 11:00:00,Payment from Customer B,20000.00,
QKH777003,2026-03-12 12:00:00,Payment from Customer C,30000.00,
"""
        batch2 = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=file2,
            file_name="mpesa_file2.csv",
            import_source=StatementImportSource.MPESA_CSV,
            user=self.user,
        )
        assert batch2.imported_count == 1
        assert batch2.duplicate_count == 1

        # Verify Outbox event was emitted atomically
        event = OutboxEvent.objects.filter(
            organization=self.org, event_type="statement.imported"
        ).first()
        assert event is not None

    def test_reconciliation_rule_1_strong_reference_match(self):
        today = timezone.now().date()
        # Create confirmed payment with reference QKH999999
        payment = Payment.objects.create(
            organization=self.org,
            payment_number="PAY-00101",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=self.customer,
            amount=Decimal("15000.0000"),
            currency="KES",
            payment_date=today,
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.CONFIRMED,
            reference="QKH999999",
            deposit_account=self.bank_account,
        )

        # Import statement transaction with matching reference
        csv_payload = f"""Date,Description,Reference,Credit
{today},M-Pesa Collection,QKH999999,15000.00
"""
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="rule1_test.csv",
            user=self.user,
        )
        trx = batch.transactions.first()

        engine = DeterministicReconciliationEngine(organization=self.org)
        candidates = engine.find_candidates(trx)

        assert len(candidates) >= 1
        cand = candidates[0]
        assert cand.rule_applied == "RULE_1_STRONG_REFERENCE"
        assert cand.confidence_score == 100
        assert cand.confidence_category == ReconciliationConfidence.HIGH
        assert cand.auto_match_permitted is True

        # Execute match
        reconciled = execute_reconciliation_match(
            statement_trx_id=str(trx.id),
            candidate_type="PAYMENT",
            candidate_id=str(payment.id),
            user=self.user,
            rule_applied=cand.rule_applied,
            confidence_score=cand.confidence_score,
            confidence_category=cand.confidence_category,
        )

        assert reconciled.reconciliation_status == StatementReconciliationStatus.RECONCILED
        assert reconciled.matched_payment == payment
        payment.refresh_from_db()
        assert payment.is_reconciled is True
        assert payment.status == PaymentStatus.RECONCILED

    def test_reconciliation_rule_2_exact_amount_and_date_window(self):
        trx_date = date(2026, 3, 15)
        # Payment 2 days earlier with no reference code
        payment = Payment.objects.create(
            organization=self.org,
            payment_number="PAY-00202",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=self.customer,
            amount=Decimal("27500.0000"),
            currency="KES",
            payment_date=trx_date - timedelta(days=2),
            payment_method=PaymentMethod.BANK_TRANSFER,
            status=PaymentStatus.CONFIRMED,
            reference="",
            deposit_account=self.bank_account,
        )

        csv_payload = f"""Date,Description,Reference,Credit
{trx_date},Transfer from Kilifi Distributorship,,27500.00
"""
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="rule2_test.csv",
            user=self.user,
        )
        trx = batch.transactions.first()

        engine = DeterministicReconciliationEngine(organization=self.org)
        candidates = engine.find_candidates(trx, date_window_days=3)

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.rule_applied == "RULE_2_EXACT_AMOUNT_DATE"
        assert cand.confidence_score >= 80  # Bonus for counterparty in narrative
        assert cand.auto_match_permitted is False  # Requires accountant review

    def test_reconciliation_rule_3_potential_match_outside_tight_window(self):
        trx_date = date(2026, 3, 20)
        # Payment 8 days earlier
        payment = Payment.objects.create(
            organization=self.org,
            payment_number="PAY-00303",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=self.customer,
            amount=Decimal("82000.0000"),
            currency="KES",
            payment_date=trx_date - timedelta(days=8),
            status=PaymentStatus.CONFIRMED,
            deposit_account=self.bank_account,
        )

        csv_payload = f"""Date,Description,Reference,Credit
{trx_date},Wire Deposit,,82000.00
"""
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="rule3_test.csv",
            user=self.user,
        )
        trx = batch.transactions.first()

        engine = DeterministicReconciliationEngine(organization=self.org)
        candidates = engine.find_candidates(trx, date_window_days=3)

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.rule_applied == "RULE_3_POTENTIAL_MATCH"
        assert cand.confidence_score == 50
        assert cand.confidence_category == ReconciliationConfidence.LOW

    def test_match_against_open_invoice_settles_and_creates_journal(self):
        today = timezone.now().date()
        invoice = Invoice.objects.create(
            organization=self.org,
            customer=self.customer,
            invoice_number="INV-MATCH-001",
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            revenue_account=Account.objects.get(organization=self.org, code="4000"),
            description="Goods",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("10000.0000"),
            tax_rate=Decimal("0.0000"),
        )
        issued_invoice = issue_invoice(invoice.id, user=self.user)

        csv_payload = f"""Date,Description,Reference,Credit
{today},Direct M-Pesa Receipt,INV-MATCH-001,10000.00
"""
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="invoice_match_test.csv",
            import_source=StatementImportSource.BANK_CSV,
            user=self.user,
        )
        trx = batch.transactions.first()

        # Match statement transaction directly to open invoice
        reconciled = execute_reconciliation_match(
            statement_trx_id=str(trx.id),
            candidate_type="INVOICE",
            candidate_id=str(issued_invoice.id),
            user=self.user,
            rule_applied="RULE_1_STRONG_REFERENCE",
        )

        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.PAID
        assert issued_invoice.outstanding_balance == Decimal("0.0000")
        assert reconciled.matched_payment is not None
        assert reconciled.matched_journal_entry is not None
        assert reconciled.matched_payment.is_reconciled is True

    def test_reconciliation_promotes_pending_payment_in_place(self):
        today = timezone.now().date()
        invoice = Invoice.objects.create(
            organization=self.org,
            customer=self.customer,
            invoice_number="INV-PENDING-RECON-1",
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            revenue_account=Account.objects.get(organization=self.org, code="4000"),
            description="Reconciliation service",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("9000.0000"),
            tax_rate=Decimal("0.0000"),
        )
        issued = issue_invoice(invoice.id, user=self.user)
        pending = Payment.objects.create(
            organization=self.org,
            payment_number="PAY-PENDING-RECON-1",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=self.customer,
            invoice=issued,
            amount=Decimal("9000.0000"),
            currency="KES",
            payment_date=today,
            payment_method=PaymentMethod.BANK_TRANSFER,
            status=PaymentStatus.PENDING,
            reference="BANK-RECON-1",
            deposit_account=self.bank_account,
        )
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=(
                f"Date,Description,Reference,Credit\n"
                f"{today},Customer receipt,BANK-RECON-1,9000.00\n"
            ),
            file_name="pending_payment_reconciliation.csv",
            user=self.user,
        )
        statement_trx = batch.transactions.first()

        reconciled = execute_reconciliation_match(
            statement_trx_id=str(statement_trx.id),
            candidate_type="PAYMENT",
            candidate_id=str(pending.id),
            user=self.user,
            reason="Matched exact amount and reference to statement evidence.",
        )

        pending.refresh_from_db()
        issued.refresh_from_db()
        assert pending.id == reconciled.matched_payment_id
        assert pending.status == PaymentStatus.RECONCILED
        assert pending.is_reconciled is True
        assert pending.journal_entry is not None
        assert pending.journal_entry.total_debit == pending.journal_entry.total_credit == Decimal("9000.0000")
        assert issued.status == InvoiceStatus.PAID
        assert issued.outstanding_balance == Decimal("0.0000")
        assert Payment.objects.filter(organization=self.org, invoice=issued).count() == 1

    def test_matching_safeguards_prevent_double_reconciliation(self):
        today = timezone.now().date()
        payment = Payment.objects.create(
            organization=self.org,
            payment_number="PAY-SAFEGUARD-1",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=self.customer,
            amount=Decimal("5000.0000"),
            currency="KES",
            payment_date=today,
            deposit_account=self.bank_account,
        )

        csv_payload = f"""Date,Description,Reference,Credit
{today},Payment Line 1,SAFE-1,5000.00
{today},Payment Line 2,SAFE-2,5000.00
"""
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="safeguards.csv",
            user=self.user,
        )
        trx1, trx2 = list(batch.transactions.all())

        # Match trx1 to payment
        execute_reconciliation_match(
            statement_trx_id=str(trx1.id),
            candidate_type="PAYMENT",
            candidate_id=str(payment.id),
            user=self.user,
        )

        # Attempting to match trx2 to the same already-reconciled payment must fail
        with pytest.raises(ReconciliationSafeguardError):
            execute_reconciliation_match(
                statement_trx_id=str(trx2.id),
                candidate_type="PAYMENT",
                candidate_id=str(payment.id),
                user=self.user,
            )

        # Attempting to re-match trx1 must fail
        with pytest.raises(ReconciliationSafeguardError):
            execute_reconciliation_match(
                statement_trx_id=str(trx1.id),
                candidate_type="PAYMENT",
                candidate_id=str(payment.id),
                user=self.user,
            )

    def test_matching_safeguards_tenant_isolation(self):
        other_user = User.objects.create_user(email="other@corp.com", password="Password123!")
        other_org = Organization.objects.create(name="Competitor Corp", slug="competitor-corp")
        Membership.objects.create(organization=other_org, user=other_user, role=RoleChoices.ACCOUNTANT)
        initialize_organization_accounts(other_org, other_user)
        other_bank = Account.objects.get(organization=other_org, code="1010")

        other_payment = Payment.objects.create(
            organization=other_org,
            payment_number="PAY-OTHER-001",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            amount=Decimal("1000.0000"),
            currency="KES",
            payment_date=timezone.now().date(),
            deposit_account=other_bank,
        )

        csv_payload = f"""Date,Description,Credit\n{timezone.now().date()},Payment,1000.00\n"""
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="tenant_iso.csv",
            user=self.user,
        )
        trx = batch.transactions.first()

        # Matching statement transaction of Org 1 against Payment of Org 2 must fail
        with pytest.raises(Exception):
            execute_reconciliation_match(
                statement_trx_id=str(trx.id),
                candidate_type="PAYMENT",
                candidate_id=str(other_payment.id),
                user=self.user,
            )

    def test_reconciliation_session_lifecycle(self):
        start = date(2026, 3, 1)
        end = date(2026, 3, 31)

        # Create session
        session = create_reconciliation_session(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            period_start=start,
            period_end=end,
            statement_opening_balance=Decimal("0.0000"),
            statement_closing_balance=Decimal("0.0000"),
            user=self.user,
        )
        assert session.status == ReconciliationSessionStatus.DRAFT
        assert session.difference == Decimal("0.0000")

        # Finalize clean session
        finalized = finalize_reconciliation_session(
            session_id=str(session.id),
            user=self.user,
        )
        assert finalized.status == ReconciliationSessionStatus.FINALIZED
        assert finalized.finalized_at is not None

        # Reopen session
        reopened = reopen_reconciliation_session(
            session_id=str(finalized.id),
            user=self.user,
            reason="Auditor requested adjustment",
        )
        assert reopened.status == ReconciliationSessionStatus.REOPENED

    def test_reconciliation_session_blocks_finalization_with_unexplained_difference(self):
        start = date(2026, 3, 1)
        end = date(2026, 3, 31)

        session = create_reconciliation_session(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            period_start=start,
            period_end=end,
            statement_opening_balance=Decimal("10000.0000"),
            statement_closing_balance=Decimal("50000.0000"),
            user=self.user,
        )
        # Difference exists (statement closing 50,000 vs ledger 0)
        assert session.difference == Decimal("50000.0000")

        with pytest.raises(ReconciliationSafeguardError):
            finalize_reconciliation_session(
                session_id=str(session.id),
                user=self.user,
                force_finalize=False,
            )

        # Force finalize allowed when explicit
        forced = finalize_reconciliation_session(
            session_id=str(session.id),
            user=self.user,
            force_finalize=True,
        )
        assert forced.status == ReconciliationSessionStatus.FINALIZED

    def test_mark_transaction_for_investigation(self):
        csv_payload = f"""Date,Description,Credit\n{timezone.now().date()},Unidentified Deposit,999.00\n"""
        batch = import_statement_file(
            organization=self.org,
            financial_account_id=str(self.bank_account.id),
            file_content=csv_payload,
            file_name="investigate_test.csv",
            user=self.user,
        )
        trx = batch.transactions.first()

        investigated = mark_transaction_for_investigation(
            statement_trx_id=str(trx.id),
            user=self.user,
            reason="Unrecognized depositor; contact bank for trace.",
        )
        assert investigated.reconciliation_status == StatementReconciliationStatus.INVESTIGATION

        outbox = OutboxEvent.objects.filter(
            organization=self.org, event_type="reconciliation.exception"
        ).first()
        assert outbox is not None

    def test_reconciliation_api_endpoints(self):
        csv_payload = f"""Date,Description,Reference,Credit\n{timezone.now().date()},API Test Deposit,REF-API-1,12000.00\n"""
        res_upload = self.client.post(
            "/api/v1/payments/statement-imports/",
            {
                "financial_account_id": str(self.bank_account.id),
                "import_source": "BANK_CSV",
                "file_content": csv_payload,
                "file_name": "api_test.csv",
            },
            format="json",
        )
        assert res_upload.status_code == 201
        batch_id = res_upload.data["id"]

        # List unreconciled
        res_unrec = self.client.get("/api/v1/payments/reconciliation/unreconciled/")
        assert res_unrec.status_code == 200
        assert len(res_unrec.data) >= 1

        trx_id = res_unrec.data[0]["id"]

        # Get candidates
        res_cand = self.client.get(f"/api/v1/payments/reconciliation/candidates/{trx_id}/")
        assert res_cand.status_code == 200

        # Mark for investigation
        res_inv = self.client.post(
            "/api/v1/payments/reconciliation/investigate/",
            {"statement_transaction_id": trx_id, "reason": "Requires customer confirmation"},
            format="json",
        )
        assert res_inv.status_code == 200
        assert res_inv.data["reconciliation_status"] == StatementReconciliationStatus.INVESTIGATION
