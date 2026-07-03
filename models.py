"""
Database models for the CSTL Control Tower admin dashboard.

Uses Flask-SQLAlchemy. In production, set DATABASE_URL to a Render Postgres
connection string; locally (and if DATABASE_URL is unset) it falls back to a
SQLite file so the app still boots and the public booking agent is unaffected.
"""
import json
import os
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _database_uri():
    """Return a SQLAlchemy URI. Normalises Render's postgres:// scheme and
    falls back to a local SQLite file when DATABASE_URL is not set."""
    url = os.environ.get('DATABASE_URL', '').strip()
    if not url:
        # Local/dev fallback. On Render's ephemeral disk this file is NOT
        # durable across redeploys — production must set DATABASE_URL.
        return 'sqlite:///cstl_control_tower.db'
    # SQLAlchemy requires the postgresql:// scheme; Render often gives postgres://
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url


class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), default='')
    phone = db.Column(db.String(80), default='')
    clinic = db.Column(db.String(40), default='waterloo')  # waterloo | bethnal_green
    marketing_consent = db.Column(db.Boolean, default=False)
    intake_complete = db.Column(db.Boolean, default=False)
    # Intake-form fields
    dob = db.Column(db.String(80), default='')
    occupation = db.Column(db.String(200), default='')
    doctor = db.Column(db.String(200), default='')
    meds = db.Column(db.Text, default='')
    conditions = db.Column(db.Text, default='')
    emergency = db.Column(db.String(300), default='')
    referred = db.Column(db.String(200), default='')
    drive_url = db.Column(db.String(500), default='')  # optional manual link
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sessions = db.relationship(
        'Session', backref='client', cascade='all, delete-orphan',
        order_by='Session.date.desc()'
    )

    def to_dict(self, include_sessions=False):
        data = {
            'id': self.id,
            'name': self.name,
            'email': self.email or '',
            'phone': self.phone or '',
            'clinic': self.clinic or 'waterloo',
            'marketing_consent': bool(self.marketing_consent),
            'intake_complete': bool(self.intake_complete),
            'dob': self.dob or '',
            'occupation': self.occupation or '',
            'doctor': self.doctor or '',
            'meds': self.meds or '',
            'conditions': self.conditions or '',
            'emergency': self.emergency or '',
            'referred': self.referred or '',
            'drive_url': self.drive_url or '',
        }
        if include_sessions:
            data['sessions'] = [s.to_dict() for s in self.sessions]
        return data


class Session(db.Model):
    __tablename__ = 'sessions'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    date = db.Column(db.String(20), default='')     # YYYY-MM-DD
    clinic = db.Column(db.String(40), default='waterloo')
    raw_note = db.Column(db.Text, default='')
    bullets_json = db.Column(db.Text, default='[]')
    calendar_event_id = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def bullets(self):
        try:
            return json.loads(self.bullets_json or '[]')
        except (ValueError, TypeError):
            return []

    @bullets.setter
    def bullets(self, value):
        self.bullets_json = json.dumps(value or [])

    def to_dict(self):
        return {
            'id': self.id,
            'client_id': self.client_id,
            'date': self.date or '',
            'clinic': self.clinic or 'waterloo',
            'raw_note': self.raw_note or '',
            'bullets': self.bullets,
            'calendar_event_id': self.calendar_event_id or '',
        }


class Enquiry(db.Model):
    __tablename__ = 'enquiries'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), default='')
    via = db.Column(db.String(40), default='WhatsApp')  # WhatsApp | Email | ...
    raw_text = db.Column(db.Text, default='')
    preview = db.Column(db.String(300), default='')
    status = db.Column(db.String(20), default='new')    # new | booked | dismissed
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name or '',
            'via': self.via or 'WhatsApp',
            'raw_text': self.raw_text or '',
            'preview': self.preview or '',
            'status': self.status or 'new',
            'client_id': self.client_id,
        }


class Setting(db.Model):
    __tablename__ = 'settings'
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, default='')


# Default settings seeded on first boot
DEFAULT_SETTINGS = {
    'access_note': (
        "The Waterloo clinic is on the first floor with stair access only "
        "(no lift). Please let me know if that's difficult and we'll find a "
        "way that works."
    ),
    'email_template_waterloo': (
        "Hi {name},\n\nLovely to hear from you. I've pencilled you in — "
        "you'll get a calendar invite shortly.\n\n{accessNote}\n\nWarmly,\nPhoenix"
    ),
    'email_template_bethnal_green': (
        "Hi {name},\n\nLovely to hear from you. I've pencilled you in at the "
        "Bethnal Green community clinic — you'll get a calendar invite "
        "shortly.\n\n{accessNote}\n\nWarmly,\nPhoenix"
    ),
}


def get_setting(key, default=''):
    row = db.session.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


def init_db(app):
    """Attach SQLAlchemy to the app and create tables. Wrapped so a database
    problem degrades the admin area gracefully instead of crashing the whole
    Flask app (which also serves the live public booking agent)."""
    app.config['SQLALCHEMY_DATABASE_URI'] = _database_uri()
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    try:
        with app.app_context():
            db.create_all()
            # Seed default settings if missing
            for key, value in DEFAULT_SETTINGS.items():
                if not db.session.get(Setting, key):
                    db.session.add(Setting(key=key, value=value))
            db.session.commit()
        return True
    except Exception as e:  # pragma: no cover - defensive
        app.logger.error("Control Tower DB init failed: %s", e)
        return False
