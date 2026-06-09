from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from . import answer_extraction_views
from . import extraction_views
# Temporarily disabled AI/RAG features for deployment
# from . import rag_views

# Router for extraction viewsets
extraction_router = DefaultRouter()
extraction_router.register(r'extraction-jobs', extraction_views.ExtractionJobViewSet, basename='extraction-job')
extraction_router.register(r'extracted-questions', extraction_views.ExtractedQuestionViewSet, basename='extracted-question')

urlpatterns = [
    # Questions
    path('questions/', views.QuestionListView.as_view(), name='question-list'),
    path('questions/<int:pk>/', views.QuestionDetailView.as_view(), name='question-detail'),
    path('questions/<int:question_id>/verify/', views.verify_question, name='verify-question'),
    path('questions/<int:question_id>/images/', views.add_question_image, name='add-question-image'),
    path('questions/images/<int:image_id>/', views.delete_question_image, name='delete-question-image'),
    path('questions/<int:question_id>/comments/', views.add_question_comment, name='add-question-comment'),
    
    # Question banks
    path('question-banks/', views.QuestionBankListView.as_view(), name='question-bank-list'),
    path('question-banks/<int:pk>/', views.QuestionBankDetailView.as_view(), name='question-bank-detail'),
    
    # Exam questions
    path('exams/<int:exam_id>/questions/', views.ExamQuestionListView.as_view(), name='exam-question-list'),
    path('exam-validate/<int:exam_id>/', views.validate_exam_questions, name='validate-exam-questions'),
    
    # Templates
    path('templates/', views.QuestionTemplateListView.as_view(), name='question-template-list'),
    path('templates/search/', views.search_templates, name='search-templates'),
    path('templates/categories/', views.get_template_categories, name='template-categories'),
    path('templates/<int:template_id>/use/', views.use_question_template, name='use-question-template'),
    
    # AI Generation
    path('ai/generate-question/', views.generate_ai_question, name='ai-generate-question'),
    path('ai/solve-question/', views.solve_question_with_ai, name='ai-solve-question'),
    
    # Bulk operations
    path('bulk-import/', views.bulk_import_questions, name='bulk-import-questions'),
    path('bulk-import-csv/', views.bulk_import_csv, name='bulk-import-csv'),
    path('bulk-import-excel/', views.bulk_import_excel, name='bulk-import-excel'),
    path('download-template/', views.download_import_template, name='download-import-template'),
    
    # Statistics
    path('statistics/', views.question_statistics, name='question-statistics'),
    
    # Optimized bulk endpoints for pattern questions
    path('pattern-questions/', views.get_pattern_questions_bulk, name='pattern-questions-bulk'),
    path('section-questions/<int:section_id>/', views.get_section_questions, name='section-questions'),
    path('fix-question-numbers/', views.fix_question_numbers, name='fix-question-numbers'),
    path('debug-pattern-questions/', views.debug_pattern_questions, name='debug-pattern-questions'),
    
    # AI Question Extraction Endpoints
    path('bulk-extract/', extraction_views.ExtractionJobViewSet.as_view({'post': 'upload_file'}), name='bulk-extract'),
    path('bulk-extract-v3/', extraction_views.ExtractionJobViewSet.as_view({'post': 'upload_v3'}), name='bulk-extract-v3'),
    # Legacy alias for backward compatibility
    path('extraction-jobs/upload-v3/', extraction_views.ExtractionJobViewSet.as_view({'post': 'upload_v3'}), name='extraction-jobs-upload-v3'),
    path('extraction-status/<uuid:pk>/', extraction_views.ExtractionJobViewSet.as_view({'get': 'get_status'}), name='extraction-status'),
    path('extracted/<uuid:pk>/', extraction_views.ExtractionJobViewSet.as_view({'get': 'get_extracted_questions'}), name='extracted-questions'),
    path('bulk-import-extracted/', extraction_views.bulk_import_extracted_questions, name='bulk-import-extracted'),
    path('extraction-history/', extraction_views.extraction_history, name='extraction-history'),
    path('download-extracted/<uuid:job_id>/', extraction_views.download_extracted_questions, name='download-extracted'),
    
    # Enhanced Extraction Endpoints
    path('pattern-structure/<int:pattern_id>/', extraction_views.get_pattern_structure, name='pattern-structure'),
    path('analyze-mismatches/', extraction_views.analyze_extraction_mismatches, name='analyze-mismatches'),
    
    # Document Pre-Analysis Endpoints
    path('pre-analyze/', extraction_views.pre_analyze_document, name='pre-analyze'),
    path('pre-analyze/<uuid:job_id>/subjects/', extraction_views.get_pre_analysis_subjects, name='pre-analysis-subjects'),
    path('pre-analyze/<uuid:job_id>/subjects/<str:subject>/download/', extraction_views.download_subject_content, name='download-subject-content'),
    path('pre-analyze/<uuid:job_id>/confirm/', extraction_views.confirm_pre_analysis, name='confirm-pre-analysis'),
    
    # Import Preview Endpoint (NEW - shows what will be imported before confirming)
    path('import-preview/<uuid:job_id>/', extraction_views.get_import_preview, name='import-preview'),
    
    # Section-Based Extraction & Import Confirmation Endpoints
    path('extract-by-section/', extraction_views.extract_questions_by_section, name='extract-by-section'),
    path('section-import-preview/', extraction_views.get_section_import_preview, name='section-import-preview'),
    path('section-capacity/<int:pattern_id>/<str:subject>/', extraction_views.get_section_capacity, name='section-capacity'),
    path('confirm-section-import/', extraction_views.confirm_section_import, name='confirm-section-import'),
    path('full-extraction-flow/', extraction_views.full_extraction_flow, name='full-extraction-flow'),
    
    # Image to Text Extraction (Mathpix OCR)
    path('image-to-text/', extraction_views.extract_text_from_image, name='image-to-text'),

    # Answer-key Extraction (upload an answer-key PDF and apply answers to existing questions)
    path('answer-keys/', answer_extraction_views.upload_answer_key, name='answer-key-upload'),
    path('answer-keys/<uuid:job_id>/status/', answer_extraction_views.get_answer_key_status, name='answer-key-status'),
    path('answer-keys/<uuid:job_id>/preview/', answer_extraction_views.get_answer_key_preview, name='answer-key-preview'),
    path('answer-keys/<uuid:job_id>/apply/', answer_extraction_views.apply_answer_key, name='answer-key-apply'),
    path('extracted-answers/<int:pk>/', answer_extraction_views.update_extracted_answer, name='extracted-answer-update'),

    # Include extraction router URLs
    path('', include(extraction_router.urls)),
    
    # AI & RAG Endpoints - Temporarily disabled for deployment
    # Uncomment these when you have more server resources and install AI packages
    # path('semantic-search/', rag_views.semantic_search_view, name='semantic-search'),
    # path('chatbot/', rag_views.chatbot_query_view, name='chatbot-query'),
    # path('chat-history/', rag_views.chat_history_view, name='chat-history'),
    # path('<int:question_id>/embed/', rag_views.embed_single_question_view, name='embed-question'),
    # path('bulk-embed/', rag_views.bulk_embed_questions_view, name='bulk-embed'),
    # path('embedding-stats/', rag_views.embedding_stats_view, name='embedding-stats'),
]
