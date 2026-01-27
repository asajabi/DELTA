from django.contrib import admin
from .models import Vehicle, Branch, Category, Part, Stock

# This makes the "Compatibility" box easier to use
class PartAdmin(admin.ModelAdmin):
    list_display = ('name', 'part_number', 'selling_price')
    filter_horizontal = ('compatible_vehicles',) # Cool selector for cars

admin.site.register(Vehicle)
admin.site.register(Branch)
admin.site.register(Category)
admin.site.register(Part, PartAdmin)
admin.site.register(Stock)