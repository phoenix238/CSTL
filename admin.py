"""
CSTL Control Tower — internal admin dashboard.

Mounted as a Flask Blueprint at /admin on the same app that serves the public
booking agent (app.py). Everything here is gated behind a single shared
password (env var ADMIN_PASSWORD). If that var is unset, the admin area
disables itself with a friendly message rather than crashing the app — the
public /chat booking flow must never be affected by admin problems.
"""
import hmac
import os
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect, url_for
)

from models import (
    db, Client, Session, Enquiry, get_setting, set_setting, DEFAULT_SETTINGS,
)
import ct_ai      # PLACEHOLDER AI (deterministic; swapped for real AI later)
import ct_rules   # booking rules + availability-grid computation

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '').strip()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def _admin_enabled():
    return bool(ADMIN_PASSWORD)


def _is_authed():
    return session.get('ct_admin') is True


@admin_bp.before_request
def _gate():
    # Static files and the login/logout endpoints are always reachable.
    if request.endpoint in ('admin.login', 'admin.do_login', 'admin.logout'):
        return None
    if not _admin_enabled():
        return render_template('admin_login.html', disabled=True), 503
    if not _is_authed():
        # API calls get JSON 401; page loads get redirected to login.
        if request.path.startswith('/admin/api/'):
            return jsonify({'error': 'unauthorized'}), 401
        return redirect(url_for('admin.login'))
    return None


@admin_bp.route('/login', methods=['GET'])
def login():
    if not _admin_enabled():
        return render_template('admin_login.html', disabled=True), 503
    if _is_authed():
        return redirect(url_for('admin.home'))
    return render_template('admin_login.html', disabled=False)


@admin_bp.route('/login', methods=['POST'])
def do_login():
    if not _admin_enabled():
        return render_template('admin_login.html', disabled=True), 503
    supplied = (request.form.get('password') or '').strip()
    if supplied and hmac.compare_digest(supplied, ADMIN_PASSWORD):
        session['ct_admin'] = True
        session.permanent = True
        return redirect(url_for('admin.home'))
    return render_template('admin_login.html', disabled=False, error=True), 401


@admin_bp.route('/logout')
def logout():
    session.pop('ct_admin', None)
    return redirect(url_for('admin.login'))


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------
@admin_bp.route('/')
def home():
    return render_template('admin.html')


# --------------------------------------------------------------------------
# Clients API
# --------------------------------------------------------------------------
CLIENT_FIELDS = [
    'name', 'email', 'phone', 'clinic', 'dob', 'occupation', 'doctor',
    'meds', 'conditions', 'emergency', 'referred', 'drive_url',
]


@admin_bp.route('/api/clients', methods=['GET'])
def list_clients():
    q = (request.args.get('q') or '').strip().lower()
    query = Client.query
    clients = query.order_by(Client.name.asc()).all()
    if q:
        clients = [
            c for c in clients
            if q in (c.name or '').lower() or q in (c.email or '').lower()
        ]
    return jsonify({'clients': [c.to_dict() for c in clients]})


@admin_bp.route('/api/clients', methods=['POST'])
def create_client():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    c = Client(name=name)
    for f in CLIENT_FIELDS:
        if f in data and f != 'name':
            setattr(c, f, (data.get(f) or '').strip())
    c.clinic = _normalise_clinic(data.get('clinic'))
    c.marketing_consent = bool(data.get('marketing_consent'))
    c.intake_complete = bool(data.get('intake_complete'))
    db.session.add(c)
    db.session.commit()
    return jsonify({'client': c.to_dict(include_sessions=True)}), 201


@admin_bp.route('/api/clients/<int:client_id>', methods=['GET'])
def get_client(client_id):
    c = db.session.get(Client, client_id)
    if not c:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'client': c.to_dict(include_sessions=True)})


@admin_bp.route('/api/clients/<int:client_id>', methods=['PUT'])
def update_client(client_id):
    c = db.session.get(Client, client_id)
    if not c:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(silent=True) or {}
    for f in CLIENT_FIELDS:
        if f in data:
            if f == 'clinic':
                c.clinic = _normalise_clinic(data.get('clinic'))
            else:
                setattr(c, f, (data.get(f) or '').strip())
    if 'marketing_consent' in data:
        c.marketing_consent = bool(data.get('marketing_consent'))
    if 'intake_complete' in data:
        c.intake_complete = bool(data.get('intake_complete'))
    db.session.commit()
    return jsonify({'client': c.to_dict(include_sessions=True)})


@admin_bp.route('/api/clients/<int:client_id>/sessions', methods=['POST'])
def add_session(client_id):
    c = db.session.get(Client, client_id)
    if not c:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(silent=True) or {}
    s = Session(
        client_id=c.id,
        date=(data.get('date') or datetime.utcnow().strftime('%Y-%m-%d')),
        clinic=_normalise_clinic(data.get('clinic') or c.clinic),
        raw_note=(data.get('raw_note') or ''),
        calendar_event_id=(data.get('calendar_event_id') or ''),
    )
    s.bullets = data.get('bullets') or []
    db.session.add(s)
    db.session.commit()
    return jsonify({'session': s.to_dict()}), 201


# --------------------------------------------------------------------------
# Today
# --------------------------------------------------------------------------
@admin_bp.route('/api/today')
def api_today():
    import pytz
    import app as main
    tz = pytz.timezone(main.TIMEZONE)
    today = datetime.now(tz)
    date_str = today.strftime('%Y-%m-%d')
    rows = []
    calendar_ok = True
    try:
        service = main.get_calendar_service()
        for ev in main.get_day_events(service, date_str):
            if not main.is_cst_event(ev):
                continue
            start = ev.get('start', {}).get('dateTime', '')
            if not start:
                continue
            t = datetime.fromisoformat(start)
            name = _parse_cst_name(ev.get('summary', ''))
            rows.append({
                'time': t.strftime('%H:%M'),
                'name': name,
                'clinic': main._infer_location(ev) or 'waterloo',
            })
        rows.sort(key=lambda r: r['time'])
    except Exception as e:  # pragma: no cover - needs live creds
        calendar_ok = False
        main.app.logger.error("today calendar read failed: %s", e)

    for r in rows:
        c = Client.query.filter(
            db.func.lower(Client.name) == r['name'].lower()).first()
        r['client_id'] = c.id if c else None
        r['isNew'] = c is None

    attn = []
    new_enq = Enquiry.query.filter_by(status='new').count()
    if new_enq:
        attn.append({
            'name': '%d new enquir%s' % (new_enq, 'ies' if new_enq > 1 else 'y'),
            'desc': 'Waiting in the inbox — nothing sent yet', 'tab': 'enquiries'})
    incomplete = Client.query.filter_by(intake_complete=False).count()
    if incomplete:
        attn.append({
            'name': '%d intake form%s outstanding' % (incomplete, 's' if incomplete > 1 else ''),
            'desc': 'Some client details are still missing', 'tab': 'clients'})

    hour = today.hour
    greeting = ('Good morning' if hour < 12 else
                'Good afternoon' if hour < 18 else 'Good evening')
    return jsonify({
        'greeting': greeting + ', Phoenix',
        'date_display': today.strftime('%A %-d %B'),
        'rows': rows,
        'attention': attn,
        'calendar_ok': calendar_ok,
    })


# --------------------------------------------------------------------------
# Calendar week
# --------------------------------------------------------------------------
@admin_bp.route('/api/calendar/week')
def api_calendar_week():
    import pytz
    import app as main
    tz = pytz.timezone(main.TIMEZONE)
    today = datetime.now(tz)
    start_str = request.args.get('start') or today.strftime('%Y-%m-%d')
    try:
        base = datetime.strptime(start_str, '%Y-%m-%d')
    except ValueError:
        base = today.replace(tzinfo=None)

    def fmt(evs, cal):
        out = []
        for ev in evs:
            s = ev.get('start', {}).get('dateTime', '')
            if not s:
                continue
            st = datetime.fromisoformat(s)
            out.append({'time': st.strftime('%H:%M'),
                        'title': ev.get('summary', '(busy)'), 'cal': cal})
        return out

    days = []
    calendar_ok = True
    try:
        service = main.get_calendar_service()
        for i in range(7):
            d = base + timedelta(days=i)
            dstr = d.strftime('%Y-%m-%d')
            day_start = tz.localize(datetime.strptime(dstr + ' 00:00', '%Y-%m-%d %H:%M'))
            day_end = day_start + timedelta(days=1)
            personal = main.get_events_from_calendar(
                service, main.CALENDAR_ID, day_start.isoformat(), day_end.isoformat())
            room = []
            if main.CHALK_FARM_CALENDAR_ID:
                room = main.get_events_from_calendar(
                    service, main.CHALK_FARM_CALENDAR_ID,
                    day_start.isoformat(), day_end.isoformat())
            events = fmt(personal, 'personal') + fmt(room, 'room')
            events.sort(key=lambda e: e['time'])
            days.append({
                'label': d.strftime('%a %-d'),
                'date': dstr,
                'is_today': dstr == today.strftime('%Y-%m-%d'),
                'events': events,
            })
    except Exception as e:  # pragma: no cover - needs live creds
        calendar_ok = False
        main.app.logger.error("calendar week read failed: %s", e)
    return jsonify({'days': days, 'calendar_ok': calendar_ok})


# --------------------------------------------------------------------------
# Availability grid (reuses find_available_slots for correct buffer logic)
# --------------------------------------------------------------------------
@admin_bp.route('/api/availability/week')
def api_availability_week():
    import pytz
    import app as main
    clinic = _normalise_clinic(request.args.get('clinic'))
    tz = pytz.timezone(main.TIMEZONE)
    now = datetime.now(tz)
    start_str = request.args.get('start') or now.strftime('%Y-%m-%d')
    try:
        base = datetime.strptime(start_str, '%Y-%m-%d')
    except ValueError:
        base = datetime.strptime(now.strftime('%Y-%m-%d'), '%Y-%m-%d')

    calendar_ok = True
    events = []
    try:
        events = _normalise_gcal_events(main, base, 7, tz)
    except Exception as e:  # pragma: no cover - needs live creds
        calendar_ok = False
        main.app.logger.error("availability calendar read failed: %s", e)

    grid = ct_rules.get_week_grid(clinic, base, events, now=now, tz=tz)
    return jsonify({
        'days': grid, 'clinic': clinic, 'calendar_ok': calendar_ok,
        'price': ct_rules.clinic_price(clinic),
        'clinic_label': ct_rules.clinic_label(clinic),
    })


def _normalise_gcal_events(main, base_date, num_days, tz):
    """Read the real Google Calendar(s) and normalise events into the shape
    ct_rules expects. A CT_FAKE_CALENDAR=1 env var injects demo events so the
    grid can be verified locally without credentials."""
    if os.environ.get('CT_FAKE_CALENDAR') == '1':
        return _fake_events(base_date, num_days)

    start = tz.localize(datetime.strptime(
        base_date.strftime('%Y-%m-%d') + ' 00:00', '%Y-%m-%d %H:%M'))
    end = start + timedelta(days=num_days)
    service = main.get_calendar_service()
    raw = main.get_events_from_calendar(
        service, main.CALENDAR_ID, start.isoformat(), end.isoformat())
    if main.CHALK_FARM_CALENDAR_ID:
        chalk = main.get_events_from_calendar(
            service, main.CHALK_FARM_CALENDAR_ID, start.isoformat(), end.isoformat())
        for ev in chalk:
            ev['_chalk'] = True
        raw = raw + chalk

    out = []
    for ev in raw:
        s = ev.get('start', {}).get('dateTime', '')
        e = ev.get('end', {}).get('dateTime', '')
        if not s or not e:
            continue
        sd = datetime.fromisoformat(s)
        ed = datetime.fromisoformat(e)
        date_str = sd.strftime('%Y-%m-%d')
        start_min = sd.hour * 60 + sd.minute
        end_min = ed.hour * 60 + ed.minute
        if ed.strftime('%Y-%m-%d') != date_str:
            end_min = 24 * 60
        is_cst = main.is_cst_event(ev)
        if ev.get('_chalk'):
            kind, title = 'chalk', (ev.get('summary') or 'Chalk Farm')
        elif is_cst:
            kind, title = 'session', _parse_cst_name(ev.get('summary', ''))
        else:
            kind, title = 'busy', 'Busy'
        out.append({'date': date_str, 'start_min': start_min, 'end_min': end_min,
                    'title': title or 'Busy', 'kind': kind, 'is_cst': is_cst})
    return out


def _fake_events(base_date, num_days):
    """Dev-only demo events (CT_FAKE_CALENDAR=1) so the grid renders locally."""
    ev = []
    for i in range(num_days):
        d = base_date + timedelta(days=i)
        if d.weekday() not in ct_rules.WORKING_DAYS:
            continue
        ds = d.strftime('%Y-%m-%d')
        ev.append({'date': ds, 'start_min': 600, 'end_min': 660,
                   'title': 'Maya — Waterloo', 'kind': 'session', 'is_cst': True})
        ev.append({'date': ds, 'start_min': 900, 'end_min': 960,
                   'title': 'Busy', 'kind': 'busy', 'is_cst': False})
    return ev


# --------------------------------------------------------------------------
# Booking (reuses create_booking_request from app.py)
# --------------------------------------------------------------------------
@admin_bp.route('/api/book', methods=['POST'])
def api_book():
    import app as main
    data = request.get_json(silent=True) or {}
    date = (data.get('date') or '').strip()
    time = (data.get('time') or '').strip()
    clinic = _normalise_clinic(data.get('clinic'))
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip()
    if not (date and time and name):
        return jsonify({'error': 'date, time and name are required'}), 400

    calendar_result = {}
    try:
        calendar_result = main.create_booking_request(date, time, clinic, name, email)
    except Exception as e:  # pragma: no cover - needs live creds
        calendar_result = {'success': False, 'error': str(e)}

    client = Client.query.filter(
        db.func.lower(Client.name) == name.lower()).first()
    created_client = False
    if not client:
        client = Client(name=name, email=email, clinic=clinic, intake_complete=False)
        db.session.add(client)
        created_client = True
    elif email and not client.email:
        client.email = email
    db.session.flush()

    s = Session(client_id=client.id, date=date, clinic=clinic,
                calendar_event_id=(calendar_result.get('event_id') or ''))
    db.session.add(s)

    enq_id = data.get('enquiry_id')
    if enq_id:
        enq = db.session.get(Enquiry, enq_id)
        if enq:
            enq.status = 'booked'
            enq.client_id = client.id
    db.session.commit()

    tmpl = get_setting('email_template_' + clinic, '')
    access = get_setting('access_note', '')
    email_text = tmpl.replace('{name}', name).replace('{accessNote}', access)
    return jsonify({
        'success': calendar_result.get('success', False),
        'calendar': calendar_result,
        'client_id': client.id,
        'created_client': created_client,
        'email_text': email_text,
        'confirmed_time': calendar_result.get('confirmed_time', ''),
    })


# --------------------------------------------------------------------------
# Enquiries
# --------------------------------------------------------------------------
@admin_bp.route('/api/enquiries', methods=['GET'])
def api_list_enquiries():
    status = request.args.get('status')
    query = Enquiry.query
    if status:
        query = query.filter_by(status=status)
    else:
        query = query.filter(Enquiry.status != 'dismissed')
    items = query.order_by(Enquiry.created_at.desc()).all()
    return jsonify({'enquiries': [e.to_dict() for e in items],
                    'new_count': Enquiry.query.filter_by(status='new').count()})


@admin_bp.route('/api/enquiries', methods=['POST'])
def api_create_enquiry():
    data = request.get_json(silent=True) or {}
    text = (data.get('raw_text') or '').strip()
    if not text:
        return jsonify({'error': 'raw_text required'}), 400
    e = Enquiry(
        raw_text=text,
        name=(data.get('name') or '').strip(),
        via=(data.get('via') or 'WhatsApp'),
        preview=text[:120],
        status='new',
    )
    db.session.add(e)
    db.session.commit()
    return jsonify({'enquiry': e.to_dict()}), 201


@admin_bp.route('/api/enquiries/<int:eid>', methods=['PUT'])
def api_update_enquiry(eid):
    e = db.session.get(Enquiry, eid)
    if not e:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'status' in data:
        e.status = data['status']
    if 'name' in data:
        e.name = (data.get('name') or '').strip()
    db.session.commit()
    return jsonify({'enquiry': e.to_dict()})


@admin_bp.route('/api/enquiries/analyse', methods=['POST'])
def api_analyse():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text required'}), 400
    result = ct_ai.analyse_enquiry(text)          # PLACEHOLDER AI
    if result.get('clinic'):
        result['clinic'] = _normalise_clinic(result['clinic'])
    return jsonify(result)


# --------------------------------------------------------------------------
# Note summarise (PLACEHOLDER AI)
# --------------------------------------------------------------------------
@admin_bp.route('/api/summarise', methods=['POST'])
def api_summarise():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text required'}), 400
    return jsonify({'bullets': ct_ai.summarise_note(text)})   # PLACEHOLDER AI


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
SETTING_KEYS = ['access_note', 'email_template_waterloo', 'email_template_bethnal_green']


@admin_bp.route('/api/settings', methods=['GET'])
def api_get_settings():
    return jsonify({k: get_setting(k) for k in SETTING_KEYS})


@admin_bp.route('/api/settings', methods=['PUT'])
def api_put_settings():
    data = request.get_json(silent=True) or {}
    for k in SETTING_KEYS:
        if k in data:
            set_setting(k, data.get(k) or '')
    return jsonify({'ok': True})


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------
@admin_bp.route('/api/import', methods=['POST'])
def api_import():
    import csv
    import io
    data = request.get_json(silent=True) or {}
    csv_text = data.get('csv') or ''
    if not csv_text.strip():
        return jsonify({'error': 'no CSV provided'}), 400
    created, skipped, rows = 0, 0, []
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        for raw in reader:
            row = {(k or '').strip().lower(): (v or '').strip()
                   for k, v in raw.items()}
            name = row.get('name', '')
            if not name:
                skipped += 1
                continue
            existing = Client.query.filter(
                db.func.lower(Client.name) == name.lower()).first()
            if existing:
                skipped += 1
                rows.append({'name': name, 'action': 'skipped (already exists)'})
                continue
            c = Client(name=name, email=row.get('email', ''),
                       phone=row.get('phone', ''),
                       clinic=_normalise_clinic(row.get('clinic')))
            db.session.add(c)
            created += 1
            rows.append({'name': name, 'action': 'added'})
        db.session.commit()
    except Exception as e:
        return jsonify({'error': 'Could not read that CSV: %s' % e}), 400
    return jsonify({'created': created, 'skipped': skipped, 'rows': rows})


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_cst_name(summary):
    s = summary or ''
    for prefix in ('PENDING: CST —', 'PENDING: CST -', 'CST —', 'CST -',
                   'PENDING:'):
        if prefix in s:
            return s.split(prefix, 1)[1].strip(' —-')
    return s.strip()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _normalise_clinic(value):
    """Map the prototype's clinic labels to the app.py canonical keys."""
    v = (value or '').strip().lower()
    if v in ('bethnal', 'bethnal_green', 'bethnal green', 'bethnalgreen'):
        return 'bethnal_green'
    return 'waterloo'
