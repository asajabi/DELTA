from datetime import timedelta
from decimal import Decimal
from io import StringIO
import re
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core import mail
from django.db import IntegrityError
from django.db.models import Sum
from django.test import RequestFactory
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import BranchAdmin, PartAdmin, UserProfileAdmin
from .chat_assistant import detect_branches_in_text, parse_chat_message
from .models import (
    AuditLog,
    Branch,
    Category,
    CreditNote,
    CreditNoteLine,
    CustomerLedgerEntry,
    CycleCountLine,
    CycleCountSession,
    Location,
    Order,
    PartBarcode,
    PartBranchCost,
    Part,
    Payment,
    PasswordResetOtp,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReceipt,
    PurchaseReceiptLine,
    Sale,
    Stock,
    StockLocation,
    StockMovement,
    Ticket,
    TransferRequest,
    UserProfile,
    Vehicle,
    Vendor,
    add_stock_to_location,
    move_stock_between_locations,
    remove_stock_from_locations,
)


class AuthPermissionTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.cashier = User.objects.create_user(username="cashier", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch},
        )
        self.admin_user = User.objects.create_superuser(
            username="refund_admin",
            email="refund-admin@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin_user,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch},
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

    def test_search_vehicle_text_does_not_raise_value_error(self):
        category = Category.objects.create(name="Engine")
        vehicle = Vehicle.objects.create(make="Nissan", model="Patrol", year=2020)
        part = Part.objects.create(
            name="Oil Filter",
            part_number="OF-100",
            category=category,
            cost_price=Decimal("10.00"),
            selling_price=Decimal("20.00"),
        )
        part.compatible_vehicles.add(vehicle)
        Stock.objects.create(part=part, branch=self.branch, quantity=5)

        self.client.login(username="cashier", password="pass12345")
        response = self.client.get(reverse("part_search"), {"vehicle": "patrol"})
        self.assertEqual(response.status_code, 200)


class RuntimeRegressionTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="HQ", code="HQ")
        self.admin = User.objects.create_superuser(
            username="runtime_admin",
            email="runtime@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch},
        )
        self.client.login(username="runtime_admin", password="pass12345")

    def test_expected_chat_and_stock_location_aliases_exist(self):
        response_chat = self.client.get("/inventory/chat/")
        response_stock_by_location = self.client.get("/inventory/stock-by-location/")
        self.assertEqual(response_chat.status_code, 200)
        self.assertEqual(response_stock_by_location.status_code, 200)

    def test_main_inventory_pages_do_not_raise_server_error(self):
        pages = [
            "/inventory/search/",
            "/inventory/pos/",
            "/inventory/orders/",
            "/inventory/history/",
            "/inventory/transfers/",
            "/inventory/reports/",
            "/inventory/locations/",
            "/inventory/stock-by-location/",
            "/inventory/audit/",
            "/inventory/chat/",
        ]
        for path in pages:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotEqual(response.status_code, 500)

    def test_navbar_renders_toggle_menu_with_required_links(self):
        response = self.client.get(reverse("part_search"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="deltaMenuToggle"')
        self.assertContains(response, 'aria-controls="deltaNavPanel"')
        self.assertContains(response, 'id="deltaNavPanel"')
        self.assertNotContains(response, 'href="#"')
        self.assertContains(response, f'href="{reverse("part_search")}"')
        self.assertContains(response, f'href="{reverse("pos_console")}"')
        self.assertContains(response, f'href="{reverse("order_list")}"')
        self.assertContains(response, f'href="{reverse("transfer_list")}"')
        self.assertContains(response, f'href="{reverse("ticket_list")}"')
        self.assertContains(response, f'href="{reverse("analytics_assistant")}"')
        self.assertContains(response, f'href="{reverse("reports_dashboard")}"')
        self.assertContains(response, f'href="{reverse("low_stock_list")}"')
        self.assertContains(response, f'href="{reverse("location_list")}"')
        self.assertContains(response, f'href="{reverse("stock_locations_view")}"')
        self.assertContains(response, f'href="{reverse("audit_log_list")}"')
        self.assertContains(response, "البحث")
        self.assertContains(response, "نقطة البيع")
        self.assertContains(response, "الطلبات")
        self.assertContains(response, "التحويلات")
        self.assertContains(response, "الدعم الفني")
        self.assertContains(response, "المساعد الذكي")
        self.assertContains(response, "التقارير")
        self.assertContains(response, "تنبيه المخزون")
        self.assertContains(response, "المواقع")
        self.assertContains(response, "المخزون حسب الموقع")
        self.assertContains(response, "سجل التدقيق")
        self.assertContains(response, "تغيير")
        self.assertContains(response, "تسجيل الخروج")

    def test_navbar_marks_active_menu_link(self):
        response = self.client.get(reverse("part_search"))
        self.assertContains(response, 'delta-nav-link active')
        self.assertContains(response, 'aria-current="page"')

    def test_camera_scanner_modal_and_triggers_render_on_key_pages(self):
        search_response = self.client.get(reverse("part_search"))
        pos_response = self.client.get(reverse("pos_console"))
        stock_response = self.client.get(reverse("stock_locations_view"))

        for response in [search_response, pos_response, stock_response]:
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'id="barcodeScannerModal"')
            self.assertContains(response, "vendor/barcode/html5-qrcode.min.js")
            self.assertContains(response, "js/barcode_scanner.js")
            self.assertContains(response, "data-barcode-scan-target")

    def test_scanner_page_uses_local_barcode_library_not_cdn(self):
        response = self.client.get(reverse("scanner_view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vendor/barcode/html5-qrcode.min.js")
        self.assertNotContains(response, "zxing" + ".min.js")
        self.assertNotContains(response, "Barcode" + "Detector")
        self.assertNotContains(response, "unpkg.com")


class BranchAdminPermissionTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.superuser = User.objects.create_superuser(
            username="branch_super",
            email="branch-super@example.com",
            password="pass12345",
        )
        self.staff = User.objects.create_user(username="branch_staff", password="pass12345", is_staff=True)

    def test_only_superuser_can_add_or_delete_branch_in_admin(self):
        admin_model = BranchAdmin(Branch, admin.site)
        request = RequestFactory().get("/admin/")

        request.user = self.superuser
        self.assertTrue(admin_model.has_add_permission(request))
        self.assertTrue(admin_model.has_delete_permission(request, self.branch))

        request.user = self.staff
        self.assertFalse(admin_model.has_add_permission(request))
        self.assertFalse(admin_model.has_delete_permission(request, self.branch))


class NavbarBranchSelectorTests(TestCase):
    def setUp(self):
        Branch.objects.create(name="الصناعية القديمة", code="OLDIND")
        Branch.objects.create(name="مخرج 18", code="EX18")
        Branch.objects.create(name="شارع الجمعية", code="ASSN")
        self.extra_branch = Branch.objects.create(name="Branch X", code="X01")
        self.admin_user = User.objects.create_superuser(
            username="nav_admin",
            email="nav-admin@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin_user,
            defaults={"role": UserProfile.Roles.ADMIN},
        )
        self.client.login(username="nav_admin", password="pass12345")

    def test_admin_branch_switcher_shows_only_three_business_branches(self):
        response = self.client.get(reverse("part_search"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الصناعية القديمة")
        self.assertContains(response, "مخرج 18")
        self.assertContains(response, "شارع الجمعية")
        self.assertNotContains(response, "Branch X")


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
        self.part_c = Part.objects.create(
            name="Crossword Book",
            part_number="CB-1500",
            category=category,
            cost_price=Decimal("50.00"),
            selling_price=Decimal("120.00"),
        )

        self.stock_a = Stock.objects.create(part=self.part_a, branch=self.branch_a, quantity=5)
        self.stock_b = Stock.objects.create(part=self.part_b, branch=self.branch_b, quantity=5)
        self.stock_c = Stock.objects.create(part=self.part_c, branch=self.branch_a, quantity=11)

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
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].part_number, "OF-001")

    def test_short_random_query_does_not_return_unrelated_parts(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.get(reverse("part_search"), {"q": "xd"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["results"])

    def test_manager_global_search_can_lookup_other_branch_part(self):
        self.client.login(username="manager", password="pass12345")

        local_response = self.client.get(reverse("part_search"), {"q": "BF-001"})
        self.assertEqual(local_response.status_code, 200)
        self.assertIsNone(local_response.context["results"])

        global_response = self.client.get(reverse("part_search"), {"global_q": "BF-001"})
        self.assertEqual(global_response.status_code, 200)
        global_results = list(global_response.context["results"])
        self.assertEqual(len(global_results), 1)
        self.assertEqual(global_results[0].part_number, "BF-001")

    def test_ajax_suggestions_do_not_return_unrelated_result_for_random_text(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.get(
            reverse("part_search"),
            {"q": "dssdfsghj"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("results"), [])

    def test_ajax_suggestions_support_contains_for_three_or_more_chars(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.get(
            reverse("part_search"),
            {"q": "ross"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        returned_parts = [row.get("part_number") for row in payload.get("results", [])]
        self.assertIn("CB-1500", returned_parts)


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

    def test_sequential_checkouts_do_not_create_negative_inventory(self):
        self.client.login(username="cashier", password="pass12345")
        second_client = self.client_class()
        second_client.login(username="cashier", password="pass12345")

        session_a = self.client.session
        session_a["cart"] = {str(self.stock.id): 5}
        session_a.save()

        session_b = second_client.session
        session_b["cart"] = {str(self.stock.id): 1}
        session_b.save()

        first_response = self.client.post(reverse("finalize_order"), {"discount": "0.00"})
        second_response = second_client.post(reverse("finalize_order"), {"discount": "0.00"})

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 0)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Sale.objects.count(), 1)

    def test_receipt_shows_customer_phone_and_reprint(self):
        self.client.login(username="cashier", password="pass12345")
        session = self.client.session
        session["cart"] = {str(self.stock.id): 1}
        session.save()

        self.client.post(
            reverse("finalize_order"),
            {
                "discount": "0.00",
                "phone_number": "0507777777",
                "customer_name": "Phone Test",
            },
        )
        order = Order.objects.latest("id")
        response = self.client.get(reverse("receipt_view", args=[order.order_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0507777777")
        self.assertContains(response, "PRINT / REPRINT")


class ActiveBranchEnforcementTests(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(name="Main", code="MAIN")
        self.branch_b = Branch.objects.create(name="North", code="NORTH")
        category = Category.objects.create(name="Filters")
        self.part = Part.objects.create(
            name="Oil Filter",
            part_number="OF-ABR",
            category=category,
            cost_price=Decimal("10.00"),
            selling_price=Decimal("20.00"),
        )
        self.stock = Stock.objects.create(part=self.part, branch=self.branch_a, quantity=10)

        self.admin = User.objects.create_superuser(
            username="branch_admin",
            email="branch-admin@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch_a},
        )
        self.manager = User.objects.create_user(username="branch_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_a},
        )

    def _set_cart(self, qty=1):
        session = self.client.session
        session["cart"] = {str(self.stock.id): qty}
        session.save()

    def test_admin_finalize_order_requires_active_branch(self):
        self.client.login(username="branch_admin", password="pass12345")
        self._set_cart(qty=1)

        response = self.client.post(reverse("finalize_order"), {"discount": "0.00"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)
        self.assertContains(response, "يجب اختيار الفرع النشط")

    def test_admin_can_set_active_branch_and_finalize_order(self):
        self.client.login(username="branch_admin", password="pass12345")
        self.client.post(reverse("set_active_branch"), {"active_branch": str(self.branch_a.id)})
        self._set_cart(qty=2)

        response = self.client.post(reverse("finalize_order"), {"discount": "0.00"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().branch, self.branch_a)

    def test_non_admin_auto_uses_profile_branch_and_no_switcher(self):
        self.client.login(username="branch_manager", password="pass12345")
        response = self.client.get(reverse("pos_console"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الفرع الحالي")
        self.assertContains(response, self.branch_a.name)
        self.assertNotContains(response, 'name="active_branch"')

    def test_transfer_approval_requires_active_branch_for_admin(self):
        transfer = TransferRequest.objects.create(
            part=self.part,
            quantity=2,
            source_branch=self.branch_a,
            destination_branch=self.branch_b,
            requested_by=self.manager,
            notes="Need stock",
        )
        self.client.login(username="branch_admin", password="pass12345")

        response = self.client.post(reverse("transfer_approve", args=[transfer.id]), {"reason": "approved"}, follow=True)
        transfer.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(transfer.status, TransferRequest.Status.REQUESTED)
        self.assertContains(response, "يجب اختيار الفرع النشط")

    def test_transfer_create_requires_active_branch_for_admin(self):
        self.client.login(username="branch_admin", password="pass12345")

        response = self.client.post(
            reverse("transfer_create_from_stock", args=[self.stock.id]),
            {"quantity": "1", "destination_branch": str(self.branch_b.id), "notes": "request"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TransferRequest.objects.count(), 0)
        self.assertContains(response, "يجب اختيار الفرع النشط")

    def test_transfer_receive_requires_active_branch_for_admin(self):
        transfer = TransferRequest.objects.create(
            part=self.part,
            quantity=1,
            source_branch=self.branch_a,
            destination_branch=self.branch_b,
            requested_by=self.manager,
            status=TransferRequest.Status.DELIVERED,
            reserved_quantity=1,
        )
        self.client.login(username="branch_admin", password="pass12345")
        response = self.client.post(reverse("transfer_confirm_receive", args=[transfer.id]), {"reason": "received"}, follow=True)

        transfer.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(transfer.status, TransferRequest.Status.DELIVERED)
        self.assertContains(response, "يجب اختيار الفرع النشط")


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


class TicketWorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="الصناعية القديمة", code="OLDIND")
        self.tech = User.objects.create_user(username="abdullah", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.tech,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch},
        )
        self.saleh = User.objects.create_user(username="saleh_user", password="pass12345", is_staff=True)
        UserProfile.objects.update_or_create(
            user=self.saleh,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )
        self.osama = User.objects.create_user(username="osama_user", password="pass12345", is_staff=True)
        UserProfile.objects.update_or_create(
            user=self.osama,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )

    def test_non_tech_can_create_ticket_with_default_assignee(self):
        self.client.login(username="saleh_user", password="pass12345")
        response = self.client.post(
            reverse("ticket_create"),
            {
                "title": "Printer issue",
                "description": "Receipt printer is not responding.",
                "priority": Ticket.Priority.HIGH,
                "branch": str(self.branch.id),
            },
        )
        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.latest("id")
        self.assertEqual(ticket.reporter, self.saleh)
        self.assertEqual(ticket.assignee, self.tech)
        self.assertEqual(ticket.status, Ticket.Status.NEW)
        self.assertEqual(ticket.priority, Ticket.Priority.HIGH)
        self.assertEqual(ticket.branch, self.branch)

    def test_non_tech_sees_only_own_tickets(self):
        Ticket.objects.create(title="S1", description="d1", reporter=self.saleh, assignee=self.tech)
        Ticket.objects.create(title="S2", description="d2", reporter=self.osama, assignee=self.tech)

        self.client.login(username="saleh_user", password="pass12345")
        response = self.client.get(reverse("ticket_list"))
        self.assertEqual(response.status_code, 200)
        tickets = list(response.context["tickets"])
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].reporter, self.saleh)

    def test_non_tech_cannot_change_ticket_status(self):
        ticket = Ticket.objects.create(title="Status", description="d", reporter=self.saleh, assignee=self.tech)
        self.client.login(username="saleh_user", password="pass12345")
        response = self.client.post(reverse("ticket_detail", args=[ticket.id]), {"status": Ticket.Status.OPEN})
        self.assertEqual(response.status_code, 403)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.NEW)

    def test_tech_can_transition_status_and_audit_logged(self):
        ticket = Ticket.objects.create(title="Workflow", description="d", reporter=self.saleh, assignee=self.tech)
        self.client.login(username="abdullah", password="pass12345")

        self.client.post(reverse("ticket_detail", args=[ticket.id]), {"status": Ticket.Status.OPEN, "internal_notes": ""})
        self.client.post(
            reverse("ticket_detail", args=[ticket.id]),
            {"status": Ticket.Status.IN_PROGRESS, "internal_notes": "working"},
        )
        self.client.post(reverse("ticket_detail", args=[ticket.id]), {"status": Ticket.Status.FIXED, "internal_notes": "done"})

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.FIXED)
        self.assertTrue(
            AuditLog.objects.filter(
                action="ticket.status_change",
                object_type="Ticket",
                object_id=str(ticket.id),
            ).exists()
        )

    def test_invalid_transition_is_rejected(self):
        ticket = Ticket.objects.create(title="Transition", description="d", reporter=self.saleh, assignee=self.tech)
        self.client.login(username="abdullah", password="pass12345")
        response = self.client.post(reverse("ticket_detail", args=[ticket.id]), {"status": Ticket.Status.FIXED}, follow=True)
        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.NEW)

    def test_superuser_can_view_and_update_all_tickets(self):
        superuser = User.objects.create_superuser(
            username="ticket_super",
            email="ticket-super@example.com",
            password="pass12345",
        )
        ticket = Ticket.objects.create(title="Super", description="d", reporter=self.saleh, assignee=self.tech)
        self.client.login(username="ticket_super", password="pass12345")
        list_response = self.client.get(reverse("ticket_list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, ticket.title)

        update_response = self.client.post(
            reverse("ticket_detail", args=[ticket.id]),
            {"status": Ticket.Status.OPEN, "internal_notes": "opened by super"},
            follow=True,
        )
        self.assertEqual(update_response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.OPEN)


class ScanWorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="الصناعية القديمة", code="OLDIND")
        self.user = User.objects.create_user(username="scan_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.user,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )
        category = Category.objects.create(name="Scan")
        self.part = Part.objects.create(
            name="Scan Part",
            part_number="SCAN-001",
            barcode="1234567890",
            category=category,
            cost_price=Decimal("5.00"),
            selling_price=Decimal("10.00"),
        )
        self.stock = Stock.objects.create(part=self.part, branch=self.branch, quantity=10)
        self.location = Location.objects.create(branch=self.branch, code="A1", name_ar="رف 1")

    def test_scan_pos_adds_item_to_cart(self):
        self.client.login(username="scan_manager", password="pass12345")
        response = self.client.post(
            reverse("scan_dispatch"),
            {"mode": "pos", "scan_code": "1234567890", "quantity": "2"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("pos_console"), response.url)
        self.assertEqual(self.client.session.get("cart", {}).get(str(self.stock.id)), 2)

    def test_scan_add_redirects_to_stock_locations_with_prefill(self):
        self.client.login(username="scan_manager", password="pass12345")
        response = self.client.post(
            reverse("scan_dispatch"),
            {"mode": "add", "scan_code": "SCAN-001", "quantity": "3"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("stock_locations_view"), response.url)
        self.assertIn(f"scan_part={self.part.id}", response.url)
        self.assertIn("scan_mode=add", response.url)

    def test_scan_info_redirects_to_info_view(self):
        self.client.login(username="scan_manager", password="pass12345")
        response = self.client.post(
            reverse("scan_dispatch"),
            {"mode": "info", "scan_code": "1234567890"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("part_insight", args=[self.part.id]))

    def test_scan_resolve_returns_single_part_match(self):
        self.client.login(username="scan_manager", password="pass12345")
        response = self.client.post(reverse("scan_resolve"), {"scan_code": "1234567890"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["part"]["id"], self.part.id)

    def test_stock_scan_apply_add_updates_stock_locations(self):
        self.client.login(username="scan_manager", password="pass12345")
        self.client.post(
            reverse("stock_scan_apply"),
            {"action": "scan", "mode": "add", "scan_code": "1234567890", "quantity": "2"},
        )
        apply_response = self.client.post(
            reverse("stock_scan_apply"),
            {
                "action": "apply",
                "mode": "add",
                "to_location_id": str(self.location.id),
                "reason": "scanner_batch_test",
            },
            follow=True,
        )
        self.assertEqual(apply_response.status_code, 200)
        row = StockLocation.objects.get(part=self.part, branch=self.branch, location=self.location)
        self.assertEqual(row.quantity, 2)

    def test_pos_scan_requires_repeat_confirmation_and_supports_undo(self):
        self.client.login(username="scan_manager", password="pass12345")
        self.client.post(reverse("scan_dispatch"), {"mode": "pos", "scan_code": "1234567890", "quantity": "1"})
        self.assertEqual(self.client.session.get("cart", {}).get(str(self.stock.id)), 1)

        self.client.post(reverse("scan_dispatch"), {"mode": "pos", "scan_code": "1234567890", "quantity": "1"})
        self.assertEqual(self.client.session.get("cart", {}).get(str(self.stock.id)), 1)

        self.client.post(reverse("scan_dispatch"), {"mode": "pos", "scan_code": "1234567890", "quantity": "1"})
        self.assertEqual(self.client.session.get("cart", {}).get(str(self.stock.id)), 2)

        self.client.post(reverse("scan_dispatch"), {"mode": "pos", "action": "undo"})
        self.assertEqual(self.client.session.get("cart", {}).get(str(self.stock.id)), 1)


class LowStockThresholdTests(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(name="Main", code="MAIN")
        self.branch_b = Branch.objects.create(name="North", code="NORTH")
        category = Category.objects.create(name="Lubricants")

        self.part_a = Part.objects.create(
            name="Oil A",
            part_number="OIL-A",
            category=category,
            cost_price=Decimal("10.00"),
            selling_price=Decimal("15.00"),
        )
        self.part_b = Part.objects.create(
            name="Oil B",
            part_number="OIL-B",
            category=category,
            cost_price=Decimal("11.00"),
            selling_price=Decimal("16.00"),
        )
        self.part_c = Part.objects.create(
            name="Oil C",
            part_number="OIL-C",
            category=category,
            cost_price=Decimal("12.00"),
            selling_price=Decimal("17.00"),
        )

        self.stock_a = Stock.objects.create(part=self.part_a, branch=self.branch_a, quantity=5, min_stock_level=5)
        self.stock_b = Stock.objects.create(part=self.part_b, branch=self.branch_a, quantity=10, min_stock_level=3)
        self.stock_c = Stock.objects.create(part=self.part_c, branch=self.branch_b, quantity=4, min_stock_level=1)

        self.manager = User.objects.create_user(username="low_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_a},
        )
        self.admin = User.objects.create_superuser(
            username="low_admin",
            email="low-admin@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch_a},
        )

        TransferRequest.objects.create(
            part=self.part_b,
            quantity=8,
            source_branch=self.branch_a,
            destination_branch=self.branch_b,
            requested_by=self.manager,
            status=TransferRequest.Status.APPROVED,
            reserved_quantity=8,
        )

    def test_low_stock_uses_available_quantity_against_min_level(self):
        self.client.login(username="low_manager", password="pass12345")
        response = self.client.get(reverse("low_stock_list"))

        self.assertEqual(response.status_code, 200)
        rows = list(response.context["low_stock_items"])
        self.assertEqual(len(rows), 2)
        part_numbers = {row.part.part_number for row in rows}
        self.assertIn("OIL-A", part_numbers)
        self.assertIn("OIL-B", part_numbers)
        self.assertNotIn("OIL-C", part_numbers)

    def test_admin_can_filter_low_stock_by_branch(self):
        self.client.login(username="low_admin", password="pass12345")
        response = self.client.get(reverse("low_stock_list"), {"branch": str(self.branch_b.id)})

        self.assertEqual(response.status_code, 200)
        rows = list(response.context["low_stock_items"])
        self.assertEqual(rows, [])

    def test_export_low_stock_csv_contains_expected_rows(self):
        self.client.login(username="low_manager", password="pass12345")
        response = self.client.get(reverse("export_low_stock_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        content = response.content.decode("utf-8-sig")
        self.assertIn("Available,Reserved,Min Level,Part Name,Part Number,Branch", content)
        self.assertIn("OIL-A", content)
        self.assertIn("OIL-B", content)
        self.assertNotIn("OIL-C", content)


class ArabicEncodingTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="الصناعية القديمة", code="DBP-01")
        self.manager = User.objects.create_user(username="arabic_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )
        self.admin_user = User.objects.create_superuser(
            username="arabic_admin",
            email="arabic-admin@example.com",
            password="pass12345",
        )

    def test_arabic_location_name_renders_in_locations_page(self):
        Location.objects.create(branch=self.branch, code="AR-1", name_ar="رف خاص", name_en="")
        self.client.login(username="arabic_manager", password="pass12345")

        response = self.client.get(reverse("location_list"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("charset=utf-8", response.headers.get("Content-Type", "").lower())
        self.assertContains(response, "رف خاص")

    def test_admin_branch_list_renders_arabic_name(self):
        self.client.login(username="arabic_admin", password="pass12345")
        response = self.client.get("/admin/inventory/branch/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("charset=utf-8", response.headers.get("Content-Type", "").lower())
        self.assertContains(response, "الصناعية القديمة")


class ArabicRepairCommandTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="???????? ???????", code="DBP-01")
        self.location = Location.objects.create(
            branch=self.branch,
            code="A3",
            name_ar="?? 3",
            name_en="",
        )

    def test_dry_run_does_not_modify_data(self):
        call_command("repair_arabic_text")
        self.branch.refresh_from_db()
        self.location.refresh_from_db()

        self.assertEqual(self.branch.name, "???????? ???????")
        self.assertEqual(self.location.name_ar, "?? 3")

    def test_apply_repairs_known_corrupted_values(self):
        call_command("repair_arabic_text", apply=True)
        self.branch.refresh_from_db()
        self.location.refresh_from_db()

        self.assertEqual(self.branch.name, "الصناعية القديمة")
        self.assertEqual(self.location.name_ar, "رف 3")

        # Idempotency: running again should keep same values.
        call_command("repair_arabic_text", apply=True)
        self.branch.refresh_from_db()
        self.location.refresh_from_db()
        self.assertEqual(self.branch.name, "الصناعية القديمة")
        self.assertEqual(self.location.name_ar, "رف 3")


class EnforceBranchesCommandTests(TestCase):
    def setUp(self):
        self.branch_old = Branch.objects.create(name="الصناعية القديمة", code="OLDIND")
        self.branch_old_dup = Branch.objects.create(name="???????? ???????", code="OLDDBG")
        self.branch_exit_alias = Branch.objects.create(name="فرع مخرج 18", code="DBP-02")
        self.branch_jam_alias = Branch.objects.create(name="فرع الجمعية", code="ASSN")

        self.user = User.objects.create_user(username="branch_fix_user", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.user,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_old_dup},
        )

        category = Category.objects.create(name="BranchFix")
        self.part = Part.objects.create(
            name="Branch Fix Part",
            part_number="BR-FIX-1",
            category=category,
            cost_price=Decimal("10.00"),
            selling_price=Decimal("20.00"),
        )
        self.location = Location.objects.create(branch=self.branch_old_dup, code="A3", name_ar="?? 3")
        self.stock_location = StockLocation.objects.create(
            part=self.part,
            branch=self.branch_old_dup,
            location=self.location,
            quantity=5,
        )

    def test_dry_run_keeps_existing_branches(self):
        call_command("enforce_branches", stdout=StringIO())

        self.assertEqual(Branch.objects.count(), 4)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile.branch_id, self.branch_old_dup.id)

    def test_apply_reassigns_and_keeps_only_three_branches(self):
        call_command("enforce_branches", apply=True, stdout=StringIO())

        names = set(Branch.objects.values_list("name", flat=True))
        self.assertEqual(
            names,
            {"الصناعية القديمة", "مخرج 18", "شارع الجمعية"},
        )
        self.assertEqual(Branch.objects.count(), 3)

        self.user.refresh_from_db()
        self.assertEqual(self.user.profile.branch.name, "الصناعية القديمة")
        self.stock_location.refresh_from_db()
        self.location.refresh_from_db()
        self.assertEqual(self.stock_location.branch.name, "الصناعية القديمة")
        self.assertEqual(self.location.branch.name, "الصناعية القديمة")

    def test_apply_stops_when_unresolved_branch_has_data(self):
        unresolved = Branch.objects.create(name="Unknown Branch", code="UNK")
        Location.objects.create(branch=unresolved, code="B9", name_ar="رف 9")

        with self.assertRaises(CommandError):
            call_command("enforce_branches", apply=True, stdout=StringIO())

    def test_apply_with_explicit_map_resolves_ambiguous_code(self):
        unresolved = Branch.objects.create(name="Unknown Branch", code="UNK")
        Location.objects.create(branch=unresolved, code="B9", name_ar="رف 9")

        call_command("enforce_branches", apply=True, map=["UNK=old"], stdout=StringIO())
        self.assertEqual(
            set(Branch.objects.values_list("name", flat=True)),
            {"الصناعية القديمة", "مخرج 18", "شارع الجمعية"},
        )


class DeltaCleanupBranchesCommandTests(TestCase):
    def setUp(self):
        self.branch_old = Branch.objects.create(name="الصناعية القديمة", code="OLDIND")
        self.branch_bad = Branch.objects.create(name="???????? ???????", code="OLDDBG")
        self.branch_exit_alias = Branch.objects.create(name="فرع مخرج 18", code="DBP-02")
        self.branch_jam_alias = Branch.objects.create(name="فرع الجمعية", code="ASSN")

        category = Category.objects.create(name="Cleanup")
        self.part = Part.objects.create(
            name="Cleanup Part",
            part_number="CLN-001",
            category=category,
            cost_price=Decimal("1.00"),
            selling_price=Decimal("2.00"),
        )
        self.location = Location.objects.create(branch=self.branch_bad, code="A1", name_ar="?? 1")
        StockLocation.objects.create(part=self.part, branch=self.branch_bad, location=self.location, quantity=2)

    def test_dry_run_does_not_change_branch_set(self):
        call_command("delta_cleanup_branches", stdout=StringIO())
        self.assertEqual(Branch.objects.count(), 4)

    def test_apply_keeps_only_three_required_arabic_branches(self):
        call_command("delta_cleanup_branches", apply=True, stdout=StringIO())
        self.assertEqual(
            set(Branch.objects.values_list("name", flat=True)),
            {"الصناعية القديمة", "مخرج 18", "شارع الجمعية"},
        )
        self.assertEqual(Branch.objects.count(), 3)

    def test_unknown_branch_is_merged_into_default_branch(self):
        unknown = Branch.objects.create(name="Unknown ???", code="UNKN")
        user = User.objects.create_user(username="cleanup_unknown", password="pass12345")
        UserProfile.objects.update_or_create(
            user=user,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": unknown},
        )

        call_command("delta_cleanup_branches", apply=True, stdout=StringIO())

        user.refresh_from_db()
        self.assertEqual(user.profile.branch.name, "الصناعية القديمة")
        self.assertEqual(Branch.objects.count(), 3)


class ArabicGarbageCleanupCommandTests(TestCase):
    def setUp(self):
        self.branch_bad = Branch.objects.create(name="???? ??", code="BAD1")
        self.branch_ok = Branch.objects.create(name="الصناعية القديمة", code="OLDIND")
        self.location_bad = Location.objects.create(
            branch=self.branch_bad,
            code="A9",
            name_ar="?? 9",
            name_en="???",
        )

    def test_dry_run_reports_without_mutation(self):
        call_command("delta_cleanup_garbled_arabic", stdout=StringIO())
        self.assertTrue(Branch.objects.filter(id=self.branch_bad.id).exists())
        self.location_bad.refresh_from_db()
        self.assertEqual(self.location_bad.name_ar, "?? 9")

    def test_apply_removes_garbled_branch_and_keeps_only_canonical_three(self):
        call_command("delta_cleanup_garbled_arabic", apply=True, stdout=StringIO())
        self.assertFalse(Branch.objects.filter(id=self.branch_bad.id).exists())
        self.assertEqual(
            set(Branch.objects.values_list("name", flat=True)),
            {"الصناعية القديمة", "مخرج 18", "شارع الجمعية"},
        )


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
        response = self.client.post(
            reverse("refund_sale", args=[self.sale.id]),
            {"next": reverse("sales_history"), "reason": "customer returned wrong item"},
        )

        self.assertEqual(response.status_code, 302)
        self.sale.refresh_from_db()
        self.stock.refresh_from_db()

        self.assertTrue(self.sale.is_refunded)
        self.assertEqual(self.stock.quantity, 6)

    def test_refund_is_idempotent_stock_not_incremented_twice(self):
        self.client.login(username="manager", password="pass12345")
        payload = {"next": reverse("sales_history"), "reason": "idempotency check"}
        self.client.post(reverse("refund_sale", args=[self.sale.id]), payload)
        self.client.post(reverse("refund_sale", args=[self.sale.id]), payload)

        self.sale.refresh_from_db()
        self.stock.refresh_from_db()

        self.assertTrue(self.sale.is_refunded)
        self.assertEqual(self.stock.quantity, 6)
        self.assertEqual(
            StockMovement.objects.filter(action="refund_in", part=self.part, branch=self.branch).count(),
            1,
        )

    def test_refund_updates_location_quantities_and_logs_movement(self):
        self.client.login(username="manager", password="pass12345")
        self.client.post(
            reverse("refund_sale", args=[self.sale.id]),
            {"next": reverse("sales_history"), "reason": "restock check"},
        )

        self.stock.refresh_from_db()
        location_total = (
            StockLocation.objects.filter(part=self.part, branch=self.branch).aggregate(total=Sum("quantity"))["total"] or 0
        )

        self.assertEqual(self.stock.quantity, 6)
        self.assertEqual(location_total, 6)
        self.assertTrue(
            StockMovement.objects.filter(
                action="refund_in",
                part=self.part,
                branch=self.branch,
                qty=self.sale.quantity,
            ).exists()
        )

    def test_cashier_cannot_refund_sale(self):
        self.client.login(username="cashier", password="pass12345")
        response = self.client.post(
            reverse("refund_sale", args=[self.sale.id]),
            {"next": reverse("sales_history"), "reason": "cashier should not refund"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_refund_requires_reason(self):
        self.client.login(username="manager", password="pass12345")
        response = self.client.post(reverse("refund_sale", args=[self.sale.id]), {"next": reverse("sales_history")})
        self.assertEqual(response.status_code, 302)
        self.sale.refresh_from_db()
        self.assertFalse(self.sale.is_refunded)


class TransferWorkflowTests(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(name="Main", code="MAIN")
        self.branch_b = Branch.objects.create(name="North", code="NORTH")

        category = Category.objects.create(name="Suspension")
        self.part = Part.objects.create(
            name="Control Arm",
            part_number="CA-001",
            category=category,
            cost_price=Decimal("100.00"),
            selling_price=Decimal("150.00"),
        )

        self.stock_a = Stock.objects.create(part=self.part, branch=self.branch_a, quantity=1)
        self.stock_b = Stock.objects.create(part=self.part, branch=self.branch_b, quantity=10)

        self.cashier_a = User.objects.create_user(username="cashier_a", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier_a,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch_a},
        )

        self.manager_a = User.objects.create_user(username="manager_a", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager_a,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_a},
        )

        self.manager_b = User.objects.create_user(username="manager_b", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager_b,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_b},
        )

        self.admin_user = User.objects.create_superuser(
            username="admin_transfer",
            email="admin-transfer@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin_user,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch_a},
        )

    def _create_requested_transfer(self, qty=4):
        return TransferRequest.objects.create(
            part=self.part,
            quantity=qty,
            source_branch=self.branch_b,
            destination_branch=self.branch_a,
            requested_by=self.cashier_a,
        )

    def test_approval_reserves_stock_and_limits_available_quantity(self):
        transfer = self._create_requested_transfer(qty=8)

        self.client.login(username="manager_b", password="pass12345")
        response = self.client.post(reverse("transfer_approve", args=[transfer.id]), {"reason": "stock available for transfer"})
        self.assertEqual(response.status_code, 302)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, TransferRequest.Status.APPROVED)
        self.assertEqual(transfer.reserved_quantity, 8)

        ajax_response = self.client.post(
            reverse("add_to_cart", args=[self.stock_b.id]),
            {"quantity": 3},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(ajax_response.status_code, 400)

    def test_completion_moves_stock_and_releases_reservation(self):
        transfer = self._create_requested_transfer(qty=4)

        self.client.login(username="manager_b", password="pass12345")
        self.client.post(reverse("transfer_approve", args=[transfer.id]), {"reason": "approved for urgent demand"})
        self.client.post(reverse("transfer_mark_picked_up", args=[transfer.id]), {"reason": "driver collected parts"})
        self.client.post(reverse("transfer_mark_delivered", args=[transfer.id]), {"reason": "driver delivered to branch"})

        self.client.logout()
        self.client.login(username="cashier_a", password="pass12345")
        receive_response = self.client.post(
            reverse("transfer_confirm_receive", args=[transfer.id]),
            {"reason": "destination branch verified receipt"},
        )
        self.assertEqual(receive_response.status_code, 302)

        transfer.refresh_from_db()
        self.stock_a.refresh_from_db()
        self.stock_b.refresh_from_db()

        self.assertEqual(transfer.status, TransferRequest.Status.RECEIVED)
        self.assertEqual(transfer.reserved_quantity, 0)
        self.assertEqual(transfer.received_by, self.cashier_a)
        self.assertEqual(self.stock_b.quantity, 6)
        self.assertEqual(self.stock_a.quantity, 5)

    def test_transfer_permissions_cashier_manager_admin(self):
        self.client.login(username="cashier_a", password="pass12345")
        create_response = self.client.post(
            reverse("transfer_create_from_stock", args=[self.stock_b.id]),
            {
                "quantity": 2,
                "destination_branch": self.branch_b.id,
                "notes": "Need urgent",
            },
        )
        self.assertEqual(create_response.status_code, 302)

        transfer = TransferRequest.objects.latest("id")
        self.assertEqual(transfer.destination_branch, self.branch_a)

        self.client.logout()
        self.client.login(username="manager_a", password="pass12345")
        manager_a_response = self.client.post(reverse("transfer_approve", args=[transfer.id]), {"reason": "should be forbidden"})
        self.assertEqual(manager_a_response.status_code, 403)

        self.client.logout()
        self.client.login(username="admin_transfer", password="pass12345")
        session = self.client.session
        session["active_branch_id"] = self.branch_b.id
        session.save()
        admin_response = self.client.post(reverse("transfer_approve", args=[transfer.id]), {"reason": "admin override"})
        self.assertEqual(admin_response.status_code, 302)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, TransferRequest.Status.APPROVED)

    def test_transfer_approve_requires_reason(self):
        transfer = self._create_requested_transfer(qty=2)
        self.client.login(username="manager_b", password="pass12345")
        response = self.client.post(reverse("transfer_approve", args=[transfer.id]), {})
        self.assertEqual(response.status_code, 302)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, TransferRequest.Status.REQUESTED)

    def test_rejecting_approved_transfer_releases_reservation(self):
        transfer = self._create_requested_transfer(qty=2)
        self.client.login(username="manager_b", password="pass12345")
        self.client.post(reverse("transfer_approve", args=[transfer.id]), {"reason": "approve first"})

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, TransferRequest.Status.APPROVED)
        self.assertEqual(transfer.reserved_quantity, 2)

        reject_response = self.client.post(reverse("transfer_reject", args=[transfer.id]), {"reason": "cancel approved"})
        self.assertEqual(reject_response.status_code, 302)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, TransferRequest.Status.REJECTED)
        self.assertEqual(transfer.reserved_quantity, 0)

    def test_transfer_list_has_new_request_cta(self):
        self.client.login(username="manager_a", password="pass12345")
        response = self.client.get(reverse("transfer_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("transfer_create"))
        self.assertContains(response, "طلب تحويل جديد")

    def test_new_transfer_request_page_creates_transfer(self):
        self.client.login(username="manager_a", password="pass12345")
        response = self.client.post(
            reverse("transfer_create"),
            {
                "part_id": self.part.id,
                "quantity": "3",
                "to_branch": str(self.branch_b.id),
                "notes": "manual form request",
            },
        )
        self.assertEqual(response.status_code, 302)
        transfer = TransferRequest.objects.latest("id")
        self.assertEqual(transfer.part, self.part)
        self.assertEqual(transfer.quantity, 3)
        self.assertEqual(transfer.source_branch, self.branch_a)
        self.assertEqual(transfer.destination_branch, self.branch_b)


class AuditLogTests(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(name="Main", code="MAIN")
        self.branch_b = Branch.objects.create(name="North", code="NORTH")

        category = Category.objects.create(name="Brakes")
        self.part = Part.objects.create(
            name="Brake Pad",
            part_number="BP-200",
            category=category,
            cost_price=Decimal("40.00"),
            selling_price=Decimal("70.00"),
        )
        self.stock_a = Stock.objects.create(part=self.part, branch=self.branch_a, quantity=4)
        self.stock_b = Stock.objects.create(part=self.part, branch=self.branch_b, quantity=12)

        self.cashier_a = User.objects.create_user(username="cashier_audit", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier_a,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch_a, "employee_id": "EMP-C01"},
        )

        self.manager_b = User.objects.create_user(username="manager_audit", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager_b,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_b, "employee_id": "EMP-M01"},
        )

        self.admin_user = User.objects.create_superuser(
            username="admin_audit",
            email="admin-audit@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin_user,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch_a, "employee_id": "EMP-A01"},
        )

    def test_sale_and_refund_create_audit_logs(self):
        self.client.login(username="cashier_audit", password="pass12345")
        session = self.client.session
        session["cart"] = {str(self.stock_a.id): 1}
        session.save()

        self.client.post(reverse("finalize_order"), {"discount": "0.00"})
        self.assertTrue(AuditLog.objects.filter(action="sale.create").exists())
        self.assertTrue(
            any(
                row.after_data.get("reason") == "sale_create"
                for row in AuditLog.objects.filter(action="stock.adjustment")
            )
        )

        sale = Sale.objects.latest("id")
        self.client.logout()
        self.client.login(username="admin_audit", password="pass12345")
        session = self.client.session
        session["active_branch_id"] = self.branch_a.id
        session.save()
        self.client.post(reverse("refund_sale", args=[sale.id]), {"reason": "quality issue"})

        self.assertTrue(AuditLog.objects.filter(action="sale.refund", object_id=str(sale.id)).exists())
        self.assertTrue(
            AuditLog.objects.filter(action="sale.refund", object_id=str(sale.id), reason="quality issue").exists()
        )
        self.assertTrue(
            any(
                row.after_data.get("reason") == "sale_refund"
                for row in AuditLog.objects.filter(action="stock.adjustment")
            )
        )

    def test_transfer_request_approve_receive_create_audit_logs(self):
        self.client.login(username="cashier_audit", password="pass12345")
        self.client.post(
            reverse("transfer_create_from_stock", args=[self.stock_b.id]),
            {"quantity": 3, "notes": "branch demand spike"},
        )
        transfer = TransferRequest.objects.latest("id")

        self.client.logout()
        self.client.login(username="manager_audit", password="pass12345")
        self.client.post(reverse("transfer_approve", args=[transfer.id]), {"reason": "approved by manager"})
        self.client.post(reverse("transfer_mark_picked_up", args=[transfer.id]), {"reason": "driver picked items"})
        self.client.post(reverse("transfer_mark_delivered", args=[transfer.id]), {"reason": "driver delivered items"})

        self.client.logout()
        self.client.login(username="cashier_audit", password="pass12345")
        self.client.post(reverse("transfer_confirm_receive", args=[transfer.id]), {"reason": "received in destination"})

        self.assertTrue(AuditLog.objects.filter(action="transfer.request", object_id=str(transfer.id)).exists())
        self.assertTrue(AuditLog.objects.filter(action="transfer.approve", object_id=str(transfer.id)).exists())
        self.assertTrue(AuditLog.objects.filter(action="transfer.receive", object_id=str(transfer.id)).exists())
        self.assertTrue(
            any(
                row.after_data.get("reason") == "transfer_receive_out"
                for row in AuditLog.objects.filter(action="stock.adjustment")
            )
        )
        self.assertTrue(
            any(
                row.after_data.get("reason") == "transfer_receive_in"
                for row in AuditLog.objects.filter(action="stock.adjustment")
            )
        )

    def test_admin_page_filters_and_admin_only_access(self):
        AuditLog.objects.create(
            actor=self.admin_user,
            actor_username="admin_audit",
            actor_employee_id="EMP-A01",
            action="sale.create",
            reason="seed_test_row",
            object_type="Sale",
            object_id="99",
            branch=self.branch_a,
            before_data={},
            after_data={"value": 1},
        )

        self.client.login(username="cashier_audit", password="pass12345")
        cashier_response = self.client.get(reverse("audit_log_list"))
        self.assertEqual(cashier_response.status_code, 302)

        self.client.logout()
        self.client.login(username="admin_audit", password="pass12345")
        admin_response = self.client.get(reverse("audit_log_list"), {"employee": "EMP-A01", "action": "sale.create"})
        self.assertEqual(admin_response.status_code, 200)
        self.assertGreaterEqual(len(admin_response.context["logs"]), 1)

    def test_admin_model_price_and_role_changes_create_logs(self):
        request = RequestFactory().post("/admin/")
        request.user = self.admin_user

        part_admin = PartAdmin(Part, admin.site)
        self.part.selling_price = Decimal("75.00")
        self.part.cost_price = Decimal("42.00")
        part_admin.save_model(request, self.part, form=None, change=True)

        profile = self.cashier_a.profile
        profile_admin = UserProfileAdmin(UserProfile, admin.site)
        profile.role = UserProfile.Roles.MANAGER
        profile_admin.save_model(request, profile, form=None, change=True)

        self.assertTrue(AuditLog.objects.filter(action="price.change", object_type="Part").exists())
        self.assertTrue(AuditLog.objects.filter(action="role.change", object_type="UserProfile").exists())


class ChatAssistantTests(TestCase):
    def setUp(self):
        self.branch_old = Branch.objects.create(name="الصناعية القديمة", code="OLD")
        self.branch_exit18 = Branch.objects.create(name="مخرج 18", code="EXIT18")
        self.branch_jam = Branch.objects.create(name="الجمعية", code="JAM")

        category = Category.objects.create(name="Oils")
        self.part_oil_1 = Part.objects.create(
            name="Engine Oil 5W30",
            part_number="OIL-1",
            category=category,
            cost_price=Decimal("20.00"),
            selling_price=Decimal("30.00"),
        )
        self.part_oil_2 = Part.objects.create(
            name="Gear Oil 80W90",
            part_number="OIL-2",
            category=category,
            cost_price=Decimal("18.00"),
            selling_price=Decimal("28.00"),
        )

        self.location_old_a3 = Location.objects.create(branch=self.branch_old, code="A3", name_ar="رف 3")
        self.location_old_b1 = Location.objects.create(branch=self.branch_old, code="B1", name_ar="رف 1")
        self.location_jam_a1 = Location.objects.create(branch=self.branch_jam, code="A1", name_ar="رف 1")

        add_stock_to_location(
            part=self.part_oil_1,
            branch=self.branch_old,
            location=self.location_old_a3,
            quantity=10,
            reason="seed",
            action="seed",
        )
        add_stock_to_location(
            part=self.part_oil_2,
            branch=self.branch_old,
            location=self.location_old_a3,
            quantity=5,
            reason="seed",
            action="seed",
        )
        add_stock_to_location(
            part=self.part_oil_1,
            branch=self.branch_exit18,
            location=Location.objects.create(branch=self.branch_exit18, code="A1", name_ar="رف 1"),
            quantity=8,
            reason="seed",
            action="seed",
        )
        add_stock_to_location(
            part=self.part_oil_1,
            branch=self.branch_jam,
            location=self.location_jam_a1,
            quantity=3,
            reason="seed",
            action="seed",
        )
        StockLocation.objects.get_or_create(
            part=self.part_oil_1,
            branch=self.branch_old,
            location=self.location_old_b1,
            defaults={"quantity": 0},
        )

        self.cashier = User.objects.create_user(username="chat_cashier", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch_jam, "employee_id": "C-CHAT-1"},
        )
        self.manager = User.objects.create_user(username="chat_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_old, "employee_id": "M-CHAT-1"},
        )
        self.admin_user = User.objects.create_superuser(
            username="chat_admin",
            email="chat-admin@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.admin_user,
            defaults={"role": UserProfile.Roles.ADMIN, "branch": self.branch_old, "employee_id": "A-CHAT-1"},
        )

    def test_parse_chat_message_detects_keywords_quantity_and_locations(self):
        parsed_add = parse_chat_message("وصل 5 OIL-1 في الصناعية القديمة رف 3")
        self.assertEqual(parsed_add["action"], "add_stock")
        self.assertEqual(parsed_add["qty"], 5)
        self.assertIn("رف 3", parsed_add["location_hints"])

        parsed_remove = parse_chat_message("خصم 2 OIL-1 في الصناعية القديمة A3 تالف")
        self.assertEqual(parsed_remove["action"], "remove_stock")
        self.assertEqual(parsed_remove["qty"], 2)
        self.assertIn("A3", parsed_remove["location_hints"])

        parsed_move = parse_chat_message("move 1 OIL-1 from A3 to B1 in الصناعية القديمة")
        self.assertEqual(parsed_move["action"], "move_stock")
        self.assertEqual(parsed_move["qty"], 1)
        self.assertIn("A3", parsed_move["location_hints"])
        self.assertIn("B1", parsed_move["location_hints"])

        parsed_lookup = parse_chat_message("where is OIL-1")
        self.assertEqual(parsed_lookup["action"], "lookup_stock")
        self.assertEqual(parsed_lookup["part_query"], "oil-1")

    def test_detect_branch_synonyms_in_arabic(self):
        branches = detect_branches_in_text("transfer from مخرج 18 to الجمعية", Branch.objects.all())
        self.assertGreaterEqual(len(branches), 2)
        self.assertEqual(branches[0].id, self.branch_exit18.id)
        self.assertEqual(branches[1].id, self.branch_jam.id)

    def test_lookup_single_word_oil_asks_for_clarification(self):
        self.client.login(username="chat_manager", password="pass12345")
        response = self.client.post(reverse("analytics_assistant"), {"message": "oil"})
        self.assertEqual(response.status_code, 200)
        last_message = response.context["chat_history"][-1]["content"].lower()
        self.assertIn("multiple parts", last_message)
        self.assertIn("oil-1", last_message)
        self.assertIn("oil-2", last_message)

    def test_lookup_where_is_part_number_finds_stock(self):
        self.client.login(username="chat_manager", password="pass12345")
        response = self.client.post(reverse("analytics_assistant"), {"message": "where is OIL-1"})
        self.assertEqual(response.status_code, 200)
        last_message = response.context["chat_history"][-1]["content"].lower()
        self.assertNotIn("no stock matched", last_message)
        self.assertIn("oil-1", last_message)

    def test_cashier_cannot_adjust_stock_by_chat(self):
        self.client.login(username="chat_cashier", password="pass12345")
        response = self.client.post(
            reverse("analytics_assistant"),
            {"message": "add 2 OIL-1 in الجمعية A1 reason receive"},
        )
        self.assertEqual(response.status_code, 200)
        last_message = response.context["chat_history"][-1]["content"]
        self.assertIn("Only manager/admin can adjust stock", last_message)
        self.assertIsNone(self.client.session.get("assistant_chat_pending_action"))

    def test_manager_cannot_adjust_other_branch(self):
        self.client.login(username="chat_manager", password="pass12345")
        response = self.client.post(
            reverse("analytics_assistant"),
            {"message": "add 2 OIL-1 in JAM A1 reason receive"},
        )
        self.assertEqual(response.status_code, 200)
        last_message = response.context["chat_history"][-1]["content"]
        self.assertIn("Managers can adjust stock only in their own branch", last_message)

    def test_write_requires_confirmation_and_updates_stock(self):
        self.client.login(username="chat_manager", password="pass12345")
        stock_before = Stock.objects.get(part=self.part_oil_1, branch=self.branch_old).quantity

        draft_response = self.client.post(
            reverse("analytics_assistant"),
            {"message": "add 2 OIL-1 in الصناعية القديمة A3 reason receive shipment"},
        )
        self.assertEqual(draft_response.status_code, 200)
        self.assertIsNotNone(self.client.session.get("assistant_chat_pending_action"))
        self.assertEqual(Stock.objects.get(part=self.part_oil_1, branch=self.branch_old).quantity, stock_before)

        confirm_response = self.client.post(
            reverse("analytics_assistant"),
            {"message": "confirm"},
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertIsNone(self.client.session.get("assistant_chat_pending_action"))
        self.assertEqual(
            Stock.objects.get(part=self.part_oil_1, branch=self.branch_old).quantity,
            stock_before + 2,
        )
        self.assertTrue(
            StockMovement.objects.filter(
                action="assistant_add",
                part=self.part_oil_1,
                branch=self.branch_old,
                qty=2,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="assistant.stock.add",
                branch=self.branch_old,
            ).exists()
        )

    def test_move_stock_confirm_updates_locations(self):
        self.client.login(username="chat_manager", password="pass12345")
        stock_a_before = StockLocation.objects.get(
            part=self.part_oil_1,
            branch=self.branch_old,
            location=self.location_old_a3,
        ).quantity
        stock_b_before = StockLocation.objects.get(
            part=self.part_oil_1,
            branch=self.branch_old,
            location=self.location_old_b1,
        ).quantity

        self.client.post(
            reverse("analytics_assistant"),
            {"message": "move 1 OIL-1 in الصناعية القديمة from A3 to B1 reason rebalance"},
        )
        pending = self.client.session.get("assistant_chat_pending_action")
        self.assertIsNotNone(pending)
        self.client.post(reverse("analytics_assistant"), {"message": "confirm"})

        stock_a_after = StockLocation.objects.get(
            part=self.part_oil_1,
            branch=self.branch_old,
            location=self.location_old_a3,
        ).quantity
        stock_b_after = StockLocation.objects.get(
            part=self.part_oil_1,
            branch=self.branch_old,
            location=self.location_old_b1,
        ).quantity
        self.assertEqual(stock_a_after, stock_a_before - 1)
        self.assertEqual(stock_b_after, stock_b_before + 1)
        self.assertTrue(
            StockMovement.objects.filter(
                action="assistant_move",
                part=self.part_oil_1,
                branch=self.branch_old,
                qty=1,
            ).exists()
        )

    def test_cashier_can_create_transfer_request_with_confirmation(self):
        self.client.login(username="chat_cashier", password="pass12345")

        self.client.post(
            reverse("analytics_assistant"),
            {"message": "transfer 3 OIL-1 from مخرج 18 to الجمعية note urgent demand"},
        )
        self.assertIsNotNone(self.client.session.get("assistant_chat_pending_action"))
        self.assertEqual(TransferRequest.objects.count(), 0)

        self.client.post(reverse("analytics_assistant"), {"message": "confirm"})
        transfer = TransferRequest.objects.latest("id")
        self.assertEqual(transfer.part, self.part_oil_1)
        self.assertEqual(transfer.quantity, 3)
        self.assertEqual(transfer.source_branch, self.branch_exit18)
        self.assertEqual(transfer.destination_branch, self.branch_jam)
        self.assertTrue(
            StockMovement.objects.filter(
                action="assistant_transfer_request",
                part=self.part_oil_1,
                branch=self.branch_exit18,
                qty=3,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="transfer.request",
                object_id=str(transfer.id),
            ).exists()
        )

    @patch("inventory.views.assistant_llm_enabled", return_value=True)
    @patch("inventory.views.generate_assistant_plan")
    def test_live_ai_chat_mode_uses_model_reply(self, mock_generate_plan, _mock_enabled):
        self.client.login(username="chat_manager", password="pass12345")
        mock_generate_plan.return_value = {
            "mode": "chat",
            "assistant_reply": "I can help with stock checks and transfer workflows.",
            "command_text": "",
        }

        response = self.client.post(reverse("analytics_assistant"), {"message": "hello there"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("help with stock checks", response.context["chat_history"][-1]["content"])
        self.assertIsNone(self.client.session.get("assistant_chat_pending_action"))
        mock_generate_plan.assert_called_once()

    @patch("inventory.views.assistant_llm_enabled", return_value=True)
    @patch("inventory.views.generate_assistant_plan")
    def test_live_ai_command_mode_still_requires_confirm_for_write(self, mock_generate_plan, _mock_enabled):
        self.client.login(username="chat_manager", password="pass12345")
        stock_before = Stock.objects.get(part=self.part_oil_1, branch=self.branch_old).quantity
        mock_generate_plan.return_value = {
            "mode": "command",
            "assistant_reply": "I prepared a stock receipt action for approval.",
            "command_text": "add 2 OIL-1 in OLD A3 reason receive shipment",
        }

        response = self.client.post(reverse("analytics_assistant"), {"message": "please receive two units"})
        self.assertEqual(response.status_code, 200)
        pending = self.client.session.get("assistant_chat_pending_action")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "add_stock")
        self.assertEqual(Stock.objects.get(part=self.part_oil_1, branch=self.branch_old).quantity, stock_before)

        self.client.post(reverse("analytics_assistant"), {"message": "confirm"})
        self.assertEqual(
            Stock.objects.get(part=self.part_oil_1, branch=self.branch_old).quantity,
            stock_before + 2,
        )


class StockLocationMovementTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        category = Category.objects.create(name="Fluids")
        self.part = Part.objects.create(
            name="Engine Oil",
            part_number="OIL-5W30",
            category=category,
            cost_price=Decimal("20.00"),
            selling_price=Decimal("35.00"),
        )
        self.user = User.objects.create_user(username="loc_mgr", password="pass12345")
        self.location_a = Location.objects.create(branch=self.branch, code="A1", name_en="Shelf A1")
        self.location_b = Location.objects.create(branch=self.branch, code="B1", name_en="Shelf B1")

    def test_add_stock_to_location_updates_branch_stock_and_logs_movement(self):
        movement = add_stock_to_location(
            part=self.part,
            branch=self.branch,
            location=self.location_a,
            quantity=10,
            reason="initial_receipt",
            actor=self.user,
            action="add",
        )

        stock_location = StockLocation.objects.get(part=self.part, branch=self.branch, location=self.location_a)
        stock = Stock.objects.get(part=self.part, branch=self.branch)

        self.assertEqual(stock_location.quantity, 10)
        self.assertEqual(stock.quantity, 10)
        self.assertEqual(movement.action, "add")
        self.assertEqual(movement.to_location, self.location_a)
        self.assertIsNone(movement.from_location)

    def test_remove_stock_from_location_updates_branch_stock_and_logs_movement(self):
        add_stock_to_location(
            part=self.part,
            branch=self.branch,
            location=self.location_a,
            quantity=8,
            reason="seed",
            actor=self.user,
            action="add",
        )

        movements = remove_stock_from_locations(
            part=self.part,
            branch=self.branch,
            from_location=self.location_a,
            quantity=3,
            reason="counter_sale",
            actor=self.user,
            action="remove",
        )

        stock_location = StockLocation.objects.get(part=self.part, branch=self.branch, location=self.location_a)
        stock = Stock.objects.get(part=self.part, branch=self.branch)

        self.assertEqual(len(movements), 1)
        self.assertEqual(stock_location.quantity, 5)
        self.assertEqual(stock.quantity, 5)
        self.assertEqual(movements[0].action, "remove")
        self.assertEqual(movements[0].from_location, self.location_a)
        self.assertIsNone(movements[0].to_location)

    def test_remove_stock_insufficient_rolls_back_without_negative_inventory(self):
        add_stock_to_location(
            part=self.part,
            branch=self.branch,
            location=self.location_a,
            quantity=3,
            reason="seed",
            actor=self.user,
            action="add",
        )

        with self.assertRaisesMessage(ValueError, "Insufficient stock in selected locations."):
            remove_stock_from_locations(
                part=self.part,
                branch=self.branch,
                from_location=self.location_a,
                quantity=5,
                reason="attempt_overdraw",
                actor=self.user,
                action="remove",
            )

        stock_location = StockLocation.objects.get(part=self.part, branch=self.branch, location=self.location_a)
        stock = Stock.objects.get(part=self.part, branch=self.branch)
        self.assertEqual(stock_location.quantity, 3)
        self.assertEqual(stock.quantity, 3)

    def test_move_stock_between_locations_keeps_branch_total_and_logs_movement(self):
        add_stock_to_location(
            part=self.part,
            branch=self.branch,
            location=self.location_a,
            quantity=9,
            reason="seed",
            actor=self.user,
            action="add",
        )

        movement = move_stock_between_locations(
            part=self.part,
            branch=self.branch,
            from_location=self.location_a,
            to_location=self.location_b,
            quantity=4,
            reason="warehouse_relayout",
            actor=self.user,
            action="move",
        )

        stock_a = StockLocation.objects.get(part=self.part, branch=self.branch, location=self.location_a)
        stock_b = StockLocation.objects.get(part=self.part, branch=self.branch, location=self.location_b)
        stock = Stock.objects.get(part=self.part, branch=self.branch)

        self.assertEqual(stock_a.quantity, 5)
        self.assertEqual(stock_b.quantity, 4)
        self.assertEqual(stock.quantity, 9)
        self.assertEqual(movement.action, "move")
        self.assertEqual(movement.from_location, self.location_a)
        self.assertEqual(movement.to_location, self.location_b)

    def test_stocklocation_save_syncs_branch_stock_total(self):
        StockLocation.objects.create(
            part=self.part,
            branch=self.branch,
            location=self.location_a,
            quantity=6,
        )
        stock = Stock.objects.get(part=self.part, branch=self.branch)
        self.assertEqual(stock.quantity, 6)


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


class EmployeeIdRulesTests(TestCase):
    def test_employee_id_is_auto_generated_and_unique(self):
        user_a = User.objects.create_user(username="auto_a", password="pass12345")
        user_b = User.objects.create_user(username="auto_b", password="pass12345")

        profile_a = user_a.profile
        profile_b = user_b.profile

        self.assertTrue(profile_a.employee_id)
        self.assertTrue(profile_b.employee_id)
        self.assertNotEqual(profile_a.employee_id, profile_b.employee_id)

    def test_employee_id_unique_constraint_is_enforced(self):
        user_a = User.objects.create_user(username="dup_a", password="pass12345")
        user_b = User.objects.create_user(username="dup_b", password="pass12345")

        profile_b = user_b.profile
        profile_b.employee_id = user_a.profile.employee_id
        with self.assertRaises(IntegrityError):
            profile_b.save(update_fields=["employee_id"])


class SeedGlobalAdminsCommandTests(TestCase):
    @patch("inventory.management.commands.seed_global_admins.getpass")
    def test_seed_global_admins_creates_expected_users_profiles_and_branches(self, mocked_getpass):
        mocked_getpass.side_effect = [
            "Saleh#123", "Saleh#123",
            "Osama#123", "Osama#123",
            "Aziz#123", "Aziz#123",
        ]

        call_command("seed_global_admins", stdout=StringIO())

        expected = [
            ("saleh", "صالح الجابري", "ADM-SALEH", "شارع الجمعية", "ASSN", "Saleh#123"),
            ("osama", "أسامة الجابري", "ADM-OSAMA", "مخرج 18", "EX18", "Osama#123"),
            ("abdulaziz", "عبدالعزيز الجابري", "ADM-AZIZ", "الصناعية القديمة", "OLDIND", "Aziz#123"),
        ]
        for username, full_name, employee_id, branch_name, branch_code, password in expected:
            user = User.objects.get(username=username)
            self.assertEqual(user.first_name, full_name)
            self.assertTrue(user.is_staff)
            self.assertTrue(user.is_superuser)
            self.assertTrue(user.check_password(password))

            profile = user.profile
            self.assertEqual(profile.role, UserProfile.Roles.ADMIN)
            self.assertEqual(profile.employee_id, employee_id)
            self.assertIsNotNone(profile.branch)
            self.assertEqual(profile.branch.name, branch_name)
            self.assertEqual(profile.branch.code, branch_code)


class BranchUserPasswordsCommandTests(TestCase):
    def test_set_branch_user_passwords_creates_or_updates_dbp_users(self):
        call_command(
            "set_branch_user_passwords",
            dbp02_password="Dbp02#Pass1",
            dbp03_password="Dbp03#Pass1",
            stdout=StringIO(),
        )

        dbp02 = User.objects.get(username="DBP02")
        dbp03 = User.objects.get(username="DBP03")
        self.assertFalse(dbp02.is_superuser)
        self.assertFalse(dbp03.is_superuser)
        self.assertTrue(dbp02.is_staff)
        self.assertTrue(dbp03.is_staff)
        self.assertTrue(dbp02.check_password("Dbp02#Pass1"))
        self.assertTrue(dbp03.check_password("Dbp03#Pass1"))
        self.assertEqual(dbp02.profile.role, UserProfile.Roles.CASHIER)
        self.assertEqual(dbp03.profile.role, UserProfile.Roles.CASHIER)
        self.assertEqual(dbp02.profile.branch.name, "مخرج 18")
        self.assertEqual(dbp03.profile.branch.name, "شارع الجمعية")


class SeedRealisticInventoryCommandTests(TestCase):
    def test_seed_realistic_inventory_dry_run_is_non_destructive(self):
        self.assertEqual(Category.objects.count(), 0)
        self.assertEqual(Part.objects.count(), 0)
        call_command("seed_realistic_inventory", stdout=StringIO())
        self.assertEqual(Category.objects.count(), 0)
        self.assertEqual(Part.objects.count(), 0)

    def test_seed_realistic_inventory_apply_creates_realistic_rows(self):
        call_command("seed_realistic_inventory", apply=True, stdout=StringIO())
        self.assertGreaterEqual(Category.objects.count(), 9)
        self.assertGreaterEqual(Part.objects.count(), 80)
        self.assertGreaterEqual(Stock.objects.count(), 240)
        self.assertTrue(Branch.objects.filter(name="الصناعية القديمة").exists())
        self.assertTrue(Branch.objects.filter(name="مخرج 18").exists())
        self.assertTrue(Branch.objects.filter(name="شارع الجمعية").exists())



class PurchasingWorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.manager = User.objects.create_user(username="po_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )
        category = Category.objects.create(name="Filters")
        self.part = Part.objects.create(
            name="Oil Filter",
            part_number="OF-7788",
            category=category,
            cost_price=Decimal("5.00"),
            selling_price=Decimal("10.00"),
        )
        self.vendor = Vendor.objects.create(vendor_code="VND-7788", name_ar="مورد تجريبي")

    def test_po_partial_and_full_receipt_updates_stock_cost_status_and_audit(self):
        self.client.login(username="po_manager", password="pass12345")
        create_response = self.client.post(
            reverse("purchase_order_create"),
            {
                "vendor_id": str(self.vendor.id),
                "branch_id": str(self.branch.id),
                "part_id": [str(self.part.id)],
                "qty_ordered": ["10"],
                "unit_cost": ["8.00"],
                "tax_rate": ["15.00"],
                "discount": ["0.00"],
                "notes": "PO for filters",
                "mark_sent": "1",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        po = PurchaseOrder.objects.latest("id")
        self.assertEqual(po.status, PurchaseOrder.Status.SENT)

        self.client.post(reverse("purchase_receipt_create", args=[po.id]), {"invoice_ref": "INV-A"})
        receipt_a = PurchaseReceipt.objects.latest("id")
        self.client.post(
            reverse("purchase_receipt_detail", args=[receipt_a.id]),
            {
                "po_line_id": str(po.lines.first().id),
                "qty_received": "4",
                "unit_cost": "8.00",
            },
        )
        self.client.post(reverse("purchase_receipt_post", args=[receipt_a.id]), {"reason": "first_receive"})
        po.refresh_from_db()
        stock = Stock.objects.get(part=self.part, branch=self.branch)
        cost_row = PartBranchCost.objects.get(part=self.part, branch=self.branch)
        self.assertEqual(po.status, PurchaseOrder.Status.PARTIAL_RECEIVED)
        self.assertEqual(stock.quantity, 4)
        self.assertEqual(cost_row.avg_cost, Decimal("8.0000"))

        self.client.post(reverse("purchase_receipt_create", args=[po.id]), {"invoice_ref": "INV-B"})
        receipt_b = PurchaseReceipt.objects.latest("id")
        self.client.post(
            reverse("purchase_receipt_detail", args=[receipt_b.id]),
            {
                "po_line_id": str(po.lines.first().id),
                "qty_received": "6",
                "unit_cost": "10.00",
            },
        )
        self.client.post(reverse("purchase_receipt_post", args=[receipt_b.id]), {"reason": "second_receive"})

        po.refresh_from_db()
        stock.refresh_from_db()
        cost_row.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(stock.quantity, 10)
        self.assertEqual(cost_row.avg_cost, Decimal("9.2000"))
        self.assertTrue(
            AuditLog.objects.filter(
                action="purchase_receipt.post",
                object_type="PurchaseReceipt",
            ).exists()
        )


class CustomerLedgerWorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.cashier = User.objects.create_user(username="ledger_cashier", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch},
        )
        self.manager = User.objects.create_user(username="ledger_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )
        category = Category.objects.create(name="Brakes")
        self.part = Part.objects.create(
            name="Brake Pad",
            part_number="BP-200",
            category=category,
            cost_price=Decimal("20.00"),
            selling_price=Decimal("40.00"),
        )
        self.stock = Stock.objects.create(part=self.part, branch=self.branch, quantity=20)

    def _checkout_order(self):
        self.client.login(username="ledger_cashier", password="pass12345")
        session = self.client.session
        session["cart"] = {str(self.stock.id): 2}
        session.save()
        self.client.post(
            reverse("finalize_order"),
            {
                "discount": "0.00",
                "phone_number": "0500001111",
                "customer_name": "Ledger Cust",
                "payment_method": "cash",
                "advance_payment": "10.00",
            },
        )
        return Order.objects.latest("id")

    def test_sale_payment_and_credit_note_update_ledger_and_stock(self):
        order = self._checkout_order()
        customer = order.customer
        self.assertIsNotNone(customer)
        self.assertTrue(
            CustomerLedgerEntry.objects.filter(
                customer=customer,
                order=order,
                entry_type=CustomerLedgerEntry.EntryType.INVOICE,
            ).exists()
        )
        self.assertTrue(
            CustomerLedgerEntry.objects.filter(
                customer=customer,
                order=order,
                entry_type=CustomerLedgerEntry.EntryType.PAYMENT,
            ).exists()
        )

        self.client.post(
            reverse("customer_add_payment", args=[customer.id]),
            {"amount": "5.00", "method": "cash", "reference": "cashbox"},
        )
        self.assertTrue(
            Payment.objects.filter(customer=customer, amount=Decimal("5.00")).exists()
        )

        self.client.logout()
        self.client.login(username="ledger_manager", password="pass12345")
        self.client.post(
            reverse("credit_note_create", args=[order.id]),
            {"reason": "returned", "return_to_stock": "1"},
        )
        self.stock.refresh_from_db()
        self.assertGreaterEqual(self.stock.quantity, 19)
        self.assertTrue(CreditNote.objects.filter(order=order).exists())
        self.assertTrue(
            CustomerLedgerEntry.objects.filter(
                customer=customer,
                order=order,
                entry_type=CustomerLedgerEntry.EntryType.CREDIT_NOTE,
            ).exists()
        )


class BarcodeInsightFlowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.manager = User.objects.create_user(username="scan_info", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )
        category = Category.objects.create(name="Ignition")
        self.part = Part.objects.create(
            name="Spark Plug",
            part_number="SP-100",
            barcode="6281002003001",
            category=category,
            cost_price=Decimal("12.00"),
            selling_price=Decimal("24.00"),
        )
        PartBarcode.objects.create(part=self.part, barcode="6281002003999")
        Stock.objects.create(part=self.part, branch=self.branch, quantity=8)

    def test_scan_info_redirects_to_part_insight_and_unmatched_to_linking(self):
        self.client.login(username="scan_info", password="pass12345")
        response = self.client.post(
            reverse("scan_dispatch"),
            {"mode": "info", "scan_code": "6281002003999"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("part_insight", args=[self.part.id]))

        insight_response = self.client.get(reverse("part_insight", args=[self.part.id]))
        self.assertEqual(insight_response.status_code, 200)
        self.assertContains(insight_response, self.part.part_number)

        unmatched_response = self.client.post(
            reverse("scan_dispatch"),
            {"mode": "info", "scan_code": "NO-BARCODE"},
        )
        self.assertEqual(unmatched_response.status_code, 302)
        self.assertIn(reverse("barcode_unmatched"), unmatched_response.url)


class TransferPartialReceiveWorkflowTests(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(name="A", code="A")
        self.branch_b = Branch.objects.create(name="B", code="B")
        category = Category.objects.create(name="Cooling")
        self.part = Part.objects.create(
            name="Water Pump",
            part_number="WP-33",
            barcode="6285550011223",
            category=category,
            cost_price=Decimal("50.00"),
            selling_price=Decimal("85.00"),
        )
        self.stock_a = Stock.objects.create(part=self.part, branch=self.branch_a, quantity=1)
        self.stock_b = Stock.objects.create(part=self.part, branch=self.branch_b, quantity=10)
        self.manager_b = User.objects.create_user(username="tp_manager_b", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager_b,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch_b},
        )
        self.cashier_a = User.objects.create_user(username="tp_cashier_a", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.cashier_a,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch_a},
        )
        self.transfer = TransferRequest.objects.create(
            part=self.part,
            quantity=4,
            source_branch=self.branch_b,
            destination_branch=self.branch_a,
            requested_by=self.cashier_a,
            status=TransferRequest.Status.REQUESTED,
        )

    def test_partial_receive_keeps_backorder_open_until_complete(self):
        self.client.login(username="tp_manager_b", password="pass12345")
        self.client.post(reverse("transfer_approve", args=[self.transfer.id]), {"reason": "ok"})
        self.client.post(reverse("transfer_mark_picked_up", args=[self.transfer.id]), {"reason": "picked"})
        self.client.post(reverse("transfer_mark_delivered", args=[self.transfer.id]), {"reason": "delivered"})

        self.client.logout()
        self.client.login(username="tp_cashier_a", password="pass12345")
        self.client.post(
            reverse("transfer_confirm_receive", args=[self.transfer.id]),
            {"reason": "partial receive", "receive_qty": "2"},
        )
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, TransferRequest.Status.DELIVERED)
        self.assertEqual(self.transfer.received_quantity, 2)
        self.assertEqual(self.transfer.remaining_quantity, 2)

        self.client.post(
            reverse("transfer_receive_scan", args=[self.transfer.id]),
            {"scan_code": "WP-33", "quantity": "2", "reason": "scan receive"},
        )
        self.transfer.refresh_from_db()
        self.stock_a.refresh_from_db()
        self.stock_b.refresh_from_db()
        self.assertEqual(self.transfer.status, TransferRequest.Status.RECEIVED)
        self.assertEqual(self.transfer.received_quantity, 4)
        self.assertEqual(self.transfer.reserved_quantity, 0)
        self.assertEqual(self.stock_a.quantity, 5)
        self.assertEqual(self.stock_b.quantity, 6)


class CycleCountWorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.manager = User.objects.create_user(username="cc_manager", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.manager,
            defaults={"role": UserProfile.Roles.MANAGER, "branch": self.branch},
        )
        category = Category.objects.create(name="Electrical")
        self.part = Part.objects.create(
            name="Battery",
            part_number="BAT-1",
            category=category,
            cost_price=Decimal("100.00"),
            selling_price=Decimal("150.00"),
        )
        Stock.objects.create(part=self.part, branch=self.branch, quantity=10)

    def test_cycle_count_submit_and_approve_adjusts_stock(self):
        self.client.login(username="cc_manager", password="pass12345")
        start_response = self.client.post(reverse("cycle_count_list"), {"notes": "monthly"})
        self.assertEqual(start_response.status_code, 302)
        session = CycleCountSession.objects.latest("id")

        self.client.post(
            reverse("cycle_count_detail", args=[session.id]),
            {"action": "add_line", "part_id": str(self.part.id), "counted_qty": "7"},
        )
        self.client.post(reverse("cycle_count_detail", args=[session.id]), {"action": "submit"})
        self.client.post(reverse("cycle_count_detail", args=[session.id]), {"action": "approve"})

        session.refresh_from_db()
        stock = Stock.objects.get(part=self.part, branch=self.branch)
        line = CycleCountLine.objects.get(session=session, part=self.part)
        self.assertEqual(line.system_qty_snapshot, 10)
        self.assertEqual(line.variance, -3)
        self.assertEqual(stock.quantity, 7)
        self.assertEqual(session.status, CycleCountSession.Status.APPROVED)
        self.assertTrue(
            AuditLog.objects.filter(
                action="cycle_count.approve",
                object_type="CycleCountSession",
                object_id=str(session.id),
            ).exists()
        )


class ViewOnlyPermissionEnforcementTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main", code="MAIN")
        self.other_branch = Branch.objects.create(name="Other", code="OTH")
        category = Category.objects.create(name="General")
        self.part = Part.objects.create(
            name="Fuse",
            part_number="FS-1",
            category=category,
            cost_price=Decimal("1.00"),
            selling_price=Decimal("2.00"),
        )
        self.stock = Stock.objects.create(part=self.part, branch=self.branch, quantity=10)
        self.view_only = User.objects.create_user(username="view_only_user", password="pass12345")
        UserProfile.objects.update_or_create(
            user=self.view_only,
            defaults={"role": UserProfile.Roles.CASHIER, "branch": self.branch},
        )
        perms = Permission.objects.filter(
            codename__in=["view_order", "view_transferrequest", "view_stock"]
        )
        self.view_only.user_permissions.add(*perms)

    def test_view_only_user_gets_403_on_mutations(self):
        self.client.login(username="view_only_user", password="pass12345")
        add_response = self.client.post(reverse("add_to_cart", args=[self.stock.id]), {"quantity": "1"})
        self.assertEqual(add_response.status_code, 403)

        transfer_response = self.client.post(
            reverse("transfer_create"),
            {
                "part_id": str(self.part.id),
                "quantity": "1",
                "to_branch": str(self.other_branch.id),
                "notes": "try",
            },
        )
        self.assertEqual(transfer_response.status_code, 403)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetOtpFlowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Reset Branch", code="RST")
        self.user = User.objects.create_user(
            username="reset_user",
            email="reset@example.com",
            password="pass12345",
        )
        UserProfile.objects.update_or_create(
            user=self.user,
            defaults={
                "role": UserProfile.Roles.CASHIER,
                "branch": self.branch,
                "phone_number": "0501234567",
            },
        )

    def _issue_email_code(self):
        response = self.client.post(
            reverse("password_reset_request"),
            {
                "username": self.user.username,
                "channel": "email",
                "contact": self.user.email,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("password_reset_verify"))
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"\b(\d{6})\b", mail.outbox[-1].body)
        self.assertIsNotNone(match)
        otp = PasswordResetOtp.objects.latest("id")
        return otp, match.group(1)

    def test_email_reset_code_sent_and_expires_in_15_minutes(self):
        otp, _code = self._issue_email_code()
        self.assertEqual(otp.channel, PasswordResetOtp.Channels.EMAIL)
        self.assertEqual(otp.destination, self.user.email)
        self.assertGreater(otp.expires_at, timezone.now() + timedelta(minutes=14))
        self.assertLess(otp.expires_at, timezone.now() + timedelta(minutes=16))

    def test_phone_reset_code_request_works_with_registered_phone(self):
        response = self.client.post(
            reverse("password_reset_request"),
            {
                "username": self.user.username,
                "channel": "phone",
                "contact": "966501234567",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("password_reset_verify"))
        otp = PasswordResetOtp.objects.latest("id")
        self.assertEqual(otp.channel, PasswordResetOtp.Channels.PHONE)
        self.assertEqual(len(mail.outbox), 0)

    def test_full_reset_flow_sets_new_password(self):
        _otp, code = self._issue_email_code()
        verify_response = self.client.post(reverse("password_reset_verify"), {"code": code})
        self.assertEqual(verify_response.status_code, 302)
        self.assertEqual(verify_response.url, reverse("password_reset_set_new"))

        new_password = "AReallyStrongPass123!"
        set_response = self.client.post(
            reverse("password_reset_set_new"),
            {"new_password1": new_password, "new_password2": new_password},
        )
        self.assertEqual(set_response.status_code, 302)
        self.assertEqual(set_response.url, reverse("login"))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertTrue(self.client.login(username=self.user.username, password=new_password))
        self.assertIsNotNone(PasswordResetOtp.objects.latest("id").used_at)

    def test_wrong_contact_does_not_issue_code(self):
        response = self.client.post(
            reverse("password_reset_request"),
            {
                "username": self.user.username,
                "channel": "email",
                "contact": "wrong@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PasswordResetOtp.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_expired_code_redirects_back_to_request(self):
        otp, code = self._issue_email_code()
        otp.expires_at = timezone.now() - timedelta(minutes=1)
        otp.save(update_fields=["expires_at"])

        response = self.client.post(reverse("password_reset_verify"), {"code": code})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("password_reset_request"))

    @override_settings(DEBUG=True)
    @patch("inventory.views.send_mail", side_effect=Exception("smtp unavailable"))
    def test_email_failure_in_debug_still_allows_verify_step(self, _mock_send_mail):
        response = self.client.post(
            reverse("password_reset_request"),
            {
                "username": self.user.username,
                "channel": "email",
                "contact": self.user.email,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تحقق من الرمز")
        self.assertContains(response, "رمز التحقق (وضع التطوير)")
        otp = PasswordResetOtp.objects.latest("id")
        self.assertIsNone(otp.used_at)


class EffectivePermissionsCommandTests(TestCase):
    def test_delta_check_user_effective_perms_outputs_permissions(self):
        user = User.objects.create_user(username="perm_user", password="pass12345")
        perm = Permission.objects.get(codename="view_stock")
        user.user_permissions.add(perm)

        out = StringIO()
        call_command("delta_check_user_effective_perms", "perm_user", stdout=out)
        output = out.getvalue()
        self.assertIn("perm_user", output)
        self.assertIn("view_stock", output)

