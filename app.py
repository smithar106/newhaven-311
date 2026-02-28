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

    # ── address pool (real New Haven streets) ─────────────────────────────────
    STREETS = [
        ('Whalley Ave',       41.3101, -72.9387),
        ('Crown St',          41.3065, -72.9258),
        ('Chapel St',         41.3080, -72.9267),
        ('Grand Ave',         41.2994, -72.9099),
        ('Dixwell Ave',       41.3178, -72.9368),
        ('Edgewood Ave',      41.3062, -72.9429),
        ('Elm St',            41.3095, -72.9307),
        ('Howard Ave',        41.2953, -72.9262),
        ('York St',           41.3082, -72.9296),
        ('Long Wharf Dr',     41.2912, -72.9166),
        ('Ella Grasso Blvd',  41.3049, -72.9436),
        ('Audubon St',        41.3118, -72.9240),
        ('Fountain St',       41.3144, -72.9344),
        ('Winthrop Ave',      41.3011, -72.9196),
        ('Blake St',          41.3063, -72.9332),
        ('Quinnipiac Ave',    41.3062, -72.8974),
        ('Orange St',         41.3100, -72.9230),
        ('Ferry St',          41.3008, -72.9108),
        ('Shelton Ave',       41.3160, -72.9460),
        ('Prospect St',       41.3188, -72.9264),
        ('Trumbull St',       41.3097, -72.9252),
        ('Maple St',          41.3130, -72.9290),
        ('Lloyd St',          41.2987, -72.9230),
        ('River St',          41.2940, -72.9210),
        ('Bradley St',        41.3155, -72.9320),
        ('Goffe St',          41.3175, -72.9420),
        ('Orchard St',        41.3090, -72.9450),
        ('Sherman Ave',       41.3200, -72.9350),
        ('Blatchley Ave',     41.3040, -72.9050),
        ('Forbes Ave',        41.2970, -72.9280),
        ('Davenport Ave',     41.2890, -72.9240),
        ('Congress Ave',      41.3020, -72.9320),
        ('George St',         41.3050, -72.9300),
        ('State St',          41.3075, -72.9200),
        ('Legion Ave',        41.2920, -72.9290),
        ('Derby Ave',         41.3000, -72.9480),
        ('Winchester Ave',    41.3250, -72.9310),
        ('Newhall St',        41.3180, -72.9290),
        ('Bassett St',        41.3070, -72.9380),
        ('Valley St',         41.3130, -72.9480),
    ]

    # ── description templates per category ────────────────────────────────────
    DESCRIPTIONS = {
        'pothole': [
            'Large pothole cracking vehicle rims at this intersection',
            'Deep pothole that caused a flat tire, needs urgent repair',
            'Multiple potholes along the block, worsening after rain',
            'Pothole near school zone, dangerous for children crossing',
            'Sinkhole forming from pothole, car bottomed out completely',
            'Road surface completely broken up, appears to be worsening',
            'Pothole cluster near bus stop, damaging commuter vehicles',
            'New pothole opened after last week\'s heavy rainfall',
        ],
        'streetlight': [
            'Street lamp dark for two weeks, safety concern at night',
            'Flickering lamp buzzing loudly, keeps neighbors awake',
            'Traffic light out at busy four-way intersection',
            'Solar walkway lights all dark along entire park path',
            'Streetlight knocked over, wires exposed on sidewalk',
            'Entire block dark after storm, multiple lights out',
            'Light sensor broken, stays on all day wasting energy',
        ],
        'graffiti': [
            'Spray-painted tags on retaining wall, highly visible from road',
            'Tags on historic building facade, appeared overnight',
            'Graffiti on playground equipment, inappropriate content',
            'Large mural-style vandalism on commercial building',
            'Tags spreading across multiple storefronts on block',
            'Graffiti on public utility box, intersection of two major streets',
            'School building tagged over the weekend',
        ],
        'abandoned_vehicle': [
            'Silver sedan with no plates, sitting in same spot 10+ days',
            'RV parked on residential street for over three weeks',
            'Burned-out vehicle partially blocking lane of traffic',
            'Flat-tired pickup truck hasn\'t moved in two weeks',
            'Stolen vehicle recovered here, needs tow',
            'Car on blocks with no engine, landlord says not theirs',
            'Commercial van expired registration, blocking hydrant',
        ],
        'illegal_dumping': [
            'Mattress and household debris dumped on sidewalk overnight',
            'Electronics and appliances dumped in back alley',
            'Construction waste piled near storm drain',
            'Household trash bags dumped near school entrance',
            'Tires stacked against fence in empty lot',
            'Furniture left in middle of public right-of-way',
            'Bags of yard waste blocking sidewalk accessibility',
        ],
        'missed_pickup': [
            'Recycling bins skipped on scheduled Tuesday collection',
            'Entire street missed on garbage day, bins still full',
            'Bulk item left curbside three pickup days in a row',
            'Holiday tree not collected weeks after the holiday',
            'Yard waste bags untouched for two collection cycles',
            'Commercial bins overflowing after missed pickup',
        ],
        'park_damage': [
            'Large branch blocking main pedestrian path after storm',
            'Vandalism to picnic tables, bolts removed',
            'Tennis court net torn down, frame bent beyond repair',
            'Playground swing chain broken, safety hazard for children',
            'Basketball hoop net missing, backboard cracked',
            'Footbridge railing loose, dangerous for pedestrians',
            'Park benches overturned and damaged overnight',
        ],
        'noise': [
            'Loud music from nearby bar audible 4 blocks away after 2AM',
            'Generator running 24/7 at adjacent construction site',
            'Bar music and crowd noise disturbing residents nightly',
            'Construction work starting before 7AM on weekends',
            'Industrial HVAC unit vibrating walls of neighboring homes',
            'Late-night outdoor events exceeding noise ordinance',
        ],
        'code_violation': [
            'Commercial dumpster overflowing, attracting pests and rodents',
            'Exterior stairs collapsed on occupied building',
            'Abandoned storefront with broken windows open to public',
            'Vacant lot overgrown, creating safety and pest concern',
            'Property fence encroaching on public right-of-way',
            'Unpermitted deck addition visible from street',
            'Business signage blocking sightlines at intersection',
        ],
        'water_sewer': [
            'Water main crack causing bubbling pavement and active leak',
            'Sewage odor from manhole strong during and after rain',
            'Fire hydrant leaking steadily into street for three days',
            'Storm drain completely clogged, flooding intersection',
            'Basement flooding from backed-up sewer line',
            'Water pressure loss affecting entire block',
            'Manhole cover missing, open hole in roadway',
        ],
        'harbor_waterfront': [
            'Dock planks rotted through at Long Wharf, safety hazard',
            'Dead fish washing ashore, possible pollution event',
            'Waterfront railing collapsed near public walkway',
            'Debris accumulation blocking boat launch access',
            'Oil sheen visible on water near marina',
            'Seawall erosion exposing unstable embankment',
        ],
        'other': [
            'Sidewalk heaved by tree root, tripping hazard',
            'Bus shelter glass shattered, sharp edges exposed',
            'Crosswalk markings completely faded at busy intersection',
            'Street sign knocked down, missing at intersection',
            'Dead tree leaning toward power lines, imminent fall risk',
            'Overhanging tree branch blocking streetlight',
        ],
    }

    # ── contact name pool ─────────────────────────────────────────────────────
    NAMES = [
        'Maria Santos','James Whitfield','Priya Nair','Carlos Rivera',
        'Susan Chen','David Okafor','Jennifer Rossi','Michael Torres',
        'Linda Park','Robert Nguyen','Angela Brown','Kevin Murphy',
        'Diane Kowalski','Thomas Adeyemi','Rachel Goldstein','Marcus Webb',
        'Fatima Hussain','Patrick O\'Brien','Yuki Tanaka','Alexa Petrov',
        'Denise Washington','Omar Khalil','Cynthia Reyes','Brandon Hall',
        'Miriam Cohen','Jamal Freeman','Nicole Deschamps','Ethan Larson',
        'Aisha Johnson','Paul Ciccone','Teresa Huang','Andre Williams',
        'Kristin Bjork','Samuel Osei','Rosa Delgado','Nathan Prescott',
        'Valeria Moretti','Derek Sims','Leah Abramowitz','Victor Pham',
    ]

    # ── status distribution (realistic for a 311 system) ─────────────────────
    # ~35% resolved/closed (older tickets), ~20% in progress, ~45% open
    STATUS_WEIGHTS = [
        ('Submitted',   20),
        ('In Review',   15),
        ('Assigned',    12),
        ('In Progress', 18),
        ('Resolved',    25),
        ('Closed',      10),
    ]
    STATUS_POOL = [s for s, w in STATUS_WEIGHTS for _ in range(w)]

    # ── category weights (potholes most common) ───────────────────────────────
    CAT_WEIGHTS = [
        ('pothole',           18),
        ('streetlight',       12),
        ('graffiti',          10),
        ('missed_pickup',      9),
        ('illegal_dumping',    9),
        ('noise',              8),
        ('code_violation',     8),
        ('water_sewer',        8),
        ('abandoned_vehicle',  7),
        ('park_damage',        6),
        ('harbor_waterfront',  3),
        ('other',              2),
    ]
    CAT_POOL = [c for c, w in CAT_WEIGHTS for _ in range(w)]

    random.seed(42)  # reproducible demo data
    rows_inserted = 0

    for i in range(1000):
        cat_id    = random.choice(CAT_POOL)
        cat_obj   = next((c for c in CATEGORIES if c['id'] == cat_id), None)
        cat_label = cat_obj['label'] if cat_obj else cat_id

        street_name, base_lat, base_lng = random.choice(STREETS)
        # scatter coordinates slightly around the street anchor
        lat = round(base_lat + random.uniform(-0.003, 0.003), 6)
        lng = round(base_lng + random.uniform(-0.003, 0.003), 6)
        num = random.randint(10, 999)
        address = f"{num} {street_name}, New Haven, CT"

        desc_list = DESCRIPTIONS.get(cat_id, DESCRIPTIONS['other'])
        description = random.choice(desc_list)

        status = random.choice(STATUS_POOL)

        # spread submissions over the past 365 days, weighted toward recent
        days_ago = int(random.betavariate(1.5, 5) * 365)  # skewed toward recent
        created_dt = now - timedelta(days=days_ago,
                                     hours=random.randint(0, 23),
                                     minutes=random.randint(0, 59))
        created = created_dt.strftime('%Y-%m-%d %H:%M:%S')

        # updated sometime after creation (sooner for resolved tickets)
        if status in ('Resolved', 'Closed'):
            update_lag = random.randint(1, min(days_ago, 14)) if days_ago > 0 else 0
            notes = random.choice([
                'Issue resolved by Dept. of Public Works. Thank you for your report.',
                'Crew dispatched and repair completed.',
                'Verified resolved on-site. Case closed.',
                'Work order completed. Please resubmit if issue recurs.',
            ])
        elif status in ('Assigned', 'In Progress'):
            update_lag = random.randint(0, min(days_ago, 5)) if days_ago > 0 else 0
            notes = random.choice([
                'Ticket assigned to Public Works. Estimated response within 5 business days.',
                'Crew scheduled for next available work order.',
                'Under review by city department.',
                '',
            ])
        else:
            update_lag = 0
            notes = ''

        updated_dt = created_dt + timedelta(days=update_lag, hours=random.randint(0, 8))
        updated = updated_dt.strftime('%Y-%m-%d %H:%M:%S')

        # ~60% of submissions include contact info
        if random.random() < 0.60:
            contact_name = random.choice(NAMES)
            first = contact_name.split()[0].lower()
            last  = contact_name.split()[-1].lower()
            contact_email = f"{first}.{last}{random.randint(1,99)}@email.com"
            contact_phone = f"203-{random.randint(200,999)}-{random.randint(1000,9999)}"
        else:
            contact_name = contact_email = contact_phone = ''

        tracking = generate_tracking()

        db.execute("""
            INSERT INTO submissions
              (tracking_number,category,category_label,description,address,
               lat,lng,photos,contact_name,contact_email,contact_phone,
               status,notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (tracking, cat_id, cat_label, description, address,
              lat, lng, '[]', contact_name, contact_email, contact_phone,
              status, notes, created, updated))
        rows_inserted += 1

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
