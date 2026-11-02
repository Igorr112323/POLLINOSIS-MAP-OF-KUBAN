"""Provision a local account interactively; run inside the backend container."""
import argparse
import getpass
import re
from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.tables import User
from app.services.auth_service import ROLES, hash_password

def main():
    parser = argparse.ArgumentParser(description='Create an application user without exposing the password in shell history')
    parser.add_argument('--username', required=True)
    parser.add_argument('--role', choices=ROLES, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,120}', args.username) or args.username in {'demo_'+r for r in ROLES}:
        parser.error('Invalid or reserved username')
    password = getpass.getpass('Password (at least 12 characters): ')
    confirmation = getpass.getpass('Confirm password: ')
    if len(password) < 12 or len(password) > 128 or password != confirmation:
        raise SystemExit('Invalid password or confirmation')
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == args.username)):
            raise SystemExit('User already exists')
        db.add(User(username=args.username, password_hash=hash_password(password), role=args.role, is_active=True))
        db.commit()
    print('User created.')

if __name__ == '__main__':
    main()
