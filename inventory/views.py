from django.shortcuts import render, redirect, get_object_or_404
from .models import Part, Vehicle, Stock  # <--- Make sure Stock is imported!
from django.shortcuts import render
from .models import Part, Vehicle
from django.db.models import Q # <--- Add this import at the top!

def part_search(request):
    vehicles = Vehicle.objects.all()
    
    # 1. Get parameters from the URL
    query_vehicle_id = request.GET.get('vehicle')
    query_barcode = request.GET.get('q') # 'q' stands for Query (Search Text)
    
    results = None
    
    # 2. Logic: Search by Car OR by Barcode
    if query_vehicle_id:
        results = Part.objects.filter(compatible_vehicles__id=query_vehicle_id)
    elif query_barcode:
        # Search if the Barcode matches OR if the Name contains the text
        results = Part.objects.filter(
            Q(part_number__icontains=query_barcode) | 
            Q(name__icontains=query_barcode)
        )

    return render(request, 'inventory/search.html', {
        'vehicles': vehicles,
        'results': results,
        'query': query_barcode # Pass this back so we can show it in the search box
    })

# 2. THE SCANNER VIEW (Must be separate!)
def scanner_view(request):
    return render(request, 'inventory/scanner.html')

def sell_part(request, stock_id):
    # 1. Find the specific stock record (e.g., Book at Riyadh Branch)
    stock = get_object_or_404(Stock, id=stock_id)
    
    # 2. Safety Check: Do we have enough?
    if stock.quantity > 0:
        stock.quantity -= 1
        stock.save()
        print(f"SOLD 1 ITEM. NEW QUANTITY: {stock.quantity}")
    
    # 3. Reload the page so they see the new number
    # We send them back to the search page (keeping their search query if possible)
    return redirect('part_search')