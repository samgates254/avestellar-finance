"""Automated tests for Financial Calculation and Decision Engine (Milestone 2).

Tests deterministic arithmetic, metric registry, EBITDA bridge, capital allocation,
investment appraisal (NPV/IRR), liquidity, leverage, efficiency, break-even,
scenario simulation, and multi-tenant security isolation.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import pytest
from rest_framework.test import APIClient

from analytics.models import (
    AccountClassificationMapping,
    CapitalExpenditure,
    CostBehavior,
    FinancialClassification,
    FinancialScenario,
)
from analytics.services.ai_assistant import AIFinancialAssistantInterface
from analytics.services.metrics import (
    AccountMappingService,
    BreakEvenService,
    CalculationStatus,
    CapitalAllocationService,
    EfficiencyService,
    FinancialDashboardService,
    FinancialScenarioService,
    InvestmentAppraisalService,
    LeverageService,
    LiquidityService,
    ProfitabilityService,
    metric_registry,
)
from identity.models import User
from ledger.models import (
    Account,
    AccountingPeriod,
    AccountCategory,
    JournalEntry,
    JournalLine,
    JournalStatus,
)
from ledger.services.initialization import initialize_organization_accounts
from ledger.services.posting import post_journal_entry
from organizations.models import Membership, Organization, RoleChoices


@pytest.mark.django_db
class TestFinancialCalculationEngine:
    """Comprehensive test suite for financial metrics and calculations."""

    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="cfo@avestellar.com", password="Password123!")
        self.viewer_user = User.objects.create_user(email="viewer@avestellar.com", password="Password123!")

        self.org = Organization.objects.create(name="Avestellar Corp", slug="avestellar-corp", currency="KES")
        self.other_org = Organization.objects.create(name="Competitor Inc", slug="competitor-inc", currency="USD")

        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT)
        Membership.objects.create(organization=self.org, user=self.viewer_user, role=RoleChoices.VIEWER)

        initialize_organization_accounts(self.org, self.user)
        initialize_organization_accounts(self.other_org, self.user)

        self.client.force_authenticate(user=self.user)

        # Standard chart accounts
        self.cash = Account.objects.get(organization=self.org, code="1000")
        self.ar = Account.objects.get(organization=self.org, code="1200")
        self.inv = Account.objects.get(organization=self.org, code="1400")
        self.ap = Account.objects.get(organization=self.org, code="2000")
        self.equity = Account.objects.get(organization=self.org, code="3000")
        self.revenue = Account.objects.get(organization=self.org, code="4000")
        self.cogs = Account.objects.get(organization=self.org, code="5000")
        self.opex = Account.objects.get(organization=self.org, code="6000")

        # Create specialized accounts for comprehensive metric tests
        self.ppe, _ = Account.objects.get_or_create(
            organization=self.org, code="1500",
            defaults={"name": "Property, Plant & Equipment", "category": AccountCategory.ASSET, "currency": "KES"}
        )
        self.st_debt, _ = Account.objects.get_or_create(
            organization=self.org, code="2100",
            defaults={"name": "Short-Term Debt", "category": AccountCategory.LIABILITY, "currency": "KES"}
        )
        self.depr, _ = Account.objects.get_or_create(
            organization=self.org, code="6200",
            defaults={"name": "Depreciation Expense", "category": AccountCategory.EXPENSE, "currency": "KES"}
        )
        self.interest, _ = Account.objects.get_or_create(
            organization=self.org, code="7000",
            defaults={"name": "Interest Expense", "category": AccountCategory.EXPENSE, "currency": "KES"}
        )
        self.tax, _ = Account.objects.get_or_create(
            organization=self.org, code="8000",
            defaults={"name": "Income Tax Expense", "category": AccountCategory.EXPENSE, "currency": "KES"}
        )

        self.period = AccountingPeriod.objects.filter(organization=self.org).first()
        self.start_date = self.period.start_date
        self.end_date = self.period.end_date

    def _post_entry(self, lines: list[tuple[Account, Decimal, Decimal]], desc: str = "Test Entry", date=None):
        je = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number=f"JE-METRIC-{JournalEntry.objects.count() + 1}",
            date=date or self.start_date,
            description=desc,
            status=JournalStatus.DRAFT,
        )
        for seq, (acc, dr, cr) in enumerate(lines, start=1):
            JournalLine.objects.create(
                journal_entry=je,
                account=acc,
                debit=dr,
                credit=cr,
                sequence=seq,
            )
        return post_journal_entry(je.id, user=self.user)

    # ==========================================================================
    # 1. PROFITABILITY & EBITDA RECONCILIATION
    # ==========================================================================

    def test_profitability_and_ebitda_bridge(self):
        # 1. Post Revenue: Dr AR 1,000,000 | Cr Revenue 1,000,000
        self._post_entry([(self.ar, Decimal("1000000.0000"), Decimal("0")), (self.revenue, Decimal("0"), Decimal("1000000.0000"))], "Revenue")
        # 2. Post COGS: Dr COGS 400,000 | Cr Inventory 400,000
        self._post_entry([(self.cogs, Decimal("400000.0000"), Decimal("0")), (self.inv, Decimal("0"), Decimal("400000.0000"))], "COGS")
        # 3. Post OPEX: Dr OPEX 200,000 | Cr AP 200,000
        self._post_entry([(self.opex, Decimal("200000.0000"), Decimal("0")), (self.ap, Decimal("0"), Decimal("200000.0000"))], "OPEX")
        # 4. Post Depreciation: Dr Depr 50,000 | Cr PPE 50,000
        self._post_entry([(self.depr, Decimal("50000.0000"), Decimal("0")), (self.ppe, Decimal("0"), Decimal("50000.0000"))], "Depreciation")
        # 5. Post Interest: Dr Interest 30,000 | Cr Cash 30,000
        self._post_entry([(self.interest, Decimal("30000.0000"), Decimal("0")), (self.cash, Decimal("0"), Decimal("30000.0000"))], "Interest")
        # 6. Post Tax: Dr Tax 20,000 | Cr Cash 20,000
        self._post_entry([(self.tax, Decimal("20000.0000"), Decimal("0")), (self.cash, Decimal("0"), Decimal("20000.0000"))], "Tax")

        # Configure explicit classifications to verify mapping service
        AccountClassificationMapping.objects.create(
            organization=self.org, account=self.depr, classification=FinancialClassification.DEPRECIATION
        )
        AccountClassificationMapping.objects.create(
            organization=self.org, account=self.interest, classification=FinancialClassification.INTEREST_EXPENSE
        )
        AccountClassificationMapping.objects.create(
            organization=self.org, account=self.tax, classification=FinancialClassification.TAX_EXPENSE
        )

        svc = ProfitabilityService(self.org)
        margins = svc.calculate_margin_summary(self.start_date, self.end_date)

        # Revenue = 1,000,000
        assert margins["revenue"].calculated_value == Decimal("1000000.0000")
        # Gross profit = 1,000,000 - 400,000 = 600,000; Margin = 60.00%
        assert margins["gross_profit"].calculated_value == Decimal("600000.0000")
        assert margins["gross_margin"].calculated_value == Decimal("60.0000")

        # Operating expenses = 200,000 (excluding COGS, Depr, Interest, Tax)
        assert margins["operating_expenses"].calculated_value == Decimal("200000.0000")
        # Operating Profit = 600,000 - 200,000 - 50,000 = 350,000
        assert margins["operating_profit"].calculated_value == Decimal("350000.0000")
        # EBIT = 350,000; EBIT margin = 35.00%
        assert margins["ebit"].calculated_value == Decimal("350000.0000")
        assert margins["ebit_margin"].calculated_value == Decimal("35.0000")

        # Net Income = 350,000 - 30,000 (Interest) - 20,000 (Tax) = 300,000; Net Margin = 30.00%
        assert margins["net_profit"].calculated_value == Decimal("300000.0000")
        assert margins["net_margin"].calculated_value == Decimal("30.0000")

        # EBITDA = Net Income (300,000) + Interest (30,000) + Tax (20,000) + Depr (50,000) = 400,000
        # EBITDA Margin = 400,000 / 1,000,000 * 100 = 40.00%
        assert margins["ebitda"].calculated_value == Decimal("400000.0000")
        assert margins["ebitda_margin"].calculated_value == Decimal("40.0000")

        # Check reconciliation bridge structure
        bridge = margins["ebitda_reconciliation"]
        assert bridge["reconciled"] is True
        assert bridge["net_income"] == "300000.0000"
        assert bridge["interest_addback"] == "30000.0000"
        assert bridge["tax_addback"] == "20000.0000"
        assert bridge["depreciation_addback"] == "50000.0000"
        assert bridge["ebitda"] == "400000.0000"

    def test_zero_revenue_margin_protection(self):
        """Margins must return UNDEFINED with zero denominator rather than ZeroDivisionError."""
        svc = ProfitabilityService(self.org)
        margins = svc.calculate_margin_summary(self.start_date, self.end_date)
        assert margins["gross_margin"].status == CalculationStatus.UNDEFINED
        assert "Revenue is zero" in margins["gross_margin"].warnings[0]

    # ==========================================================================
    # 2. CAPITAL ALLOCATION, CAPEX & RETURNS
    # ==========================================================================

    def test_capex_and_cost_behavior(self):
        # Create an explicit CAPEX project record
        CapitalExpenditure.objects.create(
            organization=self.org,
            project_code="CAPEX-SERVER-01",
            name="New Data Center Servers",
            asset_category="MACHINERY_EQUIPMENT",
            amount=Decimal("150000.0000"),
            currency="KES",
            incurred_date=self.start_date,
            account=self.ppe,
        )

        # Create Fixed and Variable OPEX classification mappings
        AccountClassificationMapping.objects.create(
            organization=self.org,
            account=self.opex,
            classification=FinancialClassification.OPEX_GENERAL,
            cost_behavior=CostBehavior.VARIABLE,
        )

        # Post OPEX 100,000: Dr OPEX 100,000 | Cr AP 100,000
        self._post_entry([(self.opex, Decimal("100000.0000"), Decimal("0")), (self.ap, Decimal("0"), Decimal("100000.0000"))])

        svc = CapitalAllocationService(self.org)
        capex_res = svc.calculate_capex(self.start_date, self.end_date)
        assert capex_res.calculated_value == Decimal("150000.0000")

        cost_behavior = svc.calculate_fixed_vs_variable_opex(self.start_date, self.end_date)
        assert Decimal(cost_behavior["variable_opex"]) == Decimal("100000.0000")
        assert Decimal(cost_behavior["fixed_opex"]) == Decimal("0.0000")

    def test_return_metrics_roa_roe_roic_roce(self):
        # Post Capital additions: Dr PPE 500,000 | Cr Equity 500,000
        self._post_entry([(self.ppe, Decimal("500000.0000"), Decimal("0")), (self.equity, Decimal("0"), Decimal("500000.0000"))], "Equity capital")
        # Post Net income 50,000: Dr Cash 50,000 | Cr Revenue 50,000
        self._post_entry([(self.cash, Decimal("50000.0000"), Decimal("0")), (self.revenue, Decimal("0"), Decimal("50000.0000"))], "Profitable sale")

        svc = CapitalAllocationService(self.org)
        roa = svc.calculate_roa(self.start_date, self.end_date)
        roe = svc.calculate_roe(self.start_date, self.end_date)
        roic = svc.calculate_roic(self.start_date, self.end_date)
        roce = svc.calculate_roce(self.start_date, self.end_date)

        assert roa.status == CalculationStatus.AVAILABLE
        assert roa.calculated_value is not None and roa.calculated_value > Decimal("0")
        assert roe.status == CalculationStatus.AVAILABLE
        assert roe.calculated_value is not None and roe.calculated_value > Decimal("0")
        assert roic.status == CalculationStatus.AVAILABLE
        assert roce.status == CalculationStatus.AVAILABLE

    def test_negative_or_zero_equity_roe_handling(self):
        """ROE on zero equity must yield UNDEFINED status with informative warning."""
        svc = CapitalAllocationService(self.org)
        roe = svc.calculate_roe(self.start_date, self.end_date)
        assert roe.status == CalculationStatus.UNDEFINED
        assert any("Zero" in w or "unavailable" in w for w in roe.warnings)

    # ==========================================================================
    # 3. INVESTMENT APPRAISAL (NPV, IRR, PAYBACK, PI)
    # ==========================================================================

    def test_investment_npv_and_irr(self):
        svc = InvestmentAppraisalService()

        # Known case: Initial = 100, Cash flows = [110], Discount = 10%
        # NPV = 110 / 1.10 - 100 = 0.0000
        # IRR = 10.0000%
        npv = svc.calculate_npv(Decimal("100"), [Decimal("110")], Decimal("0.10"))
        assert npv.status == CalculationStatus.AVAILABLE
        assert npv.calculated_value == Decimal("0.0000")

        irr = svc.calculate_irr(Decimal("100"), [Decimal("110")])
        assert irr.status == CalculationStatus.AVAILABLE
        assert irr.calculated_value == Decimal("10.0000")

    def test_irr_no_solution_and_ambiguous_cases(self):
        svc = InvestmentAppraisalService()

        # All negative cash flows after investment -> No positive IRR can ever yield NPV = 0
        irr_loss = svc.calculate_irr(Decimal("100"), [Decimal("-10"), Decimal("-20")])
        assert irr_loss.status in (CalculationStatus.UNDEFINED, CalculationStatus.NOT_APPLICABLE)

        # Ambiguous multiple sign changes: -100, +230, -132 (Descartes' rule: 2 sign changes)
        irr_ambiguous = svc.calculate_irr(Decimal("100"), [Decimal("230"), Decimal("-132")])
        assert len(irr_ambiguous.warnings) > 0
        assert any("sign changes" in w for w in irr_ambiguous.warnings)

    def test_payback_periods_and_profitability_index(self):
        svc = InvestmentAppraisalService()
        # Initial 1000, 300 per year for 4 years at 0%
        # Simple payback = 1000 / 300 = 3.33 years
        payback = svc.calculate_payback_period(
            Decimal("1000"), [Decimal("300"), Decimal("300"), Decimal("300"), Decimal("300")], Decimal("0.10")
        )
        assert payback.status == CalculationStatus.AVAILABLE
        assert payback.calculated_value == Decimal("3.3333")
        assert "discounted_payback_period" in payback.input_values

        # Profitability Index: Initial 100, cash flow [110] at 10% -> PV = 100 / 100 = 1.0000
        pi = svc.calculate_profitability_index(Decimal("100"), [Decimal("110")], Decimal("0.10"))
        assert pi.status == CalculationStatus.AVAILABLE
        assert pi.calculated_value == Decimal("1.0000")

    # ==========================================================================
    # 4. LIQUIDITY & LEVERAGE
    # ==========================================================================

    def test_liquidity_ratios_and_zero_denominator(self):
        # Initial state: no entries -> current liabilities = 0
        svc = LiquidityService(self.org)
        cr_zero = svc.calculate_current_ratio(as_of_date=self.end_date)
        assert cr_zero.status == CalculationStatus.UNDEFINED
        assert "Current liabilities are zero" in cr_zero.warnings[0]

        # Post: Dr Cash 300,000, Dr AR 100,000, Dr Inventory 100,000 | Cr AP 200,000, Cr ST Debt 50,000, Cr Equity 250,000
        self._post_entry([
            (self.cash, Decimal("300000.0000"), Decimal("0")),
            (self.ar, Decimal("100000.0000"), Decimal("0")),
            (self.inv, Decimal("100000.0000"), Decimal("0")),
            (self.ap, Decimal("0"), Decimal("200000.0000")),
            (self.st_debt, Decimal("0"), Decimal("50000.0000")),
            (self.equity, Decimal("0"), Decimal("250000.0000")),
        ], "Liquidity setup")

        # Current Assets = 300k + 100k + 100k = 500k
        # Current Liabilities = 200k + 50k = 250k
        # Working Capital = 500k - 250k = 250k
        wc = svc.calculate_working_capital(as_of_date=self.end_date)
        assert wc.calculated_value == Decimal("250000.0000")

        # Current Ratio = 500,000 / 250,000 = 2.0000
        cr = svc.calculate_current_ratio(as_of_date=self.end_date)
        assert cr.calculated_value == Decimal("2.0000")

        # Quick Ratio = (500,000 - 100,000) / 250,000 = 1.6000
        qr = svc.calculate_quick_ratio(as_of_date=self.end_date)
        assert qr.calculated_value == Decimal("1.6000")

        # Cash Ratio = 300,000 / 250,000 = 1.2000
        cash_ratio = svc.calculate_cash_ratio(as_of_date=self.end_date)
        assert cash_ratio.calculated_value == Decimal("1.2000")

        # Cash runway with 50,000 monthly burn: 300,000 / 50,000 = 6.0 months
        runway = svc.calculate_cash_runway(as_of_date=self.end_date, monthly_burn_rate=Decimal("50000"))
        assert runway.calculated_value == Decimal("6.0000")

    def test_leverage_ratios(self):
        # Post: Dr Cash 100k | Cr ST Debt 100k
        # Dr PPE 400k | Cr Equity 400k
        self._post_entry([
            (self.cash, Decimal("100000.0000"), Decimal("0")),
            (self.st_debt, Decimal("0"), Decimal("100000.0000")),
            (self.ppe, Decimal("400000.0000"), Decimal("0")),
            (self.equity, Decimal("0"), Decimal("400000.0000")),
        ], "Leverage setup")

        svc = LeverageService(self.org)
        total_debt = svc.calculate_total_debt(as_of_date=self.end_date)
        net_debt = svc.calculate_net_debt(as_of_date=self.end_date)
        dte = svc.calculate_debt_to_equity(as_of_date=self.end_date)

        # Total Debt = 100k
        assert total_debt.calculated_value == Decimal("100000.0000")
        # Net Debt = 100k debt - 100k cash = 0k
        assert net_debt.calculated_value == Decimal("0.0000")
        # Debt / Equity = 100k / 400k = 0.2500
        assert dte.calculated_value == Decimal("0.2500")

    # ==========================================================================
    # 5. EFFICIENCY METRICS (DIO, DSO, DPO, CCC)
    # ==========================================================================

    def test_operating_efficiency_and_cash_conversion_cycle(self):
        # 1. Post Revenue: Dr AR 365,000 | Cr Revenue 365,000
        self._post_entry([(self.ar, Decimal("365000.0000"), Decimal("0")), (self.revenue, Decimal("0"), Decimal("365000.0000"))])
        # 2. Post COGS: Dr COGS 182,500 | Cr AP 182,500
        self._post_entry([(self.cogs, Decimal("182500.0000"), Decimal("0")), (self.ap, Decimal("0"), Decimal("182500.0000"))])
        # 3. Post Inventory purchase: Dr Inventory 18,250 | Cr AP 18,250
        self._post_entry([(self.inv, Decimal("18250.0000"), Decimal("0")), (self.ap, Decimal("0"), Decimal("18250.0000"))])

        svc = EfficiencyService(self.org)
        dso = svc.calculate_dso(self.start_date, self.end_date)
        dio = svc.calculate_dio(self.start_date, self.end_date)
        dpo = svc.calculate_dpo(self.start_date, self.end_date)
        ccc = svc.calculate_cash_conversion_cycle(self.start_date, self.end_date)

        assert dso.status == CalculationStatus.AVAILABLE
        assert dio.status == CalculationStatus.AVAILABLE
        assert dpo.status == CalculationStatus.AVAILABLE
        assert ccc.status == CalculationStatus.AVAILABLE
        assert ccc.calculated_value is not None

    # ==========================================================================
    # 6. BREAK-EVEN & CONTRIBUTION MARGIN
    # ==========================================================================

    def test_single_and_multi_product_break_even(self):
        svc = BreakEvenService()

        # Fixed costs = 10,000, Price = 100, Var Cost = 60
        # CM = 40, CM Ratio = 40%, BE Units = 10,000 / 40 = 250 units, BE Sales = 10,000 / 0.40 = 25,000
        be = svc.calculate_break_even(
            fixed_costs=Decimal("10000"),
            unit_selling_price=Decimal("100"),
            unit_variable_cost=Decimal("60"),
            actual_units=Decimal("300"),
        )
        assert be["contribution_margin_per_unit"] == "40.0000"
        assert be["contribution_margin_ratio"] == "40.0000"
        assert be["break_even_units"] == "250.0000"
        assert be["break_even_sales"] == "25000.0000"
        # Margin of safety = 300 units - 250 units = 50 units (5,000 revenue)
        assert be["margin_of_safety_units"] == "50.0000"

        # Multi-product break-even
        products = [
            {"name": "Standard", "price": "100", "variable_cost": "60", "sales_mix_pct": "60"},
            {"name": "Premium", "price": "200", "variable_cost": "100", "sales_mix_pct": "40"},
        ]
        multi_be = svc.calculate_multi_product_break_even(Decimal("20000"), products)
        assert multi_be["status"] == "available"
        assert Decimal(multi_be["break_even_total_revenue"]) > Decimal("0")

    def test_negative_contribution_margin_handling(self):
        svc = BreakEvenService()
        # Selling price 50 < Variable cost 70 -> Negative CM of -20
        be = svc.calculate_break_even(Decimal("5000"), Decimal("50"), Decimal("70"))
        assert be["status"] == "undefined"
        assert any("Selling price does not cover unit variable cost" in w for w in be["warnings"])

    # ==========================================================================
    # 7. FINANCIAL SCENARIOS (NO LEDGER MUTATION)
    # ==========================================================================

    def test_financial_scenario_simulation_ledger_immutability(self):
        # Record baseline journal count
        initial_entry_count = JournalEntry.objects.count()

        svc = FinancialScenarioService(self.org)
        simulation = svc.simulate_scenario(
            base_start_date=self.start_date,
            base_end_date=self.end_date,
            revenue_growth_pct=Decimal("20.0"),
            cogs_change_pct=Decimal("5.0"),
            opex_change_pct=Decimal("10.0"),
            planned_capex=Decimal("50000"),
            working_capital_buffer_pct=Decimal("15.0"),
            save_scenario=True,
            name="2027 Expansion",
        )

        assert simulation["parameters"]["revenue_growth_pct"] == "20.0"
        assert simulation["scenario_id"] is not None

        # Critical Invariant: Ledger entries MUST NOT be created or mutated by simulations
        assert JournalEntry.objects.count() == initial_entry_count

    # ==========================================================================
    # 8. AI ASSISTANT DETERMINISTIC EXPLANATIONS
    # ==========================================================================

    def test_ai_assistant_deterministic_reconciliation(self):
        # Post: Dr AR 100k | Cr Rev 100k, Dr Depr 10k | Cr PPE 10k
        self._post_entry([(self.ar, Decimal("100000.0000"), Decimal("0")), (self.revenue, Decimal("0"), Decimal("100000.0000"))])
        self._post_entry([(self.depr, Decimal("10000.0000"), Decimal("0")), (self.ppe, Decimal("0"), Decimal("10000.0000"))])

        ai = AIFinancialAssistantInterface(self.org)
        explanation = ai.explain_ebitda_reconciliation(self.start_date, self.end_date)

        assert explanation["metric"] == "ebitda"
        assert "narrative" in explanation
        assert "reconciliation_bridge" in explanation

        # Evaluate hurdle check
        hurdle_eval = ai.evaluate_investment_hurdle(
            initial_investment=Decimal("100"),
            cash_flows=[Decimal("120")],
            discount_rate=Decimal("0.10"),
            hurdle_rate=Decimal("15.0"),
        )
        assert hurdle_eval["meets_hurdle"] is True
        assert "RECOMMENDED" in hurdle_eval["narrative"]

    # ==========================================================================
    # 9. API ENDPOINTS & MULTI-TENANT ISOLATION
    # ==========================================================================

    def test_api_endpoints_and_tenant_isolation(self):
        # 1. Financial Dashboard
        resp = self.client.get(
            "/api/v1/analytics/financial-dashboard/",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        assert resp.status_code == 200
        assert "profitability" in resp.data
        assert "liquidity" in resp.data
        assert "efficiency" in resp.data

        # 2. Metric Catalog
        cat_resp = self.client.get("/api/v1/analytics/metrics/", HTTP_X_ORGANIZATION_ID=str(self.org.id))
        assert cat_resp.status_code == 200
        assert cat_resp.data["count"] > 10

        # 3. Calculators: NPV
        npv_resp = self.client.post(
            "/api/v1/analytics/calculators/npv/",
            data={"initial_investment": "100.00", "cash_flows": ["60.00", "60.00"], "discount_rate": "0.10"},
            format="json",
        )
        assert npv_resp.status_code == 200
        assert Decimal(npv_resp.data["calculated_value"]) > Decimal("0")

        # 4. Multi-tenant Isolation: User is NOT a member of other_org
        forbidden_resp = self.client.get(
            "/api/v1/analytics/financial-dashboard/",
            HTTP_X_ORGANIZATION_ID=str(self.other_org.id),
        )
        assert forbidden_resp.status_code in (403, 400)

        # 5. Role Permission: Viewer cannot create CAPEX
        self.client.force_authenticate(user=self.viewer_user)
        viewer_post = self.client.post(
            "/api/v1/analytics/capital-expenditure/",
            data={"project_code": "P1", "name": "Hack", "amount": "1000", "incurred_date": "2026-01-01"},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
            format="json",
        )
        assert viewer_post.status_code == 403
