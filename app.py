"""New Haven 311 — Mobile citizen services app."""
import os, json, csv, io, uuid
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, jsonify, g, Response)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY']         = os.environ.get('SECRET_KEY', 'newhaven311secret')
app.config['UPLOAD_FOLDER']      = os.path.join('static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'newhaven2026')

CITY_NAME    = "New Haven"
CITY_SHORT   = "NHV"
CITY_TAGLINE = "Non-Emergency City Services"

# ── database backend detection ────────────────────────────────────────────────
_DB_URL = os.environ.get('DATABASE_URL', '')
if _DB_URL.startswith('postgres://'):
    _DB_URL = _DB_URL.replace('postgres://', 'postgresql://', 1)
USE_PG = _DB_URL.startswith('postgresql')


class DBConn:
    def __init__(self):
        if USE_PG:
            import psycopg2, psycopg2.extras
            self._conn = psycopg2.connect(
                _DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            import sqlite3
            self._conn = sqlite3.connect('newhaven311.db')
            self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USE_PG:
            sql = sql.replace('?', '%s')
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur
        return self._conn.execute(sql, params)

    def commit(self): self._conn.commit()
    def close(self):  self._conn.close()


CATEGORIES = [
    {'id': 'pothole',           'label': 'Pothole / Road Damage',   'icon': '🕳️',  'color': '#C53030'},
    {'id': 'streetlight',       'label': 'Streetlight Outage',      'icon': '💡',  'color': '#B7791F'},
    {'id': 'graffiti',          'label': 'Graffiti',                'icon': '🎨',  'color': '#6B46C1'},
    {'id': 'abandoned_vehicle', 'label': 'Abandoned Vehicle',       'icon': '🚗',  'color': '#C05621'},
    {'id': 'illegal_dumping',   'label': 'Illegal Dumping',         'icon': '🗑️',  'color': '#744210'},
    {'id': 'missed_pickup',     'label': 'Missed Garbage Pickup',   'icon': '♻️',  'color': '#276749'},
    {'id': 'park_damage',       'label': 'Park / Tree Damage',      'icon': '🌳',  'color': '#22543D'},
    {'id': 'noise',             'label': 'Noise Complaint',         'icon': '🔊',  'color': '#2C5282'},
    {'id': 'code_violation',    'label': 'Code Violation',          'icon': '🏠',  'color': '#9B2335'},
    {'id': 'water_sewer',       'label': 'Water / Sewer Issue',     'icon': '💧',  'color': '#2B6CB0'},
    {'id': 'harbor_waterfront', 'label': 'Harbor / Waterfront',     'icon': '⚓',  'color': '#065666'},
    {'id': 'other',             'label': 'Other',                   'icon': '📋',  'color': '#4A5568'},
]

STATUSES = ['Submitted', 'In Review', 'Assigned', 'In Progress', 'Resolved', 'Closed']


# ── database ──────────────────────────────────────────────────────────────────

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        g._database = DBConn()
    return g._database

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    db = DBConn()
    sql_type = "SERIAL" if USE_PG else "INTEGER"
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    lat_type = "DOUBLE PRECISION" if USE_PG else "REAL"
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS submissions (
            id              {pk},
            tracking_number TEXT    UNIQUE NOT NULL,
            category        TEXT    NOT NULL,
            category_label  TEXT    NOT NULL,
            description     TEXT,
            address         TEXT,
            lat             {lat_type},
            lng             {lat_type},
            photos          TEXT    DEFAULT '[]',
            contact_name    TEXT,
            contact_email   TEXT,
            contact_phone   TEXT,
            status          TEXT    DEFAULT 'Submitted',
            notes           TEXT    DEFAULT '',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()
    db.close()


# ── auth ──────────────────────────────────────────────────────────────────────

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response('Unauthorized', 401,
                            {'WWW-Authenticate': 'Basic realm="New Haven 311 Admin"'})
        return f(*args, **kwargs)
    return decorated


# ── helpers ───────────────────────────────────────────────────────────────────

def generate_tracking():
    year   = datetime.now().year
    suffix = str(uuid.uuid4().int)[:5].zfill(5)
    return f"{CITY_SHORT}-{year}-{suffix}"

ALLOWED = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', categories=CATEGORIES,
                           city=CITY_NAME, tagline=CITY_TAGLINE)


@app.route('/submit', methods=['POST'])
def submit():
    db  = get_db()
    cat = request.form.get('category', '')
    cat_obj   = next((c for c in CATEGORIES if c['id'] == cat), None)
    cat_label = cat_obj['label'] if cat_obj else cat

    lat = request.form.get('lat', '')
    lng = request.form.get('lng', '')

    photos = []
    for i in range(3):
        f = request.files.get(f'photo_{i}')
        if f and f.filename and allowed_file(f.filename):
            fname = secure_filename(f"{uuid.uuid4().hex}_{f.filename}")
            f.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
            photos.append(f'/static/uploads/{fname}')

    tracking = generate_tracking()
    db.execute("""
        INSERT INTO submissions
          (tracking_number, category, category_label, description, address,
           lat, lng, photos, contact_name, contact_email, contact_phone)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        tracking, cat, cat_label,
        request.form.get('description', ''),
        request.form.get('address', ''),
        float(lat) if lat else None,
        float(lng) if lng else None,
        json.dumps(photos),
        request.form.get('contact_name', ''),
        request.form.get('contact_email', ''),
        request.form.get('contact_phone', ''),
    ))
    db.commit()
    return redirect(url_for('confirm', tracking=tracking))


@app.route('/confirm/<tracking>')
def confirm(tracking):
    db  = get_db()
    row = db.execute('SELECT * FROM submissions WHERE tracking_number=?',
                     (tracking,)).fetchone()
    if not row:
        return redirect(url_for('index'))
    sub = dict(row)
    sub['photos'] = json.loads(sub.get('photos', '[]'))
    cat_obj = next((c for c in CATEGORIES if c['id'] == sub['category']), None)
    return render_template('confirm.html', sub=sub, cat=cat_obj,
                           city=CITY_NAME)


@app.route('/track')
def track():
    tracking = request.args.get('tracking', '').strip().upper()
    sub, cat, status_index = None, None, 0
    if tracking:
        db  = get_db()
        row = db.execute('SELECT * FROM submissions WHERE tracking_number=?',
                         (tracking,)).fetchone()
        if row:
            sub = dict(row)
            sub['photos'] = json.loads(sub.get('photos', '[]'))
            cat = next((c for c in CATEGORIES if c['id'] == sub['category']), None)
            status_index = STATUSES.index(sub['status']) if sub['status'] in STATUSES else 0
    return render_template('track.html', sub=sub, cat=cat,
                           tracking=tracking, statuses=STATUSES,
                           status_index=status_index, city=CITY_NAME)


@app.route('/admin')
@require_admin
def admin():
    db   = get_db()
    rows = db.execute('SELECT * FROM submissions ORDER BY created_at DESC').fetchall()
    submissions = []
    for row in rows:
        s = dict(row)
        s['photos']  = json.loads(s.get('photos', '[]'))
        s['cat_obj'] = next((c for c in CATEGORIES if c['id'] == s['category']), None)
        submissions.append(s)

    from collections import Counter
    cat_counts    = Counter(s['category_label'] for s in submissions)
    status_counts = Counter(s['status'] for s in submissions)

    return render_template('admin.html',
        submissions=submissions,
        total=len(submissions),
        open_count=sum(1 for s in submissions if s['status'] not in ('Resolved','Closed')),
        resolved=status_counts.get('Resolved', 0),
        cat_counts=dict(cat_counts),
        status_counts=dict(status_counts),
        categories=CATEGORIES,
        statuses=STATUSES,
        city=CITY_NAME,
    )


@app.route('/admin/update/<int:sub_id>', methods=['POST'])
@require_admin
def admin_update(sub_id):
    db = get_db()
    db.execute("""
        UPDATE submissions
        SET status=?, notes=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (request.form.get('status'), request.form.get('notes',''), sub_id))
    db.commit()
    return redirect(url_for('admin'))


@app.route('/admin/export')
@require_admin
def admin_export():
    db   = get_db()
    rows = db.execute('SELECT * FROM submissions ORDER BY created_at DESC').fetchall()
    out  = io.StringIO()
    w    = csv.writer(out)
    w.writerow(['Tracking Number','Category','Description','Address',
                'Latitude','Longitude','Contact Name','Contact Email',
                'Contact Phone','Status','Notes','Submitted At','Updated At'])
    for row in rows:
        s = dict(row)
        w.writerow([s.get(k,'') for k in [
            'tracking_number','category_label','description','address',
            'lat','lng','contact_name','contact_email','contact_phone',
            'status','notes','created_at','updated_at']])
    out.seek(0)
    fname = f"newhaven311_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment;filename={fname}'})


init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5007))
    print(f"New Haven 311 running on http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=True)
