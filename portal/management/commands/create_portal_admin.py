import getpass
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from portal.models import AuditEvent, Profile

class Command(BaseCommand):
    help='Create a portal administrator without financial permissions. Password is prompted securely.'
    def add_arguments(self,parser):
        parser.add_argument('--username',required=True)
        parser.add_argument('--email',required=True)
    def handle(self,*args,**options):
        user=get_user_model()(username=options['username'],email=options['email'])
        if get_user_model().objects.filter(username=user.username).exists():
            raise CommandError('Username exists. Use changepassword to recover an existing account.')
        password=getpass.getpass('Password: ')
        if password!=getpass.getpass('Confirm password: '): raise CommandError('Passwords do not match.')
        try:
            user.full_clean(exclude=['password'])
            validate_password(password,user)
        except ValidationError as exc: raise CommandError('; '.join(exc.messages)) from exc
        with transaction.atomic():
            user.set_password(password); user.save()
            Profile.objects.create(user=user,role='admin')
            AuditEvent.objects.create(actor=user,action='account.bootstrap_admin',details={'username':user.username})
        self.stdout.write(self.style.SUCCESS('Portal administrator created. Sign in and configure institutions, policy and scoped accounts.'))
