from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Branch, Category, Order, Part, Sale, Stock


class CheckoutFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cashier', password='pass12345')
        self.branch = Branch.objects.create(name='Main', code='MAIN')
        self.category = Category.objects.create(name='Filters')
        self.part = Part.objects.create(
            name='Oil Filter',
            part_number='OF-001',
            category=self.category,
            cost_price=Decimal('10.00'),
            selling_price=Decimal('20.00'),
        )
        self.stock = Stock.objects.create(part=self.part, branch=self.branch, quantity=5)

    def test_finalize_order_creates_order_and_sale_and_deducts_stock(self):
        self.client.login(username='cashier', password='pass12345')

        session = self.client.session
        session['cart'] = {str(self.stock.id): 2}
        session.save()

        response = self.client.post(
            reverse('finalize_order'),
            {
                'discount': '5.00',
                'vat_amount': '5.25',
                'grand_total': '40.25',
                'subtotal_input': '45.00',
                'phone_number': '0501234567',
                'customer_name': 'Abu Ahmed',
                'customer_car': 'Camry 2018',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Sale.objects.count(), 1)

        order = Order.objects.get()
        sale = Sale.objects.get()

        self.assertEqual(sale.order, order)
        self.assertEqual(sale.quantity, 2)
        self.assertEqual(sale.price_at_sale, Decimal('20.00'))
        self.assertEqual(sale.cost_at_sale, Decimal('10.00'))

        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 3)

        self.assertEqual(self.client.session.get('cart'), {})


class SalesExportTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_superuser(
            username='manager',
            email='manager@example.com',
            password='pass12345',
        )
        self.branch = Branch.objects.create(name='Main', code='MAIN')
        self.category = Category.objects.create(name='Brakes')
        self.part = Part.objects.create(
            name='Brake Pad',
            part_number='BP-100',
            category=self.category,
            cost_price=Decimal('50.00'),
            selling_price=Decimal('80.00'),
        )
        self.sale = Sale.objects.create(
            part=self.part,
            branch=self.branch,
            seller=self.manager,
            quantity=2,
            price_at_sale=Decimal('80.00'),
            cost_at_sale=Decimal('50.00'),
        )

    def test_export_sales_csv_returns_csv_content(self):
        self.client.login(username='manager', password='pass12345')

        response = self.client.get(reverse('export_sales_csv'))

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        self.assertIn('attachment; filename=', response['Content-Disposition'])

        decoded = response.content.decode('utf-8-sig')
        self.assertIn('Order ID,Part Name,Part Number,Branch,Seller,Quantity,Sale Price,Cost,Profit', decoded)
        self.assertIn('Brake Pad', decoded)
        self.assertIn('BP-100', decoded)


class SaleModelTests(TestCase):
    def test_total_profit_is_zero_for_refunded_sale(self):
        user = User.objects.create_user(username='u1', password='pass12345')
        branch = Branch.objects.create(name='Main', code='MAIN')
        category = Category.objects.create(name='Electrical')
        part = Part.objects.create(
            name='Battery',
            part_number='BAT-12V',
            category=category,
            cost_price=Decimal('100.00'),
            selling_price=Decimal('150.00'),
        )

        sale = Sale.objects.create(
            part=part,
            branch=branch,
            seller=user,
            quantity=2,
            price_at_sale=Decimal('150.00'),
            cost_at_sale=Decimal('100.00'),
            is_refunded=True,
        )

        self.assertEqual(sale.total_profit, 0)
