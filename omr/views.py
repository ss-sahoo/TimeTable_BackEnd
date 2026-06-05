"""
OMR App Views
API endpoints for OMR sheet generation and evaluation
"""
import os
import tempfile
# Force reload for filtering fix
from django.shortcuts import get_object_or_404
from django.core.files.storage import default_storage
from rest_framework import status, viewsets, permissions
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser

from django.utils import timezone
from exams.models import Exam, ExamAttempt
from .models import OMRSheet, OMRSubmission, AnswerKey
from .serializers import (
    OMRSheetSerializer,
    OMRSheetGenerateSerializer,
    OMRSubmissionSerializer,
    OMRSubmissionUploadSerializer,
    AnswerKeySerializer,
)
from .services.generator import OMRGeneratorService
from .services.evaluator import OMREvaluatorService


class OMRSheetViewSet(viewsets.ModelViewSet):
    """
    ViewSet for OMR Sheet management.
    """
    serializer_class = OMRSheetSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter OMR sheets by user's institute"""
        user = self.request.user
        if user.role == 'super_admin':
            return OMRSheet.objects.all()
        queryset = OMRSheet.objects.filter(exam__institute=user.institute)
        
        # Filter by exam_id if provided
        exam_id = self.request.query_params.get('exam_id')
        if exam_id:
            queryset = queryset.filter(exam_id=exam_id)
            
        return queryset
    
    @action(detail=False, methods=['post'], url_path='generate/(?P<exam_id>[^/.]+)')
    def generate_for_exam(self, request, exam_id=None):
        """
        Generate OMR sheet for an exam.

        POST /api/omr/sheets/generate/{exam_id}/
        """
        # Tenant scope: only allow operating on exams in the caller's institute.
        # Super admins retain global access; everyone else is restricted.
        exam_qs = Exam.objects.all() if request.user.role == 'super_admin' \
            else Exam.objects.filter(institute=request.user.institute)
        exam = get_object_or_404(exam_qs, id=exam_id)
        
        # Validate exam mode
        effective_mode = exam.exam_mode
        if effective_mode == 'online' and exam.pattern:
            effective_mode = getattr(exam.pattern, 'exam_mode', 'online')
            
        if effective_mode != 'offline_omr':
            return Response(
                {'error': 'Exam mode must be "offline_omr" to generate OMR sheet'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate request data
        serializer = OMRSheetGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Use candidate fields from request or fallback to exam.omr_config
        candidate_fields = serializer.validated_data.get('candidate_fields')
        if not candidate_fields and isinstance(exam.omr_config, dict):
            candidate_fields = exam.omr_config.get('candidate_fields', [])
            
        # Create OMR sheet record
        omr_sheet = OMRSheet.objects.create(
            exam=exam,
            candidate_fields=candidate_fields or [],
        )
        
        try:
            # Generate the OMR sheet
            generator = OMRGeneratorService(exam)
            generator.generate_and_save(
                omr_sheet,
                candidate_fields=candidate_fields,
            )
            
            # Update exam flags
            exam.omr_sheet_generated = True
            if omr_sheet.pdf_file:
                exam.omr_sheet_file = omr_sheet.pdf_file
            exam.omr_config = {
                'candidate_fields': omr_sheet.candidate_fields,
                'question_config': omr_sheet.question_config,
            }
            exam.omr_metadata = omr_sheet.metadata
            exam.save()
            
            # 4. Auto-sync answer key from questions to ensure consistency
            try:
                evaluator = OMREvaluatorService(None)
                evaluator.exam = exam 
                answer_key_data = evaluator._build_answer_key_from_questions()
                
                AnswerKey.objects.update_or_create(
                    exam=exam,
                    defaults={
                        'answers': answer_key_data,
                        'created_by': request.user
                    }
                )
            except Exception as e:
                print(f"[OMR GENERATE] Answer key auto-sync failed: {e}")

            return Response(
                OMRSheetSerializer(omr_sheet, context={'request': request}).data,
                status=status.HTTP_201_CREATED
            )
            
        except Exception as e:
            return Response(
                {'error': str(e), 'omr_sheet': OMRSheetSerializer(omr_sheet).data},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        """
        Get download URL for OMR sheet PDF.
        
        GET /api/omr/sheets/{id}/download/
        """
        omr_sheet = self.get_object()
        
        if not omr_sheet.pdf_file:
            return Response(
                {'error': 'OMR sheet PDF not available'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        return Response({
            'pdf_url': request.build_absolute_uri(omr_sheet.pdf_file.url),
            'metadata': omr_sheet.metadata,
        })


class OMRSubmissionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for OMR Submission management.
    """
    serializer_class = OMRSubmissionSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    
    def get_queryset(self):
        """Filter submissions based on user role"""
        user = self.request.user
        if user.role == 'super_admin':
            queryset = OMRSubmission.objects.all()
        elif user.role in ['institute_admin', 'exam_admin', 'teacher']:
            queryset = OMRSubmission.objects.filter(omr_sheet__exam__institute=user.institute)
        else:
            queryset = OMRSubmission.objects.filter(student=user)
            
        # Filter by exam_id if provided
        exam_id = self.request.query_params.get('exam_id')
        if exam_id:
            queryset = queryset.filter(omr_sheet__exam_id=exam_id)
            
        return queryset
    
    @action(detail=False, methods=['post'], url_path='upload/(?P<exam_id>[^/.]+)')
    def upload_for_exam(self, request, exam_id=None):
        """
        Upload scanned OMR sheets for evaluation.

        POST /api/omr/submissions/upload/{exam_id}/
        """
        # Tenant scope: prevent uploading OMR submissions against another
        # institute's exam by guessing the exam id.
        exam_qs = Exam.objects.all() if request.user.role == 'super_admin' \
            else Exam.objects.filter(institute=request.user.institute)
        exam = get_object_or_404(exam_qs, id=exam_id)
        
        # Validate exam mode
        if exam.exam_mode != 'offline_omr':
            return Response(
                {'error': 'Exam mode must be "offline_omr" to upload OMR sheets'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get primary OMR sheet
        omr_sheet = OMRSheet.objects.filter(exam=exam, is_primary=True).first()
        if not omr_sheet:
            return Response(
                {'error': 'No OMR sheet found for this exam. Generate one first.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Handle file uploads
        files = request.FILES.getlist('files')
        if not files:
            return Response(
                {'error': 'No files uploaded'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Save uploaded files - both to storage and locally for processing
        saved_paths = []
        local_temp_paths = []
        for file in files:
            # Save to cloud storage for record keeping
            file_path = f"omr_uploads/{exam.id}/{file.name}"
            saved_path = default_storage.save(file_path, file)
            saved_paths.append(saved_path)  # Store cloud path (relative)
            
            # Also save locally for processing
            file.seek(0)  # Reset file position after saving to storage
            file_ext = os.path.splitext(file.name)[1]
            with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
                for chunk in file.chunks():
                    tmp.write(chunk)
                local_temp_paths.append(tmp.name)
        
        # Determine student
        student_id = request.data.get('student_id')
        if student_id:
            from accounts.models import User
            student = get_object_or_404(User, id=student_id)
        else:
            student = request.user
        
        # Create or get ExamAttempt for OMR
        # For OMR, we usually just want one attempt record that tracks the results
        attempt, created = ExamAttempt.objects.get_or_create(
            exam=exam,
            student=student,
            defaults={
                'status': 'submitted',
                'started_at': timezone.now(),
                'submitted_at': timezone.now(),
                'attempt_number': 1
            }
        )
        
        # Create submission
        submission = OMRSubmission.objects.create(
            omr_sheet=omr_sheet,
            student=student,
            attempt=attempt,
            scanned_files=saved_paths,
        )
        
        # Evaluate if auto_evaluate is requested
        if request.data.get('auto_evaluate', True):
            try:
                evaluator = OMREvaluatorService(submission)
                evaluator.evaluate_and_save()
            except Exception as e:
                # Submission is saved but evaluation failed - log the error
                print(f"[OMR UPLOAD] Auto-evaluation failed: {e}")
        
        # Clean up local temp files
        for temp_path in local_temp_paths:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except:
                pass
        
        return Response(
            OMRSubmissionSerializer(submission, context={'request': request}).data,
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=True, methods=['post'])
    def evaluate(self, request, pk=None):
        """
        Trigger evaluation for a submission.
        
        POST /api/omr/submissions/{id}/evaluate/
        """
        submission = self.get_object()
        
        if submission.status == 'evaluated':
            return Response(
                {'warning': 'Submission already evaluated', 'results': submission.evaluation_results},
                status=status.HTTP_200_OK
            )
        
        try:
            evaluator = OMREvaluatorService(submission)
            evaluator.evaluate_and_save()
            
            return Response(
                OMRSubmissionSerializer(submission, context={'request': request}).data,
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'])
    def results(self, request, pk=None):
        """
        Get detailed evaluation results.
        
        GET /api/omr/submissions/{id}/results/
        """
        submission = self.get_object()
        
        if submission.status != 'evaluated':
            return Response(
                {'error': f'Submission not yet evaluated. Status: {submission.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        return Response({
            'candidate_info': submission.candidate_info,
            'score': float(submission.score) if submission.score else 0,
            'max_score': float(submission.max_score) if submission.max_score else 0,
            'percentage': float(submission.percentage) if submission.percentage else 0,
            'evaluation': submission.evaluation_results,
            'annotated_pdf_url': request.build_absolute_uri(submission.annotated_pdf.url) if submission.annotated_pdf else None,
        })


class AnswerKeyViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Answer Key management.
    """
    serializer_class = AnswerKeySerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter answer keys by user's institute"""
        user = self.request.user
        if user.role == 'super_admin':
            return AnswerKey.objects.all()
        return AnswerKey.objects.filter(exam__institute=user.institute)
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
    
    @action(detail=False, methods=['get', 'post'], url_path='exam/(?P<exam_id>[^/.]+)')
    def by_exam(self, request, exam_id=None):
        """
        Get or create answer key for an exam.

        GET /api/omr/answer-keys/exam/{exam_id}/ - Get answer key
        POST /api/omr/answer-keys/exam/{exam_id}/ - Create/update answer key
        """
        exam_qs = Exam.objects.all() if request.user.role == 'super_admin' \
            else Exam.objects.filter(institute=request.user.institute)
        exam = get_object_or_404(exam_qs, id=exam_id)
        
        if request.method == 'GET':
            try:
                answer_key = AnswerKey.objects.get(exam=exam)
                return Response(AnswerKeySerializer(answer_key).data)
            except AnswerKey.DoesNotExist:
                return Response(
                    {'error': 'No answer key found for this exam'},
                    status=status.HTTP_404_NOT_FOUND
                )
        else:  # POST
            answers = request.data.get('answers', {})
            
            answer_key, created = AnswerKey.objects.update_or_create(
                exam=exam,
                defaults={
                    'answers': answers,
                    'created_by': request.user,
                }
            )
            
            return Response(
                AnswerKeySerializer(answer_key).data,
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
            )
    
    @action(detail=False, methods=['post'], url_path='set-answers/(?P<exam_id>[^/.]+)')
    def set_answers(self, request, exam_id=None):
        """
        Directly set answer key via JSON input.
        
        POST /api/omr/answer-keys/set-answers/{exam_id}/
        
        Expected format:
        {
            "Q1": {"correct": ["A"], "marks": 4, "negative": 1},
            "Q2": {"correct": ["B"], "marks": 4, "negative": 1},
            ...
            "Q21": {"correct": ["1234"], "marks": 4, "negative": 0},  # For integer type questions
        }
        
        Each question entry must have:
        - correct: List of correct answer(s) - e.g. ["A"], ["B", "C"], ["1234"]
        - marks: Positive marks for correct answer
        - negative: Negative marks for wrong answer (0 for no negative marking)
        """
        exam_qs = Exam.objects.all() if request.user.role == 'super_admin' \
            else Exam.objects.filter(institute=request.user.institute)
        exam = get_object_or_404(exam_qs, id=exam_id)

        # The request body is the answer key directly
        answers = request.data
        
        # Validate the format
        if not isinstance(answers, dict):
            return Response(
                {'error': 'Invalid format. Expected JSON object with question answers.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        errors = []
        validated_answers = {}
        
        for field, data in answers.items():
            # Validate field name
            if not (field.startswith('Q') and field[1:].isdigit()):
                errors.append(f"Invalid question field: {field}. Expected format like 'Q1', 'Q2', etc.")
                continue
            
            # Validate data structure
            if not isinstance(data, dict):
                errors.append(f"{field}: Expected object with 'correct', 'marks', 'negative' fields.")
                continue
            
            # Validate required fields
            if 'correct' not in data:
                errors.append(f"{field}: Missing 'correct' field.")
                continue
            
            if not isinstance(data.get('correct'), list):
                errors.append(f"{field}: 'correct' must be a list (e.g. ['A'] or ['B', 'C']).")
                continue
            
            if len(data.get('correct', [])) == 0:
                errors.append(f"{field}: 'correct' list cannot be empty.")
                continue
            
            # Set defaults for optional fields
            marks = data.get('marks', 1)
            negative = data.get('negative', 0)
            
            try:
                marks = float(marks)
                negative = float(negative)
            except (ValueError, TypeError):
                errors.append(f"{field}: 'marks' and 'negative' must be numbers.")
                continue
            
            validated_answers[field] = {
                'correct': data['correct'],
                'marks': marks,
                'negative': negative
            }
        
        if errors:
            return Response(
                {
                    'error': 'Validation failed',
                    'details': errors
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not validated_answers:
            return Response(
                {'error': 'No valid answers provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create or update answer key
        answer_key, created = AnswerKey.objects.update_or_create(
            exam=exam,
            defaults={
                'answers': validated_answers,
                'created_by': request.user,
            }
        )
        
        return Response(
            {
                'message': f"Answer key {'created' if created else 'updated'} successfully",
                'exam_id': exam.id,
                'questions_count': len(validated_answers),
                'answer_key': AnswerKeySerializer(answer_key).data
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )
    
    @action(detail=False, methods=['post'], url_path='upload-master/(?P<exam_id>[^/.]+)', parser_classes=[MultiPartParser, FormParser])
    def upload_master(self, request, exam_id=None):
        """
        Upload a bubbled master sheet to define the answer key.
        
        POST /api/omr/answer-keys/upload-master/{exam_id}/
        """
        exam = get_object_or_404(Exam, id=exam_id)
        omr_sheet = OMRSheet.objects.filter(exam=exam, is_primary=True).first()
        
        if not omr_sheet:
            return Response(
                {'error': 'No primary OMR sheet generated for this exam. Generate one first.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        file = request.FILES.get('file')
        if not file:
            return Response(
                {'error': 'No file uploaded'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get file extension
        file_ext = os.path.splitext(file.name)[1].lower()
        
        # Save master sheet temporarily
        tmp_path = None
        image_paths = []
        
        try:
            # Create temp file
            with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
                for chunk in file.chunks():
                    tmp.write(chunk)
                tmp_path = tmp.name
            
            # Convert PDF to images if needed
            if file_ext == '.pdf':
                from .services.evaluator_core import convert_pdf_to_images
                image_paths = convert_pdf_to_images(tmp_path)
            else:
                # It's already an image
                image_paths = [tmp_path]
            
            # Import extraction function
            from .services.evaluator_core import extract_responses_with_details
            
            # Use metadata from the omr_sheet
            layout_data = omr_sheet.metadata
            if not layout_data:
                return Response(
                    {'error': 'OMR sheet metadata not found'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            print(f"[DEBUG] Processing {len(image_paths)} images")
            print(f"[DEBUG] Metadata has {len(layout_data.get('bubbles', []))} bubbles defined")
            
            # Extract bubbled responses (pass list of image paths)
            responses, all_evaluated = extract_responses_with_details(image_paths, layout_data)
            
            print(f"[DEBUG] Total evaluated bubbles: {len(all_evaluated)}")
            print(f"[DEBUG] Responses found: {responses}")
            
            # Log some fill ratios for debugging
            filled_bubbles = [b for b in all_evaluated if b.is_filled]
            print(f"[DEBUG] Filled bubbles count: {len(filled_bubbles)}")
            if all_evaluated:
                sample_ratios = [(b.field_name, b.value, b.fill_ratio) for b in all_evaluated[:20]]
                print(f"[DEBUG] Sample fill ratios: {sample_ratios}")
            
            # Convert responses to AnswerKey format
            new_answers = {}
            for field, values in responses.items():
                if field.startswith('Q'):
                    # It's a question field
                    if values:
                        # Find marks/negative from exam questions
                        from questions.models import ExamQuestion
                        try:
                            q_num = int(field[1:])
                            mapping = ExamQuestion.objects.get(exam=exam, question_number=q_num)
                            marks = float(mapping.marks)
                            negative = float(mapping.negative_marks)
                        except:
                            marks = 1.0
                            negative = 0.0
                            
                        new_answers[field] = {
                            'correct': values,
                            'marks': marks,
                            'negative': negative
                        }
            
            # Update or create the AnswerKey
            answer_key, created = AnswerKey.objects.update_or_create(
                exam=exam,
                defaults={
                    'answers': new_answers,
                    'created_by': request.user,
                }
            )
            
            return Response({
                'message': 'Master answer key uploaded and extracted successfully',
                'answer_key': AnswerKeySerializer(answer_key).data,
                'extracted_count': len(new_answers)
            })
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            return Response(
                {'error': f'Failed to extract answer key: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        finally:
            # Clean up temp files
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
            for img_path in image_paths:
                if img_path != tmp_path and os.path.exists(img_path):
                    os.remove(img_path)


# Convenience API endpoints
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def exam_omr_status(request, exam_id):
    """
    Get OMR status for an exam.
    
    GET /api/omr/exam/{exam_id}/status/
    """
    exam = get_object_or_404(Exam, id=exam_id)
    
    omr_sheet = OMRSheet.objects.filter(exam=exam, is_primary=True).first()
    answer_key = AnswerKey.objects.filter(exam=exam).exists()
    submissions_count = OMRSubmission.objects.filter(omr_sheet__exam=exam).count()
    evaluated_count = OMRSubmission.objects.filter(
        omr_sheet__exam=exam, status='evaluated'
    ).count()
    
    return Response({
        'exam_id': exam.id,
        'exam_mode': exam.exam_mode,
        'omr_sheet_generated': omr_sheet is not None and omr_sheet.status == 'generated',
        'omr_sheet_id': omr_sheet.id if omr_sheet else None,
        'pdf_url': request.build_absolute_uri(omr_sheet.pdf_file.url) if omr_sheet and omr_sheet.pdf_file else None,
        'answer_key_exists': answer_key,
        'submissions_count': submissions_count,
        'evaluated_count': evaluated_count,
    })
