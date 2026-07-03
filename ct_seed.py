"""
Control Tower — demo seed data.

Seeds the CRM with the prototype's clients + enquiries on first boot (only if
the clients table is empty) so the app looks and behaves like the design out of
the box. Phoenix can edit/clear these; real appointments still come from his
Google Calendar. This is CRM data only — it never touches the calendar.
"""
from models import db, Client, Session, Enquiry

# (fields..., [sessions]) — clinic uses the app's canonical keys
_CLIENTS = [
    dict(name='Maya Okafor', email='maya.okafor@gmail.com', phone='07700 900321',
         clinic='waterloo', marketing_consent=True, intake_complete=True,
         dob='14 Mar 1988', occupation='Architect', doctor='Dr Amin — Borough Medical Centre',
         meds='None', conditions='Migraines (diagnosed 2019)',
         emergency='Sam Okafor · 07700 900322', referred='Google search',
         sessions=[
             ('24 Jun 2026', 'waterloo',
              ['Migraine frequency down to one this fortnight', 'Worked occipital base and sacrum',
               'Strong stillpoint mid-session', 'Keep monthly rhythm'],
              'Migraines down to one since last time. Started supine, occipital hold — lots of heat. Sacrum settled quickly. Stillpoint around 30 mins, long. Advised hydration and a gentle walk after.'),
             ('27 May 2026', 'waterloo',
              ['First session after referral', 'Baseline: weekly migraines, jaw clenching at night',
               'Gentle introduction, full protocol', 'Rebooked monthly'],
              'Intake reviewed. Weekly migraines, clenches jaw at night. Very settled on the table. Went gently — cranial vault, sacral hold. Rebooked for a month.'),
         ]),
    dict(name='Daniel Reeve', email='d.reeve@outlook.com', phone='07700 900412',
         clinic='bethnal_green', marketing_consent=False, intake_complete=False,
         dob='02 Nov 1991', occupation='Chef', doctor='Dr Osei — Globe Town Surgery',
         meds='Ibuprofen (occasional)', conditions='Tension headaches, lower back pain',
         emergency='Ella Reeve · 07700 900413', referred='Friend — Maya Okafor', sessions=[]),
    dict(name='Priya Shah', email='priya.shah@me.com', phone='07700 900733',
         clinic='waterloo', marketing_consent=True, intake_complete=True,
         dob='30 Jul 1979', occupation='Solicitor', doctor='Dr Lam — Waterloo Health Centre',
         meds='Levothyroxine 50µg', conditions='Hypothyroidism, chronic neck tension',
         emergency='Anil Shah · 07700 900734', referred='Instagram',
         sessions=[
             ('30 Jun 2026', 'waterloo', ['(draft) Neck tension easing', 'Worked upper cervicals'],
              'DRAFT — neck better this week, worked C1–C2, need to finish this note…'),
             ('09 Jun 2026', 'waterloo', ['Post-holiday reset', 'Sleep improved', 'Focused on diaphragm work'],
              'Back from holiday, sleeping better. Diaphragm work, respiratory and pelvic. Quiet session.'),
         ]),
    dict(name='Sarah Kimani', email='sarah.kimani@gmail.com', phone='07700 900518',
         clinic='waterloo', marketing_consent=True, intake_complete=True,
         dob='21 Sep 1984', occupation='Teacher', doctor='Dr Field — Kennington Park Practice',
         meds='None', conditions='Sciatica (resolved), stress',
         emergency='David Kimani · 07700 900519', referred='Colleague',
         sessions=[
             ('12 May 2026', 'waterloo', ['Sciatica fully settled', 'Focus shifted to stress and sleep', 'Long stillpoint'],
              'Sciatica gone. Work stress high — focused on parasympathetic settling. Long stillpoint. She may rebook over summer.'),
         ]),
    dict(name='Amara Wilson', email='amara.w@hotmail.com', phone='07700 900611',
         clinic='bethnal_green', marketing_consent=True, intake_complete=True,
         dob='05 Jan 1996', occupation='Nurse', doctor='Dr Chen — Bethnal Green Health Centre',
         meds='Sertraline 50mg', conditions='Anxiety, TMJ tension',
         emergency='Grace Wilson · 07700 900612', referred='Instagram',
         sessions=[
             ('22 Jun 2026', 'bethnal_green', ['Jaw noticeably softer', 'Worked TMJ and cranial base', 'Sleep improving'],
              'Jaw much softer this session. TMJ work, cranial base. Sleeping through most nights now.'),
         ]),
    dict(name='Tom Hartley', email='tom.hartley@gmail.com', phone='07700 900822',
         clinic='waterloo', marketing_consent=False, intake_complete=True,
         dob='17 Jun 1972', occupation='Taxi driver', doctor='Dr Adeoye — Lambeth Walk Group',
         meds='Amlodipine 5mg', conditions='Lower back pain, hypertension (managed)',
         emergency='Jean Hartley · 07700 900823', referred='Wife',
         sessions=[
             ('19 Jun 2026', 'waterloo', ['Back pain easing with fortnightly sessions', 'Sacral and lumbar focus', 'Advised gentle stretching'],
              'Back easing. Sacral + lumbar holds, good response. Showed him two gentle stretches for between sessions.'),
         ]),
    dict(name='Beatrice Lang', email='bea.lang@icloud.com', phone='07700 900934',
         clinic='bethnal_green', marketing_consent=True, intake_complete=True,
         dob='28 Feb 2001', occupation='Illustrator', doctor='Dr Novak — Globe Town Surgery',
         meds='None', conditions='Migraines with aura',
         emergency='Petra Lang · 07700 900935', referred='Friend',
         sessions=[
             ('16 Jun 2026', 'bethnal_green', ['One aura episode since last session', 'Cranial vault and occiput focus', 'Discussed screen-break routine'],
              'One aura in three weeks — big improvement. Vault + occiput. Talked about screen breaks; she is drawing 10h days.'),
         ]),
]

_ENQUIRIES = [
    dict(name='Jonah M', via='WhatsApp', status='new',
         raw_text="Hi Phoenix! I'm Jonah — a friend of Maya's. I've been having tension headaches for months and she said craniosacral really helped her. I'm out east near Victoria Park so Bethnal Green would be easiest for me. Could do next Tuesday or Wednesday after 5? My number is 07700 900412. Thanks!"),
    dict(name='Sarah Kimani', via='Email', status='new',
         raw_text="Hi Phoenix, it's Sarah Kimani — I'd love to come back in. Could I book another Waterloo session? Weekday mornings work best for me. Thanks so much, Sarah"),
]


def seed_if_empty(app):
    """Insert demo data if the clients table is empty. Safe to call on boot."""
    try:
        with app.app_context():
            if Client.query.count() > 0:
                return False
            for source in _CLIENTS:
                data = dict(source)                 # copy — never mutate the module data
                sessions = data.pop('sessions', [])
                client = Client(**data)
                db.session.add(client)
                db.session.flush()
                for date, clinic, bullets, raw in sessions:
                    s = Session(client_id=client.id, date=date, clinic=clinic, raw_note=raw)
                    s.bullets = bullets
                    db.session.add(s)
            for e in _ENQUIRIES:
                enq = Enquiry(preview=e['raw_text'][:120], **e)
                db.session.add(enq)
            db.session.commit()
            return True
    except Exception as exc:  # pragma: no cover - defensive
        app.logger.error("Control Tower seed failed: %s", exc)
        return False
