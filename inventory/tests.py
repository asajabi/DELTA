from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Branch, Category, Order, Part, Sale, Stock, UserProfile


class AuthPermissionTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.cashier = User.objects.create_user(username="cashier", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch},
        )

        self.manager = User.objects.create_user(username="manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )

    def test_search_requires_login(self):
        response = self.client.get(reverse("part_search"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_cashier_cannot_access_manager_report_pages(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.get(reverse("sales_history"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_manager_can_access_sales_history(self):
        self.client.login(username="manager", password="pass12345")
        response = self.client.get(reverse("sales_history"))
        self.assertEqual(response.status_code, 200)


class BranchIsolationTests(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(name="Main", code="MAIN")
        self.branch_b = Branch.objects.create(name="North", code="NORTH")
        category = Category.objects.create(name="Filters")

        self.part_a = Part.objects.create(
            name="Oil Filter",
            part_number="OF-001",
            category=category,
            cost_price=Decimal("10.00"),
            selling_price=Decimal("20.00"),
        )
        self.part_b = Part.objects.create(
            name="Brake Filter",
            part_number="BF-001",
            category=category,
            cost_price=Decimal("8.00"),
            selling_price=Decimal("15.00"),
        )

        self.stock_a = Stock.objects.create(part=self.part_a, branch=self.branch_a, quantity=5)
        self.stock_b = Stock.objects.create(part=self.part_b, branch=self.branch_b, quantity=5)

        self.cashier = User.objects.create_user(username="cashier", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch_a},
        )

        self.manager = User.objects.create_user(username="manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_a},
        )

    def test_cashier_sees_only_own_branch_stock(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.get(reverse("part_search"), {"q": "filter"})

        self.assertEqual(response.status_code, 200)
        results = list(response.context["results"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].part_number, "OF-001")

    def test_cashier_cannot_add_other_branch_stock_to_cart(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.post(reverse("add_to_cart", args=[self.stock_b.id]), {"quantity": 1})
        self.assertEqual(response.status_code, 404)

    def test_manager_can_see_all_branch_stock(self):
        self.client.login(username="manager", password="pass12345")
        response = self.client.get(reverse("part_search"), {"q": "filter"})

        self.assertEqual(response.status_code, 200)
        results = list(response.context["results"])
        self.assertEqual(len(results), 2)


class CheckoutFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cashier", password="pass12345")
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        UserProfile.objects.update_or_create(
            user=self.user,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch},
        )

        self.category = Category.objects.create(name="Filters")
        self.part = Part.objects.create(
            name="Oil Filter",
            part_number="OF-001",
            category=self.category,
            cost_price=Decimal("10.00"),
            selling_price=Decimal("20.00"),
        )
        self.stock = Stock.objects.create(part=self.part, branch=self.branch, quantity=5)

    def test_finalize_order_creates_order_sale_and_deducts_stock(self):
        self.client.login(username="cashier", password="pass12345")

        session = self.client.session
        session["cart"] = {str(self.stock.id): 2}
        session.save()

        response = self.client.post(
            reverse("finalize_order"),
            {
                "discount": "5.00",
                "phone_number": "0501234567",
                "customer_name": "Abu Ahmed",
                "customer_car": "Camry 2018",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Sale.objects.count(), 1)

        order = Order.objects.get()
        sale = Sale.objects.get()

        self.assertEqual(order.branch, self.branch)
        self.assertEqual(order.subtotal, Decimal("40.00"))
        self.assertEqual(order.discount_amount, Decimal("5.00"))
        self.assertEqual(order.vat_amount, Decimal("5.25"))
        self.assertEqual(order.grand_total, Decimal("40.25"))

        self.assertEqual(sale.order, order)
        self.assertEqual(sale.quantity, 2)
        self.assertEqual(sale.price_at_sale, Decimal("20.00"))
        self.assertEqual(sale.cost_at_sale, Decimal("10.00"))

        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 3)
        self.assertEqual(self.client.session.get("cart"), {})

    def test_finalize_order_rejects_insufficient_stock(self):
        self.client.login(username="cashier", password="pass12345")
        session = self.client.session
        session["cart"] = {str(self.stock.id): 99}
        session.save()

        response = self.client.post(reverse("finalize_order"), {"discount": "0.00"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Sale.objects.count(), 0)


class SalesExportTests(TestCase):
    def setUp(self):
        self.branch_main = Branch.objects.create(name="Main", code="MAIN")
        self.branch_north = Branch.objects.create(name="North", code="NORTH")
        self.manager = User.objects.create_user(username="manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_main},
        )

        category = Category.objects.create(name="Brakes")
        part_a = Part.objects.create(
            name="=Risky Name",
            part_number="BP-100",
            category=category,
            cost_price=Decimal("50.00"),
            selling_price=Decimal("80.00"),
        )
        part_b = Part.objects.create(
            name="Brake Sensor",
            part_number="BS-101",
            category=category,
            cost_price=Decimal("20.00"),
            selling_price=Decimal("30.00"),
        )

        Sale.objects.create(
            part=part_a,
            branch=self.branch_main,
            seller=self.manager,
            quantity=2,
            price_at_sale=Decimal("80.00"),
            cost_at_sale=Decimal("50.00"),
        )
        Sale.objects.create(
            part=part_b,
            branch=self.branch_north,
            seller=self.manager,
            quantity=1,
            price_at_sale=Decimal("30.00"),
            cost_at_sale=Decimal("20.00"),
        )

    def _decode_streaming_response(self, response):
        chunks = []
        for chunk in response.streaming_content:
            if isinstance(chunk, bytes):
                chunks.append(chunk)
            else:
                chunks.append(chunk.encode("utf-8"))
        return b"".join(chunks).decode("utf-8")

    def test_export_sales_csv_contains_bom_and_expected_columns(self):
        self.client.login(username="manager", password="pass12345")
        response = self.client.get(reverse("export_sales_csv"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("attachment; filename=", response["Content-Disposition"])

        decoded = self._decode_streaming_response(response)
        self.assertTrue(decoded.startswith("\ufeff"))
        self.assertIn(
            "Order ID,Part Name,Part Number,Branch,Seller,Quantity,Sale Price,Cost,Profit,Date Sold",
            decoded,
        )
        self.assertIn("BP-100", decoded)

    def test_export_sales_csv_sanitizes_formula_injection(self):
        self.client.login(username="manager", password="pass12345")
        response = self.client.get(reverse("export_sales_csv"))

        decoded = self._decode_streaming_response(response)
        self.assertIn("'=Risky Name", decoded)

    def test_export_sales_csv_branch_filter(self):
        self.client.login(username="manager", password="pass12345")
        response = self.client.get(reverse("export_sales_csv"), {"branch": self.branch_main.id})

        decoded = self._decode_streaming_response(response)
        self.assertIn("BP-100", decoded)
        self.assertNotIn("BS-101", decoded)


class RefundFlowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        category = Category.objects.create(name="Engine")
        self.part = Part.objects.create(
            name="Spark Plug",
            part_number="SP-001",
            category=category,
            cost_price=Decimal("5.00"),
            selling_price=Decimal("12.00"),
        )
        self.stock = Stock.objects.create(part=self.part, branch=self.branch, quantity=4)

        self.manager = User.objects.create_user(username="manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )

        self.cashier = User.objects.create_user(username="cashier", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch},
        )

        self.sale = Sale.objects.create(
            part=self.part,
            branch=self.branch,
            seller=self.manager,
            quantity=2,
            price_at_sale=Decimal("12.00"),
            cost_at_sale=Decimal("5.00"),
        )

    def test_manager_can_refund_sale_and_restock_inventory(self):
        self.client.login(username="manager", password="pass12345")
        response = self.client.post(reverse("refund_sale", args=[self.sale.id]), {"next": reverse("sales_history")})

        self.assertEqual(response.status_code, 302)
        self.sale.refresh_from_db()
        self.stock.refresh_from_db()

        self.assertTrue(self.sale.is_refunded)
        self.assertEqual(self.stock.quantity, 6)

    def test_refund_is_idempotent_stock_not_incremented_twice(self):
        self.client.login(username="manager", password="pass12345")
        self.client.post(reverse("refund_sale", args=[self.sale.id]), {"next": reverse("sales_history")})
        self.client.post(reverse("refund_sale", args=[self.sale.id]), {"next": reverse("sales_history")})

        self.sale.refresh_from_db()
        self.stock.refresh_from_db()

        self.assertTrue(self.sale.is_refunded)
        self.assertEqual(self.stock.quantity, 6)

    def test_cashier_cannot_refund_sale(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.post(reverse("refund_sale", args=[self.sale.id]), {"next": reverse("sales_history")})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)


class SaleModelTests(TestCase):
    def test_total_profit_is_zero_for_refunded_sale(self):
        user = User.objects.create_user(username="u1", password="pass12345")
        branch = Branch.objects.create(name="Main", code="MAIN")
        category = Category.objects.create(name="Electrical")
        part = Part.objects.create(
            name="Battery",
            part_number="BAT-12V",
            category=category,
            cost_price=Decimal("100.00"),
            selling_price=Decimal("150.00"),
        )

        sale = Sale.objects.create(
            part=part,
            branch=branch,
            seller=user,
            quantity=2,
            price_at_sale=Decimal("150.00"),
            cost_at_sale=Decimal("100.00"),
            is_refunded=True,
        )

        self.assertEqual(sale.total_profit, Decimal("0.00"))
