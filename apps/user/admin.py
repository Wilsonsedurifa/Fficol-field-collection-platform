from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, ClientAccess

admin.site.register(User,UserAdmin)


@admin.register(ClientAccess)
class ClientAccessAdmin(admin.ModelAdmin):
    list_display=("user","client","is_active")
    list_filter=("is_active","client")
    search_fields=("user__username","client__name","client__code")
