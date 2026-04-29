from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import time
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

rooms = {}
users = {}
nick_to_sid = {}


def get_user_list():
    return [{'nickname': u['nickname']} for sid, u in users.items()]


def get_room_list():
    return [{
        'id': rid,
        'name': r['name'],
        'has_password': bool(r['password']),
        'user_count': len(r['users'])
    } for rid, r in rooms.items()]


def dm_room_id(sid1, sid2):
    return 'dm_' + '_'.join(sorted([sid1, sid2]))


@app.route('/')
def index():
    return render_template('index.html')


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
    
    if current_room and current_room in rooms:
        rooms[current_room]['users'].pop(sid, None)
        emit('system', f'{nickname}님이 나갔습니다', room=current_room)
        emit('user_count', len(rooms[current_room]['users']), room=current_room)
        if len(rooms[current_room]['users']) == 0:
            del rooms[current_room]
    
    if nickname in nick_to_sid:
        del nick_to_sid[nickname]
    
    del users[sid]
    
    emit('user_list', get_user_list(), broadcast=True)
    emit('room_list', get_room_list(), broadcast=True)
    print(f'[퇴장] {nickname}')


@socketio.on('login')
def handle_login(data):
    sid = request.sid
    nickname = data.get('nickname', '').strip()
    
    if not nickname:
        emit('login_error', '닉네임을 입력하세요')
        return
    if len(nickname) > 12:
        emit('login_error', '닉네임은 12자 이내')
        return
    if nickname in nick_to_sid:
        emit('login_error', '이미 사용중인 닉네임입니다')
        return
    
    users[sid] = {'nickname': nickname, 'current_room': None}
    nick_to_sid[nickname] = sid
    
    emit('login_success', {'nickname': nickname})
    emit('user_list', get_user_list(), broadcast=True)
    emit('room_list', get_room_list())
    print(f'[로그인] {nickname}')


@socketio.on('create_room')
def handle_create_room(data):
    name = data.get('name', '').strip()
    password = data.get('password', '').strip()
    
    if not name:
        emit('error_msg', '방 이름을 입력하세요')
        return
    if len(name) > 20:
        emit('error_msg', '방 이름은 20자 이내')
        return
    
    room_id = str(uuid.uuid4())[:8]
    rooms[room_id] = {
        'name': name,
        'password': password,
        'created_at': time.time(),
        'users': {}
    }
    
    emit('room_list', get_room_list(), broadcast=True)
    emit('room_created', {'id': room_id, 'name': name})
    print(f'[방생성] {name}')


@socketio.on('join_room')
def handle_join(data):
    sid = request.sid
    if sid not in users:
        return
    
    room_id = data.get('room_id')
    password = data.get('password', '')
    
    if not room_id or room_id not in rooms:
        emit('join_error', '존재하지 않는 방입니다')
        return
    
    room = rooms[room_id]
    if room['password'] and room['password'] != password:
        emit('join_error', '비밀번호가 틀렸습니다')
        return
    
    nickname = users[sid]['nickname']
    
    old_room = users[sid].get('current_room')
    if old_room and old_room in rooms and not old_room.startswith('dm_'):
        leave_room(old_room)
        rooms[old_room]['users'].pop(sid, None)
        emit('system', f'{nickname}님이 나갔습니다', room=old_room)
        emit('user_count', len(rooms[old_room]['users']), room=old_room)
        if len(rooms[old_room]['users']) == 0:
            del rooms[old_room]
            emit('room_list', get_room_list(), broadcast=True)
    
    join_room(room_id)
    room['users'][sid] = nickname
    users[sid]['current_room'] = room_id
    
    emit('join_success', {
        'room_id': room_id,
        'room_name': room['name'],
        'type': 'room'
    })
    emit('system', f'{nickname}님이 입장했습니다', room=room_id)
    emit('user_count', len(room['users']), room=room_id)
    emit('room_list', get_room_list(), broadcast=True)
    print(f'[입장] {nickname} → {room["name"]}')


@socketio.on('start_dm')
def handle_start_dm(data):
    sid = request.sid
    if sid not in users:
        return
    
    target_nick = data.get('nickname', '').strip()
    if target_nick not in nick_to_sid:
        emit('error_msg', '상대방을 찾을 수 없습니다')
        return
    
    target_sid = nick_to_sid[target_nick]
    if target_sid == sid:
        emit('error_msg', '자기 자신과는 DM할 수 없습니다')
        return
    
    my_nick = users[sid]['nickname']
    dm_id = dm_room_id(sid, target_sid)
    
    old_room = users[sid].get('current_room')
    if old_room and old_room in rooms and not old_room.startswith('dm_'):
        leave_room(old_room)
        rooms[old_room]['users'].pop(sid, None)
        emit('system', f'{my_nick}님이 나갔습니다', room=old_room)
        emit('user_count', len(rooms[old_room]['users']), room=old_room)
        if len(rooms[old_room]['users']) == 0:
            del rooms[old_room]
            emit('room_list', get_room_list(), broadcast=True)
    
    join_room(dm_id)
    users[sid]['current_room'] = dm_id
    
    emit('join_success', {
        'room_id': dm_id,
        'room_name': f'💬 {target_nick}',
        'type': 'dm',
        'target': target_nick
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
    
    if current_room.startswith('dm_'):
        parts = current_room.replace('dm_', '').split('_')
        for other_sid in parts:
            if other_sid != sid and other_sid in users:
                join_room(current_room, sid=other_sid)
    
    emit('message', {
        'nickname': nickname,
        'msg': msg,
        'time': time.time()
    }, room=current_room)
    print(f'[메시지] {nickname}: {msg}')


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
    
    if current_room.startswith('dm_'):
        parts = current_room.replace('dm_', '').split('_')
        for other_sid in parts:
            if other_sid != sid and other_sid in users:
                join_room(current_room, sid=other_sid)
    
    emit('typing', {
        'nickname': nickname,
        'typing': is_typing
    }, room=current_room, include_self=False)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5614))
    socketio.run(app, host='0.0.0.0', port=port)
