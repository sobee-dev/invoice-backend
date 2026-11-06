from django.urls import path

from .views import dashboard, outstanding_overview

urlpatterns = [
    path('dashboard/', dashboard, name='reports-dashboard'),
    path('outstanding/', outstanding_overview, name='reports-outstanding'),
]