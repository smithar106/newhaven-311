"""New Haven 311 — Mobile citizen services app."""
import os, json, csv, io, uuid
from datetime import datetime, timedelta
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
    from collections import Counter
    db   = get_db()
    rows = db.execute('SELECT * FROM submissions ORDER BY created_at DESC').fetchall()
    submissions = []
    for row in rows:
        s = dict(row)
        s['photos']  = json.loads(s.get('photos', '[]'))
        s['cat_obj'] = next((c for c in CATEGORIES if c['id'] == s['category']), None)
        submissions.append(s)

    now = datetime.utcnow()

    # KPI: this week
    week_ago  = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    this_week = sum(1 for s in submissions if str(s['created_at'])[:10] >= week_ago)

    # KPI: avg resolution days
    res_times = []
    for s in submissions:
        if s['status'] in ('Resolved', 'Closed') and s.get('updated_at') and s.get('created_at'):
            try:
                c = datetime.fromisoformat(str(s['created_at'])[:19])
                u = datetime.fromisoformat(str(s['updated_at'])[:19])
                d = (u - c).total_seconds() / 86400
                if d >= 0:
                    res_times.append(d)
            except Exception:
                pass
    avg_resolution = round(sum(res_times) / len(res_times), 1) if res_times else 0

    # Chart: daily counts last 30 days
    daily_labels, daily_counts_list = [], []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i))
        daily_labels.append(day.strftime('%b %d'))
        daily_counts_list.append(sum(
            1 for s in submissions if str(s['created_at'])[:10] == day.strftime('%Y-%m-%d')
        ))

    # Chart: by category
    cat_counts = Counter(s['category_label'] for s in submissions)
    cat_chart_labels, cat_chart_values, cat_chart_colors = [], [], []
    for label, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        cat_obj = next((c for c in CATEGORIES if c['label'] == label), None)
        cat_chart_labels.append(label)
        cat_chart_values.append(count)
        cat_chart_colors.append(cat_obj['color'] if cat_obj else '#4A5568')

    # Chart: by status
    STATUS_COLORS = {
        'Submitted':   '#2B6CB0', 'In Review':  '#D69E2E',
        'Assigned':    '#805AD5', 'In Progress':'#C53030',
        'Resolved':    '#276749', 'Closed':     '#718096',
    }
    status_counts = Counter(s['status'] for s in submissions)
    status_chart_labels = list(status_counts.keys())
    status_chart_values = [status_counts[k] for k in status_chart_labels]
    status_chart_colors = [STATUS_COLORS.get(k, '#4A5568') for k in status_chart_labels]

    return render_template('admin.html',
        submissions         = submissions,
        total               = len(submissions),
        open_count          = sum(1 for s in submissions if s['status'] not in ('Resolved','Closed')),
        resolved            = status_counts.get('Resolved', 0),
        this_week           = this_week,
        avg_resolution      = avg_resolution,
        daily_labels        = json.dumps(daily_labels),
        daily_counts        = json.dumps(daily_counts_list),
        cat_chart_labels    = json.dumps(cat_chart_labels),
        cat_chart_values    = json.dumps(cat_chart_values),
        cat_chart_colors    = json.dumps(cat_chart_colors),
        status_chart_labels = json.dumps(status_chart_labels),
        status_chart_values = json.dumps(status_chart_values),
        status_chart_colors = json.dumps(status_chart_colors),
        categories          = CATEGORIES,
        statuses            = STATUSES,
        city                = CITY_NAME,
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
    return redirect(url_for('admin_ticket', sub_id=sub_id))


@app.route('/admin/ticket/<int:sub_id>')
@require_admin
def admin_ticket(sub_id):
    db  = get_db()
    row = db.execute('SELECT * FROM submissions WHERE id=?', (sub_id,)).fetchone()
    if not row:
        return redirect(url_for('admin'))
    sub = dict(row)
    sub['photos']  = json.loads(sub.get('photos', '[]'))
    sub['cat_obj'] = next((c for c in CATEGORIES if c['id'] == sub['category']), None)
    status_index   = STATUSES.index(sub['status']) if sub['status'] in STATUSES else 0
    return render_template('admin_ticket.html',
        sub=sub, statuses=STATUSES, status_index=status_index, city=CITY_NAME)


@app.route('/admin/seed-demo', methods=['POST'])
@require_admin
def admin_seed_demo():
    import random
    db  = get_db()
    now = datetime.utcnow()
    DEMO = [
        ('pothole','Large pothole cracking vehicle rims near the intersection','148 Whalley Ave, New Haven, CT',41.3101,-72.9387,28,'Resolved'),
        ('streetlight','Street lamp dark for two weeks, safety concern at night','55 Crown St, New Haven, CT',41.3065,-72.9258,26,'Resolved'),
        ('graffiti','Spray-painted tags on retaining wall, highly visible','22 Chapel St, New Haven, CT',41.3080,-72.9267,25,'Resolved'),
        ('abandoned_vehicle','Silver sedan, no plates, sitting 10+ days','310 Grand Ave, New Haven, CT',41.2994,-72.9099,24,'Closed'),
        ('illegal_dumping','Mattress and debris dumped on sidewalk overnight','88 Dixwell Ave, New Haven, CT',41.3178,-72.9368,23,'In Progress'),
        ('missed_pickup','Recycling bins not collected on scheduled Tuesday','72 Edgewood Ave, New Haven, CT',41.3062,-72.9429,22,'Resolved'),
        ('park_damage','Large branch blocking main path in Edgewood Park','Edgewood Park, New Haven, CT',41.3039,-72.9474,21,'Resolved'),
        ('noise','Loud music from bar audible 4 blocks away after 2AM','200 Orange St, New Haven, CT',41.3100,-72.9230,20,'In Review'),
        ('pothole','Several potholes along full block damaging tires','400 Elm St, New Haven, CT',41.3095,-72.9307,19,'In Progress'),
        ('code_violation','Commercial dumpster overflowing, attracting pests','510 Howard Ave, New Haven, CT',41.2953,-72.9262,18,'Assigned'),
        ('water_sewer','Water main crack causing bubbling pavement and leak','33 York St, New Haven, CT',41.3082,-72.9296,17,'Resolved'),
        ('harbor_waterfront','Dock planks rotted through at Long Wharf, safety hazard','Long Wharf Dr, New Haven, CT',41.2912,-72.9166,16,'Assigned'),
        ('streetlight','Traffic light out at busy four-way intersection','Whalley Ave & Ella Grasso Blvd, NH, CT',41.3049,-72.9436,15,'In Progress'),
        ('graffiti','Tags on historic building facade, appears fresh','100 Audubon St, New Haven, CT',41.3118,-72.9240,14,'Submitted'),
        ('abandoned_vehicle','RV parked on residential street 3 weeks','45 Fountain St, New Haven, CT',41.3144,-72.9344,13,'In Review'),
        ('pothole','Deep pothole forming sinkhole, car bottomed out','201 Winthrop Ave, New Haven, CT',41.3011,-72.9196,12,'Assigned'),
        ('missed_pickup','Entire street skipped on garbage day','66 Blake St, New Haven, CT',41.3063,-72.9332,12,'Resolved'),
        ('park_damage','Vandalism to picnic tables in Wooster Square Park','Wooster Square Park, New Haven, CT',41.3028,-72.9155,11,'In Review'),
        ('illegal_dumping','Household trash dumped near school storm drain','390 Quinnipiac Ave, New Haven, CT',41.3062,-72.8974,10,'Submitted'),
        ('noise','Generator running 24/7 at construction site','900 Chapel St, New Haven, CT',41.3070,-72.9192,10,'Assigned'),
        ('water_sewer','Sewage odor from manhole during heavy rain','120 Ferry St, New Haven, CT',41.3008,-72.9108,9,'In Progress'),
        ('code_violation','Exterior stairs collapsed, building appears occupied','175 Shelton Ave, New Haven, CT',41.3160,-72.9460,8,'Assigned'),
        ('pothole','Pothole causing bikes to crash near Yale campus','Prospect St & Sachem St, New Haven, CT',41.3188,-72.9264,7,'Submitted'),
        ('streetlight','Flickering lamp buzzing all night near apartments','44 Trumbull St, New Haven, CT',41.3097,-72.9252,7,'In Review'),
        ('harbor_waterfront','Dead fish washing ashore at Lighthouse Point','Lighthouse Point Park, New Haven, CT',41.2566,-72.8983,6,'Submitted'),
        ('graffiti','Tags on playground equipment, needs urgent removal','East Rock Park, New Haven, CT',41.3288,-72.9168,6,'Submitted'),
        ('missed_pickup','Christmas tree left curbside not picked up','27 Maple St, New Haven, CT',41.3130,-72.9290,5,'In Review'),
        ('abandoned_vehicle','Burned-out vehicle blocking one lane of traffic','180 Quinnipiac Ave, New Haven, CT',41.3060,-72.8990,5,'Submitted'),
        ('park_damage','Tennis court net torn down, frame bent beyond use','Beaver Ponds Park, New Haven, CT',41.3230,-72.9380,4,'Submitted'),
        ('pothole','New pothole opened after street flooding last week','55 Lloyd St, New Haven, CT',41.2987,-72.9230,4,'Submitted'),
        ('water_sewer','Fire hydrant leaking steadily into street for days','300 Whalley Ave, New Haven, CT',41.3060,-72.9360,3,'In Review'),
        ('noise','Bar music audible inside homes 4 blocks away nightly','138 Crown St, New Haven, CT',41.3059,-72.9244,3,'Submitted'),
        ('illegal_dumping','Electronics and appliances dumped in alley overnight','211 River St, New Haven, CT',41.2940,-72.9210,2,'Submitted'),
        ('code_violation','Abandoned storefront with broken windows open to public','88 Grand Ave, New Haven, CT',41.3000,-72.9110,1,'Submitted'),
        ('streetlight','Solar walkway lights all dark along entire park path','West River Memorial Park, NH, CT',41.3078,-72.9520,1,'Submitted'),
    ]
    for cat_id, desc, addr, lat, lng, days_ago, status in DEMO:
        cat_obj   = next((c for c in CATEGORIES if c['id'] == cat_id), None)
        cat_label = cat_obj['label'] if cat_obj else cat_id
        tracking  = generate_tracking()
        created   = (now - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')
        updated   = (now - timedelta(days=max(0, days_ago - random.randint(1,4)))).strftime('%Y-%m-%d %H:%M:%S')
        notes     = 'Issue resolved by Dept. of Public Works. Thank you for your report.' if status in ('Resolved','Closed') else ''
        db.execute("""
            INSERT INTO submissions
              (tracking_number,category,category_label,description,address,
               lat,lng,photos,contact_name,contact_email,contact_phone,
               status,notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (tracking,cat_id,cat_label,desc,addr,lat,lng,'[]','','','',status,notes,created,updated))
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
