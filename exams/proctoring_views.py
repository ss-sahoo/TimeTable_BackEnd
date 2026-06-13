"""
API views for AI proctoring and cheating detection
"""
from rest_framework import generics, permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django.db.models import Q, Avg, Count
from django.utils import timezone
from datetime import timedelta
import json

from .models import Exam, ExamAttempt, ExamProctoring, ExamViolation, ProctoringVideoClip, ProctoringSnapshot
from .ai_proctoring import mediapipe_proctoring as ai_proctoring
from .serializers import ExamSerializer
from accounts.models import User


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def analyze_exam_session(request, attempt_id):
    """Analyze an exam session for cheating detection"""
    try:
        attempt = ExamAttempt.objects.get(id=attempt_id)
        user = request.user
        
        # Check permissions
        if user.role in ['student', 'STUDENT'] and attempt.student != user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        if user.role in ['student', 'STUDENT'] and not user.can_view_exam(attempt.exam.id):
            return Response({'error': 'Access denied to this exam'}, status=status.HTTP_403_FORBIDDEN)
        
        # Run AI analysis
        analysis = ai_proctoring.analyze_exam_session(attempt_id)
        
        if 'error' in analysis:
            return Response({'error': analysis['error']}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response(analysis)
        
    except ExamAttempt.DoesNotExist:
        return Response({'error': 'Exam attempt not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to analyze session: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def detect_real_time_violations(request, attempt_id):
    """Detect real-time violations during exam"""
    try:
        attempt = ExamAttempt.objects.get(id=attempt_id)
        user = request.user
        
        # Check permissions
        if user.role in ['student', 'STUDENT'] and attempt.student != user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Get event data from request
        event_data = request.data
        
        # Run real-time detection
        detection = ai_proctoring.detect_real_time_violations(attempt_id, event_data)
        
        if 'error' in detection:
            return Response({'error': detection['error']}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response(detection)
        
    except ExamAttempt.DoesNotExist:
        return Response({'error': 'Exam attempt not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to detect violations: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_proctoring_dashboard(request, exam_id):
    """Get comprehensive proctoring dashboard for an exam"""
    try:
        exam = Exam.objects.get(id=exam_id)
        user = request.user
        
        # Check permissions
        if not user.can_manage_exams():
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Get dashboard data
        dashboard = ai_proctoring.get_proctoring_dashboard(exam_id)
        
        if 'error' in dashboard:
            return Response({'error': dashboard['error']}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response(dashboard)
        
    except Exam.DoesNotExist:
        return Response({'error': 'Exam not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to get proctoring dashboard: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_violations(request, exam_id):
    """Get all violations for an exam"""
    try:
        exam = Exam.objects.get(id=exam_id)
        user = request.user
        
        # Check permissions
        if not user.can_manage_exams():
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Get violations
        violations = ExamViolation.objects.filter(
            attempt__exam=exam
        ).select_related('attempt', 'attempt__student').order_by('-detected_at')
        
        violation_data = []
        for violation in violations:
            violation_data.append({
                'id': violation.id,
                'attempt_id': violation.attempt.id,
                'student_id': violation.attempt.student.id,
                'student_name': violation.attempt.student.get_full_name() or violation.attempt.student.email,
                'violation_type': violation.violation_type,
                'description': violation.description,
                'severity': violation.severity,
                'confidence': violation.confidence,
                'details': json.loads(violation.details) if violation.details else {},
                'detected_at': violation.detected_at.isoformat()
            })
        
        return Response({
            'exam_id': exam_id,
            'total_violations': len(violation_data),
            'violations': violation_data
        })
        
    except Exam.DoesNotExist:
        return Response({'error': 'Exam not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to get violations: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_student_proctoring_history(request, student_id):
    """Get proctoring history for a specific student"""
    try:
        user = request.user
        
        # Check permissions
        if user.role in ['student', 'STUDENT'] and user.id != student_id:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        student = User.objects.get(id=student_id)
        
        # Get proctoring data
        attempts = ExamAttempt.objects.filter(
            student=student,
            status='submitted'
        ).select_related('exam').order_by('-submitted_at')
        
        proctoring_data = []
        for attempt in attempts:
            try:
                proctoring = ExamProctoring.objects.get(attempt=attempt)
                proctoring_data.append({
                    'attempt_id': attempt.id,
                    'exam_id': attempt.exam.id,
                    'exam_title': attempt.exam.title,
                    'risk_score': proctoring.risk_score,
                    'analyzed_at': proctoring.analyzed_at.isoformat() if proctoring.analyzed_at else None,
                    'violations_count': ExamViolation.objects.filter(attempt=attempt).count()
                })
            except ExamProctoring.DoesNotExist:
                proctoring_data.append({
                    'attempt_id': attempt.id,
                    'exam_id': attempt.exam.id,
                    'exam_title': attempt.exam.title,
                    'risk_score': None,
                    'analyzed_at': None,
                    'violations_count': 0
                })
        
        return Response({
            'student_id': student_id,
            'student_name': student.get_full_name() or student.email,
            'proctoring_history': proctoring_data
        })
        
    except User.DoesNotExist:
        return Response({'error': 'Student not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to get proctoring history: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def update_proctoring_settings(request, exam_id):
    """Update proctoring settings for an exam"""
    try:
        exam = Exam.objects.get(id=exam_id)
        user = request.user
        
        # Check permissions
        if not user.can_manage_exams():
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Update proctoring settings
        settings = request.data
        
        # Update exam proctoring settings
        if 'enable_ai_proctoring' in settings:
            exam.enable_ai_proctoring = settings['enable_ai_proctoring']
        
        if 'enable_face_detection' in settings:
            exam.enable_face_detection = settings['enable_face_detection']
        
        if 'enable_screen_recording' in settings:
            exam.enable_screen_recording = settings['enable_screen_recording']
        
        if 'enable_audio_monitoring' in settings:
            exam.enable_audio_monitoring = settings['enable_audio_monitoring']
        
        if 'enable_tab_switching_detection' in settings:
            exam.enable_tab_switching_detection = settings['enable_tab_switching_detection']
        
        if 'enable_copy_paste_detection' in settings:
            exam.enable_copy_paste_detection = settings['enable_copy_paste_detection']
        
        if 'enable_mouse_tracking' in settings:
            exam.enable_mouse_tracking = settings['enable_mouse_tracking']
        
        if 'enable_keyboard_tracking' in settings:
            exam.enable_keyboard_tracking = settings['enable_keyboard_tracking']
        
        exam.save()
        
        return Response({
            'success': True,
            'message': 'Proctoring settings updated successfully',
            'settings': {
                'enable_ai_proctoring': exam.enable_ai_proctoring,
                'enable_face_detection': exam.enable_face_detection,
                'enable_screen_recording': exam.enable_screen_recording,
                'enable_audio_monitoring': exam.enable_audio_monitoring,
                'enable_tab_switching_detection': exam.enable_tab_switching_detection,
                'enable_copy_paste_detection': exam.enable_copy_paste_detection,
                'enable_mouse_tracking': exam.enable_mouse_tracking,
                'enable_keyboard_tracking': exam.enable_keyboard_tracking
            }
        })
        
    except Exam.DoesNotExist:
        return Response({'error': 'Exam not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to update proctoring settings: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_proctoring_statistics(request, exam_id):
    """Get proctoring statistics for an exam"""
    try:
        exam = Exam.objects.get(id=exam_id)
        user = request.user
        
        # Check permissions
        if not user.can_manage_exams():
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Get statistics
        attempts = ExamAttempt.objects.filter(exam=exam, status='submitted')
        proctoring_data = ExamProctoring.objects.filter(attempt__exam=exam)
        violations = ExamViolation.objects.filter(attempt__exam=exam)
        
        # Calculate statistics
        total_attempts = attempts.count()
        analyzed_attempts = proctoring_data.count()
        high_risk_attempts = proctoring_data.filter(risk_score__gte=0.8).count()
        medium_risk_attempts = proctoring_data.filter(risk_score__gte=0.5, risk_score__lt=0.8).count()
        low_risk_attempts = proctoring_data.filter(risk_score__lt=0.5).count()
        
        # Violation statistics
        violation_types = {}
        for violation in violations:
            violation_type = violation.violation_type
            violation_types[violation_type] = violation_types.get(violation_type, 0) + 1
        
        # Risk score distribution
        risk_scores = [p.risk_score for p in proctoring_data if p.risk_score is not None]
        avg_risk_score = sum(risk_scores) / len(risk_scores) if risk_scores else 0
        
        # Time-based statistics
        now = timezone.now()
        recent_violations = violations.filter(detected_at__gte=now - timedelta(days=7)).count()
        
        statistics = {
            'total_attempts': total_attempts,
            'analyzed_attempts': analyzed_attempts,
            'analysis_coverage': (analyzed_attempts / total_attempts * 100) if total_attempts > 0 else 0,
            'risk_distribution': {
                'high_risk': high_risk_attempts,
                'medium_risk': medium_risk_attempts,
                'low_risk': low_risk_attempts
            },
            'risk_percentages': {
                'high_risk': (high_risk_attempts / total_attempts * 100) if total_attempts > 0 else 0,
                'medium_risk': (medium_risk_attempts / total_attempts * 100) if total_attempts > 0 else 0,
                'low_risk': (low_risk_attempts / total_attempts * 100) if total_attempts > 0 else 0
            },
            'average_risk_score': round(avg_risk_score, 3),
            'total_violations': violations.count(),
            'violation_types': violation_types,
            'recent_violations': recent_violations,
            'violation_rate': (violations.count() / total_attempts * 100) if total_attempts > 0 else 0
        }
        
        return Response({
            'exam_id': exam_id,
            'exam_title': exam.title,
            'statistics': statistics,
            'generated_at': timezone.now().isoformat()
        })
        
    except Exam.DoesNotExist:
        return Response({'error': 'Exam not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to get proctoring statistics: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def record_proctoring_event(request, attempt_id):
    """Record a proctoring event during exam"""
    try:
        attempt = ExamAttempt.objects.get(id=attempt_id)
        user = request.user
        
        # Check permissions
        if user.role in ['student', 'STUDENT'] and attempt.student != user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Get or create proctoring record
        proctoring, created = ExamProctoring.objects.get_or_create(attempt=attempt)
        
        # Get event data
        event_data = request.data
        event_type = event_data.get('event_type')
        
        # Update proctoring record based on event type
        if event_type == 'mouse_movement':
            mouse_movements = json.loads(proctoring.mouse_movements or '[]')
            mouse_movements.append(event_data)
            proctoring.mouse_movements = json.dumps(mouse_movements)
        
        elif event_type == 'keyboard_event':
            keyboard_events = json.loads(proctoring.keyboard_events or '[]')
            keyboard_events.append(event_data)
            proctoring.keyboard_events = json.dumps(keyboard_events)
        
        elif event_type == 'tab_switch':
            tab_switches = json.loads(proctoring.tab_switches or '[]')
            tab_switches.append(event_data)
            proctoring.tab_switches = json.dumps(tab_switches)
        
        elif event_type == 'copy_paste':
            copy_paste_events = json.loads(proctoring.copy_paste_events or '[]')
            copy_paste_events.append(event_data)
            proctoring.copy_paste_events = json.dumps(copy_paste_events)
        
        elif event_type == 'device_info':
            proctoring.device_info = json.dumps(event_data)
        
        elif event_type == 'browser_info':
            proctoring.browser_info = json.dumps(event_data)
        
        elif event_type == 'screen_resolution':
            proctoring.screen_resolution = event_data.get('resolution')
        
        elif event_type == 'timezone':
            proctoring.timezone = event_data.get('timezone')
        
        elif event_type == 'ip_address':
            proctoring.ip_address = event_data.get('ip_address')
        
        elif event_type == 'user_agent':
            proctoring.user_agent = event_data.get('user_agent')
        
        proctoring.save()
        
        # Run real-time violation detection
        detection = ai_proctoring.detect_real_time_violations(attempt_id, event_data)
        
        return Response({
            'success': True,
            'event_recorded': True,
            'violations_detected': detection.get('violations_detected', 0),
            'violations': detection.get('violations', [])
        })
        
    except ExamAttempt.DoesNotExist:
        return Response({'error': 'Exam attempt not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'Failed to record proctoring event: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_unified_violations(request, attempt_id):
    """Get all security violations and proctoring evidence for an attempt"""
    try:
        attempt = ExamAttempt.objects.get(id=attempt_id)
        user = request.user
        
        # Check permissions
        if user.role in ['student', 'STUDENT'] and attempt.student != user:
            # Check if student is allowed to manage exams (can_manage_exams is usually for admins)
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
            
        # Get all violation entries
        violations = ExamViolation.objects.filter(attempt=attempt).order_by('-timestamp')
        
        # Get video clips
        video_clips = []
        clips = ProctoringVideoClip.objects.filter(attempt=attempt).order_by('-timestamp')
        for clip in clips:
            video_clips.append({
                'id': clip.id,
                'video_file': clip.video_file.url if clip.video_file else None,
                'timestamp': clip.timestamp.isoformat(),
                'duration_seconds': clip.duration_seconds,
                'metadata': clip.metadata
            })
            
        violation_data = []
        for v in violations:
            image_url = v.screenshot.url if v.screenshot else None
            
            # Fallback: Check if there's a linked ProctoringSnapshot in metadata
            if not image_url and v.metadata and v.metadata.get('snapshot_id'):
                try:
                    snap = ProctoringSnapshot.objects.get(id=v.metadata['snapshot_id'])
                    if snap.image:
                        image_url = snap.image.url
                except:
                    pass
                    
            violation_data.append({
                'id': v.id,
                'type': v.violation_type,
                'type_display': v.get_violation_type_display(),
                'timestamp': v.timestamp.isoformat(),
                'screenshot_url': image_url,
                'metadata': v.metadata
            })
            
        return Response({
            'attempt_id': attempt_id,
            'student_name': attempt.student.get_full_name() or attempt.student.email,
            'exam_title': attempt.exam.title,
            'total_violations': len(violation_data),
            'violations': violation_data,
            'video_clips': video_clips
        })
        
    except ExamAttempt.DoesNotExist:
        return Response({'error': 'Attempt not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
