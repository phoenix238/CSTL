# Upload these 9 files to GitHub

Replace 2 existing files, add 7 new ones. For each: open the file here, select
all, copy — then paste into GitHub (pencil icon to replace, "Create new file"
to add).

## Replace (already exist on GitHub)
1. `app.py`
2. `requirements.txt`

## Add new (top level, next to app.py)
3. `admin.py`
4. `models.py`
5. `ct_rules.py`
6. `ct_ai.py`
7. `ct_seed.py`

## Add new (inside a `templates` folder — type the full path as the filename
## when creating it on GitHub, e.g. `templates/admin.html`)
8. `templates/admin.html`
9. `templates/admin_login.html`

## Do NOT touch
`index.html`, `chatbot-index.html` — your existing homepage/chat widget.
Nothing here changes them. This folder deliberately doesn't include them.

## After uploading
In Render, add 3 environment variables before it fully works:
- `ADMIN_PASSWORD` — a password you choose, to lock the new `/admin` page
- `SECRET_KEY` — any long random string
- `DATABASE_URL` — from a new Render Postgres database (so client records
  aren't lost every time you update the code)

Then visit `yoursite.onrender.com/admin` and sign in.
