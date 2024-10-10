import React, { useEffect, useRef, useState } from 'react';
import { io } from 'socket.io-client'; 
import AudioTranscription from './components/AudioTranscription';
import './App.css';

const App = () => {
  const [transcription, setTranscription] = useState("");
  const socket = useRef(null);

  const [tri, setTri] = useState(false)

  useEffect(() => {
    socket.current = io("http://localhost:8080", {
      transports: ["websocket"] 
    });

    socket.current.on("connect", () => {
      console.log("Socket.IO connection opened.");
    });

    socket.current.on("transcription_result", (data) => {
      console.log("Received message:", data['transcription']);
      setTranscription((prev) => prev + data['transcription'] + " ");
    });

    socket.current.on("disconnect", () => {
      console.log("Socket.IO connection closed.");
    });

    socket.current.on("error", (error) => {
      console.error("Socket.IO error:", error);
    });

    return () => {
      if (socket.current) {
        setTri(true)
      }
    };
  }, []);

  if (!socket.current) {
    return <div>Loading...</div>;
  }

  return (
    <div className="app">
      <AudioTranscription socket={socket.current} transcription={transcription} />
    </div>
  );
};

export default App;