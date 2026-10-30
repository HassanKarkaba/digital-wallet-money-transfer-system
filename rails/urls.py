from django.urls import path

from . import views

app_name = 'rails'
urlpatterns = [path('integrations/simulator/events/', views.simulator_events, name='simulator_events')]
