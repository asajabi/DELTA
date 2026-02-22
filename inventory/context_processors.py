from .models import UserProfile


def nav_context(request):
    if not request.user.is_authenticated:
        return {"nav_is_manager": False}

    default_role = UserProfile.Roles.ADMIN if request.user.is_superuser else UserProfile.Roles.CASHIER
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={"role": default_role},
    )

    nav_is_manager = (
        request.user.is_superuser
        or profile.role in {UserProfile.Roles.MANAGER, UserProfile.Roles.ADMIN}
        or request.user.groups.filter(name__in=["manager", "admin"]).exists()
    )
    return {"nav_is_manager": nav_is_manager}
