import React, { useState, useRef } from 'react';
import './AudioTranscription.css';

const AudioTranscription = ({ socket, transcription }) => {
  const [isRecording, setIsRecording] = useState(false); 
  const mediaRecorderRef = useRef(null); 
  const audioStreamRef = useRef(null);

  const startRecording = async () => {
    setIsRecording(true);
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioStreamRef.current = stream;

    console.log("MediaRecorder starting..."); 

    const mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
    mediaRecorderRef.current = mediaRecorder;

    mediaRecorder.onstart = () => {
      console.log("Recording started!"); 
    };

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0 && socket && socket.connected) {
        console.log("Sending audio chunk...");
        socket.emit('audio_chunk', event.data);
      }
    };

    mediaRecorder.start(250);
  };

  const stopRecording = () => {
    setIsRecording(false);
    if (mediaRecorderRef.current) {
      mediaRecorderRef.current.stop(); 
    }

    if (audioStreamRef.current) {
      audioStreamRef.current.getTracks().forEach(track => track.stop()); 
    }

    if (socket && socket.connected) {
      socket.emit('stop_recording', "Recording stopped");
    }
  };

  return (
    <div className="transcription-container">
      <h2 className="title">Real-time Speech Transcription</h2>
      <div className="buttons">
        <button className={`btn ${isRecording ? 'btn-disabled' : 'btn-start'}`} onClick={startRecording} disabled={isRecording}>
          Start Recording
        </button>
        <button className={`btn ${!isRecording ? 'btn-disabled' : 'btn-stop'}`} onClick={stopRecording} disabled={!isRecording}>
          Stop Recording
        </button>
      </div>
      <div className="transcription-results">
        <h3>Transcription Results:</h3>
        <p>{transcription}</p>
      </div>
    </div>
  );
};

export default AudioTranscription;