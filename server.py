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

# DB 연결 정보
DATABASE_URL = os.environ.get('DATABASE_URL')

# 메모리 캐시: 현재 접속한 사용자 (sid 단위)
users = {}  # {sid: {nickname, current_room}}
nick_to_sid = {}  # {nickname: sid}
typing_state = {}  # {room_id: set of nicknames}


@contextmanager
def get_db():
    """DB 커넥션 컨텍스트 매니저"""
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
    """간단한 DB 쿼리 헬퍼"""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params or ())
        if fetch:
            result = cur.fetchall()
            return [dict(r) for r in result]
        return None


def db_query_one(sql, params=None):
    """한 행만 가져오기"""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return dict(row) if row else None


def get_room_list():
    """방 목록 (DB에서)"""
    rooms = db_query(
        """SELECT id, name, password, created_at FROM rooms ORDER BY created_at DESC""",
        fetch=True
    )
    result = []
    for r in rooms:
        # 현재 접속자 수 계산 (메모리에서)
        user_count = sum(1 for u in users.values() if u.get('current_room') == r['id'])
        result.append({
            'id': r['id'],
            'name': r['name'],
            'has_password': bool(r['password']),
            'user_count': user_count
        })
    return result


def get_user_list():
    return [{'nickname': u['nickname']} for u in users.values()]


def dm_room_id(nick1, nick2):
    """DM 방 ID (양쪽 닉네임 정렬)"""
    return 'dm_' + '_'.join(sorted([nick1, nick2]))


def save_message(room_id, nickname, msg):
    """메시지 DB 저장"""
    db_query(
        """INSERT INTO messages (room_id, nickname, msg) VALUES (%s, %s, %s)""",
        (room_id, nickname, msg)
    )


def get_recent_messages(room_id, limit=50):
    """최근 메시지 조회"""
    msgs = db_query(
        """SELECT nickname, msg, EXTRACT(EPOCH FROM created_at) as time
           FROM messages WHERE room_id = %s 
           ORDER BY created_at DESC LIMIT %s""",
        (room_id, limit),
        fetch=True
    )
    # Decimal -> float 변환 (JSON 직렬화 위해)
    for m in msgs:
        if m.get('time') is not None:
            m['time'] = float(m['time'])
    # 오래된 것부터 정렬
    msgs.reverse()
    return msgs


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/register', methods=['POST'])
def register():
    """회원가입"""
    data = request.json
    nickname = data.get('nickname', '').strip()
    password = data.get('password', '')
    
    if not nickname:
        return jsonify({'error': '닉네임을 입력하세요'}), 400
    if len(nickname) > 12:
        return jsonify({'error': '닉네임은 12자 이내'}), 400
    if len(password) < 4:
        return jsonify({'error': '비밀번호는 4자 이상'}), 400
    
    # 중복 체크
    existing = db_query_one("SELECT id FROM users WHERE nickname = %s", (nickname,))
    if existing:
        return jsonify({'error': '이미 사용중인 닉네임입니다'}), 400
    
    # 가입
    pw_hash = generate_password_hash(password)
    db_query(
        """INSERT INTO users (nickname, password) VALUES (%s, %s)""",
        (nickname, pw_hash)
    )
    return jsonify({'success': True})


@app.route('/api/login', methods=['POST'])
def login():
    """로그인"""
    data = request.json
    nickname = data.get('nickname', '').strip()
    password = data.get('password', '')
    
    user = db_query_one("SELECT * FROM users WHERE nickname = %s", (nickname,))
    if not user:
        return jsonify({'error': '존재하지 않는 닉네임입니다'}), 400
    
    if not check_password_hash(user['password'], password):
        return jsonify({'error': '비밀번호가 틀렸습니다'}), 400
    
    return jsonify({'success': True, 'nickname': nickname})


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
        # 입력 중 상태 제거
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
    """로그인 후 socket 연결"""
    sid = request.sid
    nickname = data.get('nickname', '').strip()
    
    if not nickname:
        return
    
    # 이미 접속한 같은 닉네임 있으면 끊어내기
    if nickname in nick_to_sid:
        old_sid = nick_to_sid[nickname]
        if old_sid != sid:
            socketio.emit('force_logout', '다른 곳에서 로그인되었습니다', room=old_sid)
            users.pop(old_sid, None)
    
    users[sid] = {'nickname': nickname, 'current_room': None}
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
    
    # 이전 방 나가기
    old_room = users[sid].get('current_room')
    if old_room:
        leave_room(old_room)
        emit('system', f'{nickname}님이 나갔습니다', room=old_room)
        if old_room in typing_state:
            typing_state[old_room].discard(nickname)
    
    join_room(room_id)
    users[sid]['current_room'] = room_id
    
    # 이전 메시지 불러오기
    messages = get_recent_messages(room_id)
    
    emit('join_success', {
        'room_id': room_id,
        'room_name': room['name'],
        'type': 'room',
        'history': messages
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
    
    # 이전 방 나가기
    old_room = users[sid].get('current_room')
    if old_room:
        leave_room(old_room)
        emit('system', f'{my_nick}님이 나갔습니다', room=old_room)
        if old_room in typing_state:
            typing_state[old_room].discard(my_nick)
    
    join_room(dm_id)
    users[sid]['current_room'] = dm_id
    
    # 상대방도 DM 방에 참여시키기
    socketio.server.enter_room(target_sid, dm_id)
    
    # 이전 DM 메시지 불러오기
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
    
    # DB에 저장
    save_message(current_room, nickname, msg)
    
    emit('message', {
        'nickname': nickname,
        'msg': msg,
        'time': time.time()
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
