from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

clients = {}

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    ip = request.remote_addr
    clients[request.sid] = ip
    print(f'[접속] {ip}')
    emit('system', f'누군가 입장했습니다.', broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    clients.pop(request.sid, None)
    emit('system', f'누군가 나갔습니다.', broadcast=True)

@socketio.on('message')
def handle_message(data):
    nickname = data.get('nickname', '익명')
    msg = data.get('msg', '')
    emit('message', {'nickname': nickname, 'msg': msg}, broadcast=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5614))
    socketio.run(app, host='0.0.0.0', port=port)
