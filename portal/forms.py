import csv
import io
import json
from decimal import Decimal
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Q
from .models import Instrument, Institution, Policy, Profile, Document, Role
from .access import scoped_institutions

class DateInput(forms.DateInput):
    input_type = 'date'

class InstrumentForm(forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    class Meta:
        model = Instrument
        fields = ['institution','kind','reference','bank_name','ifsc','amount','issue_date','maturity_date','validity_date','policy']
        widgets = {field:DateInput() for field in ['issue_date','maturity_date','validity_date']}
        labels = {'reference':'Instrument / transaction reference','ifsc':'Bank IFSC','amount':'Deposit amount (₹)','validity_date':'Separate validity deadline (if applicable)','policy':'Applicable policy'}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['institution'].queryset = scoped_institutions(user)
        self.fields['policy'].queryset = Policy.objects.filter(Q(is_active=True)|Q(pk=self.instance.policy_id))
        if self.instance.submitted_at:
            self.fields['policy'].disabled = True
            self.fields['institution'].disabled = True
        self.fields['amount'].widget.attrs.update(min='0.01',step='0.01')
        self.fields['expected_version'].initial = self.instance.version if self.instance.pk else 0
        self.fields['ifsc'].help_text = '11-character Indian Financial System Code; e.g. SBIN0000123.'
        self.fields['validity_date'].help_text = 'Only enter this when the instrument has a separate validity condition.'

class UploadForm(forms.Form):
    category = forms.ChoiceField(choices=[('instrument','Instrument / deposit confirmation'),('supporting','Supporting record'),('verification','Independent verification evidence'),('release','Release condition evidence'),('completion','Execution / payment evidence')])
    file = forms.FileField(help_text='PDF, JPEG or PNG, up to 10 MB. Submitted versions are preserved.',widget=forms.ClearableFileInput(attrs={'accept':'.pdf,.jpg,.jpeg,.png'}))
    def __init__(self,*args,role='',instrument=None,**kwargs):
        super().__init__(*args,**kwargs)
        allowed = []
        if role=='institution' and instrument:
            if instrument.status in ('draft','corrections'): allowed = ['instrument','supporting']
            elif instrument.status=='accepted' and not instrument.release_requests.filter(status__in=['recommended','approved']).exists(): allowed=['release']
        elif role=='reviewer' and instrument:
            if instrument.status in ('submitted','review','corrections','accepted'): allowed.append('verification')
            if instrument.release_requests.filter(status='approved').exists(): allowed.append('completion')
        self.fields['category'].choices = [c for c in self.fields['category'].choices if c[0] in allowed]

class ActionForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    note = forms.CharField(required=False,widget=forms.Textarea(attrs={'rows':3}),label='Scrutiny observation / decision reason')

class VerificationForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    outcome = forms.ChoiceField(choices=[('confirmed','Independently confirmed'),('discrepant','Discrepancy found'),('unable','Unable to verify')])
    source = forms.CharField(max_length=255,label='Bank / authorized financial source')
    method = forms.ChoiceField(choices=[('bank_portal','Authorized bank verification portal'),('direct_correspondence','Direct bank correspondence'),('bank_branch','Direct confirmation with issuing branch')])
    reference = forms.CharField(max_length=255,label='Independent confirmation reference')
    verified_on = forms.DateField(widget=DateInput,initial=timezone.localdate)
    evidence = forms.ModelChoiceField(queryset=Document.objects.none(),help_text='Upload independent verification evidence on the instrument first.')
    confirmed_reference = forms.CharField(max_length=255,required=False)
    confirmed_amount = forms.DecimalField(max_digits=18,decimal_places=2,min_value=Decimal('0.01'),required=False)
    confirmed_issue_date = forms.DateField(widget=DateInput,required=False)
    confirmed_maturity_date = forms.DateField(widget=DateInput,required=False)
    def __init__(self,*args,instrument,**kwargs):
        super().__init__(*args,**kwargs)
        self.instrument = instrument
        self.fields['evidence'].queryset = Document.objects.filter(instrument=instrument,category='verification')
        self.fields['expected_version'].initial = instrument.version
    def clean(self):
        data=super().clean()
        if data.get('outcome')=='confirmed':
            required=['confirmed_reference','confirmed_amount','confirmed_issue_date']+(['confirmed_maturity_date'] if self.instrument.kind=='fdr' else [])
            for field in required:
                if not data.get(field): self.add_error(field,'Required to confirm instrument details independently.')
        if data.get('verified_on') and data['verified_on']>timezone.localdate():
            self.add_error('verified_on','Verification cannot be dated in the future.')
        return data

class ReleaseForm(forms.Form):
    kind = forms.ChoiceField(choices=[('release','Release'),('discharge','Discharge'),('refund','Refund')])
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows':4}))
    amount = forms.DecimalField(max_digits=18,decimal_places=2,min_value=Decimal('0.01'),required=False,label='Refund amount (₹)',help_text='For refunds, the full principal is used when blank. This portal records external execution; it does not send money.')

class ReleaseActionForm(ActionForm):
    conditions = forms.MultipleChoiceField(required=False,widget=forms.CheckboxSelectMultiple,label='Conditions checked against supporting evidence')
    evidence = forms.ModelChoiceField(queryset=Document.objects.none(),required=False)
    execution_reference = forms.CharField(max_length=250,required=False,label='External execution / payment reference')
    execution_date = forms.DateField(widget=DateInput,required=False)
    def __init__(self,*args,case,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['conditions'].choices = [(str(c),str(c)) for c in case.policy.release_conditions]
        self.fields['evidence'].queryset = Document.objects.filter(instrument=case.instrument,category__in=['release','completion'])
        self.fields['expected_version'].initial = case.version

class ImportForm(forms.Form):
    file = forms.FileField(label='CSV file',help_text='UTF-8 CSV, maximum 200 rows / 1 MB. New records are imported as drafts; documents and scrutiny are still required.',widget=forms.ClearableFileInput(attrs={'accept':'.csv'}))

class InstitutionForm(forms.ModelForm):
    class Meta:
        model=Institution
        fields=['code','name','email','region']

class UserForm(forms.Form):
    username=forms.CharField(max_length=150)
    first_name=forms.CharField(max_length=150)
    email=forms.EmailField()
    password=forms.CharField(widget=forms.PasswordInput,help_text='At least 12 characters. Share the temporary password through a secure channel.')
    role=forms.ChoiceField(choices=Role.choices)
    institutions=forms.ModelMultipleChoiceField(queryset=Institution.objects.all(),required=False)
    def clean(self):
        data=super().clean()
        user=get_user_model()(username=data.get('username',''),email=data.get('email',''),first_name=data.get('first_name',''))
        if get_user_model().objects.filter(username=data.get('username')).exists(): self.add_error('username','Username already exists.')
        try: validate_password(data.get('password',''),user)
        except ValidationError as exc: self.add_error('password',exc)
        scopes=data.get('institutions',[])
        if data.get('role')=='institution' and len(scopes)!=1: self.add_error('institutions','Institution operators require exactly one institution.')
        if data.get('role') in ('reviewer','approver','auditor') and not scopes: self.add_error('institutions','Assign at least one institution; unassigned records are inaccessible.')
        return data

class AssignmentForm(forms.Form):
    user=forms.ModelChoiceField(queryset=get_user_model().objects.all())
    role=forms.ChoiceField(choices=Role.choices)
    institutions=forms.ModelMultipleChoiceField(queryset=Institution.objects.all(),required=False)
    is_active=forms.BooleanField(required=False,initial=True)
    def clean(self):
        data=super().clean()
        if data.get('role')=='institution' and len(data.get('institutions',[]))!=1:
            self.add_error('institutions','Institution operators require exactly one institution.')
        return data

class PolicyForm(forms.ModelForm):
    required_documents=forms.MultipleChoiceField(choices=[('instrument','Instrument / deposit confirmation'),('supporting','Additional supporting record')],initial=['instrument'],widget=forms.CheckboxSelectMultiple)
    conditions_text=forms.CharField(widget=forms.Textarea(attrs={'rows':3}),label='Release conditions (one per line)')
    reminders_text=forms.CharField(initial='90,60,30,7,1',label='Reminder days before deadline')
    class Meta:
        model=Policy
        fields=['name','version','reference','required_documents','review_sla_days','illustrative','is_active']
    def clean_reminders_text(self):
        try:
            days=sorted(set(int(x.strip()) for x in self.cleaned_data['reminders_text'].split(',')),reverse=True)
            if not days or any(x<0 or x>3650 for x in days): raise ValueError()
            return days
        except ValueError: raise ValidationError('Enter comma-separated whole days between 0 and 3650.')
    def clean_conditions_text(self):
        conditions=[x.strip() for x in self.cleaned_data['conditions_text'].splitlines() if x.strip()]
        if not conditions: raise ValidationError('At least one documented release condition is required.')
        return conditions
    def save(self,commit=True):
        policy=super().save(commit=False)
        policy.release_conditions=self.cleaned_data['conditions_text']
        policy.reminder_days=self.cleaned_data['reminders_text']
        if commit: policy.save()
        return policy
