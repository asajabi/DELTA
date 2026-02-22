import csv
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db.models import Q
from django.contrib import messages
from .models import Part, Vehicle, Stock, Sale, Order, Customer, Branch

# --- SECURITY HELPER ---
def is_manager(user):
    return user.is_superuser

# --- 1. DASHBOARD & SEARCH (الكتالوج) ---
@login_required
def part_search(request):
    vehicles = Vehicle.objects.all().order_by('make', 'model', 'year')
    query_vehicle_id = request.GET.get('vehicle')
    query_barcode = request.GET.get('q')
    
    results = None
    
    if query_vehicle_id:
        results = Part.objects.filter(compatible_vehicles__id=query_vehicle_id)
    elif query_barcode:
        results = Part.objects.filter(
            Q(part_number__icontains=query_barcode) | 
            Q(name__icontains=query_barcode) |
            Q(barcode=query_barcode)
        )

    # حساب عدد عناصر السلة للشارة (Badge)
    cart = request.session.get('cart', {})
    cart_total_count = sum(cart.values())

    return render(request, 'inventory/search.html', {
        'vehicles': vehicles,
        'results': results,
        'query': query_barcode,
        'cart_total_count': cart_total_count
    })

# --- 2. POS SYSTEM (نقطة البيع - الكاشير) ---
@login_required
def pos_console(request):
    """
    شاشة موحدة: اليسار للبحث والإضافة، واليمين للفاتورة الحالية.
    """
    # أ. منطق البحث داخل الـ POS
    query = request.GET.get('q')
    search_results = []
    
    if query:
        search_results = Stock.objects.filter(
            Q(part__part_number__icontains=query) |
            Q(part__name__icontains=query) |
            Q(part__barcode=query)
        )

    # ب. منطق عرض السلة
    cart = request.session.get('cart', {})
    cart_items = []
    total_price = 0
    total_qty = 0

    for stock_id, quantity in cart.items():
        # نستخدم filter().first() لتجنب الأخطاء لو القطعة حذفت
        stock = Stock.objects.filter(id=stock_id).first()
        if stock:
            subtotal = stock.part.selling_price * quantity
            total_price += subtotal
            total_qty += quantity
            cart_items.append({
                'stock': stock,
                'quantity': quantity,
                'subtotal': subtotal
            })

    return render(request, 'inventory/pos_console.html', {
        'results': search_results,
        'query': query,
        'cart_items': cart_items,
        'subtotal': total_price, # المجموع الكلي
        'cart_total_count': total_qty
    })

# --- 3. CART FUNCTIONS (تم الإصلاح: Redirect بدلاً من JSON) ---

@login_required
def add_to_cart(request, stock_id):
    """
    تضيف للسلة وتعيد المستخدم لنفس الصفحة التي جاء منها.
    """
    stock = get_object_or_404(Stock, id=stock_id)
    
    # محاولة جلب الكمية من الرابط (لزر الإضافة السريع) أو الفورم
    try:
        quantity = int(request.POST.get('quantity', request.GET.get('qty', 1)))
    except ValueError:
        quantity = 1

    # تحديد الصفحة للعودة إليها (search أو pos)
    next_url = request.GET.get('next') or request.META.get('HTTP_REFERER', 'part_search')

    # التحقق من توفر الكمية
    if stock.quantity < quantity:
        messages.error(request, f"الكمية غير متوفرة! المتبقي {stock.quantity} فقط.")
        return redirect(next_url)

    # تحديث الجلسة (Session)
    cart = request.session.get('cart', {})
    stock_id_str = str(stock_id)
    
    current_qty = cart.get(stock_id_str, 0)
    cart[stock_id_str] = current_qty + quantity
    
    request.session['cart'] = cart
    request.session.modified = True # مهم جداً لحفظ التغيير
    
    messages.success(request, f"تمت إضافة {stock.part.name}")
    
    return redirect(next_url)

@login_required
def update_cart_item(request, stock_id):
    if request.method == 'POST':
        cart = request.session.get('cart', {})
        stock_id_str = str(stock_id)
        
        # قراءة الكمية الجديدة من الإدخال
        try:
            quantity = int(request.POST.get('quantity', 0))
        except ValueError:
            quantity = 0

        if stock_id_str in cart:
            if quantity > 0:
                # التحقق من الحد الأقصى للمخزون
                stock = Stock.objects.get(id=stock_id)
                if quantity <= stock.quantity:
                    cart[stock_id_str] = quantity
                else:
                    messages.warning(request, f"الحد الأقصى المتوفر: {stock.quantity}")
                    cart[stock_id_str] = stock.quantity
            else:
                # حذف العنصر إذا الكمية 0
                del cart[stock_id_str]

        request.session['cart'] = cart
        request.session.modified = True
        
    return redirect(request.META.get('HTTP_REFERER', 'cart_view'))

@login_required
def clear_cart(request):
    request.session['cart'] = {}
    request.session.modified = True
    return redirect('pos_console')

@login_required
def cart_view(request):
    # صفحة السلة التفصيلية (للمراجعة قبل الدفع)
    cart = request.session.get('cart', {})
    cart_items = []
    current_subtotal = 0
    estimated_profit = 0
    
    for stock_id, quantity in cart.items():
        stock = Stock.objects.filter(id=stock_id).first()
        if stock:
            total_price = stock.part.selling_price * quantity
            total_cost = stock.part.cost_price * quantity
            cart_items.append({
                'stock': stock,
                'quantity': quantity,
                'total_price': total_price,
                'unit_price': stock.part.selling_price,
                'unit_cost': stock.part.cost_price,
            })
            current_subtotal += total_price
            estimated_profit += (total_price - total_cost)

    return render(request, 'inventory/pos_checkout.html', {
        'cart_items': cart_items,
        'subtotal': current_subtotal,
        'estimated_profit': estimated_profit,
    })

# --- 4. CHECKOUT & ORDERS (إتمام البيع) ---

@login_required
def finalize_order(request):
    if request.method == 'POST':
        cart = request.session.get('cart', {})
        if not cart:
            return redirect('pos_console')

        # 1. الحسابات المالية
        try:
            subtotal = float(request.POST.get('subtotal_input', 0))
            discount = float(request.POST.get('discount', 0))
            vat_amount = float(request.POST.get('vat_amount', 0))
            grand_total = float(request.POST.get('grand_total', 0))
        except ValueError:
            subtotal = discount = vat_amount = grand_total = 0

        # 2. بيانات العميل
        customer_phone = request.POST.get('phone_number')
        customer_name = request.POST.get('customer_name')
        customer_obj = None
        
        if customer_phone:
            customer_obj, created = Customer.objects.get_or_create(
                phone_number=customer_phone,
                defaults={'name': customer_name or 'Unknown'}
            )

        # 3. إنشاء الطلب
        order = Order.objects.create(
             seller=request.user,
             customer=customer_obj,
             subtotal=subtotal,
             vat_amount=vat_amount,
             discount_amount=discount,
             grand_total=grand_total,
        )

        # 4. حفظ المبيعات وخصم المخزون
        for stock_id, qty in cart.items():
            stock = Stock.objects.filter(id=stock_id).first()
            if stock and stock.quantity >= qty:
                # خصم الكمية
                stock.quantity -= qty
                stock.save()
                
                # حساب السعر الفعلي بعد الخصم (للدقة في التقارير)
                discount_factor = (subtotal - discount) / subtotal if subtotal > 0 else 1
                actual_price = float(stock.part.selling_price) * discount_factor

                Sale.objects.create(
                    order=order,
                    part=stock.part,
                    branch=stock.branch,
                    seller=request.user,
                    quantity=qty,
                    price_at_sale=actual_price, # السعر الصافي
                    cost_at_sale=stock.part.cost_price
                )

        request.session['cart'] = {} # تفريغ السلة
        request.session.modified = True
        return redirect('receipt_view', order_id=order.order_id)

    return redirect('pos_console')

@login_required
def receipt_view(request, order_id):
    order = get_object_or_404(Order, order_id=order_id)
    return render(request, 'inventory/receipt.html', {'order': order})

# --- 5. REPORTS & TOOLS ---

@login_required
def order_list(request):
    orders = Order.objects.all().order_by('-created_at')
    return render(request, 'inventory/order_list.html', {'orders': orders})

def scanner_view(request):
    return render(request, 'inventory/scanner.html')

@login_required
def sell_part(request, stock_id):
    # دالة قديمة للبيع السريع (تحولك للكاشير الآن)
    return redirect('pos_console')

@login_required
@user_passes_test(is_manager)
def sales_history(request):
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    branch_id = request.GET.get('branch')

    sales_query = Sale.objects.all().order_by('-date_sold')

    if start_date and end_date:
        sales_query = sales_query.filter(date_sold__date__range=[start_date, end_date])
    
    if branch_id:
        sales_query = sales_query.filter(branch_id=branch_id)

    # حساب المجاميع
    total_revenue = sum(s.total_revenue for s in sales_query)
    total_profit = sum(s.total_profit for s in sales_query)
    
    branches = Stock.objects.values('branch__id', 'branch__name').distinct()

    context = {
        'sales': sales_query,
        'total_revenue': total_revenue,
        'total_profit': total_profit,
        'branches': branches,
        'start_date': start_date,
        'end_date': end_date
    }
    return render(request, 'inventory/sales_history.html', context)

@login_required
@user_passes_test(is_manager)
def export_sales_csv(request):
    # تصدير التقرير كملف CSV
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="sales_{timezone.now().date()}.csv"'
    response.write(u'\ufeff'.encode('utf8')) # لدعم العربية في Excel

    writer = csv.writer(response)
    writer.writerow(['Date', 'Part', 'Branch', 'Qty', 'Price', 'Total'])

    sales = Sale.objects.all().order_by('-date_sold')
    for sale in sales:
        writer.writerow([
            sale.date_sold.date(),
            sale.part.name,
            sale.branch.name if sale.branch else "-",
            sale.quantity,
            sale.price_at_sale,
            sale.total_revenue
        ])

    return response

@login_required
@user_passes_test(is_manager)
def low_stock_list(request):
    low_stock_items = Stock.objects.filter(quantity__lte=5).order_by('quantity')
    return render(request, 'inventory/low_stock.html', {'low_stock_items': low_stock_items})

def vehicle_catalog(request):
    vehicles = Vehicle.objects.all().order_by('make', 'model', 'year')
    grouped_vehicles = {}
    for v in vehicles:
        if v.model not in grouped_vehicles:
            grouped_vehicles[v.model] = []
        grouped_vehicles[v.model].append(v)

    return render(request, 'inventory/vehicle_catalog.html', {
        'grouped_vehicles': grouped_vehicles
    })