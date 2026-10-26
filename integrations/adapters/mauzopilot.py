import logging
import uuid
from decimal import Decimal
from typing import Any
import httpx
from django.db import transaction
from django.utils import timezone

from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from integrations.models import ExternalObjectMapping, IntegrationSyncLog, MauzoPilotConfig
from ledger.models import Account, AccountCategory
from organizations.models import Organization
from payments.models import Payment, PaymentMethod
from payments.services import record_and_confirm_customer_payment

logger = logging.getLogger(__name__)


class MauzoPilotAdapter:
    """Integration adapter implementing the verified interface contract for MauzoPilot."""

    def __init__(self, organization: Organization, client: httpx.Client | None = None):
        self.organization = organization
        self.client = client
        self.config = MauzoPilotConfig.objects.filter(organization=organization).first()

    def test_connection(self) -> dict[str, Any]:
        """Validates upstream endpoint connectivity, timeouts, and auth header format."""
        if not self.config:
            return {
                "status": "UNCONFIGURED",
                "message": "MauzoPilot configuration has not been created for this organization.",
                "connected": False,
            }

        if not self.config.is_active:
            return {
                "status": "DISABLED",
                "message": "MauzoPilot connector is intentionally disabled until credentials are confirmed.",
                "connected": False,
            }

        if not self.config.api_key:
            return {
                "status": "MISSING_CREDENTIALS",
                "message": "MauzoPilot API key is required.",
                "connected": False,
            }

        url = f"{self.config.base_url.rstrip('/')}/health"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
            "X-Client-System": "avestellar-finance",
        }

        should_close = False
        client = self.client
        if client is None:
            client = httpx.Client(timeout=float(self.config.request_timeout))
            should_close = True

        try:
            resp = client.get(url, headers=headers)
            connected = (200 <= resp.status_code < 300)
            status_label = "CONNECTED" if connected else "UNAVAILABLE"
            return {
                "status": status_label,
                "status_code": resp.status_code,
                "connected": connected,
                "base_url": self.config.base_url,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "message": f"Connection failed: {str(exc)}",
                "connected": False,
                "base_url": self.config.base_url,
            }
        finally:
            if should_close:
                client.close()

    @transaction.atomic
    def sync_customer(self, external_customer_data: dict[str, Any]) -> Customer:
        """Idempotently syncs an operational customer from MauzoPilot into AVESTELLAR FINANCE.

        Guarantees that repeated ingestion under the same external ID resolves to the identical Customer record.
        """
        ext_id = str(external_customer_data.get("id") or external_customer_data.get("customer_id") or "")
        if not ext_id:
            raise ValueError("External customer payload must contain a valid 'id' or 'customer_id'.")

        name = external_customer_data.get("name", "").strip() or f"MauzoPilot Customer {ext_id}"
        email = external_customer_data.get("email", "").strip()
        phone = external_customer_data.get("phone", "").strip()
        tax_pin = external_customer_data.get("tax_pin", "").strip()

        # Check existing external mapping
        mapping = ExternalObjectMapping.objects.filter(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="customer",
            external_id=ext_id,
        ).first()

        if mapping:
            customer = Customer.objects.filter(id=mapping.internal_id, organization=self.organization).first()
            if customer:
                return customer

        # Create new Customer
        customer = Customer.objects.create(
            organization=self.organization,
            name=name,
            email=email,
            phone=phone,
            tax_pin=tax_pin,
            currency=self.organization.currency,
        )

        ExternalObjectMapping.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="customer",
            external_id=ext_id,
            internal_type="customer",
            internal_id=customer.id,
        )

        IntegrationSyncLog.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            action="SYNC_CUSTOMER",
            status="SUCCESS",
            details={"external_id": ext_id, "customer_id": str(customer.id), "name": name},
        )

        return customer

    @transaction.atomic
    def sync_sales_order_to_draft_invoice(self, external_order_data: dict[str, Any]) -> Invoice:
        """Translates a confirmed MauzoPilot sales order into a validated draft sales invoice.

        Safeguards:
        1. Validates order status is confirmed/fulfilled.
        2. Checks external mapping to prevent duplicate invoice generation.
        3. Creates a DRAFT invoice; does NOT post to the general ledger without validation.
        """
        ext_order_id = str(external_order_data.get("id") or external_order_data.get("order_number") or "")
        if not ext_order_id:
            raise ValueError("Order payload must contain an 'id' or 'order_number'.")

        order_status = str(external_order_data.get("status", "confirmed")).lower()
        if order_status not in ("confirmed", "completed", "fulfilled", "paid"):
            raise ValueError(f"Sales order {ext_order_id} is in status '{order_status}', expected confirmed/completed.")

        # Idempotency check
        existing_mapping = ExternalObjectMapping.objects.filter(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="sales_order",
            external_id=ext_order_id,
        ).first()

        if existing_mapping:
            inv = Invoice.objects.filter(id=existing_mapping.internal_id, organization=self.organization).first()
            if inv:
                return inv

        # Resolve customer
        cust_payload = external_order_data.get("customer") or {
            "id": external_order_data.get("customer_id", f"cust-{ext_order_id}"),
            "name": external_order_data.get("customer_name", "Valued Customer"),
        }
        customer = self.sync_customer(cust_payload)

        # Locate default revenue account
        revenue_account = (
            Account.objects.filter(
                organization=self.organization, system_role="REVENUE", is_active=True
            ).first()
            or Account.objects.filter(
                organization=self.organization, category=AccountCategory.REVENUE, is_active=True
            ).first()
        )
        if not revenue_account:
            raise ValueError("No active Revenue account found to map invoice line items.")

        today = timezone.now().date()
        invoice_number = f"INV-MP-{ext_order_id}"

        invoice = Invoice.objects.create(
            organization=self.organization,
            customer=customer,
            invoice_number=invoice_number,
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT,
            currency=self.organization.currency,
            notes=f"Ingested from MauzoPilot Order #{ext_order_id}",
        )

        items_data = external_order_data.get("items", [])
        if not items_data:
            # Single aggregate line fallback
            total_amt = Decimal(str(external_order_data.get("total_amount", "0.00")))
            items_data = [{"description": f"Sales Order {ext_order_id}", "quantity": "1", "unit_price": str(total_amt), "tax_rate": "0.0"}]

        subtotal = Decimal("0.0000")
        tax_total = Decimal("0.0000")
        for item in items_data:
            qty = Decimal(str(item.get("quantity", "1")))
            price = Decimal(str(item.get("unit_price", "0")))
            tax_rate = Decimal(str(item.get("tax_rate", "0.0")))
            line = InvoiceLineItem.objects.create(
                invoice=invoice,
                revenue_account=revenue_account,
                description=item.get("description", "Product Sale"),
                quantity=qty,
                unit_price=price,
                tax_rate=tax_rate,
            )
            subtotal += line.quantity * line.unit_price
            tax_total += line.tax_amount

        invoice.subtotal = subtotal
        invoice.tax_amount = tax_total
        invoice.total_amount = subtotal + tax_total
        invoice.outstanding_balance = invoice.total_amount
        invoice.save(update_fields=["subtotal", "tax_amount", "total_amount", "outstanding_balance"])

        ExternalObjectMapping.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="sales_order",
            external_id=ext_order_id,
            internal_type="invoice",
            internal_id=invoice.id,
        )

        IntegrationSyncLog.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            action="SYNC_SALES_ORDER",
            status="SUCCESS",
            details={"order_id": ext_order_id, "invoice_id": str(invoice.id), "total": str(invoice.total_amount)},
        )

        return invoice

    @transaction.atomic
    def sync_payment_collection(self, external_payment_data: dict[str, Any]) -> Payment:
        """Processes and reconciles an incoming payment collection event from MauzoPilot."""
        ext_pay_id = str(external_payment_data.get("id") or external_payment_data.get("payment_id") or "")
        if not ext_pay_id:
            raise ValueError("Payment payload must contain a valid 'id' or 'payment_id'.")

        amount = Decimal(str(external_payment_data.get("amount", "0")))
        ref = str(external_payment_data.get("reference") or ext_pay_id)
        method_str = str(external_payment_data.get("payment_method", "MPESA")).upper()
        payment_method = PaymentMethod.MPESA if "MPESA" in method_str else PaymentMethod.BANK_TRANSFER

        # Check existing payment mapping
        existing_mapping = ExternalObjectMapping.objects.filter(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="payment",
            external_id=ext_pay_id,
        ).first()

        if existing_mapping:
            payment = Payment.objects.filter(id=existing_mapping.internal_id, organization=self.organization).first()
            if payment:
                return payment

        # Locate associated invoice
        ext_order_id = str(external_payment_data.get("order_id", ""))
        invoice = None
        if ext_order_id:
            order_mapping = ExternalObjectMapping.objects.filter(
                organization=self.organization,
                external_system="mauzopilot",
                external_type="sales_order",
                external_id=ext_order_id,
            ).first()
            if order_mapping:
                invoice = Invoice.objects.filter(id=order_mapping.internal_id, organization=self.organization).first()

        if not invoice:
            inv_id = external_payment_data.get("invoice_id")
            if inv_id:
                invoice = Invoice.objects.filter(id=inv_id, organization=self.organization).first()

        if not invoice:
            raise ValueError(f"Cannot settle payment {ext_pay_id}: no matching invoice found for order {ext_order_id}.")

        # If invoice is still draft, issue it first to create the receivable
        if invoice.status == InvoiceStatus.DRAFT:
            invoice = issue_invoice(invoice.id)

        # Deposit account: M-Pesa clearing or bank account
        deposit_acc = (
            Account.objects.filter(
                organization=self.organization, system_role="CLEARING", is_active=True
            ).first()
            or Account.objects.filter(
                organization=self.organization, system_role="BANK", is_active=True
            ).first()
            or Account.objects.filter(
                organization=self.organization, system_role="CASH", is_active=True
            ).first()
        )
        if not deposit_acc:
            raise ValueError("No active clearing or bank account found to deposit payment.")

        payment = record_and_confirm_customer_payment(
            invoice_id=invoice.id,
            amount=amount,
            deposit_account_id=deposit_acc.id,
            payment_method=payment_method,
            payment_number=f"PAY-MP-{ext_pay_id}",
            reference=ref,
        )

        ExternalObjectMapping.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="payment",
            external_id=ext_pay_id,
            internal_type="payment",
            internal_id=payment.id,
        )

        IntegrationSyncLog.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            action="SYNC_PAYMENT",
            status="SUCCESS",
            details={"payment_id": str(payment.id), "amount": str(amount), "reference": ref},
        )

        return payment
