from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile


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
