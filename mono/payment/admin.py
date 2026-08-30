from django.contrib import admin
from .models import Payment

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id","user","target_type","target_id","amount","currency","status","authority","ref_id","created_at")
    list_filter = ("status","target_type")
    search_fields = ("authority","ref_id","user__email")
