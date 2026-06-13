from django.urls import path, include
from . import views, scheduling_views, email_views, predictive_views, proctoring_views, center_dashboard_views

urlpatterns = [
    # Center Dashboard
    path('center-dashboard-stats/', center_dashboard_views.center_dashboard_stats, name='center-dashboard-stats'),
    path('center-activity/', center_dashboard_views.center_activity, name='center-activity'),
    path('center-batch-stats/', center_dashboard_views.center_batch_stats, name='center-batch-stats'),
    path('center-upcoming-exams/', center_dashboard_views.center_upcoming_exams, name='center-upcoming-exams'),
    
    # Exams
    path('exams/', views.ExamListView.as_view(), name='exam-list'),
    path('exams/bulk-delete/', views.bulk_delete_exams, name='bulk-delete-exams'),
    path('exams/<int:pk>/', views.ExamDetailView.as_view(), name='exam-detail'),
    path('exams/<int:pk>/question-paper/', views.download_question_paper, name='download-question-paper'),
    path('exams/<int:exam_id>/dashboard/', views.exam_dashboard, name='exam-dashboard'),
    path('exams/<int:exam_id>/eligible-students/', views.exam_eligible_students, name='exam-eligible-students'),
    
    # Exam attempts
    path('exams/<int:exam_id>/attempts/', views.ExamAttemptListView.as_view(), name='exam-attempt-list'),
    path('attempts/', views.AllExamAttemptsListView.as_view(), name='all-exam-attempts-list'),
    path('attempts/export/', views.export_all_attempts, name='export-all-exam-attempts'),
    path('attempts/<int:pk>/', views.ExamAttemptDetailView.as_view(), name='exam-attempt-detail'),
    
    # Exam actions
    path('start-exam/', views.start_exam, name='start-exam'),
    path('submit-exam/', views.submit_exam, name='submit-exam'),
    
    # Invitations
    path('exams/<int:exam_id>/invitations/', views.ExamInvitationListView.as_view(), name='exam-invitation-list'),
    
    # Analytics
    path('exams/<int:pk>/analytics/', views.ExamAnalyticsView.as_view(), name='exam-analytics'),
    path('exams/<int:exam_id>/analytics-dashboard/', views.exam_analytics_dashboard, name='exam-analytics-dashboard'),
    path('exams/<int:exam_id>/results-dashboard/', views.exam_results_dashboard, name='exam-results-dashboard'),
    path('exams/<int:exam_id>/student-result/<int:student_id>/', views.exam_student_result_detail, name='exam-student-result-detail'),
    
    # Enhanced Analytics Endpoints
    path('exams/<int:exam_id>/analytics/statistics/', views.exam_statistics_detailed, name='exam-statistics-detailed'),
    path('exams/<int:exam_id>/analytics/heatmap/', views.exam_heatmap_data, name='exam-heatmap-data'),
    path('exams/<int:exam_id>/analytics/histogram/', views.exam_histogram_data, name='exam-histogram-data'),
    path('exams/<int:exam_id>/analytics/boxplot/', views.exam_boxplot_data, name='exam-boxplot-data'),
    path('exams/<int:exam_id>/analytics/questions/', views.exam_question_analytics, name='exam-question-analytics'),
    path('exams/<int:exam_id>/analytics/evaluation/', views.exam_evaluation_analytics, name='exam-evaluation-analytics'),
    path('exams/<int:exam_id>/analytics/graphs/', views.exam_performance_graphs, name='exam-performance-graphs'),
    
    # Export and AI Insights
    path('exams/<int:exam_id>/export/', views.exam_export_data, name='exam-export-data'),
    path('exams/<int:exam_id>/export/csv/', views.export_exam_results_csv, name='export-exam-results-csv'),
    path('exams/<int:exam_id>/export/excel/', views.export_exam_results_excel, name='export-exam-results-excel'),
    path('exams/<int:exam_id>/export/pdf/', views.export_exam_results_pdf, name='export-exam-results-pdf'),
    path('exams/<int:exam_id>/ai-insights/', views.exam_ai_insights, name='exam-ai-insights'),
    
    # Evaluation System
    path('evaluation/attempts/<int:attempt_id>/evaluate/', views.evaluate_exam_attempt, name='evaluate-exam-attempt'),
    path('evaluation/exams/<int:exam_id>/progress/', views.get_evaluation_progress, name='get-evaluation-progress'),
    path('evaluation/attempts/<int:attempt_id>/questions/', views.get_question_evaluations, name='get-question-evaluations'),
    path('evaluation/questions/<int:evaluation_id>/manual/', views.manual_evaluate_question, name='manual-evaluate-question'),
    path('evaluation/questions/<int:evaluation_id>/ai/', views.ai_evaluate_question, name='ai-evaluate-question'),
    path('evaluation/exams/<int:exam_id>/batches/', views.get_evaluation_batches, name='get-evaluation-batches'),
    path('evaluation/exams/<int:exam_id>/settings/', views.update_evaluation_settings, name='update-evaluation-settings'),
    path('evaluation/exams/<int:exam_id>/pending/', views.get_pending_evaluations, name='get-pending-evaluations'),
    path('evaluation/exams/<int:exam_id>/batch-ai/', views.batch_ai_evaluate, name='batch-ai-evaluate'),
    
    # Security and Proctoring
    path('attempts/<int:attempt_id>/violations/', views.log_violation, name='log-violation'),
    path('attempts/<int:attempt_id>/violations/history/', views.get_violations, name='get-violations'),
    path('attempts/<int:attempt_id>/proctoring/snapshot/', views.upload_snapshot, name='upload-snapshot'),
    path('attempts/<int:attempt_id>/proctoring/snapshots/', views.get_proctoring_snapshots, name='get-proctoring-snapshots'),
    path('proctoring/upload-clip/', views.upload_proctoring_clip, name='upload-proctoring-clip'),
    path('attempts/<int:attempt_id>/proctoring/incidents/', views.log_proctoring_incident, name='log-proctoring-incident'),
    path('attempts/<int:attempt_id>/auto-save/', views.auto_save_answers, name='auto-save-answers'),
    path('validate-access/', views.validate_exam_access, name='validate-exam-access'),
    path('attempts/<int:attempt_id>/results/', views.get_exam_result, name='get-exam-result'),
    path('attempts/<int:attempt_id>/answer-sheet/', views.get_answer_sheet_pdf, name='get-answer-sheet-pdf'),
    path('exams/<int:exam_id>/attempts/latest/', views.get_latest_exam_attempt, name='get-latest-exam-attempt'),
    path('violation-dashboard/', views.violation_dashboard, name='violation-dashboard'),
    path('student-dashboard/', views.student_dashboard_data, name='student-dashboard'),
    
    # Admin Dashboard
    path('admin-dashboard/', views.admin_dashboard_data, name='admin-dashboard'),
    
    # Public Exam Access
    path('exams/<int:exam_id>/public-link/', views.public_exam_link_details, name='public-exam-link-details'),
    path('public-access/<uuid:token>/', views.public_exam_details, name='public-exam-details'),
    path('public-access/', views.public_exam_access, name='public-exam-access'),
    
    # Student Analytics
    path('student-analytics/', include('exams.student_analytics_urls')),
    
    # Scheduling & Timezone
    path('timezones/', scheduling_views.get_timezones, name='get-timezones'),
    path('exams/<int:exam_id>/schedule-info/', scheduling_views.get_exam_schedule_info, name='get-exam-schedule-info'),
    path('exams/<int:exam_id>/reschedule/', scheduling_views.request_exam_reschedule, name='request-exam-reschedule'),
    path('exams/<int:exam_id>/reschedule-requests/', scheduling_views.get_reschedule_requests, name='get-reschedule-requests'),
    path('reschedule-requests/', scheduling_views.get_student_reschedule_requests, name='get-student-reschedule-requests'),
    path('reschedule-requests/<int:reschedule_id>/review/', scheduling_views.review_reschedule_request, name='review-reschedule-request'),
    
    # Email invitations
    path('exams/<int:exam_id>/invitations/send/', email_views.send_exam_invitations, name='send-exam-invitations'),
    path('exams/<int:exam_id>/invitations/', email_views.get_exam_invitations, name='get-exam-invitations'),
    path('invitations/<int:invitation_id>/resend/', email_views.resend_invitation, name='resend-invitation'),
    path('invitations/<int:invitation_id>/accept/', email_views.accept_invitation, name='accept-invitation'),
    path('invitations/<int:invitation_id>/decline/', email_views.decline_invitation, name='decline-invitation'),
    path('invitations/student/', email_views.get_student_invitations, name='get-student-invitations'),
    path('exams/<int:exam_id>/reminders/', email_views.send_reminder_emails, name='send-reminder-emails'),
    
    # Predictive Analytics
    path('students/<int:student_id>/exams/<int:exam_id>/predict/', predictive_views.get_student_performance_prediction, name='student-performance-prediction'),
    path('exams/<int:exam_id>/difficulty-predict/', predictive_views.get_exam_difficulty_prediction, name='exam-difficulty-prediction'),
    path('exams/<int:exam_id>/at-risk-students/', predictive_views.get_at_risk_students, name='at-risk-students'),
    path('exams/<int:exam_id>/performance-insights/', predictive_views.get_performance_insights, name='performance-insights'),
    path('students/<int:student_id>/analytics/', predictive_views.get_student_analytics_dashboard, name='student-analytics-dashboard'),
    path('exams/<int:exam_id>/performance-comparison/', predictive_views.get_performance_comparison, name='performance-comparison'),
    
    # AI Proctoring
    path('attempts/<int:attempt_id>/analyze/', proctoring_views.analyze_exam_session, name='analyze-exam-session'),
    path('attempts/<int:attempt_id>/detect-violations/', proctoring_views.detect_real_time_violations, name='detect-real-time-violations'),
    path('exams/<int:exam_id>/proctoring-dashboard/', proctoring_views.get_proctoring_dashboard, name='proctoring-dashboard'),
    path('exams/<int:exam_id>/violations/', proctoring_views.get_violations, name='get-violations'),
    path('students/<int:student_id>/proctoring-history/', proctoring_views.get_student_proctoring_history, name='student-proctoring-history'),
    path('exams/<int:exam_id>/proctoring-settings/', proctoring_views.update_proctoring_settings, name='update-proctoring-settings'),
    path('exams/<int:exam_id>/proctoring-statistics/', proctoring_views.get_proctoring_statistics, name='proctoring-statistics'),
    path('attempts/<int:attempt_id>/proctoring-event/', proctoring_views.record_proctoring_event, name='record-proctoring-event'),
    
    # Geolocation
    path('capture-location/', views.capture_location, name='capture-location'),
    path('attempt/<int:attempt_id>/location/', views.get_attempt_location, name='get-attempt-location'),
]
