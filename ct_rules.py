"""
Control Tower — booking rules and availability-grid computation.

Pure business logic: clinic definitions, the per-clinic "events to create"
scheme, and the exact 7-day / 15-minute availability grid the design shows.

Deliberately import-free of the app's chatbot/AI. It works on a NORMALISED
list of calendar events (built by admin.py from the real Google Calendar, or
injected as fakes in tests), so this module is unit-testable with no creds.

Normalised event shape:
    {'date': 'YYYY-MM-DD', 'start_min': int, 'end_min': int,
     'title': str, 'kind': 'session'|'chalk'|'busy', 'is_cst': bool}
"""
from datetime import datetime, timedelta

WORKING_DAYS = {0, 2, 3, 4}      # Mon, Wed, Thu, Fri (matches app.py)
GRID_START_MIN = 8 * 60          # axis top (08:00)
GRID_END_MIN = 21 * 60           # axis bottom (21:00)
WORK_START_MIN = 10 * 60         # first bookable start (10:00)
WORK_LAST_START_MIN = 20 * 60    # last session starts 20:00
SESSION_MIN = 60
STEP = 15
NON_CST_BUFFER = 90              # 1.5 h either side of non-CST events (matches app.py)
MIN_NOTICE_SEC = 24 * 3600

CLINICS = {
    'waterloo': {'label': 'Waterloo', 'price': '£80', 'gap': 0},
    'bethnal_green': {'label': 'Bethnal Green', 'price': '£30–60 sliding scale', 'gap': 15},
}


def fmt(minutes):
    return '%02d:%02d' % (minutes // 60, minutes % 60)


def clinic_label(clinic):
    return CLINICS.get(clinic, CLINICS['waterloo'])['label']


def clinic_price(clinic):
    return CLINICS.get(clinic, CLINICS['waterloo'])['price']


def events_to_create(clinic, name, start_min):
    """The calendar events a booking would create, mirroring the prototype.
    Waterloo → personal session + R5 room block; Bethnal → personal session
    inside a 2 h Chalk-Farm block."""
    name = name or 'Client'
    if clinic == 'bethnal_green':
        return [
            {'title': 'Phoenix', 'cal': 'Chalk Farm calendar · 2 h block',
             'range': fmt(start_min - 30) + '–' + fmt(start_min + 90), 'kind': 'chalk'},
            {'title': name + ' — Bethnal Green', 'cal': 'Personal calendar',
             'range': fmt(start_min) + '–' + fmt(start_min + 60), 'kind': 'session'},
        ]
    return [
        {'title': name + ' — Waterloo', 'cal': 'Personal calendar',
         'range': fmt(start_min) + '–' + fmt(start_min + 60), 'kind': 'session'},
        {'title': 'R5 - Phoenix', 'cal': 'Room calendar',
         'range': fmt(start_min) + '–' + fmt(start_min + 60), 'kind': 'room'},
    ]


def _kind_rank(k):
    return {'chalk': 3, 'session': 2, 'busy': 1}.get(k, 1)


def _merge_for_display(events):
    """Merge overlapping events into single display blocks (combined label)."""
    evs = sorted(events, key=lambda e: (e['start_min'], e['end_min']))
    merged = []
    for e in evs:
        if merged and e['start_min'] < merged[-1]['end_min']:
            last = merged[-1]
            last['end_min'] = max(last['end_min'], e['end_min'])
            if e.get('title') and e['title'] not in last['title']:
                last['title'] = last['title'] + ' + ' + e['title']
            if _kind_rank(e.get('kind')) > _kind_rank(last.get('kind')):
                last['kind'] = e.get('kind')
        else:
            merged.append(dict(e))
    return merged


def _bookable(start_min, clinic, events, date_str, now, tz):
    """Can a 60-min session start here? Respects working window, per-clinic gap,
    1.5 h buffer around non-CST events, and 24 h minimum notice."""
    if start_min < WORK_START_MIN or start_min > WORK_LAST_START_MIN:
        return False
    session_end = start_min + SESSION_MIN
    gap = CLINICS.get(clinic, CLINICS['waterloo'])['gap']
    for e in events:
        buf = gap if e.get('is_cst') else NON_CST_BUFFER
        if e['start_min'] < session_end + buf and e['end_min'] > start_min - buf:
            return False
    if now is not None and tz is not None:
        try:
            slot_dt = tz.localize(datetime.strptime(
                date_str + ' ' + fmt(start_min), '%Y-%m-%d %H:%M'))
            if (slot_dt - now).total_seconds() < MIN_NOTICE_SEC:
                return False
        except Exception:
            pass
    return True


def get_week_grid(clinic, start_date, events, now=None, tz=None):
    """Return 7 day dicts. Each open day has `blocks`: an ordered mix of
    {'type':'busy', ...labelled...} and {'type':'free', subslots:[...]} where
    each 15-min subslot carries a `free` boolean."""
    days = []
    for i in range(7):
        d = start_date + timedelta(days=i)
        date_str = d.strftime('%Y-%m-%d')
        label = d.strftime('%a %-d')
        if d.weekday() not in WORKING_DAYS:
            days.append({'label': label, 'date': date_str, 'closed': True, 'blocks': []})
            continue

        raw = []
        for e in events:
            if e.get('date') != date_str:
                continue
            s = max(e['start_min'], GRID_START_MIN)
            en = min(e['end_min'], GRID_END_MIN)
            if en > s:
                item = dict(e)
                item['start_min'], item['end_min'] = s, en
                raw.append(item)

        merged = _merge_for_display(raw)
        blocks = []
        m = GRID_START_MIN
        while m < GRID_END_MIN:
            covering = next((b for b in merged if b['start_min'] <= m < b['end_min']), None)
            if covering:
                blocks.append({
                    'type': 'busy',
                    'start_min': covering['start_min'], 'end_min': covering['end_min'],
                    'time': fmt(covering['start_min']) + '–' + fmt(covering['end_min']),
                    'title': covering.get('title') or 'Busy',
                    'kind': covering.get('kind', 'busy'),
                })
                m = covering['end_min']
                continue
            nxt = min([b['start_min'] for b in merged if b['start_min'] > m] + [GRID_END_MIN])
            subslots = []
            s = m
            while s < nxt:
                subslots.append({
                    'min': s, 'time': fmt(s),
                    'free': _bookable(s, clinic, raw, date_str, now, tz),
                })
                s += STEP
            if subslots:
                blocks.append({'type': 'free', 'start_min': m, 'end_min': nxt,
                               'subslots': subslots})
            m = nxt
        days.append({'label': label, 'date': date_str, 'closed': False, 'blocks': blocks})
    return days
