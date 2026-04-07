from flask import Flask, render_template, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
import os
import uuid
from datetime import datetime
import cv2
import threading
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'video_stream_secret_key_2024'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///videos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 确保上传目录存在
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 数据库模型
class Video(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    filename = db.Column(db.String(200), nullable=False)
    cover = db.Column(db.String(200))
    views = db.Column(db.Integer, default=0)
    likes = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(db.String(36), db.ForeignKey('video.id'), nullable=False)
    username = db.Column(db.String(50), nullable=False, default='匿名用户')
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# 路由
@app.route('/')
def index():
    videos = Video.query.order_by(Video.created_at.desc()).all()
    return render_template('index.html', videos=videos)

@app.route('/video/<video_id>')
def video_page(video_id):
    video = Video.query.get_or_404(video_id)
    video.views += 1
    db.session.commit()
    comments = Comment.query.filter_by(video_id=video_id).order_by(Comment.created_at.desc()).all()
    return render_template('video.html', video=video, comments=comments)

@app.route('/api/video/<video_id>/like', methods=['POST'])
def like_video(video_id):
    video = Video.query.get_or_404(video_id)
    video.likes += 1
    db.session.commit()
    return jsonify({'likes': video.likes})

@app.route('/api/video/<video_id>/comments', methods=['GET'])
def get_comments(video_id):
    comments = Comment.query.filter_by(video_id=video_id).order_by(Comment.created_at.desc()).all()
    return jsonify([{
        'id': c.id,
        'username': c.username,
        'content': c.content,
        'created_at': c.created_at.strftime('%Y-%m-%d %H:%M')
    } for c in comments])

@app.route('/api/video/<video_id>/comment', methods=['POST'])
def add_comment(video_id):
    data = request.get_json()
    comment = Comment(
        video_id=video_id,
        username=data.get('username', '匿名用户'),
        content=data.get('content', '')
    )
    db.session.add(comment)
    db.session.commit()

    # 广播新评论
    socketio.emit('new_comment', {
        'id': comment.id,
        'video_id': video_id,
        'username': comment.username,
        'content': comment.content,
        'created_at': comment.created_at.strftime('%Y-%m-%d %H:%M')
    }, room=video_id)

    return jsonify({'success': True, 'comment': {
        'id': comment.id,
        'username': comment.username,
        'content': comment.content,
        'created_at': comment.created_at.strftime('%Y-%m-%d %H:%M')
    }})

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        title = request.form.get('title', '')
        description = request.form.get('description', '')
        video_file = request.files.get('video')

        if video_file and video_file.filename:
            filename = f"{uuid.uuid4()}_{video_file.filename}"
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            video_file.save(filepath)

            video = Video(
                title=title,
                description=description,
                filename=filename
            )
            db.session.add(video)
            db.session.commit()

            return jsonify({'success': True, 'message': '视频上传成功!'})

    return render_template('upload.html')

# WebSocket 事件
@socketio.on('join_video')
def join_video(data):
    video_id = data.get('video_id')
    if video_id:
        # 使用 session 来追踪观众
        emit('user_joined', {'count': 1}, broadcast=True)

@socketio.on('leave_video')
def leave_video(data):
    video_id = data.get('video_id')
    if video_id:
        emit('user_left', {'count': -1}, broadcast=True)

# 摄像头直播功能
def generate_frames():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
        return
    
    try:
        while True:
            success, frame = cap.read()
            if not success:
                break
            
            # 调整分辨率
            frame = cv2.resize(frame, (640, 480))
            
            # 添加时间戳
            font = cv2.FONT_HERSHEY_SIMPLEX
            text = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cv2.putText(frame, text, (10, 30), font, 1, (0, 255, 0), 2, cv2.LINE_AA)
            
            # 编码为JPEG
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
    finally:
        cap.release()

# 本地视频文件直播功能
def generate_video_frames(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
        return
    
    try:
        while True:
            success, frame = cap.read()
            if not success:
                # 视频播放完毕，重新开始
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            
            # 调整分辨率
            frame = cv2.resize(frame, (854, 480))
            
            # 编码为JPEG
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
    finally:
        cap.release()

# 直播路由
@app.route('/live')
def live():
    return render_template('live.html')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video_file_feed/<filename>')
def video_file_feed(filename):
    video_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(video_path):
        return Response(generate_video_frames(video_path), mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        return "视频文件不存在", 404

# 创建数据库
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=9000)