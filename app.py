from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import pytz
import json
import os
import smtplib
from email.mime.text import MIMEText

app = Flask(__name__)
CORS(app)

# Session cookie signing for the /admin area. Falls back to a dev key so the
# public app still boots if SECRET_KEY is unset (admin login won't be secure
# until a real SECRET_KEY is set on Render).
app.secret_key = os.environ.get('SECRET_KEY', 'dev-insecure-change-me')
app.permanent_session_lifetime = timedelta(days=30)

# --- CSTL Control Tower admin dashboard -----------------------------------
# Loaded in a try/except so any DB/import problem degrades the admin area
# gracefully and never crashes the public booking agent (/, /chat, /health).
try:
    from models import init_db
    from admin import admin_bp
    import ct_seed
    init_db(app)
    ct_seed.seed_if_empty(app)
    app.register_blueprint(admin_bp)
except Exception as _ct_err:  # pragma: no cover - defensive
    app.logger.error("Control Tower admin failed to load: %s", _ct_err)

CALENDAR_ID = 'phoenix@tanner.me'
CHALK_FARM_CALENDAR_ID = os.environ.get('CHALK_FARM_CALENDAR_ID', '')
OWNER_EMAIL = 'phoenix@tanner.me'
SCOPES = ['https://www.googleapis.com/auth/calendar']
TIMEZONE = 'Europe/London'
SESSION_DURATION = 60

anthropic_client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

LOCATIONS = {
    'waterloo': {
        'name': 'Waterloo / Blackfriars',
        'price': '£80',
        'gap_minutes': 0,
    },
    'bethnal_green': {
        'name': 'Bethnal Green Community Clinic',
        'price': '£30-60 (sliding scale)',
        'gap_minutes': 15,
    }
}


def get_calendar_service():
    credentials_info = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info, scopes=SCOPES
    )
    return build('calendar', 'v3', credentials=credentials)


def is_all_day_event(event):
    """All-day events use 'date' not 'dateTime' on their start field."""
    return 'date' in event.get('start', {}) and 'dateTime' not in event.get('start', {})


def all_day_blocks_date(event, date_str):
    """Return True if an all-day event covers the given date."""
    if not is_all_day_event(event):
        return False
    start_date = event['start'].get('date', '')
    end_date = event['end'].get('date', '')  # exclusive
    return start_date <= date_str < end_date


def get_events_from_calendar(service, calendar_id, time_min, time_max):
    try:
        return service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute().get('items', [])
    except Exception:
        return []


def get_day_events(service, date_str):
    tz = pytz.timezone(TIMEZONE)
    day_start = tz.localize(datetime.strptime(f"{date_str} 00:00", "%Y-%m-%d %H:%M"))
    day_end = day_start + timedelta(days=1)
    time_min = day_start.isoformat()
    time_max = day_end.isoformat()
    events = get_events_from_calendar(service, CALENDAR_ID, time_min, time_max)
    if CHALK_FARM_CALENDAR_ID:
        events += get_events_from_calendar(service, CHALK_FARM_CALENDAR_ID, time_min, time_max)
    return events


def is_date_fully_blocked(service, date_str):
    """Return True if any all-day event covers the entire date on either calendar."""
    tz = pytz.timezone(TIMEZONE)
    window_start = tz.localize(datetime.strptime(f"{date_str} 00:00", "%Y-%m-%d %H:%M")) - timedelta(days=30)
    window_end = tz.localize(datetime.strptime(f"{date_str} 23:59", "%Y-%m-%d %H:%M"))
    time_min = window_start.isoformat()
    time_max = window_end.isoformat()
    events = get_events_from_calendar(service, CALENDAR_ID, time_min, time_max)
    if CHALK_FARM_CALENDAR_ID:
        events += get_events_from_calendar(service, CHALK_FARM_CALENDAR_ID, time_min, time_max)
    return any(all_day_blocks_date(ev, date_str) for ev in events)


def is_first_cst_of_day(service, date_str, time_str):
    tz = pytz.timezone(TIMEZONE)
    requested_start = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
    for event in get_day_events(service, date_str):
        summary = event.get('summary', '')
        if 'CST' in summary or 'PENDING' in summary:
            event_start_str = event['start'].get('dateTime', '')
            if event_start_str:
                event_start = datetime.fromisoformat(event_start_str)
                if event_start < requested_start:
                    return False
    return True


def has_location_conflict(service, date_str, time_str, location):
    tz = pytz.timezone(TIMEZONE)
    requested_start = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
    requested_end = requested_start + timedelta(minutes=SESSION_DURATION)

    other_keywords = {
        'waterloo': ['bethnal', 'community'],
        'bethnal_green': ['waterloo', 'blackfriars'],
    }
    keywords = other_keywords.get(location, [])

    for event in get_day_events(service, date_str):
        text = (event.get('summary', '') + event.get('description', '') + event.get('location', '')).lower()
        if any(kw in text for kw in keywords):
            start_str = event['start'].get('dateTime', '')
            end_str = event['end'].get('dateTime', '')
            if start_str and end_str:
                ev_start = datetime.fromisoformat(start_str)
                ev_end = datetime.fromisoformat(end_str)
                gap_after = (requested_start - ev_end).total_seconds() / 3600
                gap_before = (ev_start - requested_end).total_seconds() / 3600
                if -0.1 < gap_after < 2 or -0.1 < gap_before < 2:
                    return True
    return False


def check_time_slot(date_str, time_str, location):
    try:
        tz = pytz.timezone(TIMEZONE)
        now = datetime.now(tz)
        start = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
        end = start + timedelta(minutes=SESSION_DURATION)

        if start.hour < 10 or start.hour > 20:
            return {"available": False, "reason": "outside_hours"}

        if (start - now).total_seconds() < 86400:
            return {"available": False, "reason": "too_soon"}

        if (start - now).days > 90:
            return {"available": False, "reason": "too_far_ahead"}

        service = get_calendar_service()

        if is_date_fully_blocked(service, date_str):
            return {"available": False, "reason": "already_booked"}

        if has_location_conflict(service, date_str, time_str, location):
            return {"available": False, "reason": "location_conflict"}

        cst_gap = timedelta(minutes=LOCATIONS[location]['gap_minutes'])
        other_gap = timedelta(minutes=90)

        wide_start = start - other_gap
        wide_end = end + other_gap

        events = get_events_from_calendar(service, CALENDAR_ID, wide_start.isoformat(), wide_end.isoformat())
        if CHALK_FARM_CALENDAR_ID:
            events += get_events_from_calendar(service, CHALK_FARM_CALENDAR_ID, wide_start.isoformat(), wide_end.isoformat())

        for event in events:
            ev_start_str = event['start'].get('dateTime', '')
            ev_end_str = event['end'].get('dateTime', '')
            if not ev_start_str or not ev_end_str:
                continue
            ev_start_dt = datetime.fromisoformat(ev_start_str)
            ev_end_dt = datetime.fromisoformat(ev_end_str)
            gap = cst_gap if is_cst_event(event) else other_gap
            if ev_start_dt < (end + gap) and ev_end_dt > (start - gap):
                return {"available": False, "reason": "already_booked"}

        return {"available": True}
    except Exception as e:
        return {"error": str(e)}


def is_cst_event(event):
    summary = event.get('summary', '')
    return 'CST' in summary or 'PENDING' in summary or 'Travel & Setup' in summary


def _infer_location(event):
    text = (event.get('summary', '') + event.get('description', '') + event.get('location', '')).lower()
    if any(kw in text for kw in ['bethnal', 'community']):
        return 'bethnal_green'
    if any(kw in text for kw in ['waterloo', 'blackfriars']):
        return 'waterloo'
    return None


def get_blocked_periods(start_date_str, end_date_str):
    """Return any fully-blocked date ranges (holidays/away weeks) within the given period."""
    try:
        tz = pytz.timezone(TIMEZONE)
        # Extend lookback window to catch multi-day events that started before the range
        window_start = tz.localize(datetime.strptime(f"{start_date_str} 00:00", "%Y-%m-%d %H:%M")) - timedelta(days=60)
        window_end = tz.localize(datetime.strptime(f"{end_date_str} 23:59", "%Y-%m-%d %H:%M"))

        service = get_calendar_service()
        events = get_events_from_calendar(service, CALENDAR_ID, window_start.isoformat(), window_end.isoformat())
        if CHALK_FARM_CALENDAR_ID:
            events += get_events_from_calendar(service, CHALK_FARM_CALENDAR_ID, window_start.isoformat(), window_end.isoformat())

        blocked_ranges = []
        for event in events:
            if not is_all_day_event(event):
                continue
            start_d = event['start'].get('date', '')
            end_d_exclusive = event['end'].get('date', '')
            if not start_d or not end_d_exclusive:
                continue

            # Google Calendar all-day end dates are exclusive, make inclusive
            end_d_inclusive = (datetime.strptime(end_d_exclusive, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

            # Skip if this event doesn't overlap with our range at all
            if end_d_inclusive < start_date_str or start_d > end_date_str:
                continue

            # Clip to requested range for display, but keep full dates too
            effective_start = max(start_d, start_date_str)
            effective_end = min(end_d_inclusive, end_date_str)

            full_start_dt = datetime.strptime(start_d, "%Y-%m-%d")
            full_end_dt = datetime.strptime(end_d_inclusive, "%Y-%m-%d")
            num_days = (full_end_dt - full_start_dt).days + 1

            blocked_ranges.append({
                "full_start": start_d,
                "full_end": end_d_inclusive,
                "display_full_start": f"{full_start_dt.strftime('%A')} {full_start_dt.day} {full_start_dt.strftime('%B')}",
                "display_full_end": f"{full_end_dt.strftime('%A')} {full_end_dt.day} {full_end_dt.strftime('%B')}",
                "duration_days": num_days,
                "first_available_after": (full_end_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
                "display_first_available_after": f"{(full_end_dt + timedelta(days=1)).strftime('%A')} {(full_end_dt + timedelta(days=1)).day} {(full_end_dt + timedelta(days=1)).strftime('%B')}"
            })

        return {"blocked_periods": blocked_ranges}
    except Exception as e:
        return {"error": str(e)}


def find_available_slots(start_date_str, end_date_str, location, max_results=4):
    try:
        tz = pytz.timezone(TIMEZONE)
        now = datetime.now(tz)
        service = get_calendar_service()

        range_start = tz.localize(datetime.strptime(f"{start_date_str} 00:00", "%Y-%m-%d %H:%M"))
        range_end = tz.localize(datetime.strptime(f"{end_date_str} 23:59", "%Y-%m-%d %H:%M"))

        all_events = get_events_from_calendar(service, CALENDAR_ID, range_start.isoformat(), range_end.isoformat())
        if CHALK_FARM_CALENDAR_ID:
            all_events += get_events_from_calendar(service, CHALK_FARM_CALENDAR_ID, range_start.isoformat(), range_end.isoformat())

        all_day_blocked_dates = set()
        for event in all_events:
            if is_all_day_event(event):
                start_d = event['start'].get('date', '')
                end_d = event['end'].get('date', '')
                if start_d and end_d:
                    cur = datetime.strptime(start_d, "%Y-%m-%d")
                    end_exc = datetime.strptime(end_d, "%Y-%m-%d")
                    while cur < end_exc:
                        all_day_blocked_dates.add(cur.strftime("%Y-%m-%d"))
                        cur += timedelta(days=1)

        events_by_date = {}
        cst_dates = {}
        for event in all_events:
            ev_start_str = event['start'].get('dateTime', '')
            if not ev_start_str:
                continue
            ev_date = datetime.fromisoformat(ev_start_str).strftime("%Y-%m-%d")
            events_by_date.setdefault(ev_date, []).append(event)
            summary = event.get('summary', '')
            if 'CST' in summary or 'PENDING' in summary:
                ev_loc = _infer_location(event)
                cst_dates.setdefault(ev_date, []).append(ev_loc)

        other_loc_keywords = {
            'waterloo': ['bethnal', 'community'],
            'bethnal_green': ['waterloo', 'blackfriars'],
        }
        gap = LOCATIONS[location]['gap_minutes']
        working_days = {0, 2, 3, 4}

        candidates = []
        current = datetime.strptime(start_date_str, "%Y-%m-%d")
        end = datetime.strptime(end_date_str, "%Y-%m-%d")

        while current <= end:
            if current.weekday() in working_days:
                date_str = current.strftime("%Y-%m-%d")

                if date_str in all_day_blocked_dates:
                    current += timedelta(days=1)
                    continue

                day_events = events_by_date.get(date_str, [])

                for hour in range(10, 21):
                    time_str = f"{hour:02d}:00"
                    slot_start = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
                    slot_end = slot_start + timedelta(minutes=SESSION_DURATION)

                    if (slot_start - now).total_seconds() < 86400:
                        continue
                    if (slot_start - now).days > 90:
                        continue

                    conflict = False
                    for ev in day_events:
                        ev_text = (ev.get('summary', '') + ev.get('description', '') + ev.get('location', '')).lower()
                        if any(kw in ev_text for kw in other_loc_keywords.get(location, [])):
                            s = ev['start'].get('dateTime', '')
                            e = ev['end'].get('dateTime', '')
                            if s and e:
                                es = datetime.fromisoformat(s)
                                ee = datetime.fromisoformat(e)
                                gap_a = (slot_start - ee).total_seconds() / 3600
                                gap_b = (es - slot_end).total_seconds() / 3600
                                if -0.1 < gap_a < 2 or -0.1 < gap_b < 2:
                                    conflict = True
                                    break
                    if conflict:
                        continue

                    cst_gap = timedelta(minutes=gap)
                    other_gap = timedelta(minutes=90)
                    overlaps = False
                    for ev in day_events:
                        if not ev['start'].get('dateTime') or not ev['end'].get('dateTime'):
                            continue
                        ev_start_dt = datetime.fromisoformat(ev['start']['dateTime'])
                        ev_end_dt = datetime.fromisoformat(ev['end']['dateTime'])
                        ev_gap = cst_gap if is_cst_event(ev) else other_gap
                        if ev_start_dt < (slot_end + ev_gap) and ev_end_dt > (slot_start - ev_gap):
                            overlaps = True
                            break
                    if overlaps:
                        continue

                    score = 0
                    if date_str in cst_dates:
                        score += 3
                        if location in cst_dates[date_str]:
                            score += 2
                    else:
                        prev = (current - timedelta(days=1)).strftime("%Y-%m-%d")
                        nxt = (current + timedelta(days=1)).strftime("%Y-%m-%d")
                        if prev in cst_dates or nxt in cst_dates:
                            score += 1

                    candidates.append((score, date_str, time_str))

            current += timedelta(days=1)

        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        slots = [
            {
                "date": d,
                "time": t,
                "display": f"{datetime.strptime(d, '%Y-%m-%d').strftime('%A')} {datetime.strptime(d, '%Y-%m-%d').day} {datetime.strptime(d, '%Y-%m-%d').strftime('%B')} at {datetime.strptime(t, '%H:%M').strftime('%I:%M%p').lstrip('0').lower()}"
            }
            for _, d, t in candidates[:max_results]
        ]
        return {"slots": slots, "total_found": len(candidates)}
    except Exception as e:
        return {"error": str(e)}


def send_notification_email(customer_name, customer_email, date_str, time_str, location_name, price):
    try:
        smtp_email = os.environ.get('SMTP_EMAIL')
        smtp_password = os.environ.get('SMTP_PASSWORD')
        if not smtp_email or not smtp_password:
            return

        tz = pytz.timezone(TIMEZONE)
        start = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
        day = start.strftime('%A')
        date_fmt = start.strftime('%-d %B')
        time_fmt = start.strftime('%-I:%M%p').lower()

        body = f"""New booking request — please confirm within 24 hours.

Client: {customer_name}
Email: {customer_email}
Date: {day} {date_fmt} at {time_fmt}
Location: {location_name}
Price: {price}

This has been added to your Google Calendar as PENDING (lavender).
Reply to {customer_email} to confirm their appointment.
"""
        msg = MIMEText(body)
        msg['Subject'] = f'Booking Request: {customer_name} — {day} {date_fmt}'
        msg['From'] = smtp_email
        msg['To'] = OWNER_EMAIL

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(smtp_email, smtp_password)
            server.send_message(msg)
    except Exception:
        pass


def create_booking_request(date_str, time_str, location, customer_name, customer_email):
    try:
        tz = pytz.timezone(TIMEZONE)
        start = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
        end = start + timedelta(minutes=SESSION_DURATION)

        loc = LOCATIONS[location]
        location_name = loc['name']
        price = loc['price']

        service = get_calendar_service()

        if is_first_cst_of_day(service, date_str, time_str):
            setup_start = start - timedelta(hours=1)
            service.events().insert(calendarId=CALENDAR_ID, body={
                'summary': f'Travel & Setup — {location_name}',
                'start': {'dateTime': setup_start.isoformat(), 'timeZone': TIMEZONE},
                'end': {'dateTime': start.isoformat(), 'timeZone': TIMEZONE},
                'colorId': '8',
            }).execute()

        event = {
            'summary': f'PENDING: CST — {customer_name}',
            'description': (
                f'AWAITING CONFIRMATION — please confirm within 24 hours\n\n'
                f'Client: {customer_name}\n'
                f'Email: {customer_email}\n'
                f'Location: {location_name}\n'
                f'Price: {price}\n\n'
                f'Craniosacral Therapy — 60 minutes'
            ),
            'location': location_name,
            'start': {'dateTime': start.isoformat(), 'timeZone': TIMEZONE},
            'end': {'dateTime': end.isoformat(), 'timeZone': TIMEZONE},
            'colorId': '1',
            'status': 'tentative',
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

        send_notification_email(customer_name, customer_email, date_str, time_str, location_name, price)

        day = start.strftime('%A')
        date_fmt = start.strftime('%-d %B')
        time_fmt = start.strftime('%-I:%M%p').lower()

        return {
            "success": True,
            "confirmed_time": f"{day} {date_fmt} at {time_fmt}",
            "location": location_name,
            "price": price
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


tools = [
    {
        "name": "get_blocked_periods",
        "description": (
            "Check for any fully-blocked periods (holidays, away weeks, etc.) within a date range. "
            "ALWAYS call this first, immediately after location is confirmed, covering tomorrow through 90 days ahead. "
            "Use the results to proactively tell the client about any gaps before asking about their preferred week or day. "
            "This prevents clients choosing a week only to discover Phoenix is unavailable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date_str": {"type": "string", "description": "Start of date range in YYYY-MM-DD format"},
                "end_date_str": {"type": "string", "description": "End of date range in YYYY-MM-DD format"}
            },
            "required": ["start_date_str", "end_date_str"]
        }
    },
    {
        "name": "check_time_slot",
        "description": "Check if a specific date, time and location is available for a 60-minute session",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_str": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time_str": {"type": "string", "description": "Time in HH:MM 24-hour format"},
                "location": {"type": "string", "enum": ["waterloo", "bethnal_green"], "description": "Which location"}
            },
            "required": ["date_str", "time_str", "location"]
        }
    },
    {
        "name": "find_available_slots",
        "description": (
            "Find up to 4 available slots in a date range, ranked to cluster near existing bookings. "
            "Use this when: (1) a requested time is unavailable — pass a range of +/-5 days around it; "
            "(2) a client asks about availability for a period e.g. 'this week', 'next Thursday'. "
            "Always prefer this over checking individual slots one by one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date_str": {"type": "string", "description": "Start of date range in YYYY-MM-DD format"},
                "end_date_str": {"type": "string", "description": "End of date range in YYYY-MM-DD format"},
                "location": {"type": "string", "enum": ["waterloo", "bethnal_green"], "description": "Which location"}
            },
            "required": ["start_date_str", "end_date_str", "location"]
        }
    },
    {
        "name": "create_booking_request",
        "description": "Create a pending booking request in the calendar. Only call this when you have a confirmed time, location, customer name and email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_str": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time_str": {"type": "string", "description": "Time in HH:MM 24-hour format"},
                "location": {"type": "string", "enum": ["waterloo", "bethnal_green"], "description": "Which location"},
                "customer_name": {"type": "string", "description": "Customer's full name"},
                "customer_email": {"type": "string", "description": "Customer's email address"}
            },
            "required": ["date_str", "time_str", "location", "customer_name", "customer_email"]
        }
    }
]


def get_system_prompt():
    today = datetime.now(pytz.timezone(TIMEZONE))
    tomorrow = today + timedelta(days=1)
    ninety_days = today + timedelta(days=90)
    days_to_friday = 4 - today.weekday() if today.weekday() <= 4 else 0
    this_friday = (today + timedelta(days=days_to_friday)).strftime('%Y-%m-%d')

    return f"""You are a warm and professional booking assistant for Phoenix, a craniosacral therapist in London. You represent CSTL — Craniosacral Therapy London.

Today is {today.strftime('%A %-d %B %Y')}. The earliest slot you can ever offer is {tomorrow.strftime('%A %-d %B')} (24-hour minimum notice applies). Bookings open up to 3 months ahead ({ninety_days.strftime('%-d %B %Y')}).

LOCATIONS AND PRICING:
- Waterloo / Blackfriars: £80 per session
- Bethnal Green Community Clinic: £30-60 sliding scale

WORKING HOURS AND DAYS:
- Hours: 10am-9pm (last session starts at 8pm)
- Main working days: Monday, Wednesday, Thursday, Friday
- Sundays: possible for urgent cases only — direct them to call Phoenix to discuss
- Saturdays: not available

BOOKING RULES:
- Minimum notice: 24 hours. If less than 24 hours, tell them warmly to call Phoenix directly
- Maximum ahead: 3 months
- Sessions are 60 minutes

LOCATION LOYALTY — CRITICAL:
- Once a client has named or chosen a location, only ever search and offer slots at that location
- NEVER suggest switching to the other location as an alternative, even if the chosen one has limited availability
- The price difference is significant (£80 vs £30-60) and clients choose deliberately — do not second-guess that choice
- Only mention the other location if the client explicitly asks about it, or volunteers that they are flexible

PROACTIVE AVAILABILITY OVERVIEW — CRITICAL:
- Immediately after location is confirmed, call get_blocked_periods with start={tomorrow.strftime('%Y-%m-%d')} and end={ninety_days.strftime('%Y-%m-%d')}
- If any blocked periods are returned (holidays, away weeks), tell the client clearly and upfront BEFORE asking which week or day they want
- Be specific and human: "Just to flag — Phoenix is away from [full_start display] to [full_end display], back on [first_available_after display]. Outside of that, he has good availability. When are you thinking?"
- If the period they mention falls entirely within a blocked range, tell them immediately and offer the next available window instead of asking day/time questions
- Never let a client go through day and time selection only to discover Phoenix is unavailable for that whole period

SHOWING AVAILABILITY:
- Always use find_available_slots — never check individual slots one by one
- Always present exactly 4 options (or all available if fewer than 4 exist)
- If a client says "this week", the range is {tomorrow.strftime('%Y-%m-%d')} to {this_friday} — never include today or days already passed
- If a client says "next week", use the following Monday to Friday
- Each slot comes with a "display" field — always use that exact text when presenting the option, never recalculate the date yourself
- Slots are ranked to cluster near Phoenix's existing bookings to keep his days efficient

THIS IS A REQUEST, NOT A CONFIRMATION:
- When a client agrees a time, collect their name and email, then use create_booking_request
- Tell the client that Phoenix will review and confirm within 24 hours by email
- Never say "you are booked" — say "your request has been sent" or "Phoenix will be in touch to confirm"

HANDLING UNAVAILABLE SLOTS:
- reason "too_soon": tell them warmly to call Phoenix directly as it is less than 24 hours notice
- reason "outside_hours": let them know sessions run between 10am and 9pm
- reason "location_conflict": explain Phoenix needs travel time between locations that day, then call find_available_slots for a +/-5 day range at the same location and offer alternatives
- reason "already_booked": call find_available_slots for a +/-5 day range at the same location and offer alternatives
- reason "too_far_ahead": let them know bookings open up to 3 months ahead

TONE:
- Warm, calm and unhurried
- Collect name and email naturally in conversation, not like a form
- Keep responses concise"""


def serialize_content(content):
    if isinstance(content, str):
        return content
    result = []
    for block in content:
        if hasattr(block, 'type'):
            if block.type == 'text':
                result.append({"type": "text", "text": block.text})
            elif block.type == 'tool_use':
                result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        else:
            result.append(block)
    return result


@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    messages = data.get('messages', [])

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=get_system_prompt(),
        tools=tools,
        messages=messages
    )

    while response.stop_reason == 'tool_use':
        tool_results = []
        for block in response.content:
            if block.type == 'tool_use':
                if block.name == 'get_blocked_periods':
                    result = get_blocked_periods(**block.input)
                elif block.name == 'check_time_slot':
                    result = check_time_slot(**block.input)
                elif block.name == 'find_available_slots':
                    result = find_available_slots(**block.input)
                elif block.name == 'create_booking_request':
                    result = create_booking_request(**block.input)
                else:
                    result = {"error": "Unknown tool"}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

        messages = messages + [
            {"role": "assistant", "content": serialize_content(response.content)},
            {"role": "user", "content": tool_results}
        ]

        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=get_system_prompt(),
            tools=tools,
            messages=messages
        )

    text = next((block.text for block in response.content if hasattr(block, 'text')), "")
    updated_messages = messages + [{"role": "assistant", "content": serialize_content(response.content)}]

    return jsonify({"response": text, "messages": updated_messages})


@app.route('/')
def index():
    return send_file('index.html')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
