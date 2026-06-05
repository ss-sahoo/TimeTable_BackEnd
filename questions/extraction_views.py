"""
API views for question extraction
"""
import os
import logging
from typing import Dict, List
from uuid import UUID
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone
from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from exam_flow_backend.throttling import BulkExtractUserRateThrottle

from questions.models import ExtractionJob, ExtractedQuestion
from questions.extraction_serializers import (
    ExtractionJobSerializer,
    ExtractedQuestionSerializer,
    ExtractionJobCreateSerializer,
    BulkImportSerializer,
    ExtractionStatusSerializer,
)
from questions.tasks import extract_questions_task, extract_questions_v3_task
from questions.services.bulk_import import BulkImportService, BulkImportError
from questions.services.section_question_extractor import SectionQuestionExtractor, SectionExtractionError
from questions.services.section_mapper import SectionMapper, ImportConfirmationFlow
from questions.services.subject_section_detector import SubjectSectionDetector

logger = logging.getLogger('extraction')


class ExtractionJobViewSet(viewsets.ModelViewSet):
    """ViewSet for managing extraction jobs"""
    
    serializer_class = ExtractionJobSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    
    def get_queryset(self):
        """Get extraction jobs for current user"""
        user = self.request.user
        from accounts.utils import get_current_db
        current_db = get_current_db() or 'default'

        # Filter by user's institute
        queryset = ExtractionJob.objects.using(current_db).filter(
            exam__institute=user.institute
        ).select_related('exam', 'pattern', 'created_by')
        
        # Filter by status if provided
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by exam if provided
        exam_id = self.request.query_params.get('exam_id')
        if exam_id:
            queryset = queryset.filter(exam_id=exam_id)
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['post'], url_path='upload', throttle_classes=[BulkExtractUserRateThrottle])
    def upload_file(self, request):
        """
        Upload a question paper and create an extraction job (V1/V3 router).

        POST /api/questions/extraction-jobs/upload/

        Multipart form fields (see ExtractionJobCreateSerializer):
            file, exam_id, pattern_id, subject (optional)

        Query params:
            use_pre_analysis: 'true' (default) — run pre-analysis first
                              for better per-subject separation.
            use_v3:           'true' (default) — use the V3 Celery pipeline.
                              Set to 'false' to fall back to V1 for legacy
                              compatibility.

        Responses:
            201/200: {"job_id": "...", "status": "queued", ...}
            400: serializer validation failure (size, type, magic-byte mismatch).
            403: exam not in caller's institute.

        Rate limited per `bulk_extract` scope.
        """
        serializer = ExtractionJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            # Get validated data
            uploaded_file = serializer.validated_data['file']
            exam_id = serializer.validated_data['exam_id']
            pattern_id = serializer.validated_data['pattern_id']
            subject = serializer.validated_data.get('subject', '')
            
            # Get exam and pattern
            from exams.models import Exam
            from patterns.models import ExamPattern
            
            exam = Exam.objects.get(id=exam_id)
            pattern = ExamPattern.objects.get(id=pattern_id)
            
            # Check permissions
            if exam.institute != request.user.institute:
                return Response(
                    {'error': 'You do not have permission to upload files for this exam'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Save uploaded file
            upload_dir = os.path.join(settings.MEDIA_ROOT, 'extraction_uploads')
            os.makedirs(upload_dir, exist_ok=True)
            
            file_path = os.path.join(upload_dir, f"{timezone.now().timestamp()}_{uploaded_file.name}")
            
            with open(file_path, 'wb+') as destination:
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)
            
            # ENHANCED: Check if pre-analysis should be used (default: true)
            use_pre_analysis = request.query_params.get('use_pre_analysis', 'true').lower() in ('true', '1', 'yes')
            use_v3 = request.query_params.get('use_v3', 'true').lower() in ('true', '1', 'yes')
            
            pre_analysis_job = None
            
            if use_pre_analysis:
                # Run pre-analysis to get subject-separated content
                try:
                    from questions.services.file_parser import FileParserService
                    from questions.services.document_pre_analyzer import DocumentPreAnalyzer
                    from questions.models import PreAnalysisJob
                    
                    logger.info(f"Running pre-analysis for {uploaded_file.name}")
                    
                    # Get pattern subjects
                    pattern_subjects = list(
                        pattern.sections.values_list('subject', flat=True).distinct()
                    )
                    
                    # Parse file
                    file_parser = FileParserService()
                    text_content = file_parser.parse_file(file_path, uploaded_file.content_type)
                    
                    # Run pre-analysis
                    analyzer = DocumentPreAnalyzer()
                    result = analyzer.analyze_document(text_content, pattern_subjects)
                    
                    # Create pre-analysis job
                    pre_analysis_job = PreAnalysisJob.objects.create(
                        pattern=pattern,
                        created_by=request.user,
                        file_name=uploaded_file.name,
                        file_type=uploaded_file.content_type,
                        file_size=uploaded_file.size,
                        file_path=file_path,
                        status='completed',
                    )
                    pre_analysis_job.mark_completed(result)
                    
                    logger.info(
                        f"Pre-analysis completed: {len(result.detected_subjects)} subjects, "
                        f"{result.total_estimated_questions} questions"
                    )
                    
                except Exception as pre_error:
                    logger.warning(f"Pre-analysis failed, continuing without it: {pre_error}")
                    pre_analysis_job = None
            
            # Create extraction job with link to pre-analysis if available
            job = ExtractionJob.objects.create(
                exam=exam,
                pattern=pattern,
                created_by=request.user,
                file_name=uploaded_file.name,
                file_type=uploaded_file.content_type,
                file_size=uploaded_file.size,
                file_path=file_path,
                status='pending',
                pre_analysis_job=pre_analysis_job,  # Link to pre-analysis for subject separation
            )
            
            # If pre-analysis was created, link it back
            if pre_analysis_job:
                pre_analysis_job.extraction_job = job
                pre_analysis_job.save(update_fields=['extraction_job'])
                logger.info(f"Linked ExtractionJob {job.id} to PreAnalysisJob {pre_analysis_job.id}")
            
            # Trigger extraction task
            try:
                if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
                    # Run synchronously for testing
                    if use_v3:
                        extract_questions_v3_task(str(job.id))
                    else:
                        extract_questions_task(str(job.id), use_v2=True)
                else:
                    # Run async with Celery
                    if use_v3:
                        extract_questions_v3_task.delay(str(job.id))
                    else:
                        extract_questions_task.delay(str(job.id), use_v2=True)
            except Exception as task_error:
                logger.warning(f"Celery task failed, running synchronously: {task_error}")
                # Fallback to synchronous V3 execution
                try:
                    from questions.services.pipeline import ExtractionPipelineV3
                    pipeline = ExtractionPipelineV3(job_id=str(job.id))
                    pipeline.run()
                except Exception as v3_error:
                    logger.warning(f"V3 fallback failed, trying V2: {v3_error}")
                    from questions.services.extraction_pipeline_v2 import ExtractionPipelineV2
                    pipeline = ExtractionPipelineV2()
                    pipeline.process_file(job.id)
            
            logger.info(
                f"Created extraction job {job.id} for file {uploaded_file.name} "
                f"by user {request.user.email} (pre_analysis: {pre_analysis_job is not None})"
            )
            
            response_data = {
                'job_id': str(job.id),
                'status': job.status,
                'message': 'File uploaded successfully. Extraction started.',
                'used_pre_analysis': pre_analysis_job is not None,
            }
            
            if pre_analysis_job:
                response_data['pre_analysis_job_id'] = str(pre_analysis_job.id)
                response_data['detected_subjects'] = pre_analysis_job.detected_subjects
                response_data['total_estimated_questions'] = pre_analysis_job.total_estimated_questions
            
            return Response(
                response_data,
                status=status.HTTP_201_CREATED
            )
            
        except Exception as e:
            logger.error(f"File upload failed: {str(e)}", exc_info=True)
            return Response(
                {'error': f'Failed to upload file: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    
    @action(detail=False, methods=['POST'], parser_classes=[MultiPartParser, FormParser], url_path='upload-v3', throttle_classes=[BulkExtractUserRateThrottle])
    def upload_v3(self, request):
        """
        Upload a question paper and kick off the V3 extraction pipeline
        (Mathpix OCR + Gemini per-subject via AgentExtractionService).

        Multipart form fields:
            file (required)            — PDF/DOCX/image
            exam_id / exam (required)  — target exam (must belong to caller's institute)
            pattern_id / pattern (req) — exam pattern
            pre_analysis_job_id (opt)  — link a confirmed PreAnalysisJob

        Responses:
            201/200: {"job_id": "...", "status": "queued", ...}
            400: missing fields / unsupported file type / magic-byte mismatch
            403: exam not in caller's institute

        Long-running: actual extraction runs in a Celery task with a 30-minute
        time limit. Poll job status via the retrieve endpoint.

        Rate limited per `bulk_extract` scope.
        """
        import os
        import uuid
        from django.core.files.storage import default_storage
        from django.conf import settings
        from django.utils.text import get_valid_filename
        from questions.tasks import extract_questions_v3_task
        from patterns.models import ExamPattern
        from exams.models import Exam

        # 1. Validate inputs
        if 'file' not in request.FILES:
            return Response({'error': 'No file uploaded'}, status=status.HTTP_400_BAD_REQUEST)
        
        uploaded_file = request.FILES['file']
        exam_id = request.data.get('exam_id') or request.data.get('exam')
        pattern_id = request.data.get('pattern_id') or request.data.get('pattern')
        
        if not exam_id or not pattern_id:
            return Response({'error': 'exam_id and pattern_id are required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            # 2. Get related objects
            exam = Exam.objects.get(id=exam_id)
            pattern = ExamPattern.objects.get(id=pattern_id)
            
            # 3. Save file manually to ensure path is correct
            file_name = get_valid_filename(uploaded_file.name)
            base, ext = os.path.splitext(file_name)
            if not ext: ext = ''
            
            # Create uploads dir if not exists
            upload_subpath = 'extraction_uploads'
            upload_dir = os.path.join(settings.MEDIA_ROOT, upload_subpath)
            os.makedirs(upload_dir, exist_ok=True)
            
            # Unique filename
            unique_name = f"{base}_{uuid.uuid4().hex[:8]}{ext}"
            file_title = unique_name
            full_path = os.path.join(upload_dir, unique_name)
            
            # Write file
            with open(full_path, 'wb+') as destination:
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)
            
            # IMPORTANT: For ExtractionJob, the file_path should be absolute or relative to media root?
            # V3 pipeline expects ABSOLUTE path usually, or media-relative.
            # Let's save absolute path for safety in the model
            
            # 4. Create Job
            job = ExtractionJob.objects.create(
                exam=exam,
                pattern=pattern,
                created_by=request.user,
                file_name=uploaded_file.name,
                file_type=uploaded_file.content_type,
                file_size=uploaded_file.size,
                file_path=full_path,
                status='pending',
            )
            
            logger.info(f"V3 Upload: Created job {job.id} for file {full_path}")
            
            # 5. Trigger V3 Task
            # Support optional subject list for targeted extraction
            # 5. Trigger V3 Task
            # Support optional subject list for targeted extraction
            subjects_list = request.data.get('subjects')
            
            # Handle list vs string from FormData
            if subjects_list:
                if isinstance(subjects_list, str):
                    try:
                        import json
                        # Try parsing as JSON first (e.g. '["Physics"]')
                        subjects_list = json.loads(subjects_list)
                    except:
                        # Fallback to comma-separated
                        subjects_list = [s.strip() for s in subjects_list.split(',') if s.strip()]
                elif isinstance(subjects_list, list):
                    # Already a list? ensuring strings
                    pass
            
            if not subjects_list:
                subjects_list = None
                
            logger.info(f"V3 Upload: Parsed subjects for extraction: {subjects_list} (type: {type(subjects_list)})")
            
            # FORCE SYNC EXECUTION for V3 to ensure it runs in user's environment
            # This bypasses Celery queue which seems to be stuck or not running
            logger.info(f"Force running V3 extraction synchronously for job {job.id} with subjects: {subjects_list}")
            try:
                 extract_questions_v3_task(str(job.id), subjects=subjects_list)
            except Exception as e:
                 logger.error(f"Sync execution failed: {e}")
                 # Fallback to delay if sync fails (unlikely)
                 extract_questions_v3_task.delay(str(job.id), subjects=subjects_list)
                
            return Response({
                'job_id': str(job.id),
                'status': 'processing', # It should be processing or completed now
                'message': 'Extraction V3 started successfully',
                'file_url': f"{settings.MEDIA_URL}{upload_subpath}/{unique_name}"
            }, status=status.HTTP_201_CREATED)
            
        except Exam.DoesNotExist:
             return Response({'error': 'Exam not found'}, status=status.HTTP_404_NOT_FOUND)
        except ExamPattern.DoesNotExist:
             return Response({'error': 'Pattern not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Upload V3 failed: {e}", exc_info=True)
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='status')
    def get_status(self, request, pk=None):
        """
        Get extraction job status

        GET /api/questions/extraction-jobs/{job_id}/status/
        """
        try:
            from accounts.utils import get_current_db
            current_db = get_current_db() or 'default'
            job = ExtractionJob.objects.using(current_db).get(id=pk)
            
            # Calculate estimated time remaining
            estimated_time = None
            if job.status == 'processing' and job.processing_time_seconds:
                # Rough estimate based on progress
                if job.progress_percent > 0:
                    elapsed = job.processing_time_seconds
                    total_estimated = (elapsed / job.progress_percent) * 100
                    estimated_time = int(total_estimated - elapsed)
            
            serializer = ExtractionStatusSerializer({
                'job_id': job.id,
                'status': job.status,
                'progress_percent': job.progress_percent,
                'total_questions_found': job.total_questions_found,
                'questions_extracted': job.questions_extracted,
                'estimated_time_remaining': estimated_time,
                'error_message': job.error_message,
            })
            
            return Response(serializer.data)
            
        except Exception as e:
            logger.error(f"Failed to get job status: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'], url_path='questions')
    def get_extracted_questions(self, request, pk=None):
        """
        Get extracted questions for a job

        GET /api/questions/extraction-jobs/{job_id}/questions/
        """
        try:
            from accounts.utils import get_current_db
            current_db = get_current_db() or 'default'
            job = ExtractionJob.objects.using(current_db).get(id=pk)

            # Get extracted questions
            questions = ExtractedQuestion.objects.using(current_db).filter(job=job).order_by('id')
            
            # Filter by review status if requested
            requires_review = request.query_params.get('requires_review')
            if requires_review is not None:
                requires_review_bool = requires_review.lower() in ['true', '1', 'yes']
                questions = questions.filter(requires_review=requires_review_bool)
            
            # Filter by import status
            is_imported = request.query_params.get('is_imported')
            if is_imported is not None:
                is_imported_bool = is_imported.lower() in ['true', '1', 'yes']
                questions = questions.filter(is_imported=is_imported_bool)
            
            serializer = ExtractedQuestionSerializer(questions, many=True)
            
            return Response({
                'job_id': str(job.id),
                'status': job.status,
                'total_questions': questions.count(),
                'questions': serializer.data
            })
            
        except Exception as e:
            logger.error(f"Failed to get extracted questions: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ExtractedQuestionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing extracted questions"""
    
    serializer_class = ExtractedQuestionSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Get extracted questions for current user's institute"""
        user = self.request.user
        from accounts.utils import get_current_db
        current_db = get_current_db() or 'default'

        queryset = ExtractedQuestion.objects.using(current_db).filter(
            job__exam__institute=user.institute
        ).select_related('job', 'imported_question')
        
        # Filter by job if provided
        job_id = self.request.query_params.get('job_id')
        if job_id:
            queryset = queryset.filter(job_id=job_id)
        
        return queryset.order_by('id')
    
    def update(self, request, *args, **kwargs):
        """Update extracted question"""
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        # Don't allow updating if already imported
        if instance.is_imported:
            return Response(
                {'error': 'Cannot update question that has already been imported'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        
        # Re-validate after update
        instance.validate()
        
        return Response(serializer.data)
    
    def destroy(self, request, *args, **kwargs):
        """Delete extracted question"""
        instance = self.get_object()
        
        # Don't allow deleting if already imported
        if instance.is_imported:
            return Response(
                {'error': 'Cannot delete question that has already been imported'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def bulk_import_extracted_questions(request):
    """
    Finalize and import questions extracted by the Agentic V3 pipeline.
    """
    from questions.services.bulk_import import BulkImportService
    job_id = request.data.get('job_id')
    mappings = request.data.get('mappings', [])
    
    try:
        service = BulkImportService()
        result = service.import_questions(job_id, mappings)
        return Response(result, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Bulk import failed: {e}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# Original bulk_import_questions below...
    """
    Bulk import extracted questions into exam
    
    POST /api/questions/bulk-import/
    """
    serializer = BulkImportSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    try:
        job_id = serializer.validated_data['job_id']
        question_ids = serializer.validated_data['question_ids']
        mappings = serializer.validated_data['mappings']
        
        # Check permissions
        job = ExtractionJob.objects.get(id=job_id)
        if job.exam.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to import questions for this exam'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Import questions
        import_service = BulkImportService()
        result = import_service.import_questions(job_id, mappings)
        
        return Response(result, status=status.HTTP_200_OK)
        
    except ExtractionJob.DoesNotExist:
        return Response(
            {'error': f'Extraction job not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except BulkImportError as e:
        logger.error(f"Bulk import failed: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Bulk import failed: {str(e)}", exc_info=True)
        return Response(
            {'error': f'Failed to import questions: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def extraction_history(request):
    """
    Get extraction history for current user
    
    GET /api/questions/extraction-history/
    """
    try:
        user = request.user
        
        # Get extraction jobs
        jobs = ExtractionJob.objects.filter(
            exam__institute=user.institute
        ).select_related('exam', 'pattern', 'created_by').order_by('-created_at')
        
        # Pagination
        page_size = int(request.query_params.get('page_size', 20))
        page = int(request.query_params.get('page', 1))
        
        start = (page - 1) * page_size
        end = start + page_size
        
        total_count = jobs.count()
        jobs_page = jobs[start:end]
        
        serializer = ExtractionJobSerializer(jobs_page, many=True)
        
        return Response({
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size,
            'results': serializer.data
        })
        
    except Exception as e:
        logger.error(f"Failed to get extraction history: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_pattern_structure(request, pattern_id):
    """
    Get full pattern structure with capacity information
    
    GET /api/questions/pattern-structure/<pattern_id>/?exam_id=<exam_id>
    
    Returns:
    {
        "pattern_id": 31,
        "pattern_name": "JEE Main 2024",
        "total_required": 90,
        "total_filled": 65,
        "total_remaining": 25,
        "subjects": {
            "physics": {
                "total_required": 30,
                "total_filled": 20,
                "sections": [
                    {
                        "section_id": 5,
                        "section_name": "Section A",
                        "question_type": "single_mcq",
                        "required": 20,
                        "current": 15,
                        "remaining": 5,
                        "status": "incomplete"
                    }
                ]
            }
        }
    }
    """
    try:
        from questions.services.capacity_calculator import CapacityCalculator
        from patterns.models import ExamPattern
        
        exam_id = request.query_params.get('exam_id')
        if not exam_id:
            return Response(
                {'error': 'exam_id parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Verify pattern exists and user has access
        try:
            pattern = ExamPattern.objects.get(id=pattern_id)
        except ExamPattern.DoesNotExist:
            return Response(
                {'error': 'Pattern not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Calculate capacity
        calculator = CapacityCalculator()
        capacity_data = calculator.calculate_pattern_capacity(int(exam_id), int(pattern_id))
        
        return Response(capacity_data, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error getting pattern structure: {e}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def analyze_extraction_mismatches(request):
    """
    Analyze mismatches between extracted questions and pattern requirements
    
    POST /api/questions/analyze-mismatches/
    
    Request:
    {
        "job_id": "uuid-here",
        "exam_id": 44,
        "pattern_id": 31
    }
    
    Returns:
    {
        "mismatches": [
            {
                "subject": "physics",
                "section_id": 5,
                "question_type": "single_mcq",
                "required": 20,
                "extracted": 25,
                "status": "overflow",
                "excess": 5
            }
        ],
        "summary": {
            "total_overflow": 10,
            "total_shortage": 5
        }
    }
    """
    try:
        from questions.services.capacity_calculator import CapacityCalculator
        
        job_id = request.data.get('job_id')
        exam_id = request.data.get('exam_id')
        pattern_id = request.data.get('pattern_id')
        
        if not all([job_id, exam_id, pattern_id]):
            return Response(
                {'error': 'job_id, exam_id, and pattern_id are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get extraction job
        try:
            job = ExtractionJob.objects.get(id=job_id)
        except ExtractionJob.DoesNotExist:
            return Response(
                {'error': 'Extraction job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get extracted questions grouped by subject
        extracted_questions = ExtractedQuestion.objects.filter(job=job)
        
        # Group by subject
        subjects_data = {}
        for eq in extracted_questions:
            subject = eq.suggested_subject or eq.assigned_subject or 'ambiguous'
            if subject not in subjects_data:
                subjects_data[subject] = []
            
            subjects_data[subject].append({
                'question_type': eq.question_type,
                'subject': subject
            })
        
        grouped_data = {'subjects': subjects_data}
        
        # Analyze mismatches
        calculator = CapacityCalculator()
        mismatch_analysis = calculator.analyze_extraction_mismatches(
            int(exam_id),
            int(pattern_id),
            grouped_data
        )
        
        return Response(mismatch_analysis, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error analyzing mismatches: {e}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_extracted_questions(request, job_id):
    """
    Download extracted questions as a text file categorized by subject
    
    GET /api/questions/download-extracted/<job_id>/
    
    Returns: Text file with questions grouped by subject
    """
    from django.http import HttpResponse
    
    try:
        # Get extraction job
        try:
            job = ExtractionJob.objects.get(id=job_id)
        except ExtractionJob.DoesNotExist:
            return Response(
                {'error': 'Extraction job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check permissions
        if job.exam.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to download this file'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get extracted questions
        questions = ExtractedQuestion.objects.filter(job=job).order_by('id')
        
        if not questions.exists():
            return Response(
                {'error': 'No questions found for this job'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Group questions by subject
        questions_by_subject = {}
        for q in questions:
            subject = q.suggested_subject or q.assigned_subject or 'Uncategorized'
            if subject not in questions_by_subject:
                questions_by_subject[subject] = []
            questions_by_subject[subject].append(q)
        
        # Build text content
        content_lines = []
        content_lines.append("=" * 60)
        content_lines.append(f"EXTRACTED QUESTIONS - {job.file_name}")
        content_lines.append(f"Pattern: {job.pattern.name}")
        content_lines.append(f"Total Questions: {questions.count()}")
        content_lines.append(f"Extraction Date: {job.created_at.strftime('%Y-%m-%d %H:%M')}")
        content_lines.append("=" * 60)
        content_lines.append("")
        
        # Add questions grouped by subject
        for subject, subject_questions in sorted(questions_by_subject.items()):
            content_lines.append("")
            content_lines.append("-" * 60)
            content_lines.append(f"SUBJECT: {subject.upper()}")
            content_lines.append(f"Total Questions: {len(subject_questions)}")
            content_lines.append("-" * 60)
            content_lines.append("")
            
            for i, q in enumerate(subject_questions, 1):
                content_lines.append(f"Q.{i} {q.question_text}")
                
                # Add options if present
                if q.options:
                    for j, opt in enumerate(q.options):
                        option_letter = chr(65 + j)  # A, B, C, D...
                        content_lines.append(f"   {option_letter}) {opt}")
                
                # Add answer
                if q.correct_answer:
                    content_lines.append(f"   Answer: {q.correct_answer}")
                
                # Add solution
                if q.solution:
                    content_lines.append(f"   Solution: {q.solution}")
                
                content_lines.append("")
        
        # Create response
        content = "\n".join(content_lines)
        
        response = HttpResponse(content, content_type='text/plain; charset=utf-8')
        filename = f"extracted_questions_{job.id}.txt"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        
        return response
        
    except Exception as e:
        logger.error(f"Error downloading extracted questions: {e}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ===========================
# Document Pre-Analysis Endpoints
# ===========================

from questions.models import PreAnalysisJob
from questions.services.document_pre_analyzer import DocumentPreAnalyzer, DocumentPreAnalysisError
from questions.services.file_parser import FileParserService


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pre_analyze_document(request):
    """
    Pre-analyze uploaded document before extraction
    
    POST /api/questions/pre-analyze/
    
    Request (multipart/form-data):
    - file: Uploaded file
    - pattern_id: Pattern ID to match subjects against
    
    Returns:
    {
        "job_id": "uuid-here",
        "is_valid": true,
        "document_type": "questions_with_answers",
        "document_type_display": "Questions with Answers",
        "detected_subjects": ["Physics", "Chemistry", "Mathematics"],
        "matched_subjects": ["Physics", "Chemistry", "Mathematics"],
        "subject_question_counts": {"Physics": 25, "Chemistry": 30, "Mathematics": 20},
        "total_estimated_questions": 75,
        "confidence": 0.92,
        "message": "Document contains 75 questions across 3 subjects"
    }
    
    Or if invalid:
    {
        "job_id": "uuid-here",
        "is_valid": false,
        "document_type": "other",
        "error_message": "This document does not contain questions. Please upload a valid question bank file."
    }
    """
    try:
        # Validate request
        uploaded_file = request.FILES.get('file')
        pattern_id = request.data.get('pattern_id')
        
        if not uploaded_file:
            return Response(
                {'error': 'No file uploaded'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not pattern_id:
            return Response(
                {'error': 'pattern_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get pattern and its subjects
        from patterns.models import ExamPattern
        
        try:
            pattern = ExamPattern.objects.get(id=pattern_id)
        except ExamPattern.DoesNotExist:
            return Response(
                {'error': 'Pattern not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check permissions
        if pattern.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to use this pattern'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get pattern subjects
        pattern_subjects = list(
            pattern.sections.values_list('subject', flat=True).distinct()
        )
        
        # Save uploaded file
        upload_dir = os.path.join(settings.MEDIA_ROOT, 'pre_analysis_uploads')
        os.makedirs(upload_dir, exist_ok=True)
        
        file_path = os.path.join(upload_dir, f"{timezone.now().timestamp()}_{uploaded_file.name}")
        
        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
        
        # Create pre-analysis job
        job = PreAnalysisJob.objects.create(
            pattern=pattern,
            created_by=request.user,
            file_name=uploaded_file.name,
            file_type=uploaded_file.content_type,
            file_size=uploaded_file.size,
            file_path=file_path,
            status='processing',
        )
        
        try:
            # Parse file to get text content
            file_parser = FileParserService()
            text_content = file_parser.parse_file(file_path, uploaded_file.content_type)
            
            # Run pre-analysis
            analyzer = DocumentPreAnalyzer()
            result = analyzer.analyze_document(text_content, pattern_subjects)
            
            # Update job with results
            job.mark_completed(result)
            
            # Build response
            response_data = {
                'job_id': str(job.id),
                'is_valid': result.is_valid,
                'document_type': result.document_type,
                'document_type_display': result.document_type_display,
                'confidence': result.confidence,
                'detected_subjects': result.detected_subjects,
                'matched_subjects': result.matched_subjects,
                'unmatched_subjects': result.unmatched_subjects,
                'subject_question_counts': result.subject_question_counts,
                'total_estimated_questions': result.total_estimated_questions,
                'document_structure': result.document_structure,
            }
            
            if result.is_valid:
                response_data['message'] = (
                    f"Document categorized into {len(result.matched_subjects)} subjects"
                )
                if result.document_structure:
                    response_data['message'] += f" with {result.document_structure.get('total_sections', 0)} section(s) detected"
            else:
                response_data['error_message'] = result.error_message
                response_data['reason'] = result.reason
            
            logger.info(
                f"Pre-analysis completed for {uploaded_file.name}: "
                f"valid={result.is_valid}, type={result.document_type}"
            )
            
            return Response(response_data, status=status.HTTP_200_OK)
            
        except DocumentPreAnalysisError as e:
            job.mark_failed(str(e))
            return Response(
                {'error': str(e), 'job_id': str(job.id)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            job.mark_failed(str(e))
            raise
            
    except Exception as e:
        logger.error(f"Pre-analysis failed: {str(e)}", exc_info=True)
        return Response(
            {'error': f'Failed to analyze document: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_pre_analysis_subjects(request, job_id):
    """
    Get subject-separated content for a pre-analysis job
    
    GET /api/questions/pre-analyze/{job_id}/subjects/
    
    Returns:
    {
        "job_id": "uuid-here",
        "subjects": [
            {
                "subject": "Physics",
                "question_count": 25,
                "content_preview": "Q1. What is velocity?...",
                "download_url": "/api/questions/pre-analyze/{job_id}/subjects/physics/download/"
            },
            ...
        ]
    }
    """
    try:
        # Get pre-analysis job
        try:
            job = PreAnalysisJob.objects.get(id=job_id)
        except PreAnalysisJob.DoesNotExist:
            return Response(
                {'error': 'Pre-analysis job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check permissions
        if job.pattern.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to view this job'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Check job status
        if job.status != 'completed':
            return Response(
                {'error': f'Pre-analysis job is {job.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not job.is_valid_document:
            return Response(
                {'error': 'Document is not valid for extraction'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Build subjects response
        subjects = []
        for subject, data in job.subject_separated_content.items():
            # Handle both new format (dict) and old format (string) for backward compatibility
            if isinstance(data, dict):
                content = data.get('content', '')
                instructions = data.get('instructions', '')
            else:
                content = str(data) if data else ''
                instructions = ''
            
            subjects.append({
                'subject': subject,
                'question_count': job.subject_question_counts.get(subject, 0),
                'content_preview': content[:500] + '...' if len(content) > 500 else content,
                'full_content_length': len(content),
                'has_instructions': bool(instructions),
                'instructions_preview': instructions[:200] + '...' if len(instructions) > 200 else instructions,
                'download_url': f'/api/questions/pre-analyze/{job_id}/subjects/{subject.lower()}/download/'
            })
        
        return Response({
            'job_id': str(job.id),
            'document_type': job.document_type,
            'total_questions': job.total_estimated_questions,
            'subjects': subjects
        })
        
    except Exception as e:
        logger.error(f"Failed to get pre-analysis subjects: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_subject_content(request, job_id, subject):
    """
    Download subject-separated content as text file
    
    GET /api/questions/pre-analyze/{job_id}/subjects/{subject}/download/
    
    Returns: Text file with subject's questions
    """
    from django.http import HttpResponse
    
    try:
        # Get pre-analysis job
        try:
            job = PreAnalysisJob.objects.get(id=job_id)
        except PreAnalysisJob.DoesNotExist:
            return Response(
                {'error': 'Pre-analysis job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check permissions
        if job.pattern.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to download this file'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Find subject (case-insensitive)
        subject_data = None
        subject_name = None
        
        for s, data in job.subject_separated_content.items():
            if s.lower() == subject.lower():
                subject_data = data
                subject_name = s
                break
        
        if subject_data is None:
            return Response(
                {'error': f'Subject "{subject}" not found in this document'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Handle both new format (dict) and old format (string) for backward compatibility
        if isinstance(subject_data, dict):
            subject_content = subject_data.get('content', '')
            subject_instructions = subject_data.get('instructions', '')
        else:
            subject_content = str(subject_data) if subject_data else ''
            subject_instructions = ''
        
        # Build file content - include instructions at the top if available, then raw content
        if subject_instructions:
            content = f"INSTRUCTIONS:\n{subject_instructions}\n\n{'='*60}\n\n{subject_content}"
        else:
            content = subject_content
        
        # Create response
        response = HttpResponse(content, content_type='text/plain; charset=utf-8')
        filename = f"{subject_name}_Questions.txt"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        
        return response
        
    except Exception as e:
        logger.error(f"Failed to download subject content: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def confirm_pre_analysis(request, job_id):
    """
    Confirm pre-analysis and proceed to extraction
    
    POST /api/questions/pre-analyze/{job_id}/confirm/
    
    Request:
    {
        "confirmed_subjects": ["Physics", "Chemistry", "Mathematics"],
        "exam_id": 44,
        "proceed_to_extraction": true
    }
    
    Returns:
    {
        "success": true,
        "message": "Subject separation confirmed. Starting extraction...",
        "extraction_job_id": "new-extraction-uuid"
    }
    """
    try:
        # Get pre-analysis job
        try:
            job = PreAnalysisJob.objects.get(id=job_id)
        except PreAnalysisJob.DoesNotExist:
            return Response(
                {'error': 'Pre-analysis job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check permissions
        if job.pattern.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to confirm this job'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Validate request
        confirmed_subjects = request.data.get('confirmed_subjects', [])
        exam_id = request.data.get('exam_id')
        proceed = request.data.get('proceed_to_extraction', True)
        
        if not exam_id:
            return Response(
                {'error': 'exam_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get exam
        from exams.models import Exam
        
        try:
            exam = Exam.objects.get(id=exam_id)
        except Exam.DoesNotExist:
            return Response(
                {'error': 'Exam not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Create extraction job with direct link to pre-analysis
        # We create this ALWAYS so we have an ID to attach questions to (even for manual subject-wise extraction)
        extraction_job = ExtractionJob.objects.create(
            exam=exam,
            pattern=job.pattern,
            created_by=request.user,
            file_name=job.file_name,
            file_type=job.file_type,
            file_size=job.file_size,
            file_path=job.file_path,
            status='pending',
            pre_analysis_job=job,  # Direct link for subject-separated extraction
        )
        
        # Also set the reverse link for backwards compatibility
        job.extraction_job = extraction_job
        job.save(update_fields=['extraction_job'])
        
        logger.info(
            f"ExtractionJob {extraction_job.id} linked to PreAnalysisJob {job.id} "
            f"with {len(job.subject_separated_content)} subjects"
        )

        if not proceed:
            return Response({
                'success': True,
                'message': 'Pre-analysis confirmed. Job created but extraction not started (manual mode).',
                'extraction_job_id': str(extraction_job.id),
                'pre_analysis_job_id': str(job.id)
            }, status=status.HTTP_201_CREATED)
        
        # Trigger extraction task if proceed is True
        try:
            if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
                extract_questions_task(str(extraction_job.id), use_v2=True)
            else:
                extract_questions_task.delay(str(extraction_job.id), use_v2=True)
        except Exception as task_error:
            logger.warning(f"Celery task failed, running synchronously: {task_error}")
            from questions.services.extraction_pipeline_v2 import ExtractionPipelineV2
            pipeline = ExtractionPipelineV2()
            pipeline.process_file(extraction_job.id)
        
        logger.info(
            f"Created extraction job {extraction_job.id} from pre-analysis {job.id}"
        )
        
        return Response({
            'success': True,
            'message': 'Subject separation confirmed. Starting extraction...',
            'extraction_job_id': str(extraction_job.id),
            'pre_analysis_job_id': str(job.id)
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Failed to confirm pre-analysis: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_import_preview(request, job_id):
    """
    Get import preview showing extraction summary and pattern capacity
    
    GET /api/questions/import-preview/{job_id}/
    
    Returns:
    {
        "extraction_summary": {
            "total_extracted": 300,
            "by_subject": {"Physics": 100, "Chemistry": 100, "Mathematics": 100},
            "by_type": {"single_mcq": 270, "numerical": 30}
        },
        "pattern_capacity": {
            "total_required": 90,
            "total_filled": 0,
            "total_remaining": 90,
            "subjects": {...}
        },
        "import_plan": {
            "will_import": 90,
            "will_skip": 210,
            "warnings": [...],
            "recommendations": [...]
        },
        "can_proceed": true
    }
    """
    try:
        from questions.services.capacity_calculator import CapacityCalculator
        from django.db.models import Count
        
        # Get extraction job
        try:
            job = ExtractionJob.objects.get(id=job_id)
        except ExtractionJob.DoesNotExist:
            return Response(
                {'error': 'Extraction job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check permissions
        if job.exam.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to view this job'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Check job status
        if job.status not in ['completed', 'partial']:
            return Response(
                {
                    'error': f'Extraction job is {job.status}',
                    'message': 'Please wait for extraction to complete',
                    'can_proceed': False
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get extracted questions
        extracted_questions = ExtractedQuestion.objects.filter(job=job)
        
        if not extracted_questions.exists():
            return Response(
                {
                    'error': 'No questions extracted',
                    'can_proceed': False
                },
                status=status.HTTP_404_NOT_FOUND
            )
        
        # 1. EXTRACTION SUMMARY
        total_extracted = extracted_questions.count()
        
        # Group by subject
        by_subject = {}
        for q in extracted_questions:
            subject = q.suggested_subject or q.assigned_subject or 'Uncategorized'
            by_subject[subject] = by_subject.get(subject, 0) + 1
        
        # Group by type
        by_type = {}
        for q in extracted_questions:
            q_type = q.question_type or 'unknown'
            by_type[q_type] = by_type.get(q_type, 0) + 1
        
        extraction_summary = {
            'total_extracted': total_extracted,
            'by_subject': by_subject,
            'by_type': by_type,
            'job_status': job.status,
            'extraction_completeness': (job.questions_extracted / job.total_questions_found * 100) if job.total_questions_found > 0 else 0
        }
        
        # 2. PATTERN CAPACITY
        calculator = CapacityCalculator()
        pattern_capacity = calculator.calculate_pattern_capacity(
            job.exam.id,
            job.pattern.id
        )
        
        # 3. IMPORT PLAN
        total_required = pattern_capacity['total_required']
        total_filled = pattern_capacity['total_filled']
        total_remaining = pattern_capacity['total_remaining']
        
        will_import = min(total_extracted, total_remaining)
        will_skip = max(0, total_extracted - total_remaining)
        overflow = max(0, total_extracted - total_required)
        
        warnings = []
        recommendations = []
        
        # Generate warnings
        if total_extracted > total_required:
            warnings.append(
                f"Pattern requires {total_required} questions but {total_extracted} were extracted"
            )
            warnings.append(
                f"{will_skip} questions will not be imported (select which ones to import)"
            )
        elif total_extracted < total_required:
            warnings.append(
                f"Pattern requires {total_required} questions but only {total_extracted} were extracted"
            )
            warnings.append(
                f"{total_required - total_extracted} questions are still needed"
            )
        
        if total_filled > 0:
            warnings.append(
                f"Exam already has {total_filled} questions. Only {total_remaining} slots remaining"
            )
        
        # Generate recommendations
        if total_extracted > total_required:
            recommendations.append("Review and select the best questions to import")
            recommendations.append("Consider quality over quantity")
            
            # Subject-specific recommendations
            for subject, count in by_subject.items():
                if subject in pattern_capacity['subjects']:
                    subject_required = pattern_capacity['subjects'][subject]['total_required']
                    if count > subject_required:
                        recommendations.append(
                            f"Select best {subject_required} out of {count} {subject} questions"
                        )
        elif total_extracted < total_required:
            recommendations.append("You may need to upload more questions")
            recommendations.append("Or adjust the pattern requirements")
        else:
            recommendations.append("Perfect match! All questions can be imported")
        
        import_plan = {
            'will_import': will_import,
            'will_skip': will_skip,
            'overflow': overflow,
            'warnings': warnings,
            'recommendations': recommendations,
            'import_strategy': 'select_best' if total_extracted > total_required else 'import_all'
        }
        
        # 4. CAN PROCEED?
        can_proceed = total_extracted > 0 and total_remaining > 0
        
        # 5. BUILD RESPONSE
        response_data = {
            'job_id': str(job.id),
            'extraction_summary': extraction_summary,
            'pattern_capacity': pattern_capacity,
            'import_plan': import_plan,
            'can_proceed': can_proceed,
            'message': 'Ready to import. Please review and confirm.' if can_proceed else 'Cannot proceed with import'
        }
        
        logger.info(
            f"Import preview for job {job_id}: "
            f"{total_extracted} extracted, {will_import} will import, {will_skip} will skip"
        )
        
        return Response(response_data, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Failed to generate import preview: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ===========================
# Section-Based Extraction & Import Confirmation Endpoints
# ===========================


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def extract_questions_by_section(request):
    """
    Extract questions for a specific subject using SectionQuestionExtractor.
    Saves questions to the ExtractionJob so they appear in review.
    
    POST /api/questions/extract-by-section/
    """
    try:
        extraction_job_id = request.data.get('extraction_job_id')
        pre_analysis_job_id = request.data.get('pre_analysis_job_id')
        subject = request.data.get('subject')
        exam_id = request.data.get('exam_id')
        
        if not subject or (not extraction_job_id and not pre_analysis_job_id):
            return Response(
                {'error': 'subject and either extraction_job_id or pre_analysis_job_id are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        job = None
        if extraction_job_id:
            # Get Extraction Job
            try:
                job = ExtractionJob.objects.get(id=extraction_job_id)
            except ExtractionJob.DoesNotExist:
                return Response({'error': 'Extraction job not found'}, status=status.HTTP_404_NOT_FOUND)
        else:
            # Try to get or create extraction job from pre-analysis
            from questions.models import PreAnalysisJob
            try:
                pre_job = PreAnalysisJob.objects.get(id=pre_analysis_job_id)
                if pre_job.extraction_job:
                    job = pre_job.extraction_job
                else:
                    if not exam_id:
                         return Response({'error': 'exam_id is required when providing pre_analysis_job_id'}, status=status.HTTP_400_BAD_REQUEST)
                    
                    from exams.models import Exam
                    exam = Exam.objects.get(id=exam_id)
                    
                    # Create job
                    job = ExtractionJob.objects.create(
                        exam=exam,
                        pattern=pre_job.pattern,
                        created_by=request.user,
                        file_name=pre_job.file_name,
                        file_type=pre_job.file_type,
                        file_size=pre_job.file_size,
                        file_path=pre_job.file_path,
                        status='pending',
                        pre_analysis_job=pre_job
                    )
                    pre_job.extraction_job = job
                    pre_job.save(update_fields=['extraction_job'])
            except PreAnalysisJob.DoesNotExist:
                return Response({'error': 'Pre-analysis job not found'}, status=status.HTTP_404_NOT_FOUND)
            except Exception as e:
                return Response({'error': f'Failed to link/create extraction job: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Check permissions
        if job.exam.institute != request.user.institute:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
            
        # Get separated content and document structure from pre-analysis
        separated_content = None
        document_structure = {}
        expected_count = 0
        
        if job.pre_analysis_job:
            # Get separated content
            if job.pre_analysis_job.subject_separated_content:
                raw_content = job.pre_analysis_job.subject_separated_content
                separated_content = {}
                
                # Normalize content format
                for s, data in raw_content.items():
                    if isinstance(data, dict):
                        separated_content[s] = data.get('content', '')
                    else:
                        separated_content[s] = str(data)
            
            # Get document structure and expected count
            document_structure = job.pre_analysis_job.document_structure or {}
            subject_counts = job.pre_analysis_job.subject_question_counts or {}
            expected_count = subject_counts.get(subject, 0)
        
        # Get subject content
        subject_text = ""
        if separated_content:
            # Use fuzzy matching for subject keys
            normalized_subj = subject.lower().strip()
            found_key = None
            for key in separated_content.keys():
                if normalized_subj == key.lower().strip():
                    found_key = key
                    break
            
            if found_key:
                content_data = separated_content[found_key]
                if isinstance(content_data, dict):
                    subject_text = content_data.get('content', '')
                else:
                    subject_text = str(content_data)
                logger.info(f"Using separated content for {subject} (match: {found_key}, length: {len(subject_text)})")
            else:
                logger.warning(f"Subject '{subject}' not found in separated content keys: {list(separated_content.keys())}")
        
        if not subject_text:
            # Fallback to full file processing
            logger.info(f"No separated content found for {subject}, processing full file")
            from questions.services.agent_extraction_service import AgentExtractionService
            service = AgentExtractionService(
                gemini_key=getattr(settings, 'GEMINI_API_KEY', ''),
                mathpix_id=getattr(settings, 'MATHPIX_APP_ID', ''),
                mathpix_key=getattr(settings, 'MATHPIX_APP_KEY', '')
            )
            exam_mode = getattr(job.pattern, 'exam_mode', None) if job.pattern else None
            all_questions = service.run_full_pipeline(
                job.file_path, 
                subjects_to_process=[subject],
                separated_content=separated_content,
                exam_mode=exam_mode
            )
        else:
            # Use SectionQuestionExtractor for better section-based extraction
            logger.info(f"Using SectionQuestionExtractor for {subject}")
            extractor = SectionQuestionExtractor(
                api_key=getattr(settings, 'GEMINI_API_KEY', '')
            )
            
            # Extract questions using section-based logic
            extraction_result = extractor.extract_questions_by_sections(
                text_content=subject_text,
                document_structure=document_structure,
                subject=subject,
                expected_question_count=expected_count
            )
            
            # Convert SectionQuestionResult objects to question dictionaries
            all_questions = []
            for section_result in extraction_result.get('sections', []):
                for question in section_result.questions:
                    # Ensure question has required fields
                    question_dict = {
                        'question_text': question.get('question_text', ''),
                        'question_type': question.get('question_type', 'single_mcq'),
                        'options': question.get('options', []),
                        'correct_answer': question.get('correct_answer', ''),
                        'solution': question.get('solution', ''),
                        'subject': subject,
                        'question_number': question.get('question_number', 0)
                    }
                    all_questions.append(question_dict)
        
        # 1. Initialize Agent Service
        from questions.services.agent_extraction_service import AgentExtractionService
        service = AgentExtractionService(
            gemini_key=getattr(settings, 'GEMINI_API_KEY', ''),
            mathpix_id=getattr(settings, 'MATHPIX_APP_ID', ''),
            mathpix_key=getattr(settings, 'MATHPIX_APP_KEY', '')
        )
        
        # 2. Run Extraction for the Subject
        # Pass separated_content so it doesn't re-OCR
        # Pass exam_mode so post-processing (e.g. image conversion) is mode-aware
        exam_mode = getattr(job.pattern, 'exam_mode', None) if job.pattern else None
        all_questions = service.run_full_pipeline(
            job.file_path,
            subjects_to_process=[subject],
            separated_content=separated_content,
            exam_mode=exam_mode
        )
        
        # 3. Save Questions to DB
        saved_questions = []
        for q_data in all_questions:
            eq = ExtractedQuestion.objects.create(
                job=job,
                question_text=q_data.get('question_text', q_data.get('question', '')),
                question_type=q_data.get('question_type', 'single_mcq'),
                options=q_data.get('options', []),
                correct_answer=q_data.get('correct_answer', q_data.get('answer', '')),
                solution=q_data.get('explanation', q_data.get('solution', '')),
                suggested_subject=subject,  # Explicitly set subject
                structure=q_data.get('subparts', {}),
                confidence_score=0.95
            )
            saved_questions.append(eq)
        
        # Update job status if needed
        if job.status == 'pending':
            job.status = 'processing'
            job.save(update_fields=['status'])
        
        job.questions_extracted += len(saved_questions)
        job.save(update_fields=['questions_extracted'])
        
        # Format response for frontend (sections grouped)
        from questions.extraction_serializers import ExtractedQuestionSerializer
        
        # Group questions by type for better organization
        questions_by_type = {}
        for q in saved_questions:
            q_type = q.question_type
            if q_type not in questions_by_type:
                questions_by_type[q_type] = []
            questions_by_type[q_type].append(q)
        
        sections_response = []
        for q_type, questions in questions_by_type.items():
            questions_serialized = ExtractedQuestionSerializer(questions, many=True).data
            sections_response.append({
                'section_name': f"{subject} - {q_type.upper()}",
                'section_type': q_type,
                'questions': questions_serialized,
                'total_extracted': len(questions),
                'expected_count': len(questions),
                'extraction_confidence': 0.95,
                'warnings': []
            })
        
        return Response({
            'success': True,
            'subject': subject,
            'job_id': str(job.id),
            'total_extracted': len(saved_questions),
            'sections': sections_response,
            'message': f"Extracted and saved {len(saved_questions)} questions for {subject}"
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Section-based extraction failed: {e}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def get_section_import_preview(request):
    """
    Get import preview showing how extracted questions map to pattern sections.
    Shows remaining capacity and what will be imported.
    
    POST /api/questions/section-import-preview/
    
    Request:
    {
        "exam_id": 44,
        "pattern_id": 31,
        "subject": "Physics",
        "extracted_sections": [
            {
                "section_name": "Section A",
                "section_type": "single_mcq",
                "questions": [...],
                "total_extracted": 20
            }
        ]
    }
    
    Returns:
    {
        "preview": {
            "exam_id": 44,
            "pattern_id": 31,
            "subject": "Physics",
            "total_extracted": 60,
            "total_will_import": 50,
            "total_overflow": 10,
            "total_remaining_after_import": 5,
            "section_mappings": [
                {
                    "pattern_section_id": 5,
                    "pattern_section_name": "Section A",
                    "question_type": "single_mcq",
                    "required_count": 20,
                    "current_count": 5,
                    "remaining_capacity": 15,
                    "extracted_count": 20,
                    "will_import_count": 15,
                    "overflow_count": 5,
                    "status": "overflow"
                }
            ]
        },
        "confirmation_message": "Found 60 questions...",
        "options": [...]
    }
    """
    try:
        exam_id = request.data.get('exam_id')
        pattern_id = request.data.get('pattern_id')
        subject = request.data.get('subject')
        extracted_sections = request.data.get('extracted_sections', [])
        import_target = request.data.get('import_target')  # New: import target selection
        
        if not all([exam_id, pattern_id, subject]):
            return Response(
                {'error': 'exam_id, pattern_id, and subject are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Verify exam access
        from exams.models import Exam
        try:
            exam = Exam.objects.get(id=exam_id)
            if exam.institute != request.user.institute:
                return Response(
                    {'error': 'You do not have permission to access this exam'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Exam.DoesNotExist:
            return Response(
                {'error': 'Exam not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get confirmation data with import target
        flow = ImportConfirmationFlow()
        confirmation_data = flow.get_confirmation_data(
            exam_id, pattern_id, subject, extracted_sections, import_target
        )
        
        return Response(confirmation_data, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Failed to get import preview: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_section_capacity(request, pattern_id, subject):
    """
    Get current section capacity for a subject.
    Shows what's filled and what's remaining.
    
    GET /api/questions/section-capacity/{pattern_id}/{subject}/?exam_id=44
    
    Returns:
    {
        "subject": "Physics",
        "sections": [
            {
                "section_id": 5,
                "section_name": "Section A",
                "question_type": "single_mcq",
                "required": 20,
                "current": 15,
                "remaining": 5,
                "status": "incomplete",
                "completion_percent": 75.0
            }
        ],
        "summary": {
            "total_required": 60,
            "total_filled": 45,
            "total_remaining": 15,
            "completion_percent": 75.0
        }
    }
    """
    try:
        exam_id = request.query_params.get('exam_id')
        
        if not exam_id:
            return Response(
                {'error': 'exam_id query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Verify exam access
        from exams.models import Exam
        try:
            exam = Exam.objects.get(id=exam_id)
            if exam.institute != request.user.institute:
                return Response(
                    {'error': 'You do not have permission to access this exam'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Exam.DoesNotExist:
            return Response(
                {'error': 'Exam not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get section details
        flow = ImportConfirmationFlow()
        section_details = flow.get_section_details(int(exam_id), int(pattern_id), subject)
        
        return Response(section_details, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Failed to get section capacity: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def confirm_section_import(request):
    """
    Confirm and execute import for a subject's questions.
    
    POST /api/questions/confirm-section-import/
    
    Request:
    {
        "exam_id": 44,
        "pattern_id": 31,
        "subject": "Physics",
        "action": "import_all",  // or "import_selected"
        "selected_question_ids": [1, 2, 3],  // Only for "import_selected"
        "extracted_sections": [...]
    }
    
    Returns:
    {
        "success": true,
        "imported_count": 50,
        "skipped_count": 10,
        "subject": "Physics",
        "message": "Successfully imported 50 questions for Physics"
    }
    """
    try:
        exam_id = request.data.get('exam_id')
        pattern_id = request.data.get('pattern_id')
        subject = request.data.get('subject')
        action = request.data.get('action', 'import_all')
        selected_ids = request.data.get('selected_question_ids', [])
        extracted_sections = request.data.get('extracted_sections', [])
        import_target = request.data.get('import_target')  # New: import target selection
        
        if not all([exam_id, pattern_id, subject]):
            return Response(
                {'error': 'exam_id, pattern_id, and subject are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if action == 'skip':
            return Response({
                'success': True,
                'imported_count': 0,
                'skipped_count': sum(s.get('total_extracted', 0) for s in extracted_sections),
                'subject': subject,
                'message': f'Skipped import for {subject}'
            }, status=status.HTTP_200_OK)
        
        # Verify exam access
        from exams.models import Exam
        try:
            exam = Exam.objects.get(id=exam_id)
            if exam.institute != request.user.institute:
                return Response(
                    {'error': 'You do not have permission to access this exam'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Exam.DoesNotExist:
            return Response(
                {'error': 'Exam not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get import preview with import target
        mapper = SectionMapper()
        preview = mapper.map_questions_to_sections(
            exam_id, pattern_id, subject, extracted_sections, import_target
        )
        
        # Prepare mappings with import target
        if action == 'import_selected' and selected_ids:
            mappings = mapper.prepare_import_mappings(preview, selected_ids, import_target)
        else:
            mappings = mapper.prepare_import_mappings(preview, None, import_target)
        
        if not mappings:
            return Response({
                'success': True,
                'imported_count': 0,
                'skipped_count': preview.total_extracted,
                'subject': subject,
                'message': 'No questions to import'
            }, status=status.HTTP_200_OK)
        
        # Import questions
        imported_count = 0
        failed_count = 0
        
        from questions.models import Question
        from patterns.models import PatternSection
        from django.db import transaction
        
        # Get next question number
        last_question = Question.objects.filter(
            exam_id=exam_id,
            is_active=True
        ).order_by('-question_number').first()
        next_number = (last_question.question_number + 1) if last_question else 1
        
        failed_errors = []
        
        logger.info(f"Starting import of {len(mappings)} questions for {subject}")
        
        for idx, mapping in enumerate(mappings):
            try:
                with transaction.atomic():
                    q_data = mapping['question_data']
                    section_id = mapping['section_id']
                    
                    logger.debug(f"Processing question {idx+1}: keys={list(q_data.keys())}")
                    
                    # Get section info
                    section = None
                    section_name = ''
                    marks = 1
                    negative_marks = 0.25
                    
                    if section_id:
                        try:
                            section = PatternSection.objects.get(id=section_id)
                            section_name = section.name
                            marks = section.marks_per_question
                            negative_marks = float(section.negative_marking)
                        except PatternSection.DoesNotExist:
                            logger.warning(f"Pattern section {section_id} not found")
                    
                    # Validate required fields
                    question_text = q_data.get('question_text', '')
                    if question_text is None:
                        question_text = ''
                    question_text = str(question_text).strip()
                    
                    if not question_text:
                        # Try to get text from other fields
                        question_text = q_data.get('text', '') or q_data.get('question', '') or ''
                        question_text = str(question_text).strip()
                    
                    if not question_text:
                        raise ValueError(f"Question text is empty. Data: {list(q_data.keys())}")
                    
                    # Get correct_answer - handle different formats
                    correct_answer = q_data.get('correct_answer', '') or q_data.get('answer', '') or ''
                    if correct_answer is None:
                        correct_answer = ''
                    if isinstance(correct_answer, list):
                        correct_answer = ', '.join(str(a) for a in correct_answer if a)
                    else:
                        correct_answer = str(correct_answer).strip()
                    
                    # Get options - ensure it's a list
                    options = q_data.get('options', [])
                    if not isinstance(options, list):
                        options = []
                    
                    # Validate question_type
                    question_type = q_data.get('question_type', 'single_mcq')
                    valid_types = ['single_mcq', 'multiple_mcq', 'numerical', 'subjective', 'true_false', 'fill_blank']
                    if question_type not in valid_types:
                        logger.warning(f"Invalid question_type '{question_type}', defaulting to 'single_mcq'")
                        question_type = 'single_mcq'
                    
                    # Calculate question_number based on section's question range
                    # question_number should be within section.start_question to section.end_question
                    # question_number_in_pattern is the subject-local number (same as question_number for this pattern)
                    question_number_in_pattern = None
                    actual_question_number = next_number  # fallback
                    
                    if section:
                        # Get count of existing questions in this section for this exam
                        existing_in_section = Question.objects.filter(
                            exam=exam,
                            pattern_section_id=section_id,
                            is_active=True
                        ).count()
                        
                        # question_number should be section.start_question + offset
                        actual_question_number = section.start_question + existing_in_section
                        
                        # question_number_in_pattern should match question_number (subject-local number)
                        # This is used by the frontend to identify which question slot is filled
                        question_number_in_pattern = actual_question_number
                    
                    # Create question
                    Question.objects.create(
                        exam=exam,
                        question_text=question_text,
                        question_type=question_type,
                        difficulty=q_data.get('difficulty', 'medium'),
                        options=options,
                        correct_answer=correct_answer,
                        solution=str(q_data.get('solution', '') or ''),
                        explanation=str(q_data.get('explanation', '') or ''),
                        marks=marks,
                        negative_marks=negative_marks,
                        subject=subject,
                        question_number=actual_question_number,
                        question_number_in_pattern=question_number_in_pattern,
                        pattern_section_id=section_id,
                        pattern_section_name=section_name,
                        institute=exam.institute,
                        created_by=request.user,
                        is_active=True
                    )
                    
                    imported_count += 1
                    next_number += 1
                    
            except Exception as e:
                error_msg = f"Q{q_data.get('question_number', '?')}: {str(e)}"
                logger.error(f"Failed to import question: {error_msg}", exc_info=True)
                failed_errors.append(error_msg)
                failed_count += 1
        
        # Log all errors for debugging
        if failed_errors:
            logger.error(f"Import failures for {subject}: {failed_errors[:5]}")
        
        skipped_count = preview.total_extracted - imported_count - failed_count
        
        response_data = {
            'success': imported_count > 0 or failed_count == 0,
            'imported_count': imported_count,
            'failed_count': failed_count,
            'skipped_count': skipped_count,
            'subject': subject,
            'message': f'Successfully imported {imported_count} questions for {subject}'
        }
        
        # Include error details if there were failures
        if failed_errors:
            response_data['errors'] = failed_errors[:10]  # First 10 errors
            response_data['message'] += f' ({failed_count} failed)'
        
        return Response(response_data, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Failed to confirm import: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def full_extraction_flow(request):
    """
    Complete extraction flow: Pre-analyze → Extract by sections → Map to pattern → Preview
    
    POST /api/questions/full-extraction-flow/
    
    Request:
    {
        "pre_analysis_job_id": "uuid-here",
        "exam_id": 44
    }
    
    Returns:
    {
        "success": true,
        "subjects": {
            "Physics": {
                "extracted_sections": [...],
                "import_preview": {...},
                "confirmation_data": {...}
            }
        },
        "summary": {
            "total_subjects": 3,
            "total_extracted": 180,
            "total_will_import": 150,
            "total_overflow": 30
        },
        "next_step": "confirm_import"
    }
    """
    try:
        pre_analysis_job_id = request.data.get('pre_analysis_job_id')
        exam_id = request.data.get('exam_id')
        
        if not pre_analysis_job_id or not exam_id:
            return Response(
                {'error': 'pre_analysis_job_id and exam_id are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get pre-analysis job
        try:
            job = PreAnalysisJob.objects.get(id=pre_analysis_job_id)
        except PreAnalysisJob.DoesNotExist:
            return Response(
                {'error': 'Pre-analysis job not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check permissions
        if job.pattern.institute != request.user.institute:
            return Response(
                {'error': 'You do not have permission to access this job'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Verify exam
        from exams.models import Exam
        try:
            exam = Exam.objects.get(id=exam_id)
            if exam.institute != request.user.institute:
                return Response(
                    {'error': 'You do not have permission to access this exam'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Exam.DoesNotExist:
            return Response(
                {'error': 'Exam not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Process each subject
        extractor = SectionQuestionExtractor()
        flow = ImportConfirmationFlow()
        
        subjects_data = {}
        total_extracted = 0
        total_will_import = 0
        total_overflow = 0
        
        for subject in job.matched_subjects:
            # Get subject content (handle new format with instructions)
            subject_data = job.subject_separated_content.get(subject, {})
            if isinstance(subject_data, dict):
                subject_content = subject_data.get('content', '')
                subject_instructions = subject_data.get('instructions', '')
            else:
                # Backward compatibility: old format (just string)
                subject_content = str(subject_data) if subject_data else ''
                subject_instructions = ''
            
            if not subject_content:
                continue
            
            # Get document structure from pre-analysis job if available
            document_structure = {'sections': []}
            
            if job.document_structure and job.document_structure.get('sections'):
                # Use the actual detected document structure
                document_structure = job.document_structure
                logger.info(f"Using detected document structure for {subject}: {len(document_structure['sections'])} sections")
            else:
                # Fallback to basic structure but try to detect question types
                logger.info(f"No document structure found, using fallback for {subject}")
                
                # Try to detect if this subject has different question types
                subject_content_lower = subject_content.lower()
                has_mcq = bool(re.search(r'\([abcd]\)', subject_content_lower) or 
                              re.search(r'\(a\).*\(b\).*\(c\).*\(d\)', subject_content_lower))
                has_numerical = bool(re.search(r'calculate|find|determine|answer.*\d+', subject_content_lower))
                has_subjective = bool(re.search(r'explain|describe|discuss|derive|prove', subject_content_lower))
                
                # Create sections based on detected types
                sections = []
                question_count = job.subject_question_counts.get(subject, 20)
                
                if has_mcq and has_numerical:
                    # Mixed document - create separate sections
                    mcq_count = int(question_count * 0.6)  # Assume 60% MCQ
                    num_count = question_count - mcq_count
                    
                    sections.append({
                        'name': f'{subject} - MCQ Section',
                        'type_hint': 'single_mcq',
                        'question_range': f'1-{mcq_count}',
                        'question_count': mcq_count,
                        'format_description': 'Multiple choice questions'
                    })
                    
                    sections.append({
                        'name': f'{subject} - Numerical Section',
                        'type_hint': 'numerical',
                        'question_range': f'{mcq_count + 1}-{question_count}',
                        'question_count': num_count,
                        'format_description': 'Numerical questions'
                    })
                elif has_subjective:
                    sections.append({
                        'name': f'{subject} - Subjective Section',
                        'type_hint': 'subjective',
                        'question_range': f'1-{question_count}',
                        'question_count': question_count,
                        'format_description': 'Subjective questions'
                    })
                elif has_numerical:
                    sections.append({
                        'name': f'{subject} - Numerical Section',
                        'type_hint': 'numerical',
                        'question_range': f'1-{question_count}',
                        'question_count': question_count,
                        'format_description': 'Numerical questions'
                    })
                else:
                    # Default to MCQ
                    sections.append({
                        'name': f'{subject} - General Section',
                        'type_hint': 'single_mcq',
                        'question_range': f'1-{question_count}',
                        'question_count': question_count,
                        'format_description': 'Mixed questions'
                    })
                
                document_structure = {'sections': sections}
            
            # Extract questions
            extraction_result = extractor.extract_questions_by_sections(
                subject_content,
                document_structure,
                subject
            )
            
            # Convert to serializable format
            extracted_sections = []
            for section_result in extraction_result['sections']:
                extracted_sections.append({
                    'section_name': section_result.section_name,
                    'section_type': section_result.section_type,
                    'questions': section_result.questions,
                    'total_extracted': section_result.total_extracted,
                    'expected_count': section_result.expected_count,
                    'extraction_confidence': section_result.extraction_confidence,
                    'warnings': section_result.warnings
                })
            
            # Get confirmation data
            confirmation_data = flow.get_confirmation_data(
                exam_id, job.pattern.id, subject, extracted_sections
            )
            
            subjects_data[subject] = {
                'extracted_sections': extracted_sections,
                'total_extracted': extraction_result['total_extracted'],
                'confirmation_data': confirmation_data
            }
            
            total_extracted += extraction_result['total_extracted']
            total_will_import += confirmation_data['preview']['total_will_import']
            total_overflow += confirmation_data['preview']['total_overflow']
        
        return Response({
            'success': True,
            'pre_analysis_job_id': str(job.id),
            'exam_id': exam_id,
            'pattern_id': job.pattern.id,
            'subjects': subjects_data,
            'summary': {
                'total_subjects': len(subjects_data),
                'total_extracted': total_extracted,
                'total_will_import': total_will_import,
                'total_overflow': total_overflow,
                'total_remaining_after_import': total_extracted - total_will_import - total_overflow
            },
            'next_step': 'confirm_import',
            'message': f'Extracted {total_extracted} questions from {len(subjects_data)} subjects. Ready for import confirmation.'
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Full extraction flow failed: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )



# ===========================
# Parse Nested Question Structure
# ===========================

def parse_nested_question_structure(extracted_text):
    """
    Parse extracted text to identify nested question structures.
    
    Identifies patterns like:
    - Main parts: (a), (b), (c) or a), b), c)
    - Sub-parts: (i), (ii), (iii) or i), ii), iii)
    - Internal choice: OR or (OR)
    - MCQ options: (A), (B), (C), (D) or A), B), C), D) or (1), (2), (3), (4)
    
    Returns a structure dict compatible with frontend expectations.
    """
    import re
    
    if not extracted_text or not extracted_text.strip():
        return {
            'question_text': '',
            'options': [],
            'is_nested': False,
            'structure': None
        }
    
    text = extracted_text.strip()
    
    # First, detect if this is a nested/structured question or simple MCQ
    
    # Check for internal choice pattern (OR between parts)
    has_or_separator = bool(re.search(r'\n\s*OR\s*\n|\n\s*\(OR\)\s*\n', text, re.IGNORECASE))
    
    # Check for alpha parts: (a), (b) or a), b)
    alpha_parts_pattern = r'(?:^|\n)\s*\(?([a-c])\)?[\.\)]\s'
    alpha_parts = re.findall(alpha_parts_pattern, text, re.IGNORECASE | re.MULTILINE)
    has_alpha_parts = len(set([p.lower() for p in alpha_parts])) >= 2
    
    # Check for roman numeral sub-parts: (i), (ii), (iii)
    roman_pattern = r'\(?(i{1,4}|iv|v|vi|vii|viii|ix|x)\)[\.\)]?\s'
    roman_parts = re.findall(roman_pattern, text, re.IGNORECASE)
    has_roman_subparts = len(roman_parts) >= 2
    
    # Check for standard MCQ options: (A), (B), (C), (D) or (1), (2), (3), (4)
    mcq_letter_pattern = r'(?:^|\n)\s*\(?([A-D])\)?[\.\)]\s'
    mcq_number_pattern = r'(?:^|\n)\s*\(?([1-4])\)[\.\)]?\s'
    mcq_letters = re.findall(mcq_letter_pattern, text, re.IGNORECASE | re.MULTILINE)
    mcq_numbers = re.findall(mcq_number_pattern, text, re.MULTILINE)
    has_mcq_options = len(mcq_letters) >= 2 or len(mcq_numbers) >= 2
    
    # Determine question type
    is_nested = has_or_separator or (has_alpha_parts and has_roman_subparts)
    
    # Extract solution/answer if present at the end
    cleaned_text, extracted_solution, extracted_answer = extract_solution_from_text(text)
    # If we found a solution, use the cleaned text for parsing structure
    if extracted_solution:
        text = cleaned_text
    
    result = None
    
    # If it's a simple MCQ (has options but no nested structure)
    if has_mcq_options and not is_nested and not has_alpha_parts:
        result = parse_simple_mcq(text)
    
    # If it's a nested question
    elif is_nested or has_alpha_parts:
        result = parse_nested_parts(text, has_or_separator)
    
    else:
        # Default: return as simple question text
        result = {
            'question_text': text,
            'options': [],
            'is_nested': False,
            'structure': None,
            'correct_answer': '',
            'solution': ''
        }
        
    # Inject extracted solution/answer if available and not already set
    if result:
        if extracted_solution and not result.get('solution'):
            result['solution'] = extracted_solution
        if extracted_answer and not result.get('correct_answer'):
            result['correct_answer'] = extracted_answer
            
    return result


def extract_solution_from_text(text):
    """
    Extract solution/answer if present at the end of the text.
    Look for patterns like "Answer:", "Ans:", "Solution:", "Sol:".
    Returns (cleaned_text, solution, correct_answer)
    """
    import re
    
    # Pattern to find solution at the end
    # Match: (Answer|Ans|Solution|Sol|Key)[:.] (content)
    # This should be at the end of the string
    solution_pattern = r'(?:Answer|Ans|Solution|Sol|Key)[\:\.]\s*(.+)$'
    match = re.search(solution_pattern, text, re.IGNORECASE | re.DOTALL)
    
    if match:
        solution_text = match.group(1).strip()
        # Remove solution part from original text
        cleaned_text = text[:match.start()].strip()
        
        # Check if it's just a short answer (e.g. "A" or "Option B") or a full solution
        correct_answer = ''
        solution = ''
        
        # If text is short (e.g. "(A)", "Option C", "45"), treat as key
        if len(solution_text) < 50 and '\n' not in solution_text:
             correct_answer = solution_text
             solution = solution_text 
        else:
             # Likely a detailed solution
             solution = solution_text
             # Try to extract short answer from start if formatted like "Option A: ..."
             # For now, just leave correct_answer empty if it's long, unless user manually edits
        
        return cleaned_text, solution, correct_answer
    
    return text, '', ''


def parse_simple_mcq(text):
    """
    Parse a simple MCQ with options (A), (B), (C), (D) or (1), (2), (3), (4).
    """
    import re
    
    # Try to find the main question (before options)
    # Pattern for options: (A), (B), (C), (D) or (1), (2), (3), (4)
    option_patterns = [
        r'(?:^|\n)\s*\(?([A-D])\)?[\.\)]\s*(.*?)(?=(?:\n\s*\(?[A-D]\)?[\.\)]|$))',  # Letter options
        r'(?:^|\n)\s*\(([1-4])\)\s*(.*?)(?=(?:\n\s*\([1-4]\)|$))',  # Number options
    ]
    
    options = []
    option_start = len(text)
    
    for pattern in option_patterns:
        matches = list(re.finditer(pattern, text, re.IGNORECASE | re.DOTALL | re.MULTILINE))
        if matches and len(matches) >= 2:
            options = [m.group(2).strip() for m in matches]
            option_start = matches[0].start()
            break
    
    # Get question text (everything before options)
    question_text = text[:option_start].strip()
    
    # Remove question number from start if present (e.g., "22." or "Q.22")
    question_text = re.sub(r'^(?:Q\.?\s*)?\d+\.?\s*', '', question_text).strip()
    
    return {
        'question_text': question_text,
        'options': options if options else [],
        'is_nested': False,
        'structure': None,
        'correct_answer': '',
        'solution': ''
    }


def parse_nested_parts(text, has_or_separator):
    """
    Parse text with nested parts (a), (b) and sub-parts (i), (ii).
    """
    import re
    
    # Remove question number from start
    text = re.sub(r'^(?:Q\.?\s*)?\d+\.?\s*', '', text).strip()
    
    nested_parts = []
    
    # Split by OR if present (internal choice)
    if has_or_separator:
        or_split = re.split(r'\n\s*OR\s*\n|\n\s*\(OR\)\s*\n', text, flags=re.IGNORECASE)
        
        for idx, part_text in enumerate(or_split):
            part_text = part_text.strip()
            if not part_text:
                continue
            
            # Parse this part
            label = chr(ord('a') + idx)  # a, b, c...
            part_data = parse_single_part(part_text, label, idx == 0)
            
            if idx > 0:
                part_data['type'] = 'choice'  # Mark as internal choice option
            
            nested_parts.append(part_data)
    else:
        # Split by (a), (b), (c) pattern
        part_pattern = r'(?:^|\n)\s*\(?([a-c])\)[\.\)]?\s*'
        parts = re.split(part_pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        
        # First element might be intro/main question text
        intro_text = parts[0].strip() if parts else ''
        
        # Process pairs (label, content)
        i = 1
        while i < len(parts) - 1:
            label = parts[i].lower()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ''
            
            part_data = parse_single_part(content, label, True)
            nested_parts.append(part_data)
            i += 2
        
        # If no parts found but has roman numerals, treat whole as single part with sub-parts
        if not nested_parts and re.search(r'\(i+\)', text, re.IGNORECASE):
            part_data = parse_single_part(text, 'a', True)
            nested_parts.append(part_data)
    
    # Extract main question text (if any)
    # Usually nested questions have the main text before (a) or include it in part (a)
    main_question_text = ''
    if nested_parts and nested_parts[0].get('text'):
        # Check if first part has a clear question intro
        first_text = nested_parts[0].get('text', '')
        if first_text and not first_text.startswith('('):
            # Use first sentence as main question if it looks like an intro
            sentences = re.split(r'(?<=[.?:])\s+', first_text)
            if len(sentences) > 1 and len(sentences[0]) > 20:
                main_question_text = sentences[0]
    
    return {
        'question_text': main_question_text,
        'options': [],
        'is_nested': True,
        'structure': {
            'nested_parts': nested_parts
        },
        'correct_answer': '',
        'solution': ''
    }


def parse_single_part(text, label, is_first=False):
    """
    Parse a single part, extracting any sub-parts (i), (ii), (iii).
    """
    import re
    
    # Check for roman numeral sub-parts
    subpart_pattern = r'\s*\(?(i{1,4}|iv|v|vi|vii|viii|ix|x)\)\s*'
    subpart_splits = re.split(subpart_pattern, text, flags=re.IGNORECASE)
    
    sub_parts = []
    main_text = subpart_splits[0].strip() if subpart_splits else text
    
    # Process pairs (label, content)
    i = 1
    while i < len(subpart_splits) - 1:
        roman_label = subpart_splits[i].lower()
        content = subpart_splits[i + 1].strip() if i + 1 < len(subpart_splits) else ''
        
        if content:
            sub_parts.append({
                'label': roman_label,
                'text': content.strip()
            })
        i += 2
    
    result = {
        'label': label,
        'text': main_text,
        'type': 'compulsory'
    }
    
    if sub_parts:
        result['sub_parts'] = sub_parts
    
    return result


# ===========================
# Image to Text Extraction (Mathpix OCR)
# ===========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def extract_text_from_image(request):
    """
    Extract text from an uploaded image using Mathpix OCR.
    Used by the AI Image to Text feature in question creation.
    
    POST /api/questions/image-to-text/
    
    Request: multipart/form-data with 'image' file
    
    Returns:
    {
        "success": true,
        "extracted_text": "The extracted question text with LaTeX preserved...",
        "has_latex": true,
        "confidence": 0.95,
        "message": "Successfully extracted text from image"
    }
    """
    try:
        # Check if image file is provided
        if 'image' not in request.FILES:
            return Response(
                {'error': 'No image file provided. Please upload an image.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        uploaded_file = request.FILES['image']
        
        # Validate file type
        allowed_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp']
        if uploaded_file.content_type not in allowed_types:
            return Response(
                {'error': f'Invalid file type: {uploaded_file.content_type}. Allowed: JPG, PNG, GIF, WebP, BMP'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate file size (max 10MB)
        max_size = 10 * 1024 * 1024  # 10MB
        if uploaded_file.size > max_size:
            return Response(
                {'error': f'File too large. Maximum size is 10MB.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Save uploaded file temporarily
        upload_dir = os.path.join(settings.MEDIA_ROOT, 'image_extraction_temp')
        os.makedirs(upload_dir, exist_ok=True)
        
        file_path = os.path.join(upload_dir, f"{timezone.now().timestamp()}_{uploaded_file.name}")
        
        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
        
        try:
            # Extract text using Mathpix
            from questions.services.mathpix_service import MathpixService, MathpixError
            
            mathpix = MathpixService()
            extracted_text = mathpix.extract_image(file_path)
            
            # Check if text contains LaTeX
            has_latex = '$' in extracted_text or '\\' in extracted_text
            
            logger.info(f"Successfully extracted {len(extracted_text)} chars from image via Mathpix")
            
            # Try Gemini-based structured Q+A parsing for higher accuracy
            parsed_structure = None
            try:
                parsed_structure = gemini_parse_question_from_text(extracted_text)
                if parsed_structure:
                    logger.info(f"Gemini structured parsing succeeded: keys={list(parsed_structure.keys())}")
            except Exception as gemini_err:
                logger.warning(f"Gemini structured parsing failed, falling back to regex: {gemini_err}")
            
            # Fallback to regex parsing if Gemini didn't work
            if not parsed_structure:
                parsed_structure = parse_nested_question_structure(extracted_text)
            
            # Clean up temp file
            try:
                os.remove(file_path)
            except:
                pass
            
            return Response({
                'success': True,
                'extracted_text': extracted_text,
                'has_latex': has_latex,
                'confidence': 0.95,
                'message': 'Successfully extracted text from image',
                'parsed_structure': parsed_structure
            }, status=status.HTTP_200_OK)
            
        except MathpixError as e:
            # Clean up temp file
            try:
                os.remove(file_path)
            except:
                pass
            
            logger.error(f"Mathpix extraction failed: {str(e)}")
            return Response(
                {'error': f'Failed to extract text: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
    except Exception as e:
        logger.error(f"Image to text extraction failed: {str(e)}", exc_info=True)
        return Response(
            {'error': f'Failed to process image: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


def gemini_parse_question_from_text(extracted_text):
    """
    Use Gemini AI to parse extracted text into structured question data.
    Returns a dict with question_text, options, correct_answer, solution, is_nested, structure.
    Returns None if Gemini is not available or parsing fails.
    """
    import json
    import re
    from django.conf import settings
    
    gemini_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not gemini_key:
        return None
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)
    except Exception:
        return None
    
    prompt = f"""You are a high-precision exam question parser. Analyze the following extracted text and return a structured JSON object.

TEXT:
{extracted_text}

Return ONLY a valid JSON object (no markdown, no explanation) with this structure:
{{
  "question_text": "The main question text (preserve all LaTeX like $...$ and $$...$$)",
  "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
  "correct_answer": "The correct answer key (e.g. 'A', 'B', '42', etc.) or empty string if not found",
  "solution": "The step-by-step solution or answer explanation, or empty string if not found",
  "question_type": "single_mcq|multiple_mcq|numerical|subjective|true_false|fill_blank",
  "is_nested": false,
  "structure": null
}}

RULES:
- If the text contains MCQ options, extract them into the "options" array and separate from question_text.
- If a correct answer is marked (e.g., "Answer: B", "Ans: C", circled option), put ONLY the letter/number in "correct_answer".
- If a detailed solution exists (e.g., "Solution: ...", "Sol: ..."), put it in "solution".
- Preserve ALL LaTeX formatting.
- For subjective questions with parts (a), (b), (c), set is_nested to true and populate structure.nested_parts.
- If no answer/solution is found, use empty strings - do NOT make up answers.
"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.1,
                    "top_p": 0.9,
                    "max_output_tokens": 4096,
                }
            )
            
            text = response.text.strip()
            
            # Extract JSON from response
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError:
                    # Fix common LaTeX escape issues
                    json_str_fixed = re.sub(r'\\(?![\\"/bfnrtu])', r'\\\\', json_str)
                    try:
                        result = json.loads(json_str_fixed)
                    except json.JSONDecodeError:
                        logger.warning("Gemini returned invalid JSON even after fix")
                        return None
                
                # Validate required fields
                if 'question_text' not in result:
                    return None
                
                # Normalize
                result.setdefault('options', [])
                result.setdefault('correct_answer', '')
                result.setdefault('solution', '')
                result.setdefault('is_nested', False)
                result.setdefault('structure', None)
                result.setdefault('question_type', 'subjective')
                
                return result
            
            return None
            
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "resource exhausted" in error_msg.lower():
                if attempt < max_retries - 1:
                    delay = 5 * (2 ** attempt)
                    logger.info(f"Gemini Rate limit hit (429). Retrying in {delay}s... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
            logger.warning(f"Gemini parse failed: {e}")
            return None

