import csv
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db.models import Q
from django.contrib import messages
from .models import Part, Vehicle, Stock, Sale, Order, Customer

# --- SECURITY HELPER ---
def is_manager(user):
    return user.is_superuser

# --- 1. DASHBOARD & SEARCH ---
def part_search(request):
    # Sort by Make (A-Z), then Model (A-Z), then Year (Newest First)
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

    # Calculate Cart Total for Badge
    cart = request.session.get('cart', {})
    cart_total_count = sum(cart.values())

    return render(request, 'inventory/search.html', {
        'vehicles': vehicles,
        'results': results,
        'query': query_barcode,
        'cart_total_count': cart_total_count
    })

# --- 2. CART FUNCTIONS ---
@login_required
@require_POST
def add_to_cart(request, stock_id):
    stock = get_object_or_404(Stock, id=stock_id)
    
    try:
        quantity = int(request.POST.get('quantity', 1))
    except ValueError:
        quantity = 1

    if stock.quantity < quantity:
        return JsonResponse({
            'success': False, 
            'message': f'Not enough stock! Only {stock.quantity} left.'
        }, status=400)

    cart = request.session.get('cart', {})
    stock_id_str = str(stock_id)
    
    current_qty = cart.get(stock_id_str, 0)
    cart[stock_id_str] = current_qty + quantity
    
    request.session['cart'] = cart
    total_items = sum(cart.values())

    return JsonResponse({
        'success': True,
        'message': f'Added {quantity} x {stock.part.name}',
        'total_items': total_items
    })

@login_required
def update_cart_item(request, stock_id):
    if request.method == 'POST':
        try:
            new_qty = int(request.POST.get('quantity', 0))
            cart = request.session.get('cart', {})
            stock_id_str = str(stock_id)

            if stock_id_str in cart:
                if new_qty > 0:
                    stock = get_object_or_404(Stock, id=stock_id)
                    if new_qty <= stock.quantity:
                        cart[stock_id_str] = new_qty
                        messages.success(request, "Cart updated.")
                    else:
                        messages.warning(request, f"Cannot add {new_qty}. Only {stock.quantity} in stock!")
                else:
                    del cart[stock_id_str]
                    messages.success(request, "Item removed.")
            
            request.session['cart'] = cart
        except ValueError:
            pass
            
    return redirect('cart_view')

@login_required
def cart_view(request):
    cart = request.session.get('cart', {})
    cart_items = []
    current_subtotal = 0
    estimated_profit = 0
    
    for stock_id, quantity in cart.items():
        # Use filter().first() to avoid crash if part was deleted
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

    context = {
        'cart_items': cart_items,
        'subtotal': current_subtotal,
        'estimated_profit': estimated_profit,
    }
    return render(request, 'inventory/pos_checkout.html', context)

@login_required
def finalize_order(request):
    if request.method == 'POST':
        cart = request.session.get('cart', {})
        if not cart:
            return redirect('part_search')

        # 1. Financials (Safe Float Conversion)
        try:
            discount = float(request.POST.get('discount') or 0)
            vat_amount = float(request.POST.get('vat_amount') or 0)
            grand_total = float(request.POST.get('grand_total') or 0)
            subtotal = float(request.POST.get('subtotal_input') or 0)
        except ValueError:
            discount = 0.0
            vat_amount = 0.0
            grand_total = 0.0
            subtotal = 0.0

        # 2. CRM (Customer Logic)
        customer_phone = request.POST.get('phone_number')
        customer_name = request.POST.get('customer_name')
        customer_car = request.POST.get('customer_car')
        customer_obj = None
        
        if customer_phone:
            customer_obj, created = Customer.objects.get_or_create(
                phone_number=customer_phone,
                defaults={'name': customer_name or 'Unknown', 'car_model': customer_car or ''}
            )
            if not created and (customer_name or customer_car):
                if customer_name: customer_obj.name = customer_name
                if customer_car: customer_obj.car_model = customer_car
                customer_obj.save()

        # 3. Create Order
        order = Order.objects.create(
             seller=request.user,
             customer=customer_obj,
             subtotal=subtotal,
             vat_amount=vat_amount,
             discount_amount=discount,
             grand_total=grand_total,
             customer_email=request.POST.get('customer_email', '')
        )

        # 4. Create Sales & Deduct Stock
        for stock_id, qty in cart.items():
            stock = Stock.objects.filter(id=stock_id).first()
            if stock and stock.quantity >= qty:
                stock.quantity -= qty
                stock.save()
                
                Sale.objects.create(
                    order=order,
                    part=stock.part,
                    branch=stock.branch,
                    seller=request.user,
                    quantity=qty,
                    price_at_sale=stock.part.selling_price,
                    cost_at_sale=stock.part.cost_price
                )

        request.session['cart'] = {}
        return redirect('receipt_view', order_id=order.order_id)

    return redirect('cart_view')

@login_required
def receipt_view(request, order_id):
    order = get_object_or_404(Order, order_id=order_id)
    
    # Calculate the base amount for the receipt label
    taxable_amount = order.subtotal - order.discount_amount
    
    return render(request, 'inventory/receipt.html', {
        'order': order,
        'taxable_amount': taxable_amount  # <--- Sending the correct number
    })

@login_required
def order_list(request):
    orders = Order.objects.all().order_by('-created_at')
    return render(request, 'inventory/order_list.html', {'orders': orders})

# --- 3. TOOLS & REPORTS ---

def scanner_view(request):
    return render(request, 'inventory/scanner.html')

@login_required
def sell_part(request, stock_id):
    # Legacy quick-sell function (can be kept or removed)
    stock = get_object_or_404(Stock, id=stock_id)
    if stock.quantity > 0:
        Sale.objects.create(
            part=stock.part,
            branch=stock.branch,
            seller=request.user,
            quantity=1,
            price_at_sale=stock.part.selling_price,
            cost_at_sale=stock.part.cost_price
        )
        stock.quantity -= 1
        stock.save()
    return redirect('part_search')

@login_required
@user_passes_test(is_manager)
def sales_history(request):
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    branch_id = request.GET.get('branch')

    sales_query = Sale.objects.all().order_by('-date_sold')

    if start_date and end_date:
        sales_query = sales_query.filter(date_sold__date__range=[start_date, end_date])
    else:
        today = timezone.now().date()
        sales_query = sales_query.filter(date_sold__date=today)

    if branch_id:
        sales_query = sales_query.filter(branch_id=branch_id)

    total_revenue = sum(s.total_revenue for s in sales_query)
    total_profit = sum(s.total_profit for s in sales_query)
    branches = Stock.objects.values('branch__id', 'branch__name').distinct()

    context = {
        'sales': sales_query,
        'total_revenue': total_revenue,
        'total_profit': total_profit,
        'branches': branches,
        'current_branch': int(branch_id) if branch_id else None,
        'start_date': start_date,
        'end_date': end_date
    }
    return render(request, 'inventory/sales_history.html', context)

@login_required
@user_passes_test(is_manager)
def export_sales_csv(request):
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    branch_id = request.GET.get('branch')

    sales_query = Sale.objects.all().order_by('-date_sold')
    
    if start_date and end_date:
        sales_query = sales_query.filter(date_sold__date__range=[start_date, end_date])
    else:
        today = timezone.now().date()
        sales_query = sales_query.filter(date_sold__date=today)

    if branch_id:
        sales_query = sales_query.filter(branch_id=branch_id)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="sales_report_{timezone.now().date()}.csv"'
    response.write(u'\ufeff'.encode('utf8'))

    writer = csv.writer(response)
    header = ['Date', 'Time', 'Order ID', 'Part Name', 'Part Number', 'Branch', 'Seller', 'Quantity', 'Sale Price', 'Cost', 'Profit']
    writer.writerow(header)

    for sale in sales_query:
        # Robust calculation
        revenue = sale.price_at_sale * sale.quantity
        cost = sale.cost_at_sale * sale.quantity
        profit = revenue - cost
        order_id = sale.order.order_id if sale.order else "N/A"
        branch_name = sale.branch.name if sale.branch else "N/A"

        writer.writerow([
            sale.date_sold.date(),
            sale.date_sold.strftime("%H:%M"),
            order_id,
            sale.part.name,
            sale.part.part_number,
            branch_name,
            sale.seller.username,
            sale.quantity,
            revenue,
            cost,
            profit
        ])

    return response

@login_required
@user_passes_test(is_manager)
def low_stock_list(request):
    low_stock_items = Stock.objects.filter(quantity__lte=10).order_by('quantity')
    return render(request, 'inventory/low_stock.html', {'low_stock_items': low_stock_items})

def vehicle_catalog(request):
    # 1. Fetch all vehicles, sorted by Model (A-Z) and Year (Oldest to Newest)
    # Note: You requested ascending 'year' (1980 -> 2026)
    vehicles = Vehicle.objects.all().order_by('make', 'model', 'year')

    # 2. Group them by Model (e.g., "Patrol": [1980 object, 1981 object...])
    grouped_vehicles = {}
    for v in vehicles:
        if v.model not in grouped_vehicles:
            grouped_vehicles[v.model] = []
        grouped_vehicles[v.model].append(v)

    return render(request, 'inventory/vehicle_catalog.html', {
        'grouped_vehicles': grouped_vehicles
    })
    
    # In inventory/views.py

def pos_console(request):
    # 1. Get the current cart
    cart = request.session.get('cart', {})
    cart_items = []
    subtotal = 0
    total_qty = 0

    # 2. Build the list of items
    for stock_id, quantity in cart.items():
        try:
            stock = Stock.objects.get(id=stock_id)
            total_price = stock.part.selling_price * quantity
            subtotal += total_price
            total_qty += quantity
            
            cart_items.append({
                'stock': stock,
                'quantity': quantity,
                'total_price': total_price,
            })
        except Stock.DoesNotExist:
            continue

    # 3. Render the "Console" template
    return render(request, 'inventory/pos_console.html', {
        'cart_items': cart_items,
        'subtotal': subtotal,
        'cart_total_count': total_qty
    })