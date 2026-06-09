from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework_simplejwt.views import TokenRefreshView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

urlpatterns = [
    path('admin/', admin.site.urls),

    # API endpoints
    path('api/auth/', include('accounts.urls')),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/patterns/', include('patterns.urls')),
    path('api/exams/', include('exams.urls')),
    path('api/questions/', include('questions.urls')),
    path('api/evaluation/', include('exams.evaluation_urls')),
    path('api/student-analytics/', include('exams.student_analytics_urls')),

    # Timetable APIs (now using accounts models)
    path('api/timetable/', include('timetable.urls')),

    # OMR Sheet Generation and Evaluation
    path('api/omr/', include('omr.urls')),

    # AI Evaluation for Subjective Exams
    path('api/ai-evaluation/', include('exams.ai_evaluation_urls')),

    # OpenAPI schema + interactive docs (drf-spectacular). Additive, read-only.
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)