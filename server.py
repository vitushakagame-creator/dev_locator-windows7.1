import os, re, json, secrets, hashlib, uuid, mimetypes, base64, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from io import BytesIO

import requests
import bcrypt
from flask import Flask, request, jsonify, session, redirect, send_file, send_from_directory, make_response, abort
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
from sqlalchemy import create_engine, String, Text, Integer, Boolean, DateTime, JSON, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parent
LOCAL_DATA = Path(os.environ.get('DATA_DIR', ROOT / 'data'))
LOCAL_UPLOADS = Path(os.environ.get('UPLOAD_DIR', ROOT / 'uploads'))
LOCAL_DATA.mkdir(parents=True, exist_ok=True)
LOCAL_UPLOADS.mkdir(parents=True, exist_ok=True)


def now_dt(): return datetime.now(timezone.utc)
def now_iso(): return now_dt().isoformat(timespec='milliseconds').replace('+00:00', 'Z')
def env_bool(name, default=False): return os.environ.get(name, str(default)).strip().lower() in ('1','true','yes','on')

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgres://'):]
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgresql://'):]
if not DATABASE_URL:
    DATABASE_URL = 'sqlite:///' + str(LOCAL_DATA / 'devspace.sqlite3')

REQUIRE_PERSISTENT_STORAGE = env_bool('REQUIRE_PERSISTENT_STORAGE', False)
S3_ENDPOINT = os.environ.get('SPACES_ENDPOINT_URL', '').strip()
S3_BUCKET = os.environ.get('SPACES_BUCKET', '').strip()
S3_REGION = os.environ.get('SPACES_REGION', '').strip()
S3_KEY = os.environ.get('SPACES_ACCESS_KEY', '').strip()
S3_SECRET = os.environ.get('SPACES_SECRET_KEY', '').strip()
if REQUIRE_PERSISTENT_STORAGE:
    if not DATABASE_URL or DATABASE_URL.startswith('sqlite:'):
        raise RuntimeError('Production requires a PostgreSQL DATABASE_URL. Configure a managed PostgreSQL database.')
    if not all((S3_ENDPOINT, S3_BUCKET, S3_REGION, S3_KEY, S3_SECRET)):
        raise RuntimeError('Production requires DigitalOcean Spaces settings: SPACES_ENDPOINT_URL, SPACES_BUCKET, SPACES_REGION, SPACES_ACCESS_KEY, SPACES_SECRET_KEY.')

engine_kwargs = {'pool_pre_ping': True}
if DATABASE_URL.startswith('sqlite:'):
    engine_kwargs['connect_args'] = {'check_same_thread': False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
DBSession = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name_key: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    github_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    github_login: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bio: Mapped[str] = mapped_column(String(240), default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    account_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_downloads: Mapped[list] = mapped_column(JSON, default=list)
    last_account_download: Mapped[str | None] = mapped_column(String(40), nullable=True)

class Project(Base):
    __tablename__ = 'projects'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(80), index=True)
    language: Mapped[str] = mapped_column(String(40), default='Другой')
    category: Mapped[str] = mapped_column(String(40), default='Другое')
    description: Mapped[str] = mapped_column(String(1200), default='')
    author: Mapped[str] = mapped_column(String(24), index=True)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    liked_by: Mapped[list] = mapped_column(JSON, default=list)
    symbol: Mapped[str] = mapped_column(String(16), default='</>')
    theme: Mapped[str] = mapped_column(String(16), default='blue')
    files: Mapped[list] = mapped_column(JSON, default=list)
    image_file: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created: Mapped[int] = mapped_column(Integer, index=True)

Base.metadata.create_all(engine)

# S3-compatible object storage is used in production so uploads survive app redeploys.
s3 = None
if all((S3_ENDPOINT, S3_BUCKET, S3_REGION, S3_KEY, S3_SECRET)):
    import boto3
    from botocore.config import Config
    s3 = boto3.client('s3', endpoint_url=S3_ENDPOINT, region_name=S3_REGION,
                      aws_access_key_id=S3_KEY, aws_secret_access_key=S3_SECRET,
                      config=Config(signature_version='s3v4', s3={'addressing_style':'virtual'}))


def storage_put(path: Path, key: str, content_type: str):
    if s3:
        s3.upload_file(str(path), S3_BUCKET, key, ExtraArgs={'ContentType': content_type or 'application/octet-stream'})
    else:
        target = LOCAL_UPLOADS / Path(key).name
        if path.resolve() != target.resolve():
            target.write_bytes(path.read_bytes())


def storage_get(key: str) -> bytes:
    if s3:
        return s3.get_object(Bucket=S3_BUCKET, Key=key)['Body'].read()
    target = LOCAL_UPLOADS / Path(key).name
    if not target.is_file(): raise FileNotFoundError(key)
    return target.read_bytes()


def storage_delete(key: str):
    try:
        if s3: s3.delete_object(Bucket=S3_BUCKET, Key=key)
        else: (LOCAL_UPLOADS / Path(key).name).unlink(missing_ok=True)
    except Exception:
        app.logger.exception('Could not delete stored object %s', key)


def save_upload(file):
    original = Path(file.filename or 'file').name
    safe = secure_filename(original) or 'file'
    suffix = ''.join(Path(safe).suffixes)[-12:]
    key = uuid.uuid4().hex + suffix
    temp = LOCAL_DATA / (uuid.uuid4().hex + '.upload')
    file.save(temp)
    size = temp.stat().st_size
    try: storage_put(temp, key, file.mimetype or mimetypes.guess_type(original)[0] or 'application/octet-stream')
    finally: temp.unlink(missing_ok=True)
    return {'id': key, 'name': original[:180] or 'file', 'size': size, 'storedName': key,
            'mime': file.mimetype or mimetypes.guess_type(original)[0] or 'application/octet-stream'}


def safe_user(u): return {'name': u.name, 'bio': u.bio or ''}
def safe_project(p, viewer=None):
    image_url = '/uploads/' + p.image_file['storedName'] if p.image_file and p.image_file.get('storedName') else ''
    return {'id':p.id, 'title':p.title, 'language':p.language, 'category':p.category,
            'description':p.description or '', 'author':p.author, 'likes':p.likes,
            'likedBy':p.liked_by or [], 'liked':bool(viewer and viewer in (p.liked_by or [])),
            'symbol':p.symbol, 'theme':p.theme, 'files':p.files or [], 'imageFile':p.image_file,
            'imageUrl':image_url, 'created':p.created}

def get_user(db, name):
    if not name: return None
    return db.scalar(select(User).where(User.name_key == str(name).strip().casefold()))
def current_user(): return session.get('user')
def auth_required():
    if not current_user(): return jsonify(error='Сначала войди в аккаунт.'), 401
    return None
def json_body(): return request.get_json(silent=True) or {}

def load_or_migrate_legacy_json():
    """Import old JSON metadata once when the new database is empty."""
    legacy = LOCAL_DATA / 'db.json'
    if not legacy.exists(): return
    with DBSession() as db:
        if db.scalar(select(func.count()).select_from(User)) or db.scalar(select(func.count()).select_from(Project)): return
        try: data = json.loads(legacy.read_text(encoding='utf-8'))
        except Exception: return
        for row in data.get('users', []):
            name = str(row.get('name') or '').strip()
            if not name or get_user(db, name): continue
            u = User(name=name[:24], name_key=name.casefold()[:24], password_hash=row.get('passwordHash'),
                     github_id=str(row.get('githubId')) if row.get('githubId') else None,
                     github_login=row.get('githubLogin'), bio=str(row.get('bio') or '')[:240],
                     created_at=row.get('createdAt') or now_iso(), account_token_hash=row.get('accountTokenHash'),
                     account_downloads=row.get('accountDownloads') or [], last_account_download=row.get('lastAccountDownload'))
            db.add(u)
        for row in data.get('projects', []):
            if not row.get('id') or not row.get('title') or not row.get('author'): continue
            db.add(Project(id=str(row['id'])[:36], title=str(row['title'])[:80], language=str(row.get('language') or 'Другой')[:40],
                           category=str(row.get('category') or 'Другое')[:40], description=str(row.get('description') or '')[:1200],
                           author=str(row['author'])[:24], likes=int(row.get('likes') or 0), liked_by=row.get('likedBy') or [],
                           symbol=str(row.get('symbol') or '</>')[:16], theme=str(row.get('theme') or 'blue')[:16],
                           files=row.get('files') or [], image_file=row.get('imageFile'), created=int(row.get('created') or 0)))
        try: db.commit()
        except IntegrityError: db.rollback(); app.logger.exception('Legacy JSON migration skipped due to conflicting records')

app = Flask(__name__)
secret = os.environ.get('SESSION_SECRET', '').strip()
if REQUIRE_PERSISTENT_STORAGE and len(secret) < 32:
    raise RuntimeError('Set SESSION_SECRET to a long random value (at least 32 characters).')
if not secret:
    secret_file = LOCAL_DATA / 'session-secret.txt'
    if secret_file.exists(): secret = secret_file.read_text(encoding='utf-8').strip()
    if len(secret) < 32:
        secret = secrets.token_hex(48); secret_file.write_text(secret, encoding='utf-8')
app.secret_key = secret
secure_cookies = env_bool('COOKIE_SECURE', REQUIRE_PERSISTENT_STORAGE)
app.config.update(MAX_CONTENT_LENGTH=110*1024*1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=secure_cookies, SESSION_COOKIE_SAMESITE='Lax',
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))
SITE_ORIGIN = os.environ.get('SITE_ORIGIN', '').rstrip('/')
SITE_URL = os.environ.get('SITE_URL', SITE_ORIGIN).rstrip('/')
API_PUBLIC_URL = os.environ.get('API_PUBLIC_URL', '').rstrip('/')
GITHUB_CLIENT_ID = os.environ.get('GITHUB_CLIENT_ID', '').strip()
GITHUB_CLIENT_SECRET = os.environ.get('GITHUB_CLIENT_SECRET', '').strip()
GITHUB_CALLBACK_URL = os.environ.get('GITHUB_CALLBACK_URL', (API_PUBLIC_URL + '/auth/github/callback') if API_PUBLIC_URL else '')

@app.get('/healthz')
def health():
    try:
        with DBSession() as db: db.execute(select(1))
        return jsonify(ok=True, database='connected', fileStorage='spaces' if s3 else 'local')
    except Exception: return jsonify(ok=False), 503

@app.get('/')
@app.get('/index.html')
def home(): return send_from_directory(ROOT, 'index.html')

@app.get('/tools/<path:filename>')
def download_tool(filename):
    # Only serve the bundled, known helper files; never expose arbitrary server paths.
    allowed = {'devspace_setup.py', 'devspace_import.py', 'devspace_data_helper.js', 'README_DevSpace_Python.md'}
    if filename not in allowed:
        abort(404)
    return send_from_directory(ROOT, filename, as_attachment=True, download_name=filename)

@app.get('/uploads/<path:filename>')
def uploaded_file(filename):
    if filename.startswith('.') or '/' in filename or '\\' in filename: abort(404)
    try: data = storage_get(filename)
    except Exception: abort(404)
    mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    return send_file(BytesIO(data), mimetype=mime, download_name=filename, conditional=False)

@app.get('/auth/github')
def github_login():
    if not GITHUB_CLIENT_ID or not GITHUB_CALLBACK_URL:
        return 'GitHub login is not configured. Set GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET and GITHUB_CALLBACK_URL.', 503
    state = secrets.token_urlsafe(32); session['githubOAuthState'] = state
    params = {'client_id':GITHUB_CLIENT_ID, 'redirect_uri':GITHUB_CALLBACK_URL, 'scope':'read:user user:email repo', 'state':state}
    return redirect('https://github.com/login/oauth/authorize?' + urlencode(params))

@app.get('/auth/github/callback')
def github_callback():
    def fail(msg): return redirect((SITE_URL or '/') + ('&' if '?' in (SITE_URL or '') else '?') + 'github_login_error=' + requests.utils.quote(msg, safe=''))
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET or not GITHUB_CALLBACK_URL: return 'GitHub OAuth is not configured.', 503
    code, state = request.args.get('code'), request.args.get('state')
    if not code or not state or not secrets.compare_digest(state, str(session.get('githubOAuthState') or '')):
        session.pop('githubOAuthState', None); return fail('Проверка GitHub-входа не прошла. Попробуй ещё раз.')
    session.pop('githubOAuthState', None)
    try:
        token_resp = requests.post('https://github.com/login/oauth/access_token', headers={'Accept':'application/json','User-Agent':'DevSpace'},
                                   json={'client_id':GITHUB_CLIENT_ID,'client_secret':GITHUB_CLIENT_SECRET,'code':code,'redirect_uri':GITHUB_CALLBACK_URL}, timeout=20)
        token = token_resp.json().get('access_token')
        if not token_resp.ok or not token: return fail('GitHub не подтвердил вход. Проверь настройки OAuth.')
        profile_resp = requests.get('https://api.github.com/user', headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'DevSpace'}, timeout=20)
        profile = profile_resp.json()
        if not profile_resp.ok or not profile.get('id') or not profile.get('login'): return fail('Не удалось получить профиль GitHub.')
        with DBSession() as db:
            u = db.scalar(select(User).where(User.github_id == str(profile['id'])))
            if not u:
                base = re.sub(r'[^\w.-]', '', str(profile['login']), flags=re.UNICODE)[:24] or 'github-user'
                name = base; n = 2
                while get_user(db, name):
                    tail = '-' + str(n); n += 1; name = base[:24-len(tail)] + tail
                u = User(name=name, name_key=name.casefold(), github_id=str(profile['id']), github_login=str(profile['login']), bio='', created_at=now_iso(), account_downloads=[])
                db.add(u)
            else: u.github_login = str(profile['login'])
            db.commit(); username = u.name
        session.clear(); session['user'] = username; session['github_access_token'] = token; session['github_login'] = str(profile['login']); session.permanent = True
        return redirect((SITE_URL or '/') + ('&' if '?' in (SITE_URL or '') else '?') + 'github_login=success')
    except Exception:
        app.logger.exception('GitHub OAuth error'); return fail('Не удалось войти через GitHub. Попробуй ещё раз.')

@app.get('/api/github/repos')
def github_repositories():
    token = session.get('github_access_token')
    if not current_user() or not token:
        return jsonify(error='Войди через GitHub, чтобы просмотреть свои репозитории.'), 401
    repos = []
    try:
        for page in range(1, 11):
            response = requests.get(
                'https://api.github.com/user/repos',
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'DevSpace'},
                params={'sort': 'updated', 'per_page': 100, 'page': page, 'affiliation': 'owner,collaborator,organization_member'},
                timeout=20,
            )
            if not response.ok:
                return jsonify(error='GitHub не отдал список репозиториев. Проверь разрешения приложения и войди через GitHub ещё раз.'), response.status_code
            batch = response.json()
            if not isinstance(batch, list):
                break
            repos.extend(batch)
            if len(batch) < 100:
                break
        return jsonify(repositories=[{
            'id': r.get('id'), 'name': r.get('name', ''), 'full_name': r.get('full_name', ''),
            'description': r.get('description') or '', 'html_url': r.get('html_url', ''),
            'private': bool(r.get('private')), 'language': r.get('language') or 'Не указан',
            'updated_at': r.get('updated_at', ''), 'default_branch': r.get('default_branch', 'main'),
            'stars': r.get('stargazers_count', 0), 'fork': bool(r.get('fork'))
        } for r in repos])
    except requests.RequestException:
        return jsonify(error='Не удалось связаться с GitHub. Попробуй ещё раз.'), 502

@app.get('/api/state')
def state():
    with DBSession() as db:
        users = db.scalars(select(User).order_by(User.id)).all()
        projects = db.scalars(select(Project).order_by(Project.created.desc())).all()
        return jsonify(users=[safe_user(u) for u in users], projects=[safe_project(p, current_user()) for p in projects], currentUser=current_user() or '')

@app.post('/api/register')
def register():
    body = json_body(); name = str(body.get('name') or '').strip(); password = str(body.get('password') or '')
    if not re.fullmatch(r'[\w .-]{2,24}', name, re.UNICODE): return jsonify(error='Имя: 2–24 символа, буквы, цифры, пробел, точка, дефис или подчёркивание.'), 400
    if len(password) < 8 or len(password) > 100: return jsonify(error='Пароль должен содержать от 8 до 100 символов.'), 400
    with DBSession() as db:
        if get_user(db, name): return jsonify(error='Это имя уже занято.'), 409
        u = User(name=name, name_key=name.casefold(), password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode(), bio='', created_at=now_iso(), account_downloads=[])
        db.add(u)
        try: db.commit()
        except IntegrityError: db.rollback(); return jsonify(error='Это имя уже занято.'), 409
    session.clear(); session['user'] = name; session.permanent = True
    return jsonify(user={'name':name,'bio':''}), 201

@app.post('/api/login')
def login():
    body = json_body(); name = str(body.get('name') or '').strip(); password = str(body.get('password') or '')
    with DBSession() as db:
        u = get_user(db, name)
        if not u or not u.password_hash: return jsonify(error='Неверное имя пользователя или пароль.'), 401
        try: valid = bcrypt.checkpw(password.encode(), u.password_hash.encode())
        except (ValueError, TypeError): valid = False
        if not valid: return jsonify(error='Неверное имя пользователя или пароль.'), 401
        username = u.name
    session.clear(); session['user'] = username; session['github_access_token'] = token; session['github_login'] = str(profile['login']); session.permanent = True
    return jsonify(user={'name':username})

@app.post('/api/logout')
def logout(): session.clear(); return jsonify(ok=True)

@app.patch('/api/profile')
def update_profile():
    denied = auth_required()
    if denied: return denied
    with DBSession() as db:
        u = get_user(db, current_user())
        if not u: return jsonify(error='Пользователь не найден.'), 404
        u.bio = str(json_body().get('bio') or '')[:240]; db.commit()
        return jsonify(user=safe_user(u))

@app.post('/api/projects')
def create_project():
    denied = auth_required()
    if denied: return denied
    title = str(request.form.get('title') or '').strip()
    if not title or len(title) > 80: return jsonify(error='Название проекта обязательно (до 80 символов).'), 400
    incoming = request.files.getlist('files'); images = request.files.getlist('image')
    if len(incoming) > 10 or len(images) > 1: return jsonify(error='Можно прикрепить не более 10 файлов и 1 изображения.'), 400
    saved = []
    try:
        files = []
        for f in incoming:
            if not f.filename: continue
            if f.content_length and f.content_length > 10*1024*1024: raise ValueError('Каждый файл должен быть не больше 10 МБ.')
            item = save_upload(f); saved.append(item['storedName']); files.append(item)
            if item['size'] > 10*1024*1024: raise ValueError('Каждый файл должен быть не больше 10 МБ.')
        img = None
        for image in images:
            if image and image.filename:
                if not (image.mimetype or '').startswith('image/'): raise ValueError('Картинка должна быть изображением.')
                img = save_upload(image); saved.append(img['storedName'])
                if img['size'] > 10*1024*1024: raise ValueError('Изображение должно быть не больше 10 МБ.')
                break
        with DBSession() as db:
            p = Project(id=str(uuid.uuid4()), title=title, language=str(request.form.get('language') or 'Другой')[:40],
                        category=str(request.form.get('category') or 'Другое')[:40], description=str(request.form.get('description') or '')[:1200],
                        author=current_user(), likes=0, liked_by=[], symbol=['</>','{ }','▦','✦','◈'][secrets.randbelow(5)],
                        theme=['mint','blue','orange'][db.scalar(select(func.count()).select_from(Project))%3], files=files,
                        image_file={'storedName':img['storedName'],'name':img['name'],'mime':img['mime']} if img else None,
                        created=int(datetime.now(timezone.utc).timestamp()*1000))
            db.add(p); db.commit(); db.refresh(p)
            return jsonify(safe_project(p, current_user())), 201
    except (ValueError, OSError) as exc:
        for key in saved: storage_delete(key)
        return jsonify(error=str(exc) or 'Не удалось сохранить файлы.'), 400
    except Exception:
        for key in saved: storage_delete(key)
        app.logger.exception('Project creation failed')
        return jsonify(error='Не удалось сохранить проект. Проверь базу данных и хранилище файлов.'), 500

@app.post('/api/projects/<project_id>/like')
def like_project(project_id):
    denied = auth_required()
    if denied: return denied
    with DBSession() as db:
        p = db.get(Project, project_id)
        if not p: return jsonify(error='Проект не найден.'), 404
        liked = list(p.liked_by or [])
        if current_user() in liked: liked.remove(current_user()); p.likes = max(0, p.likes-1)
        else: liked.append(current_user()); p.likes += 1
        p.liked_by = liked; db.commit(); db.refresh(p)
        return jsonify(safe_project(p, current_user()))

@app.delete('/api/projects/<project_id>')
def delete_project(project_id):
    denied = auth_required()
    if denied: return denied
    with DBSession() as db:
        p = db.get(Project, project_id)
        if not p: return jsonify(error='Проект не найден.'), 404
        if p.author != current_user(): return jsonify(error='Удалять можно только свои проекты.'), 403
        keys = [f.get('storedName') for f in (p.files or []) if f.get('storedName')]
        if p.image_file and p.image_file.get('storedName'): keys.append(p.image_file['storedName'])
        db.delete(p); db.commit()
    for key in keys: storage_delete(key)
    return jsonify(ok=True)

@app.get('/api/projects/<project_id>/files/<file_id>')
def download_project_file(project_id, file_id):
    with DBSession() as db:
        p = db.get(Project, project_id)
        f = next((x for x in (p.files or []) if x.get('id') == file_id), None) if p else None
        if not f: return 'Файл не найден', 404
        key = Path(f.get('storedName') or '').name
        filename = f.get('name') or 'project-file'; mime = f.get('mime') or 'application/octet-stream'
    try: data = storage_get(key)
    except Exception: return 'Файл отсутствует в постоянном хранилище', 404
    resp = send_file(BytesIO(data), mimetype=mime, as_attachment=True, download_name=filename, conditional=False)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/api/account/export', methods=['GET','POST'])
def export_account():
    denied = auth_required()
    if denied: return denied
    with DBSession() as db:
        u = get_user(db, current_user())
        if not u: return jsonify(error='Пользователь не найден.'), 404
        token = secrets.token_hex(32); downloaded = now_iso(); u.account_token_hash = hashlib.sha256(token.encode()).hexdigest()
        u.last_account_download = downloaded; history = list(u.account_downloads or []); history.append(downloaded); u.account_downloads = history[-100:]
        owned = db.scalars(select(Project).where(Project.author == u.name).order_by(Project.created.desc())).all()
        projects = []
        for p in owned:
            files = []
            for f in (p.files or []):
                try: data = 'data:' + (f.get('mime') or 'application/octet-stream') + ';base64,' + base64.b64encode(storage_get(f.get('storedName',''))).decode()
                except Exception: data = ''
                files.append({'id':f.get('id'),'name':f.get('name'),'size':f.get('size'),'mime':f.get('mime') or 'application/octet-stream','data':data})
            q = {k:getattr(p, {'likedBy':'liked_by','imageFile':'image_file'}.get(k,k), None) for k in ('id','title','language','category','description','author','likes','created','symbol','theme')}
            q['likedBy'] = p.liked_by or []; q['files'] = files; projects.append(q)
        stats = {'projects':len(owned),'likesReceived':sum(int(p.likes or 0) for p in owned),
                 'likesGiven':sum(1 for p in db.scalars(select(Project)).all() if u.name in (p.liked_by or [])),
                 'totalProjectFiles':sum(len(p.files or []) for p in owned)}
        payload = {'type':'devspace-account','version':2,'name':u.name,'token':token,'bio':u.bio or '',
                   'createdAt':u.created_at,'downloadedAt':downloaded,'lastDownloadAt':downloaded,
                   'downloadHistory':u.account_downloads,'stats':stats,'projects':projects}
        db.commit()
    resp = jsonify(payload); resp.headers['Cache-Control'] = 'no-store'; return resp

@app.post('/api/account/import')
def import_account():
    body = json_body(); name = str(body.get('name') or '').strip(); token = str(body.get('token') or '')
    if body.get('type') != 'devspace-account' or not name or not re.fullmatch(r'[a-f0-9]{64}', token, re.I): return jsonify(error='Файл аккаунта повреждён или имеет неверный формат.'), 400
    with DBSession() as db:
        u = get_user(db, name); token_hash = hashlib.sha256(token.encode()).hexdigest()
        if not u or not u.account_token_hash or not secrets.compare_digest(token_hash, u.account_token_hash): return jsonify(error='Файл аккаунта недействителен. Войди по паролю и скачай новый файл.'), 401
        username = u.name
    session.clear(); session['user'] = username; session['github_access_token'] = token; session['github_login'] = str(profile['login']); session.permanent = True
    return jsonify(user={'name':username})

@app.errorhandler(413)
def too_large(_): return jsonify(error='Загружаемые файлы слишком большие.'), 413
@app.errorhandler(Exception)
def handle_error(exc):
    if isinstance(exc, HTTPException): return jsonify(error=exc.description), exc.code
    app.logger.exception('Unhandled request error')
    return jsonify(error='Ошибка сервера. Проверь настройки и журналы запуска.'), 500

# Keep old JSON data available for one-time migration when moving from an earlier build.
try: load_or_migrate_legacy_json()
except Exception: app.logger.exception('Legacy data migration failed')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8080')))
