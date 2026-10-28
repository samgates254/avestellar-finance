"""Statement-import upload ceilings and file-type allow-listing.

The frontend blocks statements larger than 15 MB before they are sent
(``frontend/src/components/statements.tsx``). These tests prove the server
enforces the same boundary independently of the browser, for both transport
shapes the endpoint accepts:

* ``file_content`` — the string form sent inside a JSON body
* ``file``         — a multipart upload

They also prove the suffix allow-list rejects executable/unknown types.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
import pytest
from rest_framework.test import APIClient

from identity.models import User
from ledger.models import Account
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices
from payments.models import StatementImportBatch
from payments.serializers import (
    ALLOWED_STATEMENT_SUFFIXES,
    DEFAULT_MAX_STATEMENT_UPLOAD_BYTES,
    StatementImportUploadSerializer,
)

STATEMENT_IMPORT_URL = "/api/v1/payments/statement-imports/"

VALID_CSV = """Date,Description,Reference,Credit
2026-03-01,Deposit,REF-UPLOAD-1,5000.00
"""


@pytest.mark.django_db
class TestStatementUploadLimits:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="uploads@avestellar.com", password="Password123!"
        )
        self.org = Organization.objects.create(
            name="Avestellar Uploads", slug="avestellar-uploads", currency="KES"
        )
        Membership.objects.create(
            organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT
        )
        initialize_organization_accounts(self.org, self.user)
        self.bank_account = Account.objects.get(organization=self.org, code="1010")
        self.client.force_authenticate(user=self.user)
        self.client.defaults["HTTP_X_ORGANIZATION_ID"] = str(self.org.id)

    def _payload(self, **overrides):
        payload = {
            "financial_account_id": str(self.bank_account.id),
            "import_source": "BANK_CSV",
            "file_name": "statement.csv",
        }
        payload.update(overrides)
        return payload

    # --- serializer unit level -------------------------------------------------

    def test_default_limit_matches_frontend_fifteen_megabytes(self):
        assert DEFAULT_MAX_STATEMENT_UPLOAD_BYTES == 15 * 1024 * 1024
        assert ".csv" in ALLOWED_STATEMENT_SUFFIXES
        assert ".exe" not in ALLOWED_STATEMENT_SUFFIXES

    def test_string_content_within_limit_is_accepted(self):
        serializer = StatementImportUploadSerializer(
            data=self._payload(file_content=VALID_CSV)
        )
        assert serializer.is_valid(), serializer.errors

    def test_string_content_over_limit_is_rejected(self):
        with override_settings(MAX_STATEMENT_UPLOAD_BYTES=1024):
            oversized = "x" * 2048
            serializer = StatementImportUploadSerializer(
                data=self._payload(file_content=oversized)
            )
            assert not serializer.is_valid()
            assert "file_content" in serializer.errors

    def test_string_content_with_unsupported_suffix_is_rejected(self):
        serializer = StatementImportUploadSerializer(
            data=self._payload(file_content=VALID_CSV, file_name="statement.exe")
        )
        assert not serializer.is_valid()
        assert "file_name" in serializer.errors

    def test_multipart_upload_over_limit_is_rejected(self):
        with override_settings(MAX_STATEMENT_UPLOAD_BYTES=1024):
            upload = SimpleUploadedFile(
                "statement.csv", b"x" * 2048, content_type="text/csv"
            )
            serializer = StatementImportUploadSerializer(
                data={
                    "financial_account_id": str(self.bank_account.id),
                    "import_source": "BANK_CSV",
                    "file": upload,
                }
            )
            assert not serializer.is_valid()
            assert "file" in serializer.errors

    def test_multipart_upload_with_unsupported_suffix_is_rejected(self):
        upload = SimpleUploadedFile(
            "statement.exe", b"not a statement", content_type="application/octet-stream"
        )
        serializer = StatementImportUploadSerializer(
            data={
                "financial_account_id": str(self.bank_account.id),
                "import_source": "BANK_CSV",
                "file": upload,
            }
        )
        assert not serializer.is_valid()
        assert "file" in serializer.errors

    # --- API level -------------------------------------------------------------

    def test_api_rejects_oversized_statement_before_ingestion(self):
        with override_settings(MAX_STATEMENT_UPLOAD_BYTES=1024):
            res = self.client.post(
                STATEMENT_IMPORT_URL,
                self._payload(file_content="x" * 4096),
                format="json",
            )
        assert res.status_code == 400, res.content
        assert "file_content" in res.json()
        assert not StatementImportBatch.objects.filter(organization=self.org).exists()

    def test_api_rejects_disallowed_file_type(self):
        upload = SimpleUploadedFile(
            "statement.exe", b"MZ not a statement", content_type="application/octet-stream"
        )
        res = self.client.post(
            STATEMENT_IMPORT_URL,
            {
                "financial_account_id": str(self.bank_account.id),
                "import_source": "BANK_CSV",
                "file": upload,
            },
            format="multipart",
        )
        assert res.status_code == 400, res.content
        assert "file" in res.json()

    def test_api_accepts_valid_statement_and_ingests_it(self):
        res = self.client.post(
            STATEMENT_IMPORT_URL, self._payload(file_content=VALID_CSV), format="json"
        )
        assert res.status_code == 201, res.content
        batch = StatementImportBatch.objects.get(organization=self.org)
        assert batch.imported_count == 1
