from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash
import os
import time
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE_URL = os.environ.get('DATABASE_URL')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

# 메모리 캐시
users = {}  # {sid: {nickname, user_id, current_room}}
nick_to_sid = {}
typing_state = {}


@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def db_query(sql, params=None, fetch=False):
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params or ())
        if fetch:
            return [dict(r) for r in cur.fetchall()]
        return None


def db_query_one(sql, params=None):
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return dict(row) if row else None


def get_room_list():
    rooms = db_query(
        """SELECT id, name, password, created_by, created_at FROM rooms ORDER BY created_at DESC""",
        fetch=True
    )
    result = []
    for r in rooms:
        user_count = sum(1 for u in users.values() if u.get('current_room') == r['id'])
        result.append({
            'id': r['id'],
            'name': r['name'],
            'has_password': bool(r['password']),
            'user_count': user_count,
            'created_by': r['created_by']
        })
    return result


def get_user_list():
    return [{'nickname': u['nickname']} for u in users.values()]


def dm_room_id(nick1, nick2):
    return 'dm_' + '_'.join(sorted([nick1, nick2]))


def save_message(room_id, user_id, nickname, msg):
    """메시지 저장하고 ID 반환"""
    result = db_query_one(
        """INSERT INTO messages (room_id, user_id, nickname, msg) 
           VALUES (%s, %s, %s, %s) RETURNING id, EXTRACT(EPOCH FROM created_at) as time""",
        (room_id, user_id, nickname, msg)
    )
    return {
        'id': result['id'],
        'time': float(result['time'])
    }


def get_recent_messages(room_id, limit=50):
    msgs = db_query(
        """SELECT m.id, m.nickname, m.msg, m.user_id,
                  EXTRACT(EPOCH FROM m.created_at) as time,
                  u.avatar
           FROM messages m
           LEFT JOIN users u ON m.user_id = u.id
           WHERE m.room_id = %s 
           ORDER BY m.created_at DESC LIMIT %s""",
        (room_id, limit),
        fetch=True
    )
    for m in msgs:
        if m.get('time') is not None:
            m['time'] = float(m['time'])
    msgs.reverse()
    return msgs


def get_user_profile(nickname):
    """프로필 조회"""
    user = db_query_one(
        """SELECT id, nickname, avatar, bio, status, created_at FROM users WHERE nickname = %s""",
        (nickname,)
    )
    if user:
        user['created_at'] = user['created_at'].isoformat() if user.get('created_at') else None
        user['is_online'] = nickname in nick_to_sid
    return user


@app.route('/')
def index():
    return render_template('index.html', 
                         supabase_url=SUPABASE_URL, 
                         supabase_key=SUPABASE_KEY)


@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    nickname = data.get('nickname', '').strip()
    password = data.get('password', '')
    
    if not nickname:
        return jsonify({'error': '닉네임을 입력하세요'}), 400
    if len(nickname) > 12:
        return jsonify({'error': '닉네임은 12자 이내'}), 400
    if len(password) < 4:
        return jsonify({'error': '비밀번호는 4자 이상'}), 400
    
    existing = db_query_one("SELECT id FROM users WHERE nickname = %s", (nickname,))
    if existing:
        return jsonify({'error': '이미 사용중인 닉네임입니다'}), 400
    
    pw_hash = generate_password_hash(password)
    db_query(
        """INSERT INTO users (nickname, password) VALUES (%s, %s)""",
        (nickname, pw_hash)
    )
    return jsonify({'success': True})


@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    nickname = data.get('nickname', '').strip()
    password = data.get('password', '')
    
    user = db_query_one("SELECT * FROM users WHERE nickname = %s", (nickname,))
    if not user:
        return jsonify({'error': '존재하지 않는 닉네임입니다'}), 400
    
    if not check_password_hash(user['password'], password):
        return jsonify({'error': '비밀번호가 틀렸습니다'}), 400
    
    return jsonify({
        'success': True, 
        'nickname': nickname,
        'user_id': user['id'],
        'avatar': user.get('avatar', '😀'),
        'bio': user.get('bio', ''),
        'status': user.get('status', '')
    })


@app.route('/api/profile/<nickname>')
def get_profile(nickname):
    """프로필 조회"""
    profile = get_user_profile(nickname)
    if not profile:
        return jsonify({'error': '사용자를 찾을 수 없습니다'}), 404
    return jsonify(profile)


@app.route('/api/profile', methods=['POST'])
def update_profile():
    """프로필 수정"""
    data = request.json
    nickname = data.get('nickname', '').strip()
    password = data.get('password', '')
    
    # 비밀번호 확인
    user = db_query_one("SELECT * FROM users WHERE nickname = %s", (nickname,))
    if not user or not check_password_hash(user['password'], password):
        return jsonify({'error': '인증 실패'}), 401
    
    avatar = data.get('avatar', '😀')[:500]
    bio = data.get('bio', '')[:100]
    status = data.get('status', '')[:50]
    
    db_query(
        """UPDATE users SET avatar = %s, bio = %s, status = %s WHERE nickname = %s""",
        (avatar, bio, status, nickname)
    )
    return jsonify({'success': True})


@socketio.on('connect')
def handle_connect():
    print(f'[연결] {request.sid}')


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid not in users:
        return
    
    nickname = users[sid]['nickname']
    current_room = users[sid].get('current_room')
    
    if current_room:
        emit('system', f'{nickname}님이 나갔습니다', room=current_room)
        if current_room in typing_state:
            typing_state[current_room].discard(nickname)
    
    if nickname in nick_to_sid:
        del nick_to_sid[nickname]
    
    del users[sid]
    
    emit('user_list', get_user_list(), broadcast=True)
    emit('room_list', get_room_list(), broadcast=True)
    print(f'[퇴장] {nickname}')


@socketio.on('connect_user')
def handle_connect_user(data):
    sid = request.sid
    nickname = data.get('nickname', '').strip()
    user_id = data.get('user_id')
    
    if not nickname:
        return
    
    if nickname in nick_to_sid:
        old_sid = nick_to_sid[nickname]
        if old_sid != sid:
            socketio.emit('force_logout', '다른 곳에서 로그인되었습니다', room=old_sid)
            users.pop(old_sid, None)
    
    users[sid] = {
        'nickname': nickname, 
        'user_id': user_id,
        'current_room': None
    }
    nick_to_sid[nickname] = sid
    
    emit('user_list', get_user_list(), broadcast=True)
    emit('room_list', get_room_list())
    print(f'[접속] {nickname}')


@socketio.on('create_room')
def handle_create_room(data):
    sid = request.sid
    if sid not in users:
        return
    
    name = data.get('name', '').strip()
    password = data.get('password', '').strip()
    
    if not name:
        emit('error_msg', '방 이름을 입력하세요')
        return
    if len(name) > 20:
        emit('error_msg', '방 이름은 20자 이내')
        return
    
    room_id = str(uuid.uuid4())[:8]
    creator = users[sid]['nickname']
    
    db_query(
        """INSERT INTO rooms (id, name, password, created_by) VALUES (%s, %s, %s, %s)""",
        (room_id, name, password, creator)
    )
    
    emit('room_list', get_room_list(), broadcast=True)
    emit('room_created', {'id': room_id, 'name': name})
    print(f'[방생성] {name} by {creator}')


@socketio.on('delete_room')
def handle_delete_room(data):
    """방 삭제 (만든 사람만)"""
    sid = request.sid
    if sid not in users:
        return
    
    room_id = data.get('room_id')
    if not room_id:
        return
    
    room = db_query_one("SELECT * FROM rooms WHERE id = %s", (room_id,))
    if not room:
        emit('error_msg', '존재하지 않는 방입니다')
        return
    
    nickname = users[sid]['nickname']
    if room['created_by'] != nickname:
        emit('error_msg', '방을 만든 사람만 삭제할 수 있습니다')
        return
    
    # 방 안에 있는 사람들 강퇴
    for s, u in list(users.items()):
        if u.get('current_room') == room_id:
            socketio.emit('room_deleted', {'message': '방이 삭제되었습니다'}, room=s)
            users[s]['current_room'] = None
    
    # 메시지 + 방 삭제
    db_query("DELETE FROM messages WHERE room_id = %s", (room_id,))
    db_query("DELETE FROM rooms WHERE id = %s", (room_id,))
    
    emit('room_list', get_room_list(), broadcast=True)
    print(f'[방삭제] {room["name"]} by {nickname}')


@socketio.on('delete_message')
def handle_delete_message(data):
    """메시지 삭제 (자기 메시지만)"""
    sid = request.sid
    if sid not in users:
        return
    
    msg_id = data.get('msg_id')
    if not msg_id:
        return
    
    msg = db_query_one("SELECT * FROM messages WHERE id = %s", (msg_id,))
    if not msg:
        return
    
    nickname = users[sid]['nickname']
    if msg['nickname'] != nickname:
        emit('error_msg', '자기 메시지만 삭제할 수 있습니다')
        return
    
    db_query("DELETE FROM messages WHERE id = %s", (msg_id,))
    
    # 같은 방에 있는 사람들한테 알림
    emit('message_deleted', {'msg_id': msg_id}, room=msg['room_id'])
    print(f'[메시지삭제] id={msg_id} by {nickname}')


@socketio.on('join_room')
def handle_join(data):
    sid = request.sid
    if sid not in users:
        return
    
    room_id = data.get('room_id')
    password = data.get('password', '')
    
    room = db_query_one("SELECT * FROM rooms WHERE id = %s", (room_id,))
    if not room:
        emit('join_error', '존재하지 않는 방입니다')
        return
    
    if room['password'] and room['password'] != password:
        emit('join_error', '비밀번호가 틀렸습니다')
        return
    
    nickname = users[sid]['nickname']
    
    old_room = users[sid].get('current_room')
    if old_room:
        leave_room(old_room)
        emit('system', f'{nickname}님이 나갔습니다', room=old_room)
        if old_room in typing_state:
            typing_state[old_room].discard(nickname)
    
    join_room(room_id)
    users[sid]['current_room'] = room_id
    
    messages = get_recent_messages(room_id)
    
    emit('join_success', {
        'room_id': room_id,
        'room_name': room['name'],
        'type': 'room',
        'history': messages,
        'created_by': room['created_by']
    })
    emit('system', f'{nickname}님이 입장했습니다', room=room_id)
    emit('room_list', get_room_list(), broadcast=True)
    print(f'[입장] {nickname} → {room["name"]}')


@socketio.on('start_dm')
def handle_start_dm(data):
    sid = request.sid
    if sid not in users:
        return
    
    target_nick = data.get('nickname', '').strip()
    if target_nick not in nick_to_sid:
        emit('error_msg', '상대방이 접속해있지 않습니다')
        return
    
    my_nick = users[sid]['nickname']
    if target_nick == my_nick:
        emit('error_msg', '자기 자신과는 DM할 수 없습니다')
        return
    
    target_sid = nick_to_sid[target_nick]
    dm_id = dm_room_id(my_nick, target_nick)
    
    old_room = users[sid].get('current_room')
    if old_room:
        leave_room(old_room)
        emit('system', f'{my_nick}님이 나갔습니다', room=old_room)
        if old_room in typing_state:
            typing_state[old_room].discard(my_nick)
    
    join_room(dm_id)
    users[sid]['current_room'] = dm_id
    
    socketio.server.enter_room(target_sid, dm_id)
    
    messages = get_recent_messages(dm_id)
    
    emit('join_success', {
        'room_id': dm_id,
        'room_name': f'💬 {target_nick}',
        'type': 'dm',
        'target': target_nick,
        'history': messages
    })
    print(f'[DM] {my_nick} → {target_nick}')


@socketio.on('message')
def handle_message(data):
    sid = request.sid
    if sid not in users:
        return
    
    current_room = users[sid].get('current_room')
    if not current_room:
        return
    
    msg = data.get('msg', '').strip()
    if not msg:
        return
    
    nickname = users[sid]['nickname']
    user_id = users[sid].get('user_id')
    
    # DB 저장 + ID 받기
    saved = save_message(current_room, user_id, nickname, msg)
    
    # 사용자 아바타도 같이 가져오기
    user_data = db_query_one("SELECT avatar FROM users WHERE id = %s", (user_id,))
    avatar = user_data.get('avatar', '😀') if user_data else '😀'
    
    emit('message', {
        'id': saved['id'],
        'nickname': nickname,
        'msg': msg,
        'time': saved['time'],
        'avatar': avatar
    }, room=current_room)
    print(f'[메시지][{current_room}] {nickname}: {msg}')


@socketio.on('typing')
def handle_typing(data):
    sid = request.sid
    if sid not in users:
        return
    
    current_room = users[sid].get('current_room')
    if not current_room:
        return
    
    nickname = users[sid]['nickname']
    is_typing = data.get('typing', False)
    
    if current_room not in typing_state:
        typing_state[current_room] = set()
    
    if is_typing:
        typing_state[current_room].add(nickname)
    else:
        typing_state[current_room].discard(nickname)
    
    emit('typing', {
        'nickname': nickname,
        'typing': is_typing
    }, room=current_room, include_self=False)


if __name__ == '__main__':
    if not DATABASE_URL:
        print('⚠️  DATABASE_URL 환경변수가 설정되지 않았습니다!')
    port = int(os.environ.get('PORT', 5614))
    socketio.run(app, host='0.0.0.0', port=port)
