"""
Run this script once to back-fill 2 default exam patterns for every existing
institute in the database.

Usage:
    python3 create_default_patterns.py
"""
import os
import django
import sys

sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'exam_flow_backend.settings')
django.setup()

from accounts.models import User, Institute
from patterns.default_patterns import create_default_patterns_for_institute


def main():
    institutes = Institute.objects.all().order_by('id')
    print(f"Found {institutes.count()} institute(s).\n")

    for inst in institutes:
        print(f"Institute [{inst.id}]: {inst.name}")

        # Pick the best available creator user
        creator = (
            inst.get_admins().first()
            or inst.users.first()
            or User.objects.filter(is_superuser=True).first()
        )

        if not creator:
            print("  [skip] No user found to assign as creator.\n")
            continue

        print(f"  Creator: {creator.email}")
        create_default_patterns_for_institute(inst, creator)
        print()

    print("Done.")


if __name__ == '__main__':
    main()
