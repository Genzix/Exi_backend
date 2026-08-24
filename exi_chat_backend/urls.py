from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def api_root(request):
    return JsonResponse(
        {
            "name": "EXI Chat API",
            "status": "ok",
            "api": "/api/",
            "admin": "/admin/",
        }
    )


urlpatterns = [
    path("", api_root),
    path("admin/", admin.site.urls),
    path("api/", include("accounts.urls")),
    path("api/", include("chats.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
