"""
Control Tower — PLACEHOLDER AI.

Everything in this module is a deterministic stand-in. There are NO LLM /
Anthropic calls here, and this module does NOT touch the old booking chatbot.

This is the single swap-point: when we design the real AI later, we replace the
bodies of `analyse_enquiry()` and `summarise_note()` here and nothing else in
the app has to change.
"""
import re


def analyse_enquiry(text):
    """PLACEHOLDER — pull booking details out of a pasted client message.

    Deterministic heuristic mirroring the prototype's behaviour. Returns
    {name, phone, clinic, preference}. Replace with real AI later.
    """
    t = (text or '').strip()

    name = ''
    m = re.search(
        r"(?:i'?m|i am|this is|it'?s|my name is)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)",
        t)
    if m:
        name = m.group(1)

    phone = ''
    m = re.search(r'(0\d{2,4}[\d ]{5,}\d)', t)
    if m:
        phone = m.group(1).strip()

    clinic = ''
    low = t.lower()
    if re.search(r'bethnal|victoria park|hackney|east london|out east|chalk farm', low):
        clinic = 'bethnal_green'
    elif re.search(r'waterloo|south bank|southwark|kennington|blackfriars', low):
        clinic = 'waterloo'

    parts = []
    days = re.findall(
        r'\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b', t, re.I)
    if days:
        uniq = list(dict.fromkeys(d.capitalize() for d in days))
        parts.append(' or '.join(uniq))
    elif re.search(r'\bweekday', low):
        parts.append('weekdays')
    tod = re.search(r'\b(mornings?|afternoons?|evenings?)\b', low)
    if tod:
        parts.append(tod.group(1))
    ab = re.search(r'\b(after|before)\s*\d{1,2}(:\d{2})?\s*(am|pm)?', low)
    if ab:
        parts.append(ab.group(0).strip())

    return {'name': name, 'phone': phone, 'clinic': clinic,
            'preference': ', '.join(parts)}


def summarise_note(text):
    """PLACEHOLDER — split a session note into bullet points.

    Mirrors the prototype's punctuation split. Replace with real AI later.
    """
    t = (text or '').strip()
    if not t:
        return []
    parts = [re.sub(r'^[—–-]\s*', '', s.strip())
             for s in re.split(r'[.!?\n]+', t)]
    parts = [p for p in parts if len(p) > 8][:6]
    return parts or [t]
