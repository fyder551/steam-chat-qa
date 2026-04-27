import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit, join_room
import requests
import re
import sqlite3
import xml.etree.ElementTree as ET
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import random
import string
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'supersecretkey'
socketio = SocketIO(app, cors_allowed_origins="*")

# Настройка загрузки изображений
UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# ========== ВСТАВЬТЕ ВАШ РЕАЛЬНЫЙ КЛЮЧ STEAM ==========
STEAM_API_KEY = 'CF7D5EE328210C09255B2A04313C6E95'
# ===================================================

def validate_login(login):
    return re.match(r'^[A-Z][A-Za-z0-9]{7,}$', login) is not None

def generate_invite_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

# ---------- База данных ----------
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        steam_id TEXT,
        steam_name TEXT,
        avatar TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS friends (
        user TEXT,
        friend TEXT,
        status TEXT DEFAULT 'pending',
        PRIMARY KEY(user, friend)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        owner TEXT NOT NULL,
        is_open BOOLEAN DEFAULT 1,
        password TEXT,
        invite_code TEXT UNIQUE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER,
        username TEXT,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user TEXT NOT NULL,
        to_user TEXT,
        group_id INTEGER,
        message TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS join_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        username TEXT,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
    )''')
    conn.commit()
    conn.close()

init_db()

def get_user(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT username, password, steam_id, steam_name, avatar FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    conn.close()
    return user

def create_user(username, password):
    if not validate_login(username):
        return False
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password, steam_id, steam_name, avatar) VALUES (?, ?, ?, ?, ?)',
                  (username, generate_password_hash(password), None, None, None))
        conn.commit()
        conn.close()
        return True
    except:
        conn.close()
        return False

def update_steam_info(username, steam_id, steam_name, avatar):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET steam_id = ?, steam_name = ?, avatar = ? WHERE username = ?',
              (steam_id, steam_name, avatar, username))
    conn.commit()
    conn.close()

def send_friend_request(from_user, to_user):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute('INSERT INTO friends (user, friend, status) VALUES (?, ?, ?)', (from_user, to_user, 'pending'))
        conn.commit()
        conn.close()
        return True
    except:
        conn.close()
        return False

def accept_friend_request(user, friend):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE friends SET status = "accepted" WHERE user = ? AND friend = ?', (friend, user))
    c.execute('INSERT OR IGNORE INTO friends (user, friend, status) VALUES (?, ?, ?)', (user, friend, 'accepted'))
    conn.commit()
    conn.close()

def get_friends(username, status='accepted'):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT friend FROM friends WHERE user = ? AND status = ?', (username, status))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_pending_requests(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT user FROM friends WHERE friend = ? AND status = "pending"', (username,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_sent_requests(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT friend FROM friends WHERE user = ? AND status = "pending"', (username,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def extract_steamid(link):
    link = link.strip()
    match = re.search(r'/profiles/(\d+)', link)
    if match:
        return match.group(1)
    match = re.search(r'/id/([^/?]+)', link)
    if match:
        username = match.group(1)
        try:
            resp = requests.get(f"https://steamcommunity.com/id/{username}/?xml=1", timeout=10)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                steamid64 = root.find('steamID64').text
                return steamid64
        except:
            return None
    return None

def get_steam_profile(steamid):
    if not STEAM_API_KEY or STEAM_API_KEY == 'ТВОЙ_КЛЮЧ_ИЗ_ШТЕЙМА':
        return None
    url = 'https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/'
    params = {'key': STEAM_API_KEY, 'steamids': steamid}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        players = data.get('response', {}).get('players', [])
        if players:
            p = players[0]
            return {'name': p.get('personaname'), 'avatar': p.get('avatarfull')}
    except:
        return None
    return None

def create_group(name, owner, is_open, password=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    invite_code = generate_invite_code()
    c.execute('INSERT INTO groups (name, owner, is_open, password, invite_code) VALUES (?, ?, ?, ?, ?)',
              (name, owner, is_open, password, invite_code))
    group_id = c.lastrowid
    c.execute('INSERT INTO group_members (group_id, username) VALUES (?, ?)', (group_id, owner))
    conn.commit()
    conn.close()
    return group_id, invite_code

def get_user_groups(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT g.id, g.name, g.owner, g.is_open FROM groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.username = ?', (username,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_group_by_id(group_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT id, name, owner, is_open, password, invite_code FROM groups WHERE id = ?', (group_id,))
    row = c.fetchone()
    conn.close()
    return row

def join_group(group_id, username, password_input=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    group = get_group_by_id(group_id)
    if not group:
        conn.close()
        return False, "Группа не найдена"
    if group[3]:
        c.execute('INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)', (group_id, username))
        conn.commit()
        conn.close()
        return True, "Вы вступили в группу"
    else:
        if password_input and password_input == group[4]:
            c.execute('INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)', (group_id, username))
            conn.commit()
            conn.close()
            return True, "Вы вступили в группу"
        else:
            conn.close()
            return False, "Неверный пароль"

def join_group_by_invite(invite_code, username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT id FROM groups WHERE invite_code = ?', (invite_code,))
    row = c.fetchone()
    if row:
        group_id = row[0]
        c.execute('INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)', (group_id, username))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

add_member_to_group = lambda group_id, username: None  # временно, но можно определить
def add_member_to_group(group_id, username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)', (group_id, username))
    conn.commit()
    conn.close()

def save_message(from_user, to_user, group_id, message):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT INTO messages (from_user, to_user, group_id, message) VALUES (?, ?, ?, ?)',
              (from_user, to_user, group_id, message))
    conn.commit()
    conn.close()

def get_messages_private(user1, user2):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''SELECT from_user, message, timestamp FROM messages
                 WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)
                 ORDER BY timestamp ASC''', (user1, user2, user2, user1))
    rows = c.fetchall()
    conn.close()
    return rows

def get_messages_group(group_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT from_user, message, timestamp FROM messages WHERE group_id=? ORDER BY timestamp ASC', (group_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# ---------- Маршруты ----------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if not username or not password:
            flash('Заполните все поля', 'error')
        elif not validate_login(username):
            flash('Логин должен начинаться с заглавной латинской буквы и содержать минимум 8 символов', 'error')
        elif create_user(username, password):
            flash('Регистрация успешна! Теперь войдите.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Пользователь с таким именем уже существует', 'error')
    return render_template('register.html')

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = get_user(username)
        if user and check_password_hash(user[1], password):
            session['user'] = username
            return redirect(url_for('dashboard'))
        else:
            flash('Неверный логин или пароль', 'error')
    return render_template('login.html')

@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    username = session['user']
    user = get_user(username)
    steam_id = user[2] if user else None
    steam_name = user[3] if user else None
    avatar = user[4] if user else None

    if request.method == 'POST':
        steam_link = request.form.get('steam_link')
        if steam_link:
            sid = extract_steamid(steam_link)
            if sid:
                profile = get_steam_profile(sid)
                if profile:
                    update_steam_info(username, sid, profile['name'], profile['avatar'])
                    flash('Steam профиль привязан!', 'success')
                else:
                    flash('Не удалось получить данные профиля', 'error')
            else:
                flash('Неверная ссылка на Steam профиль', 'error')
        return redirect(url_for('dashboard'))

    if not steam_id:
        return render_template('dashboard.html', user=username, steam_name=None, steam_id=None, avatar=None,
                               friends=[], groups=[], pending_requests=[], sent_requests=[])

    groups = get_user_groups(username)
    friends = get_friends(username)
    pending_requests = get_pending_requests(username)
    sent_requests = get_sent_requests(username)
    return render_template('dashboard.html', user=username, steam_name=steam_name, steam_id=steam_id, avatar=avatar,
                           friends=friends, groups=groups, pending_requests=pending_requests, sent_requests=sent_requests)

@app.route('/add_friend', methods=['POST'])
def add_friend():
    if 'user' not in session:
        return redirect(url_for('login'))
    friend_username = request.form.get('friend_username')
    if friend_username and get_user(friend_username):
        if send_friend_request(session['user'], friend_username):
            flash(f'Заявка отправлена {friend_username}', 'success')
        else:
            flash('Заявка уже была отправлена', 'error')
    else:
        flash('Пользователь не найден', 'error')
    return redirect(url_for('dashboard'))

@app.route('/accept_friend/<friend>')
def accept_friend(friend):
    if 'user' not in session:
        return redirect(url_for('login'))
    accept_friend_request(session['user'], friend)
    flash(f'Вы друзья с {friend}', 'success')
    return redirect(url_for('dashboard'))

@app.route('/create_group_page')
def create_group_page():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('create_group.html')

@app.route('/create_group', methods=['POST'])
def create_group_route():
    if 'user' not in session:
        return redirect(url_for('login'))
    name = request.form.get('name')
    is_open = request.form.get('is_open') == 'on'
    password = request.form.get('password') if not is_open else None
    if name:
        group_id, invite_code = create_group(name, session['user'], is_open, password)
        flash(f'Группа {name} создана! Код приглашения: {invite_code}', 'success')
    else:
        flash('Введите название', 'error')
    return redirect(url_for('dashboard'))

@app.route('/invite_to_group', methods=['POST'])
def invite_to_group():
    if 'user' not in session:
        return redirect(url_for('login'))
    group_id = request.form.get('group_id')
    friend = request.form.get('friend')
    if group_id and group_id.isdigit() and friend:
        friends = get_friends(session['user'])
        if friend in friends:
            add_member_to_group(int(group_id), friend)
            flash(f'{friend} приглашён в группу', 'success')
        else:
            flash('Это не ваш друг', 'error')
    else:
        flash('Ошибка', 'error')
    return redirect(url_for('dashboard'))

@app.route('/join_group', methods=['POST'])
def join_group_route():
    if 'user' not in session:
        return redirect(url_for('login'))
    group_id_str = request.form.get('group_id')
    password = request.form.get('password')
    if group_id_str and group_id_str.isdigit():
        success, msg = join_group(int(group_id_str), session['user'], password)
        flash(msg, 'success' if success else 'error')
    else:
        flash('ID группы должен быть числом', 'error')
    return redirect(url_for('dashboard'))

@app.route('/join_by_invite', methods=['POST'])
def join_by_invite():
    if 'user' not in session:
        return redirect(url_for('login'))
    invite_code = request.form.get('invite_code')
    if invite_code and join_group_by_invite(invite_code, session['user']):
        flash('Вы вступили по приглашению', 'success')
    else:
        flash('Неверный код', 'error')
    return redirect(url_for('dashboard'))

@app.route('/upload_image', methods=['POST'])
def upload_image():
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    file = request.files.get('image')
    if file and file.filename:
        filename = secure_filename(f"{datetime.now().timestamp()}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return jsonify({'url': f'/static/uploads/{filename}'})
    return jsonify({'error': 'No file'}), 400

@app.route('/chat')
def chat():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if not user[2]:
        flash('Сначала привяжите Steam', 'error')
        return redirect(url_for('dashboard'))
    friends = get_friends(session['user'])
    groups = get_user_groups(session['user'])
    friends_avatars = {}
    for f in friends:
        u = get_user(f)
        friends_avatars[f] = u[4] if u else ''
    my_avatar = user[4] or ''
    return render_template('chat.html', user=session['user'], friends=friends, groups=groups,
                           friends_avatars=friends_avatars, my_avatar=my_avatar)

@app.route('/get_messages')
def get_messages():
    if 'user' not in session:
        return jsonify([])
    with_user = request.args.get('with')
    group_id = request.args.get('group')
    if with_user:
        msgs = get_messages_private(session['user'], with_user)
    elif group_id and group_id.isdigit():
        msgs = get_messages_group(int(group_id))
    else:
        msgs = []
    result = []
    for msg in msgs:
        u = get_user(msg[0])
        avatar = u[4] if u else ''
        result.append((msg[0], msg[1], msg[2], avatar))
    return jsonify(result)

@app.route('/get_steam_id/<username>')
def get_steam_id(username):
    if 'user' not in session:
        return jsonify({'steam_id': None})
    u = get_user(username)
    return jsonify({'steam_id': u[2] if u else None})

@app.route('/steam_profile/<steam_id>')
def steam_profile(steam_id):
    return redirect(f'https://steamcommunity.com/profiles/{steam_id}/')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

# ---------- WebSocket ----------
active_users = {}

@socketio.on('connect')
def handle_connect():
    if 'user' in session:
        username = session['user']
        active_users[username] = request.sid
        for f in get_friends(username):
            if f in active_users:
                emit('status_change', {'user': username, 'status': 'online'}, room=active_users[f])

@socketio.on('disconnect')
def handle_disconnect():
    if 'user' in session:
        username = session['user']
        if username in active_users:
            del active_users[username]
        for f in get_friends(username):
            if f in active_users:
                emit('status_change', {'user': username, 'status': 'offline'}, room=active_users[f])

@socketio.on('join')
def handle_join(data):
    join_room(data['room'])

@socketio.on('send')
def handle_send(data):
    from_user = session['user']
    room = data['room']
    msg = data['msg']
    if room.startswith('private_'):
        parts = room.split('_')
        if len(parts) == 3:
            _, u1, u2 = parts
            to_user = u2 if u1 == from_user else u1
            save_message(from_user, to_user, None, msg)
    elif room.startswith('group_'):
        group_id = int(room.split('_')[1])
        save_message(from_user, None, group_id, msg)
    u = get_user(from_user)
    avatar = u[4] if u else ''
    emit('new_message', {'from': from_user, 'msg': msg, 'avatar': avatar}, room=room)
# ---------- Управление группами ----------
def rename_group(group_id, new_name):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE groups SET name = ? WHERE id = ?', (new_name, group_id))
    conn.commit()
    conn.close()

def delete_group(group_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
    c.execute('DELETE FROM groups WHERE id = ?', (group_id,))
    conn.commit()
    conn.close()

def change_group_type(group_id, is_open, password=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE groups SET is_open = ?, password = ? WHERE id = ?', (is_open, password, group_id))
    conn.commit()
    conn.close()

def unlink_steam(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET steam_id = NULL, steam_name = NULL, avatar = NULL WHERE username = ?', (username,))
    conn.commit()
    conn.close()

@app.route('/rename_group', methods=['POST'])
def rename_group_route():
    if 'user' not in session:
        return redirect(url_for('login'))
    group_id = request.form.get('group_id')
    new_name = request.form.get('new_name')
    if group_id and group_id.isdigit() and new_name:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT owner FROM groups WHERE id = ?', (int(group_id),))
        row = c.fetchone()
        conn.close()
        if row and row[0] == session['user']:
            rename_group(int(group_id), new_name)
            flash('Группа переименована', 'success')
        else:
            flash('Нет прав на переименование', 'error')
    else:
        flash('Неверные данные', 'error')
    return redirect(url_for('dashboard'))

@app.route('/delete_group', methods=['POST'])
def delete_group_route():
    if 'user' not in session:
        return redirect(url_for('login'))
    group_id = request.form.get('group_id')
    if group_id and group_id.isdigit():
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT owner FROM groups WHERE id = ?', (int(group_id),))
        row = c.fetchone()
        conn.close()
        if row and row[0] == session['user']:
            delete_group(int(group_id))
            flash('Группа удалена', 'success')
        else:
            flash('Нет прав на удаление', 'error')
    else:
        flash('Неверные данные', 'error')
    return redirect(url_for('dashboard'))

@app.route('/edit_group_settings', methods=['POST'])
def edit_group_settings_route():
    if 'user' not in session:
        return redirect(url_for('login'))
    group_id = request.form.get('group_id')
    is_open = request.form.get('is_open') == 'on'
    password = request.form.get('password') if not is_open else None
    if group_id and group_id.isdigit():
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT owner FROM groups WHERE id = ?', (int(group_id),))
        row = c.fetchone()
        conn.close()
        if row and row[0] == session['user']:
            change_group_type(int(group_id), is_open, password)
            flash('Настройки группы обновлены', 'success')
        else:
            flash('Нет прав на изменение', 'error')
    else:
        flash('Неверные данные', 'error')
    return redirect(url_for('dashboard'))

@app.route('/unlink_steam', methods=['POST'])
def unlink_steam_route():
    if 'user' not in session:
        return redirect(url_for('login'))
    unlink_steam(session['user'])
    flash('Steam аккаунт отвязан. Вы можете привязать другой.', 'success')
    return redirect(url_for('dashboard'))
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)