from flask import Flask
from flask_socketio import SocketIO, emit
import time 

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", transports=["websocket"])

@app.route('/')
def index():
    return "Flask Server is Running on localhost: 8080"

@socketio.on('connect')
def connect():
    print('Client connected')

@socketio.on('audio_chunk')
def handle_audio_chunk(audio_data):
    print("Received audio chunk")
    time.sleep(1)
    simulated_transcription = "This is a simulated transcription."
    print(simulated_transcription)
    emit('transcription_result', {'transcription': simulated_transcription})

@socketio.on('stop_recording')
def handle_stop_recording(msg):
    print(f"Recording stopped: {msg}")
    emit('transcription_result', {'transcription': 'Recording has stopped.'})

@socketio.on('disconnect')
def disconnect():
    print('Client disconnected')

if __name__ == '__main__':
    print("Server starts running ...")
    socketio.run(app, host='127.0.0.1', port=8080, debug=True)