import csv
import io
import secrets
import hashlib
from datetime import timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from . import services
from .access import scoped_instruments, scoped_institutions, require_role
from .documents import upload_document, read_document
from .forms import (InstrumentForm, UploadForm, ActionForm, VerificationForm, ReleaseForm,
    ReleaseActionForm, ImportForm, InstitutionForm, UserForm, PolicyForm, AssignmentForm)
from .models import (Instrument, Institution, Policy, Profile, Document, Submission, Observation,
    Verification, ReleaseRequest, AuditEvent, Notification, WorkerHeartbeat)
from .reporting import metrics_for, filtered_instruments

INSTRUMENT_FIELDS = ['institution','kind','reference','bank_name','ifsc','amount','issue_date','maturity_date','validity_date','policy']
CSV_FIELDS = ['institution_code','kind','reference','bank_name','ifsc','amount','issue_date','maturity_date','validity_date']

def role(user):
    profile=Profile.objects.filter(user=user).first()
    return profile.role if profile else ''

def instrument_for(user,pk):
    return get_object_or_404(scoped_instruments(user).select_related('institution','policy','predecessor'),pk=pk)

def show_error(request,exc):
    if isinstance(exc,ValidationError):
        text=' '.join(exc.messages)
    elif isinstance(exc,IntegrityError):
        text='This change conflicts with an existing record or another action. Refresh and try again.'
    else:
        text='Another change is in progress. Refresh the record and try again.'
    messages.error(request,text)

def audit(user,action,**details):
    AuditEvent.objects.create(actor=user,action=action,details=details)

def landing(request):
    if request.user.is_authenticated:
        return redirect('portal:dashboard')
    return render(request,'portal/landing.html')

@login_required
def dashboard(request):
    if role(request.user)=='admin': return redirect('portal:administration')
    qs=scoped_instruments(request.user)
    today=timezone.localdate()
    attention=qs.filter(Q(status__in=['submitted','review','corrections'])|Q(status='accepted',maturity_date__lte=today+timedelta(days=90))|Q(status='accepted',validity_date__lte=today+timedelta(days=90))).select_related('institution').order_by('maturity_date','submitted_at')[:8]
    events=AuditEvent.objects.filter(institution__in=scoped_institutions(request.user)).select_related('actor','instrument').order_by('-created_at')[:6]
    return render(request,'portal/dashboard.html',{'metrics':metrics_for(qs),'attention':attention,'recent_events':events,'heartbeat':WorkerHeartbeat.objects.filter(name='deadlines').first() or WorkerHeartbeat.objects.first()})

@login_required
def instrument_list(request):
    qs=filtered_instruments(scoped_instruments(request.user),request.GET).select_related('institution').order_by('-created_at')
    return render(request,'portal/instrument_list.html',{'page_obj':Paginator(qs,25).get_page(request.GET.get('page')),'filters':request.GET,'institutions':scoped_institutions(request.user)})

@login_required
def instrument_new(request,pk=None):
    require_role(request.user,'institution')
    predecessor=instrument_for(request.user,pk) if pk else None
    initial={}
    if predecessor:
        initial={field:getattr(predecessor,field) for field in ['institution','kind','bank_name','ifsc','amount','policy']}
    else:
        initial={'institution':scoped_institutions(request.user).first(),'policy':Policy.objects.filter(is_active=True).order_by('-pk').first(),'issue_date':timezone.localdate()}
    form=InstrumentForm(request.POST or None,user=request.user,initial=initial)
    if request.method=='POST' and form.is_valid():
        try:
            instrument=services.create_instrument(request.user,{k:form.cleaned_data[k] for k in INSTRUMENT_FIELDS},predecessor=predecessor)
            messages.success(request,'Draft created. Upload the required documents, then submit for review.')
            return redirect('portal:instrument_detail',pk=instrument.pk)
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    return render(request,'portal/instrument_form.html',{'form':form,'title':'Renew or replace instrument' if predecessor else 'Register an instrument','predecessor':predecessor})

@login_required
def instrument_edit(request,pk):
    instrument=instrument_for(request.user,pk)
    require_role(request.user,'institution')
    if instrument.status not in ('draft','corrections'): raise PermissionDenied('Only drafts and returned submissions can be edited.')
    form=InstrumentForm(request.POST or None,user=request.user,instance=instrument)
    if request.method=='POST' and form.is_valid():
        try:
            services.edit_instrument(request.user,instrument,{k:form.cleaned_data[k] for k in INSTRUMENT_FIELDS},form.cleaned_data['expected_version'])
            messages.success(request,'Draft updated. Earlier submitted revisions remain in the history.')
            return redirect('portal:instrument_detail',pk=pk)
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    return render(request,'portal/instrument_form.html',{'form':form,'title':'Edit instrument','instrument':instrument})

@login_required
def instrument_detail(request,pk):
    instrument=instrument_for(request.user,pk)
    current_role=role(request.user)
    can_edit=current_role=='institution' and instrument.status in ('draft','corrections')
    upload_form=UploadForm(role=current_role,instrument=instrument)
    return render(request,'portal/instrument_detail.html',{
        'instrument':instrument,'documents':Document.objects.filter(instrument=instrument).select_related('uploaded_by'),
        'submissions':Submission.objects.filter(instrument=instrument).order_by('-number'),
        'observations':Observation.objects.filter(instrument=instrument).select_related('actor').order_by('-created_at'),
        'verifications':Verification.objects.filter(instrument=instrument).select_related('actor','evidence').order_by('-created_at'),
        'audit_events':AuditEvent.objects.filter(instrument=instrument).select_related('actor').order_by('-created_at')[:50],
        'release_cases':ReleaseRequest.objects.filter(instrument=instrument).order_by('-created_at'),
        'can_edit':can_edit,'can_submit':can_edit,'can_review':current_role=='reviewer' and instrument.status in ('submitted','review'),
        'can_verify':current_role=='reviewer' and instrument.status in ('submitted','review','corrections','accepted'),
        'can_renew':current_role=='institution' and instrument.status=='accepted','can_release':current_role=='institution' and instrument.status=='accepted',
        'can_upload':bool(upload_form.fields['category'].choices),
        'upload_form':upload_form,'action_form':ActionForm(initial={'expected_version':instrument.version}),
        'verification_form':VerificationForm(instrument=instrument),
    })

@login_required
@require_POST
def instrument_submit(request,pk):
    instrument=instrument_for(request.user,pk)
    form=ActionForm(request.POST)
    if form.is_valid():
        try:
            services.submit_instrument(request.user,instrument,form.cleaned_data['expected_version'])
            messages.success(request,'Submitted for regulatory review. A permanent revision has been recorded.')
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    else: messages.error(request,'Invalid submission. Refresh the record and try again.')
    return redirect('portal:instrument_detail',pk=pk)

@login_required
@require_POST
def instrument_action(request,pk):
    instrument=instrument_for(request.user,pk)
    form=ActionForm(request.POST)
    if form.is_valid():
        try:
            if request.POST.get('action')=='withdraw':
                services.withdraw_instrument(request.user,instrument,form.cleaned_data['expected_version'])
            else:
                services.review_instrument(request.user,instrument,request.POST.get('action'),form.cleaned_data['note'],form.cleaned_data['expected_version'])
            messages.success(request,'Action recorded in the instrument history.')
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    else: messages.error(request,'Invalid action. Refresh the record and try again.')
    return redirect('portal:instrument_detail',pk=pk)

@login_required
@require_POST
def document_upload(request,pk):
    instrument=instrument_for(request.user,pk)
    form=UploadForm(request.POST,request.FILES,role=role(request.user),instrument=instrument)
    if form.is_valid():
        try:
            upload_document(request.user,instrument,form.cleaned_data['file'],form.cleaned_data['category'])
            messages.success(request,'Document validated and stored securely.')
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    else:
        messages.error(request,' '.join(error for errors in form.errors.values() for error in errors))
    return redirect('portal:instrument_detail',pk=pk)

@login_required
def document_download(request,pk):
    document=get_object_or_404(Document.objects.select_related('instrument'),pk=pk,instrument__in=scoped_instruments(request.user))
    try: content=read_document(document)
    except (ValidationError,OSError):
        messages.error(request,'The document is unavailable or failed its integrity check. Contact the administrator.')
        return redirect('portal:instrument_detail',pk=document.instrument_id)
    AuditEvent.objects.create(actor=request.user,institution=document.instrument.institution,instrument=document.instrument,action='document.downloaded',details={'document_id':str(document.pk)})
    from django.utils.http import content_disposition_header
    response=HttpResponse(content,content_type=document.mime_type)
    response['Content-Disposition']=content_disposition_header(True,document.original_name)
    response['Cache-Control']='private, no-store'
    response['X-Content-Type-Options']='nosniff'
    return response

@login_required
def verification(request,pk):
    require_role(request.user,'reviewer')
    instrument=instrument_for(request.user,pk)
    form=VerificationForm(request.POST or None,instrument=instrument)
    if request.method=='POST' and form.is_valid():
        data=form.cleaned_data
        confirmed={key.removeprefix('confirmed_'):str(value) for key,value in data.items() if key.startswith('confirmed_') and value is not None and value!=''}
        try:
            services.record_verification(request.user,instrument,outcome=data['outcome'],source=data['source'],method=data['method'],reference=data['reference'],verified_on=data['verified_on'],evidence=data['evidence'],confirmed_values=confirmed,expected_version=data['expected_version'])
            messages.success(request,'Independent verification evidence recorded separately from submitted information.')
            return redirect('portal:instrument_detail',pk=pk)
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    return render(request,'portal/generic_form.html',{'form':form,'title':'Record independent verification','description':'Use direct evidence from an authorized bank source. Institution-submitted documents alone do not establish independent confirmation.','submit_label':'Record verification','instrument':instrument})

@login_required
def release_new(request,pk):
    require_role(request.user,'institution')
    instrument=instrument_for(request.user,pk)
    form=ReleaseForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            case=services.start_release(request.user,instrument,**form.cleaned_data)
            messages.success(request,'Request submitted. Release remains subject to review and competent-authority approval.')
            return redirect('portal:release_detail',pk=case.pk)
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    return render(request,'portal/release_form.html',{'form':form,'instrument':instrument,'title':'Request release, discharge or refund'})

@login_required
def release_detail(request,pk):
    case=get_object_or_404(ReleaseRequest.objects.select_related('instrument','instrument__institution','policy','reviewer','approver'),pk=pk,instrument__in=scoped_instruments(request.user))
    current_role=role(request.user)
    return render(request,'portal/release_detail.html',{'case':case,'form':ReleaseActionForm(case=case),'documents':Document.objects.filter(instrument=case.instrument,category__in=['release','completion']),
        'can_recommend':current_role=='reviewer' and case.status=='submitted','can_decide':current_role=='approver' and case.status=='recommended',
        'can_complete':current_role=='reviewer' and case.status=='approved','can_resubmit':current_role=='institution' and case.status=='corrections',
        'can_withdraw':current_role=='institution' and case.status in ('submitted','corrections','recommended'),
        'audit_events':AuditEvent.objects.filter(instrument=case.instrument).select_related('actor').order_by('-created_at')[:30]})

@login_required
@require_POST
def release_action(request,pk):
    case=get_object_or_404(ReleaseRequest.objects.select_related('instrument','policy'),pk=pk,instrument__in=scoped_instruments(request.user))
    form=ReleaseActionForm(request.POST,case=case)
    if form.is_valid():
        data=form.cleaned_data
        try:
            services.transition_release(request.user,case,request.POST.get('action'),expected_version=data['expected_version'],note=data['note'],conditions={c:True for c in data['conditions']},evidence=data['evidence'],execution_reference=data['execution_reference'],execution_date=data['execution_date'])
            messages.success(request,'Request updated. Approval and completed execution are recorded separately.')
        except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    else: messages.error(request,' '.join(error for errors in form.errors.values() for error in errors))
    return redirect('portal:release_detail',pk=pk)

@login_required
def notifications(request):
    qs=Notification.objects.filter(user=request.user).filter(Q(instrument__isnull=True)|Q(instrument__in=scoped_instruments(request.user))).order_by('-created_at')
    return render(request,'portal/notifications.html',{'page_obj':Paginator(qs,25).get_page(request.GET.get('page')),'notifications':qs[:100]})

@login_required
@require_POST
def notification_read(request,pk):
    notification=get_object_or_404(Notification.objects.filter(Q(instrument__isnull=True)|Q(instrument__in=scoped_instruments(request.user))),user=request.user,pk=pk)
    Notification.objects.filter(pk=notification.pk,read_at__isnull=True).update(read_at=timezone.now())
    return redirect('portal:notifications')

@login_required
def reports(request):
    report_type=request.GET.get('report','pending')
    params=request.GET.copy(); params['report']=report_type
    if report_type=='releases':
        qs=ReleaseRequest.objects.filter(instrument__in=scoped_instruments(request.user)).select_related('instrument','instrument__institution').order_by('-created_at')
    else:
        qs=filtered_instruments(scoped_instruments(request.user),params).select_related('institution').order_by('-created_at')
    page=Paginator(qs,25).get_page(request.GET.get('page'))
    return render(request,'portal/reports.html',{'metrics':metrics_for(scoped_instruments(request.user)),'rows':page,'page_obj':page,'report_type':report_type,'filters':params})

def safe_csv(value):
    text='' if value is None else str(value)
    return "'"+text if text.lstrip().startswith(('=','+','-','@','\t','\r')) else text

@login_required
def export(request):
    require_role(request.user,'institution','reviewer','approver','auditor')
    response=HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition']='attachment; filename="fdr-register.csv"'
    response['Cache-Control']='private, no-store'
    response.write('\ufeff')
    writer=csv.writer(response)
    if request.GET.get('template'):
        writer.writerow(CSV_FIELDS)
        return response
    if request.GET.get('report')=='releases':
        writer.writerow(['institution_code','instrument_reference','request_type','status','refund_amount','requested_at','execution_reference','execution_date'])
        qs=ReleaseRequest.objects.filter(instrument__in=scoped_instruments(request.user)).select_related('instrument','instrument__institution')
        for case in qs.iterator():
            writer.writerow([safe_csv(value) for value in [case.instrument.institution.code,case.instrument.reference,case.kind,case.status,case.amount,case.created_at,case.execution_reference,case.execution_date]])
        audit(request.user,'records.exported',count=qs.count(),report='releases')
        return response
    writer.writerow(CSV_FIELDS+['administrative_status','temporal_status','bank_confirmation'])
    qs=filtered_instruments(scoped_instruments(request.user),request.GET).select_related('institution')
    for item in qs.iterator():
        writer.writerow([safe_csv(value) for value in [item.institution.code,item.kind,item.reference,item.bank_name,item.ifsc,item.amount,item.issue_date,item.maturity_date,item.validity_date,item.status,item.temporal_status,item.bank_status]])
    audit(request.user,'records.exported',count=qs.count(),report=request.GET.get('report','register'))
    return response

def import_rows(user,rows):
    parsed=[]
    errors=[]
    seen=set()
    policy=Policy.objects.filter(is_active=True).order_by('-pk').first()
    scopes={item.code:item for item in scoped_institutions(user)}
    for number,row in enumerate(rows,start=2):
        institution=scopes.get(row.get('institution_code','').strip())
        raw={key:row.get(key,'').strip() for key in CSV_FIELDS if key!='institution_code'}
        raw.update(institution=institution.pk if institution else '',policy=policy.pk if policy else '')
        form=InstrumentForm(raw,user=user)
        identity=(raw['ifsc'].upper(),raw['reference'].casefold(),raw['issue_date'])
        if identity in seen: errors.append(f'Row {number}: duplicate reference, issuing branch and issue date within this file.')
        seen.add(identity)
        if form.is_valid():
            data={key:form.cleaned_data[key] for key in INSTRUMENT_FIELDS}
            if Instrument.objects.filter(ifsc__iexact=data['ifsc'],reference__iexact=data['reference'],issue_date=data['issue_date']).exclude(status__in=['rejected','withdrawn']).exists():
                errors.append(f'Row {number}: an instrument with this issuing branch, reference and issue date is already registered. Use the existing record or its renewal workflow.')
            parsed.append(data)
        else:
            errors.append(f"Row {number}: "+' '.join(f'{field}: {error}' for field,items in form.errors.items() for error in items))
    return parsed,errors

@login_required
def import_records(request):
    require_role(request.user,'institution')
    form=ImportForm(request.POST or None,request.FILES or None)
    context={'form':form}
    if request.method=='POST' and request.POST.get('confirm'):
        preview=request.session.get('csv_preview',{})
        if not preview or not secrets.compare_digest(str(request.POST.get('token','')),preview.get('token','')) or timezone.now().timestamp()-preview.get('time',0)>1800:
            messages.error(request,'Import preview expired. Upload the file again.')
        else:
            parsed,errors=import_rows(request.user,preview['rows'])
            if not errors:
                try:
                    with transaction.atomic():
                        # Lock a stable row to serialize replayed confirmations on PostgreSQL;
                        # SQLite uses IMMEDIATE transactions for the same guarantee.
                        get_user_model().objects.select_for_update().get(pk=request.user.pk)
                        digest=hashlib.sha256(preview['token'].encode()).hexdigest()
                        if AuditEvent.objects.filter(actor=request.user,action='records.imported',details__token_digest=digest).exists():
                            raise ValidationError('This import has already been completed.')
                        parsed,current_errors=import_rows(request.user,preview['rows'])
                        if current_errors: raise ValidationError(current_errors)
                        for data in parsed: services.create_instrument(request.user,data)
                        audit(request.user,'records.imported',count=len(parsed),token_digest=digest)
                    request.session.pop('csv_preview',None)
                    messages.success(request,f'{len(parsed)} draft records imported. Upload documents and submit each record for review.')
                    return redirect('portal:instruments')
                except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
            context.update(errors=errors,preview=preview.get('rows'),token=preview.get('token'))
    elif request.method=='POST' and form.is_valid():
        upload=form.cleaned_data['file']
        try:
            if upload.size>1024*1024: raise ValueError('CSV files must be at most 1 MB.')
            reader=csv.DictReader(io.StringIO(upload.read().decode('utf-8-sig')))
            if set(reader.fieldnames or [])!=set(CSV_FIELDS) or len(reader.fieldnames or [])!=len(CSV_FIELDS): raise ValueError('CSV headers must match the downloadable template exactly.')
            rows=list(reader)
            if not 1<=len(rows)<=200: raise ValueError('Import between 1 and 200 rows at a time.')
            if any(None in row or any(value is None for value in row.values()) for row in rows): raise ValueError('Every row must have the same number of columns as the header.')
            _,errors=import_rows(request.user,rows)
            token=secrets.token_urlsafe(24)
            request.session['csv_preview']={'rows':rows,'token':token,'time':timezone.now().timestamp()}
            context.update(preview=rows,errors=errors,token=token)
        except (UnicodeError,ValueError,csv.Error) as exc: form.add_error('file',str(exc))
    return render(request,'portal/import.html',context)

@login_required
def administration(request):
    require_role(request.user,'admin')
    forms={'user':UserForm(auto_id='user_%s'),'institution':InstitutionForm(auto_id='institution_%s'),'policy':PolicyForm(auto_id='policy_%s'),'assignment':AssignmentForm(auto_id='assignment_%s')}
    kind=request.POST.get('form_type')
    if request.method=='POST' and kind=='retire_policy':
        try: policy_id=int(request.POST.get('policy_id',''))
        except (TypeError,ValueError): return HttpResponse('A valid policy identifier is required.',status=400)
        with transaction.atomic():
            policy=get_object_or_404(Policy.objects.select_for_update(),pk=policy_id)
            policy.is_active=False; policy.save(update_fields=['is_active'])
            audit(request.user,'policy.retired',object_id=policy.pk)
        messages.success(request,'Policy retired for new cases. Existing cases keep their recorded requirements.')
        return redirect('portal:administration')
    if request.method=='POST' and kind in forms:
        form=type(forms[kind])(request.POST,auto_id=kind+'_%s')
        forms[kind]=form
        if form.is_valid():
            try:
                with transaction.atomic():
                    data=form.cleaned_data
                    if kind=='user':
                        user=get_user_model().objects.create_user(username=data['username'],email=data['email'],first_name=data['first_name'],password=data['password'])
                        profile=Profile.objects.create(user=user,role=data['role'])
                        profile.institutions.set(data['institutions'])
                        audit(request.user,'account.created',username=user.username,role=profile.role)
                    elif kind=='assignment':
                        user=data['user']
                        if user.pk==request.user.pk: raise ValidationError('Use a different administrator to change your own access.')
                        profile,_=Profile.objects.get_or_create(user=user)
                        profile.role=data['role']; profile.save()
                        profile.institutions.set(data['institutions'])
                        user.is_active=data['is_active']; user.save(update_fields=['is_active'])
                        audit(request.user,'account.access_changed',username=user.username,role=profile.role,active=user.is_active,institutions=list(data['institutions'].values_list('code',flat=True)))
                    else:
                        obj=form.save()
                        audit(request.user,f'{kind}.created',object_id=str(obj.pk))
                messages.success(request,'Administration change saved and audited.')
                return redirect('portal:administration')
            except (ValidationError,IntegrityError,OperationalError) as exc: show_error(request,exc)
    return render(request,'portal/administration.html',{'institutions':Institution.objects.all(),'profiles':Profile.objects.select_related('user').prefetch_related('institutions'),'policies':Policy.objects.order_by('-pk'),**{key+'_form':value for key,value in forms.items()}})

@login_required
def audit_log(request):
    require_role(request.user,'reviewer','approver','auditor','admin')
    if role(request.user)=='admin': qs=AuditEvent.objects.filter(institution__isnull=True)
    else: qs=AuditEvent.objects.filter(institution__in=scoped_institutions(request.user))
    qs=qs.select_related('actor','institution','instrument').order_by('-created_at')
    return render(request,'portal/audit.html',{'page_obj':Paginator(qs,50).get_page(request.GET.get('page'))})

def health(request):
    try:
        with connection.cursor() as cursor: cursor.execute('SELECT 1')
    except Exception:
        return JsonResponse({'status':'unavailable'},status=503)
    return JsonResponse({'status':'ok'})
