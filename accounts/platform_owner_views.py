from rest_framework import status, permissions, generics
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.db import transaction
from .models import Institute, User
from .platform_serializers import PlatformInstituteSerializer, CreateInstituteWithAdminSerializer, PlatformUserSerializer

class IsPlatformOwner(permissions.BasePermission):
    """
    Custom permission to only allow platform owners.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == 'platform_owner')

class PlatformInstituteListView(generics.ListAPIView):
    """
    List all institutes for the platform owner.
    """
    permission_classes = [IsPlatformOwner]
    serializer_class = PlatformInstituteSerializer
    queryset = Institute.objects.all().order_by('-created_at')

class PlatformInstituteDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Retrieve, update or delete an institute for the platform owner.
    """
    permission_classes = [IsPlatformOwner]
    serializer_class = PlatformInstituteSerializer
    queryset = Institute.objects.all()

class PlatformUserListView(generics.ListAPIView):
    """
    List all users across all institutes for the platform owner.
    Supports filtering by role via query param ?role=super_admin
    """
    permission_classes = [IsPlatformOwner]
    serializer_class = PlatformUserSerializer

    def get_queryset(self):
        from django.db.models import Q
        # Show ONLY Super Admins (any institute) AND users who are NOT yet linked to any institute
        queryset = User.objects.filter(
            Q(role='super_admin') | 
            Q(institute__isnull=True)
        ).order_by('-created_at')
        
        role = self.request.query_params.get('role')
        if role:
            queryset = queryset.filter(role=role)
        return queryset

@api_view(['POST'])
@permission_classes([IsPlatformOwner])
def create_institute_with_admin(request):
    """
    Atomic endpoint for platform owner to create a new institute and its super admin.
    """
    serializer = CreateInstituteWithAdminSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    data = serializer.validated_data
    
    # 1. Validation logic
    if Institute.objects.filter(name__iexact=data['name']).exists():
        return Response({'detail': f'Institute "{data["name"]}" already exists.'}, status=status.HTTP_400_BAD_REQUEST)
    
    if User.objects.filter(email__iexact=data['admin_email']).exists():
        return Response({'detail': f'A user with email "{data["admin_email"]}" already exists.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with transaction.atomic():
            # 2. Create Institute
            institute = Institute.objects.create(
                name=data['name'],
                subdomain=data.get('subdomain') or None,
                domain=data.get('domain') or None,
                contact_email=data['contact_email'],
                is_verified=True
            )
            
            # 3. Create Super Admin User
            user = User.objects.create_user(
                email=data['admin_email'],
                username=data['admin_username'],
                first_name=data['admin_first_name'],
                last_name=data['admin_last_name'],
                password=data['admin_password'],
                role='super_admin',
                institute=institute,
                is_staff=True
            )
            
            institute.created_by = user
            institute.save()
            
            # Auto-create 2 default exam patterns for this new institute
            try:
                from patterns.default_patterns import create_default_patterns_for_institute
                create_default_patterns_for_institute(institute, user)
            except Exception:
                pass  # Don't block creation if pattern setup fails
            
            return Response({
                'message': 'Institute and Super Admin created successfully.',
                'institute': PlatformInstituteSerializer(institute).data
            }, status=status.HTTP_201_CREATED)
            
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
