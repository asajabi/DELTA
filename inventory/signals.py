from django.contrib.auth.models import User
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import StockLocation, UserProfile, sync_stock_total_from_locations


@receiver(post_save, sender=User)
def ensure_user_profile(sender, instance, created, **kwargs):
    defaults = {
        "role": UserProfile.Roles.ADMIN if instance.is_superuser else UserProfile.Roles.CASHIER
    }
    if created:
        UserProfile.objects.create(user=instance, **defaults)
        return

    profile, _ = UserProfile.objects.get_or_create(user=instance, defaults=defaults)
    if instance.is_superuser and profile.role != UserProfile.Roles.ADMIN:
        profile.role = UserProfile.Roles.ADMIN
        profile.save(update_fields=["role"])


@receiver(post_save, sender=StockLocation)
def sync_stock_on_stocklocation_save(sender, instance, raw=False, **kwargs):
    if raw:
        return
    sync_stock_total_from_locations(part_id=instance.part_id, branch_id=instance.branch_id)


@receiver(post_delete, sender=StockLocation)
def sync_stock_on_stocklocation_delete(sender, instance, **kwargs):
    sync_stock_total_from_locations(part_id=instance.part_id, branch_id=instance.branch_id)
