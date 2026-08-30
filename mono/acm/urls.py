"""
URL configuration for acm project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from accounts.views_oauth import GithubLoginView, GithubCallbackView
from acm.views_uploads import UploadView


# Simple healthcheck api
def healthz(_): return HttpResponse("ok")

urlpatterns = [
    # admin shit
    path('api/admin/', admin.site.urls),

    # app shits
    path("api/notification/", include("notification.urls")),
    path("api/accounts/", include("accounts.urls")),
    path("api/presentations/", include("presentations.urls")),
    path("api/competitions/", include("competitions.urls")),
    path("api/payment/", include("payment.urls")),

    # healthcheck shit
    path("healthz", healthz),

    # swagger shits
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/swagger/", SpectacularSwaggerView.as_view(url_name="schema")),

    # Storage utils
    path('api/upload/', UploadView.as_view(), name='api-upload'),
]
