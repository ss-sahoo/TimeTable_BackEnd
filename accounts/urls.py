from django.urls import path
from . import views
from .analytics_views import dashboard_analytics
from .admin_center_views import assign_center_to_admin, remove_center_from_admin, get_available_centers
from .timetable_views import list_people
from .password_reset_views import forgot_password, reset_password, validate_reset_token
from .timetable_auth_views import (
    SuperAdminLoginView,
    AdminLoginView,
    TeacherLoginView,
    StudentLoginView,
    StaffLoginView,
    ManagerLoginView,
    change_password as timetable_change_password,
)
from .device_session_views import (
    check_device_view,
    logout_device_view,
    active_sessions_view,
    delete_session_view,
)

urlpatterns = [
    # Authentication (generic exam auth)
    path('register/', views.user_registration_view, name='user-register'),
    path('login/', views.user_login_view, name='user-login'),
    path('logout/', views.user_logout_view, name='user-logout'),
    path('forgot-password/', forgot_password, name='forgot-password'),
    path('reset-password/', reset_password, name='reset-password'),
    path('validate-reset-token/', validate_reset_token, name='validate-reset-token'),

    # Role-based auth endpoints (same style as timetable, but under /api/auth/)
    path('superadmin/login/', SuperAdminLoginView.as_view(), name='exam-superadmin-login'),
    path('admin/login/', AdminLoginView.as_view(), name='exam-admin-login'),
    path('teacher/login/', TeacherLoginView.as_view(), name='exam-teacher-login'),
    path('student/login/', StudentLoginView.as_view(), name='exam-student-login'),
    path('staff/login/', StaffLoginView.as_view(), name='exam-staff-login'),
    path('manager/login/', ManagerLoginView.as_view(), name='exam-manager-login'),
    path('auth/change-password/', timetable_change_password, name='exam-change-password'),
    
    # User management
    path('profile/', views.UserProfileView.as_view(), name='user-profile'),
    path('change-password/', views.ChangePasswordView.as_view(), name='change-password'),
    path('dashboard/', views.user_dashboard_view, name='user-dashboard'),
    
    # User listing
    path('users/', views.UserListView.as_view(), name='user-list'),
    path('users/<int:pk>/', views.UserDetailView.as_view(), name='user-detail'),
    path('users/<int:pk>/reset-password/', views.admin_reset_password, name='admin-reset-password'),
    path('people/', list_people, name='all-people'),  # Generic unified view
    
    # Permissions
    path('permissions/', views.UserPermissionListView.as_view(), name='user-permission-list'),
    
    # Institutes
    path('institutes/', views.InstituteListCreateView.as_view(), name='institute-list-create'),
    path('institutes/<int:pk>/', views.InstituteDetailView.as_view(), name='institute-detail'),
    path('institutes/<int:pk>/update/', views.InstituteUpdateView.as_view(), name='institute-update'),
    path('institutes/<int:institute_id>/users/', views.InstituteUserListView.as_view(), name='institute-user-list'),
    path('institute-settings/', views.InstituteSettingsView.as_view(), name='institute-settings'),
    path('institute-search/', views.institute_search, name='institute-search'),
    path('leave-institute/', views.leave_institute, name='leave-institute'),
    
    # Institute Invitations
    path('invitations/', views.InstituteInvitationListView.as_view(), name='institute-invitation-list'),
    path('invitations/<int:pk>/', views.InstituteInvitationDetailView.as_view(), name='institute-invitation-detail'),
    path('invitations/<int:invitation_id>/accept/', views.accept_invitation, name='accept-invitation'),
    path('invitations/<int:invitation_id>/decline/', views.decline_invitation, name='decline-invitation'),
    path('my-invitations/', views.my_invitations, name='my-invitations'),
    
    # Analytics
    path('analytics/dashboard/', dashboard_analytics, name='dashboard-analytics'),
    
    # Admin Center Assignment
    path('assign-center/', assign_center_to_admin, name='assign-center'),
    path('remove-center/', remove_center_from_admin, name='remove-center'),
    path('available-centers/', get_available_centers, name='available-centers'),
    
    # Device Session Management
    path('check-device/', check_device_view, name='check-device'),
    path('logout-device/', logout_device_view, name='logout-device'),
    path('active-sessions/', active_sessions_view, name='active-sessions'),
    path('session/<str:fingerprint>/', delete_session_view, name='delete-session'),
    
    # Activity Logs
    path('activity-logs/', views.ActivityLogListView.as_view(), name='activity-log-list'),
]
