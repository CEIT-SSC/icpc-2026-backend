from django.urls import path
from .views import (
    CourseDetailView,
    CourseListView,
    CourseSessionsView,
    MyRegistrationsView,
    RegistrationCreateView,
    SkyroomLinkView,
)

urlpatterns = [
    path("offerings/", CourseListView.as_view()),
    # Fetch the course by slug (e.g., "algorithms-bootcamp")
    path("course/<slug:slug>/", CourseDetailView.as_view()),
    path("course/<slug:slug>/sessions/", CourseSessionsView.as_view()),
    # Create a registration (must be logged in & verified)
    path("register/", RegistrationCreateView.as_view()),
    # List my registrations
    path("me/registrations/", MyRegistrationsView.as_view()),
    path("participation/link/", SkyroomLinkView.as_view())
]
