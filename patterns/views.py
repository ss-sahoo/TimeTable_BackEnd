from rest_framework import generics, permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django.db import transaction
from django.db.models import Q
from .models import Subject, ExamPattern, PatternSection, PatternTemplate
from .serializers import (
    SubjectSerializer, ExamPatternSerializer, ExamPatternCreateSerializer, 
    PatternSectionSerializer, PatternTemplateSerializer, PatternSectionCreateSerializer
)
from questions.models import Question, ExamQuestion
from questions.serializers import QuestionSerializer
import json


class SubjectListView(generics.ListCreateAPIView):
    """List and create subjects"""
    serializer_class = SubjectSerializer
    permission_classes = [permissions.IsAuthenticated]
    ordering_fields = ['name', 'code', 'created_at', 'updated_at']
    search_fields = ['name', 'code']

    def get_queryset(self):
        user = self.request.user
        if user.is_institute_admin():
            return Subject.objects.filter(institute=user.institute, is_active=True)
        return Subject.objects.none()

    def perform_create(self, serializer):
        serializer.save(institute=self.request.user.institute)


class SubjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a subject"""
    serializer_class = SubjectSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_institute_admin():
            return Subject.objects.filter(institute=user.institute)
        return Subject.objects.none()


class ExamPatternListView(generics.ListCreateAPIView):
    """List and create exam patterns"""
    permission_classes = [permissions.IsAuthenticated]
    ordering_fields = ['name', 'created_at', 'updated_at', 'total_questions', 'total_marks', 'total_duration']
    search_fields = ['name', 'description']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ExamPatternCreateSerializer
        return ExamPatternSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_institute_admin():
            return ExamPattern.objects.filter(institute=user.institute)
        return ExamPattern.objects.filter(institute=user.institute, is_active=True)

    def perform_create(self, serializer):
        serializer.save(
            institute=self.request.user.institute,
            created_by=self.request.user
        )

    def create(self, request, *args, **kwargs):
        """Override create to handle duplicate pattern names gracefully and return full serialized data"""
        pattern_name = request.data.get('name', '')
        user_institute = request.user.institute
        
        # Check if pattern with same name already exists
        if ExamPattern.objects.filter(name=pattern_name, institute=user_institute).exists():
            return Response(
                {
                    'error': 'Pattern name already exists',
                    'detail': f'A pattern named "{pattern_name}" is already exists in the exam . please check the exam patterns.',
                    'field': 'name'
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Use the prescribed create serializer for validation and creation
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            
            # Save the instance
            instance = serializer.save(
                institute=self.request.user.institute,
                created_by=self.request.user
            )
            
            # Return full data using the detail serializer
            response_serializer = ExamPatternSerializer(instance, context=self.get_serializer_context())
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            # Catch any other database errors
            return Response(
                {
                    'error': 'Database error',
                    'detail': str(e)
                },
                status=status.HTTP_400_BAD_REQUEST
            )


class ExamPatternDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Get, update, and delete exam patterns"""
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return ExamPatternCreateSerializer
        return ExamPatternSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_institute_admin():
            return ExamPattern.objects.filter(institute=user.institute)
        return ExamPattern.objects.filter(institute=user.institute, is_active=True)

    def perform_update(self, serializer):
        user = self.request.user
        if not user.can_create_exams():
            raise permissions.PermissionDenied("You don't have permission to update patterns")
        serializer.save()

    def perform_destroy(self, instance):
        # Clear legacy question references that still store pattern_section_id
        section_ids = list(instance.sections.values_list('id', flat=True))
        if section_ids:
            try:
                Question.objects.filter(pattern_section_id__in=section_ids).update(
                    pattern_section_id=None,
                    pattern_section_name=''
                )
            except Exception:
                # Column may not exist in legacy databases; ignore and continue
                Question.objects.filter(pattern_section_id__in=section_ids).update(
                    pattern_section_id=None
                )
        super().perform_destroy(instance)


class PatternSectionListView(generics.ListCreateAPIView):
    """List and create pattern sections"""
    serializer_class = PatternSectionSerializer
    permission_classes = [permissions.IsAuthenticated]
    ordering_fields = ['start_question', 'end_question', 'name', 'subject', 'created_at']

    def get_queryset(self):
        pattern_id = self.kwargs.get('pattern_id')
        # Scope by caller's institute so users from other tenants can't see
        # sections of a pattern they don't own (returns 404, not 403).
        return PatternSection.objects.filter(
            pattern_id=pattern_id,
            pattern__institute=self.request.user.institute,
        )

    def perform_create(self, serializer):
        pattern_id = self.kwargs.get('pattern_id')
        pattern = ExamPattern.objects.get(id=pattern_id)

        # Check permissions
        user = self.request.user
        if not user.can_create_exams() or pattern.institute != user.institute:
            raise permissions.PermissionDenied("You don't have permission to add sections")

        serializer.save(pattern=pattern)


class PatternSectionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Get, update, and delete pattern sections"""
    serializer_class = PatternSectionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        pattern_id = self.kwargs.get('pattern_id')
        # Scope by caller's institute so PATCH/DELETE on another tenant's
        # pattern section returns 404 instead of succeeding.
        return PatternSection.objects.filter(
            pattern_id=pattern_id,
            pattern__institute=self.request.user.institute,
        )


class PatternTemplateListView(generics.ListAPIView):
    """List available pattern templates"""
    queryset = PatternTemplate.objects.filter(is_public=True)
    serializer_class = PatternTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def create_pattern_from_template(request, template_id):
    """Create a new pattern from a template"""
    try:
        template = PatternTemplate.objects.get(id=template_id, is_public=True)
    except PatternTemplate.DoesNotExist:
        return Response({'error': 'Template not found'}, status=status.HTTP_404_NOT_FOUND)
    
    user = request.user
    if not user.can_create_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    with transaction.atomic():
        pattern = ExamPattern.objects.create(
            name=f"{template.name} - {user.institute.name}",
            description=template.description,
            institute=user.institute,
            total_questions=template.total_questions,
            total_duration=template.total_duration,
            total_marks=template.total_marks,
            created_by=user
        )
        
        # Create sections based on template
        sections_data = template.template_data.get('sections', [])
        for section_data in sections_data:
            PatternSection.objects.create(pattern=pattern, **section_data)
    
    serializer = ExamPatternSerializer(pattern)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def pattern_validation(request, pattern_id):
    """Validate pattern structure"""
    try:
        pattern = ExamPattern.objects.get(id=pattern_id)
    except ExamPattern.DoesNotExist:
        return Response({'error': 'Pattern not found'}, status=status.HTTP_404_NOT_FOUND)
    
    user = request.user
    if pattern.institute != user.institute:
        return Response({'error': 'Access denied'}, status=status.HTTP_403_FORBIDDEN)
    
    sections = pattern.sections.all().order_by('start_question')
    validation_result = {
        'is_valid': True,
        'errors': [],
        'warnings': [],
        'coverage': 0
    }
    
    # Check question coverage
    covered_questions = set()
    for section in sections:
        section_questions = set(range(section.start_question, section.end_question + 1))
        
        # Check for overlaps
        if covered_questions.intersection(section_questions):
            validation_result['errors'].append(f"Overlapping questions in section: {section.name}")
            validation_result['is_valid'] = False
        
        covered_questions.update(section_questions)
    
    # Check total coverage
    expected_questions = set(range(1, pattern.total_questions + 1))
    if covered_questions != expected_questions:
        missing = expected_questions - covered_questions
        extra = covered_questions - expected_questions
        
        if missing:
            validation_result['errors'].append(f"Missing questions: {sorted(missing)}")
            validation_result['is_valid'] = False
        
        if extra:
            validation_result['warnings'].append(f"Extra questions: {sorted(extra)}")
    
    validation_result['coverage'] = len(covered_questions) / pattern.total_questions * 100
    
    return Response(validation_result)


# New pattern question assignment views
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_pattern_questions(request, pattern_id):
    """
    List sections and their templated questions for a pattern owned by the
    caller's institute.

    Responses:
        200: {"pattern": {...}, "sections_with_questions": [...]}
        404: pattern does not exist or belongs to a different institute.

    Unexpected exceptions fall through to the custom exception handler, which
    returns a normalized 500 (no traceback leaked to the client).
    """
    user = request.user
    try:
        pattern = ExamPattern.objects.get(id=pattern_id, institute=user.institute)
    except ExamPattern.DoesNotExist:
        return Response(
            {'error': 'Pattern not found'},
            status=status.HTTP_404_NOT_FOUND,
        )

    sections = PatternSection.objects.filter(pattern=pattern).order_by('start_question')

    pattern_questions = []
    for section in sections:
        questions = Question.objects.filter(
            pattern_section_id=section.id,
            institute=user.institute,
            is_active=True,
            exam__isnull=True
        ).order_by('question_number_in_pattern')

        section_questions = [
            {
                'id': question.id,
                'question_text': question.question_text,
                'question_type': question.question_type,
                'difficulty': question.difficulty,
                'marks': question.marks,
                'question_number_in_pattern': question.question_number_in_pattern,
                'options': question.options if question.question_type in ['mcq', 'single_mcq', 'multiple_mcq'] else [],
                'correct_answer': question.correct_answer,
                'explanation': question.explanation,
            }
            for question in questions
        ]

        pattern_questions.append({
            'section': {
                'id': section.id,
                'name': section.name,
                'subject': section.subject,
                'start_question': section.start_question,
                'end_question': section.end_question,
                'marks_per_question': section.marks_per_question,
                'question_type': section.question_type,
            },
            'questions': section_questions,
        })

    return Response({
        'pattern': {
            'id': pattern.id,
            'name': pattern.name,
            'description': pattern.description,
            'total_questions': pattern.total_questions,
            'total_marks': pattern.total_marks,
            'total_duration': pattern.total_duration,
        },
        'sections_with_questions': pattern_questions,
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def assign_pattern_questions_to_exam(request):
    """
    Replace an exam's questions with the questions templated by the given
    pattern. Both exam and pattern must belong to the caller's institute.

    Request body:
        {
            "exam_id": ...,
            "pattern_id": ...,
            "use_existing_questions": true   # optional, default true
        }

    Responses:
        201: when `use_existing_questions=True` and rows were created.
        200: when only the pattern structure was attached (no questions copied).
        400: missing `exam_id` or `pattern_id`.
        404: exam or pattern not found in caller's institute.

    Side effects: deletes any existing `ExamQuestion` rows for the exam before
    recreating them — wrapped in a single transaction.
    """
    from exams.models import Exam

    exam_id = request.data.get('exam_id')
    pattern_id = request.data.get('pattern_id')
    use_existing_questions = request.data.get('use_existing_questions', True)

    if not exam_id or not pattern_id:
        return Response(
            {'error': 'exam_id and pattern_id are required'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = request.user
    try:
        exam = Exam.objects.get(id=exam_id, institute=user.institute)
    except Exam.DoesNotExist:
        return Response({'error': 'Exam not found'}, status=status.HTTP_404_NOT_FOUND)

    try:
        pattern = ExamPattern.objects.get(id=pattern_id, institute=user.institute)
    except ExamPattern.DoesNotExist:
        return Response({'error': 'Pattern not found'}, status=status.HTTP_404_NOT_FOUND)

    if not use_existing_questions:
        return Response({
            'message': 'Pattern structure assigned to exam (no questions copied)',
            'pattern_name': pattern.name,
            'assigned_questions': [],
        })

    sections = PatternSection.objects.filter(pattern=pattern).order_by('start_question')
    assigned_questions = []
    with transaction.atomic():
        ExamQuestion.objects.filter(exam=exam).delete()
        for section in sections:
            questions_qs = Question.objects.filter(
                pattern_section_id=section.id,
                institute=user.institute,
                is_active=True,
            ).filter(exam_id=exam_id).order_by('question_number_in_pattern')

            for question in questions_qs:
                exam_question = ExamQuestion.objects.create(
                    exam=exam,
                    question=question,
                    question_number=question.question_number_in_pattern or 1,
                    section_name=section.name,
                    marks=section.marks_per_question,
                    negative_marks=0.25,
                    order=question.question_number_in_pattern or 1,
                )
                truncated_text = (
                    question.question_text[:100] + '...'
                    if len(question.question_text) > 100
                    else question.question_text
                )
                assigned_questions.append({
                    'id': exam_question.id,
                    'question_id': question.id,
                    'question_text': truncated_text,
                    'section_name': section.name,
                    'question_number': exam_question.question_number,
                    'marks': exam_question.marks,
                })

    return Response(
        {
            'message': f'Successfully assigned {len(assigned_questions)} questions to exam',
            'assigned_questions': assigned_questions,
            'pattern_name': pattern.name,
        },
        status=status.HTTP_201_CREATED,
    )