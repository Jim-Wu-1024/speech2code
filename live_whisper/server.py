import os
import time
import json
import functools
import threading
import logging
from enum import Enum
from typing import List, Optional

import numpy as np
import torch
from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed

from faster_whisper.transcribe import WhisperModel

logging.basicConfig(level=logging.INFO)


class ClientManager:
    def __init__(self, max_clients=4, max_connection_time=600):
        """
        Initializes the ClientManager with specified limits on client connections and connection durations.

        Args:
            max_clients (int, optional): The maximum number of simultaneous client connections allowed. Defaults to 4.
            max_connection_time (int, optional): The maximum duration (in seconds) a client can stay connected. Defaults
                                                 to 600 seconds (10 minutes).
        """
        self.clients = {}
        self.start_times = {}
        self.max_clients = max_clients
        self.max_connection_time = max_connection_time

    def add_client(self, websocket, client):
        """
        Adds a client and their connection start time to the tracking dictionaries.

        Args:
            websocket: The websocket associated with the client to add.
            client: The client object to be added and tracked.
        """
        self.clients[websocket] = client
        self.start_times[websocket] = time.time()

    def get_client(self, websocket):
        """
        Retrieves a client associated with the given websocket.

        Args:
            websocket: The websocket associated with the client to retrieve.

        Returns:
            The client object if found, False otherwise.
        """
        if websocket in self.clients:
            return self.clients[websocket]
        return False

    def remove_client(self, websocket):
        """
        Removes a client and their connection start time from the tracking dictionaries. Performs cleanup on the
        client if necessary.

        Args:
            websocket: The websocket associated with the client to be removed.
        """
        client = self.clients.pop(websocket, None)
        if client:
            client.cleanup()
        self.start_times.pop(websocket, None)

    def get_wait_time(self):
        """
        Calculates the estimated wait time for new clients based on the remaining connection times of current clients.

        Returns:
            The estimated wait time in minutes for new clients to connect. Returns 0 if there are available slots.
        """
        wait_time = None
        for start_time in self.start_times.values():
            current_client_time_remaining = self.max_connection_time - (time.time() - start_time)
            if wait_time is None or current_client_time_remaining < wait_time:
                wait_time = current_client_time_remaining
        return wait_time / 60 if wait_time is not None else 0

    def is_server_full(self, websocket, options):
        """
        Checks if the server is at its maximum client capacity and sends a wait message to the client if necessary.

        Args:
            websocket: The websocket of the client attempting to connect.
            options: A dictionary of options that may include the client's unique identifier.

        Returns:
            True if the server is full, False otherwise.
        """
        if len(self.clients) >= self.max_clients:
            wait_time = self.get_wait_time()
            response = {"uid": options["uid"], "status": "WAIT", "message": wait_time}
            websocket.send(json.dumps(response))
            return True
        return False

    def is_client_timeout(self, websocket):
        """
        Checks if a client has exceeded the maximum allowed connection time and disconnects them if so, issuing a warning.

        Args:
            websocket: The websocket associated with the client to check.

        Returns:
            True if the client's connection time has exceeded the maximum limit, False otherwise.
        """
        elapsed_time = time.time() - self.start_times[websocket]
        if elapsed_time >= self.max_connection_time:
            self.clients[websocket].disconnect()
            logging.warning(f"Client with uid '{self.clients[websocket].client_uid}' disconnected due to overtime.")
            return True
        return False


class BackendType(Enum):
    FASTER_WHISPER = "faster_whisper"

    @staticmethod
    def valid_types() -> List[str]:
        # Returns a list of valid backend types as strings.
        return [backend_type.value for backend_type in BackendType]
    
    @staticmethod
    def is_valid(backend: str) -> bool:
        return backend in BackendType.valid_types()
    
    def is_faster_whisper(self) -> bool:
        return self == BackendType.FASTER_WHISPER


class TranscriptionServer():
    RATE = 16000

    def __init__(self):
        self.client_manager = ClientManager()
        self.no_voice_activity_chunks = 0
        self.use_vad = True
        self.single_model = False

    def initialize_client(
        self, websocket, options, model_path 
    ):
        # A ServerClientBase Class provides service to client
        client: Optional[ServeClientBase] = None

        self.backend = BackendType.FASTER_WHISPER

        if self.backend.is_faster_whisper():
            if model_path is not None and os.path.exists(model_path):
                logging.info(f"Using model {model_path}")
                options["model"] = model_path

            client = ServeClientFasterWhisper(
                websocket,
                client_uid=options["uid"],
                model=options["model"],
                initial_prompt=options.get("initial_prompt"),
                vad_parameters=options.get("vad_parameters"),
                use_vad=self.use_vad,
                single_model=self.single_model,
                )
            logging.info("Running faster_whisper backend.")

        if client is None:
            raise ValueError(f"Backend type {self.backend.value} not recognized or not handled.")

        self.client_manager.add_client(websocket, client)

    def get_audio_from_websocket(self, websocket):
        """
        Receives audio buffer from websocket and creates a numpy array out of it.

        Args:
            websocket: The websocket to receive audio from.

        Returns:
            A numpy array containing the audio.
        """
        frame_data = websocket.recv()
        if frame_data == b"END_OF_AUDIO":
            return False
        return np.frombuffer(frame_data, dtype=np.float32)
    
    def handle_new_connection(self, websocket, model_path):
        try:
            logging.info("New client connected")
            options = websocket.recv()
            options = json.loads(options)
            self.use_vad = options.get("use_vad")

            if self.client_manager.is_server_full(websocket, options):
                websocket.close()
                return False  # Indicates that the connection should not continue
            
            self.initialize_client(websocket, options, model_path)
            return True
        except json.JSONDecodeError:
            logging.error("Failed to decode JSON from client")
            return False
        except ConnectionClosed:
            logging.info("Connection closed by client")
            return False
        except Exception as e:
            logging.error(f"Error during new connection initialization: {str(e)}")
            return False 
    
    def process_audio_frames(self, websocket):
        frame_np = self.get_audio_from_websocket(websocket)
        client = self.client_manager.get_client(websocket)
        if frame_np is False:
            return False
        
        client.add_frames(frame_np)
        return True

    def recv_audio(self, websocket, backend: BackendType = BackendType.FASTER_WHISPER, model_path=None):
        """
        Receive audio chunks from a client in an infinite loop.

        Continuously receives audio frames from a connected client
        over a WebSocket connection. It processes the audio frames using a
        voice activity detection (VAD) model to determine if they contain speech
        or not. If the audio frame contains speech, it is added to the client's
        audio data for ASR.
        If the maximum number of clients is reached, the method sends a
        "WAIT" status to the client, indicating that they should wait
        until a slot is available.
        If a client's connection exceeds the maximum allowed time, it will
        be disconnected, and the client's resources will be cleaned up.

        Args:
            websocket (WebSocket): The WebSocket connection for the client.
            model_path (str): path to custom faster whisper model.

        Raises:
            Exception: If there is an error during the audio frame processing.
        """
        self.backend = backend
        if not self.handle_new_connection(websocket, model_path):
            return
        
        try:
            while not self.client_manager.is_client_timeout(websocket):
                if not self.process_audio_frames(websocket):
                    break
        except ConnectionClosed:
            logging.info("Connection closed by client")
        except Exception as e:
            logging.error(f"Unexpected error: {str(e)}")
        finally:
            if self.client_manager.get_client(websocket):
                self.cleanup(websocket)
                websocket.close()
            del websocket

    def run(self,
            host,
            port=9090,
            backend="faster_whisper",
            model_path=None,
            single_model=False):
        """
        Run the transcription server.

        Args:
            host (str): The host address to bind the server.
            port (int): The port number to bind the server.
        """
        if model_path is not None and not os.path.exists(model_path):
            raise ValueError(f"Custom faster_whisper model '{model_path}' is not a valid path.")
        if single_model:
            if model_path:
                logging.info("Custom model option was provided. Switching to single model mode.")
                self.single_model = True
            else:
                logging.info("Single model mode currently only works with custom models.")
        
        with serve(
            functools.partial(
                self.recv_audio,
                backend=BackendType(backend),
                model_path=model_path,
            ),
            host,
            port
        ) as server:
            server.serve_forever()

    def cleanup(self, websocket):
        """
        Cleans up resources associated with a given client's websocket.

        Args:
            websocket: The websocket associated with the client to be cleaned up.
        """
        if self.client_manager.get_client(websocket):
            self.client_manager.remove_client(websocket)
    

class ServeClientBase(object):
    RATE = 16000
    SERVER_READY = "SERVER_READY"
    DISCONNECT = "DISCONNECT"

    def __init__(self, client_uid, websocket):
        self.client_uid = client_uid
        self.websocket = websocket  # WebSocket connection to communicate with the client

        # Audio data and frames
        self.frames = b""  # Holds the raw audio frames received from the client
        self.timestamp_offset = 0.0  # Keeps track of the timestamp offset for the audio chunks
        self.frames_np = None  # NumPy array for audio frames, used for model inference
        self.frames_offset = 0.0  # Similar to timestamp_offset, used to track the current frame position

        # Transcription and output variables
        self.text = []  # Stores the transcribed text segments
        self.current_out = ''  # Holds the current transcription output
        self.prev_out = ''  # Holds the previous transcription output to detect repetition
        self.t_start = None  # Timestamp when transcription starts, used for timing pauses and outputs
        self.exit = False  # A flag to control when to stop the transcription process
        self.same_output_threshold = 0  # Tracks how many times the same output has been detected in a row

        # Parameters for handling pauses and previous outputs
        self.show_prev_out_thresh = 5  # If there's no output from Whisper, show the previous output for 5 seconds
        self.add_pause_thresh = 3  # If there's no speech detected for 3 seconds, add a blank segment for pause

        # Transcript management
        self.transcript = []  # Stores the full transcript, including all segments and pauses
        self.send_last_n_segments = 10  # Controls how many recent segments to send to the client during an update

        # Text formatting options
        self.pick_previous_segments = 2  # The number of previous segments to include when formatting text output

        # Threading control
        self.lock = threading.Lock()  # A lock to manage thread-safe access to shared resources

    def speech_to_text(self):
        raise NotImplementedError
    
    def transcribe_audio(self):
        raise NotImplementedError
    
    def handle_transcription_output(self):
        raise NotImplementedError
    
    def add_frames(self, frame_np):
        """Add audio frames to the ongoing audio stream buffer.
           
           This method is responsible for maintaining the audio stream buffer, allowing the continuous addition
           of audio frames as they are received. It also ensures that the buffer does not exceed a specified size
           to prevent excessive memory usage.

           If the buffer size exceeds a threshold (45s of audio data), it discards the oldest 30s 
           of audio data to maintain a reasonable buffer size. If the buffer is empty, it initializes it with provided
           audio frame. The audio stream buffer is used for real-time processing of audio data for transcription.

           Args:
               frame_np(ndarray): The audio frame data as a Numpy array.
        """
        self.lock.acquire()

        # Checks whether the audio buffer (frames_np) contains more than 45 seconds of audio data.
        if self.frames_np is not None and self.frames_np[0] > 45 * self.RATE:
            self.frames_offset += 30
            self.frames_np = self.frames_np[int(30 * self.RATE):]  # Removes the oldest 30 seconds of audio by slicing the buffer

            # If timestamp_offset is behind, it is updated to match frames_offset. 
            # This is useful when no speech is detected and the transcription timing hasn’t updated for a while.
            if self.timestamp_offset < self.frames_offset:
                self.timestamp_offset = self.frames_offset
        
        # Initialize or update buffer
        if self.frames_np is None:
            self.frames_np = frame_np.copy()
        else:
            self.frames_np = np.concatenate((self.frames_np, frame_np), axis=0)

        self.lock.release()

    def clip_audio_if_no_valid_segment(self):
        """
        Update the timestamp offset based on audio buffer status.
        Clip audio if the current chunk exceeds 30s, this basically implies that
        no valid segment for the last 30s from whisper
        """
        if self.frames_np[int((self.timestamp_offset - self.frames_offset) * self.RATE):].shape[0] > 25 * self.RATE:
            duration = self.frames_np.shape[0] / self.RATE
            self.timestamp_offset = self.frames_offset + duration - 5

    def get_audio_chunk_for_processing(self):
        """
        Retrieves the next chunk of audio for processing based on the current offsets.

        Calculates which part of the audio data should be processed next, based on 
        the difference between the current timestamp offset and frame's offset, scaled
        by the audio sample rate (16000). It then returns this chunk of audio data along with its 
        duration in seconds.

        Returns:
            tuple: A tuple containing:
                - input bytes (ndarray): The next chunk of audio data to be processed.
                - duration (float): The duration of the audio chunk in seconds.
        """
        samples_take = max(0, (self.timestamp_offset - self.frames_offset) * self.RATE)
        input_bytes = self.frames_np[int(samples_take):].copy()
        duration = input_bytes.shape[0] / self.RATE
        return input_bytes, duration
    
    def prepare_segments(self, last_segment=None):
        """
        Prepare the segments of transcribed text to be sent to the client.

        This method complies the recent segments of transcribed text, ensuring that only the
        specified number of the most recent segments are included. It also appends the most 
        recent segment of text if provided (which is considered incomplete because the possibility
        of the last word being truncated in the audio chunk).

        Args:
            last_segment (str, optional): The most recent segment of transcribed text to be added 
                                          to the list of segments. Defaults to None.

        Returns:
            list: A list of transcribed text segments to be sent to the client.
        """
        segments = []
        if len(self.transcript) >= self.send_last_n_segments:
            segments = self.transcript[-self.send_last_n_segments:].copy()
        else:
            segments = self.transcript.copy()

        if last_segment is not None:
            segments = segments + [last_segment]

        return segments
    
    def get_audio_chunk_duration(self, input_bytes):
        """
        Calculates the duration of the provided audio chunk.

        Args:
            input_bytes (ndarray): The audio chunk for which to calculate the duration.

        Returns:
            float: The duration of the audio chunk in seconds.
        """
        return input_bytes.shape[0] / self.RATE

    def send_transcription_to_client(self, segments):
        """
        Send the specified transcription segments to the client over the websocket connection.

        This method formats the transcription segments into JSON object and attempts to send
        the object to the client. If an error occurs during the send operation, it logs the error.

        Returns:
            segments (list): A list of transcription segments to be sent to the client.
        """
        try:
            self.websocket.send(
                json.dumps({
                    "uid": self.client_uid,
                    "segments": segments
                })
            )
        except Exception as e:
            logging.error(f"[ERROR]: Sending data to client: {e}")

    def disconnect(self):
        """
        Notify the client of disconnection and send a disconnect message.

        This method sends a disconnect message to the client via the WebSocket connection to notify them
        that the transcription service is disconnecting gracefully.

        """
        self.websocket.send(json.dumps({
            "uid": self.client_uid,
            "message": self.DISCONNECT
        }))

    def cleanup(self):
        """
        Perform cleanup tasks before exiting the transcription service.

        This method performs necessary cleanup tasks, including stopping the transcription thread, marking
        the exit flag to indicate the transcription thread should exit gracefully, and destroying resources
        associated with the transcription process.

        """
        logging.info("Cleaning up.")
        self.exit = True


class ServeClientFasterWhisper(ServeClientBase):
    SINGLE_MODEL = None
    SINGLE_MODEL_LOCK = threading.Lock()  # Prevents multiple threads from accessing the model simultaneously when using the shared model

    def __init__(self, websocket, task="transcribe", language="en", client_uid=None, model="base.en", 
                 initial_prompt=None, vad_parameters=None, use_vad=True, single_model=False):
        """
        Initialize a ServeClient instance.
        The Whisper model is initialized based on the client's language (defaults to en) and device availability.
        The transcription thread is started upon initialization. A "SERVER_READY" message is sent to the client to
        indicate that the server is ready.

        Args:
            websocket (Websocket): The Websocket connection for the client.
            task (str, optional): The task type, defaults to "transcribe".
            language (str, optional): The language for transcription, defaults to "en".
            client_uid (str, optional): A unique identifier for the client.
            model (str, optional): The whisper model size, defaults to "base.en".
            initial_prompt (str, optional): Prompt for Whisper inference.
            single_model (bool, optional): Whether to instantiate a new model for each client connection, defaults to False.
        """
        super().__init__(client_uid, websocket)
        self.model_sizes = ["small.en", "base.en", "medium.en"]  # Currently, only support English

        if not os.path.exists(model):
            self.model_sizes_or_path = os.path.join("./model", self.check_valid_model(model))
        else:
            self.model_sizes_or_path = model

        self.language = "en" if self.model_sizes_or_path.endswith("en") else language
        self.task = task
        self.initial_prompt = initial_prompt
        self.vad_parameters = vad_parameters or {"threshold": 0.5}
        self.no_speech_thresh = 0.45

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda":
            major, _ = torch.cuda.get_device_capability(device)
            self.compute_type = "float16" if major >= 7 else "float32"
        else:
            self.compute_type = "int8" 

        if self.model_sizes_or_path is None:
            return
        
        logging.info(f"Using Device={device} with precision {self.compute_type}")

        if single_model:
            if ServeClientFasterWhisper.SINGLE_MODEL is None:
                self.create_model(device)
                ServeClientFasterWhisper.SINGLE_MODEL = self.transcriber
            else:
                self.transcriber = ServeClientFasterWhisper.SINGLE_MODEL
        else:
            self.create_model(device)

        self.use_vad = use_vad

        # Threading
        self.trans_thread = threading.Thread(target=self.speech_to_text)
        self.trans_thread.start()
        self.websocket.send(
            json.dumps({
                "uid": self.client_uid,
                "message": self.SERVER_READY,
                "backend": "faster_whisper"
            })
        )

    def check_valid_model(self, model_size):
        """
        Check if it's a valid whisper model size.

        Args:
            model_size (str): The name of the model size to check.

        Returns:
            str: The model size if valid, None otherwise.
        """
        if model_size not in self.model_sizes:
            self.websocket.send(
                json.dumps({
                    "uid": self.client_uid,
                    "status": "ERROR",
                    "message": f"Invalid model size {model_size}. Available choices: {self.model_sizes}" 
                })
            )
            return None
        return model_size
    
    def create_model(self, device):
        """
        Instantiates a new model, sets it as the transcriber.
        """
        self.transcriber = WhisperModel(
            self.model_sizes_or_path,
            device=device,
            compute_type=self.compute_type,
            local_files_only=False
        )

    def transcribe_audio(self, input_sample):
        """
        Transcribes the provided audio sample using the configured transcriber instance.

        Args:
            input_sample (np.array): The audio chunk to be transcribed. This should be a NumPy
                                    array representing the audio data.

        Returns:
            The transcription result from the transcriber. The exact format of this result
            depends on the implementation of the `transcriber.transcribe` method but typically
            includes the transcribed text.
        """
        if ServeClientFasterWhisper.SINGLE_MODEL:
            ServeClientFasterWhisper.SINGLE_MODEL_LOCK.acquire()

        result, _ = self.transcriber.transcribe(
            input_sample,
            initial_prompt=self.initial_prompt,
            language=self.language,
            task=self.task,
            vad_filter=self.use_vad,
            vad_parameters=self.vad_parameters if self.use_vad else None)
        
        if ServeClientFasterWhisper.SINGLE_MODEL:
            ServeClientFasterWhisper.SINGLE_MODEL_LOCK.release()

        return result

    def get_previous_output(self):
        """
        Retrieves previously generated transcription outputs if no new transcription is available
        from the current audio chunks.

        Checks the time since the last transcription output and, if it is within a specified
        threshold, returns the most recent segments of transcribed text. It also manages
        adding a pause (blank segment) to indicate a significant gap in speech based on a defined
        threshold.

        Returns:
            segments (list): A list of transcription segments. This may include the most recent
                            transcribed text segments or a blank segment to indicate a pause
                            in speech.
        """
        segments = []
        if self.t_start is None:
            self.t_start = time.time()
        if time.time() - self.t_start < self.show_prev_out_thresh:
            segments = self.prepare_segments()

        # add a blank if there is no speech for 3 seconds
        if len(self.text) and self.text[-1] != '':
            if time.time() - self.t_start > self.add_pause_thresh:
                self.text.append('')
        return segments
    
    def format_segment(self, start, end, text):
        """
        Formats a transcription segment with precise start and end times alongside the transcribed text.

        Args:
            start (float): The start time of the transcription segment in seconds.
            end (float): The end time of the transcription segment in seconds.
            text (str): The transcribed text corresponding to the segment.

        Returns:
            dict: A dictionary representing the formatted transcription segment, including
                'start' and 'end' times as strings with three decimal places and the 'text'
                of the transcription.
        """
        return {
            "start": "{:.3f}".format(start),
            "end": "{:.3f}".format(end),
            "text": text
        }

    def update_segments(self, segments, duration):
        """
        Processes the segments from whisper. Appends all the segments to the list
        except for the last segment assuming that it is incomplete.

        Updates the ongoing transcript with transcribed segments, including their start and end times.
        Complete segments are appended to the transcript in chronological order. Incomplete segments
        (assumed to be the last one) are processed to identify repeated content. If the same incomplete
        segment is seen multiple times, it updates the offset and appends the segment to the transcript.
        A threshold is used to detect repeated content and ensure it is only included once in the transcript.
        The timestamp offset is updated based on the duration of processed segments. The method returns the
        last processed segment, allowing it to be sent to the client for real-time updates.

        Args:
            segments(dict) : dictionary of segments as returned by whisper
            duration(float): duration of the current chunk

        Returns:
            dict or None: The last processed segment with its start time, end time, and transcribed text.
                     Returns None if there are no valid segments to process.
        """
        offset = None
        self.current_out = ''
        last_segment = None

        # process complete segments
        if len(segments) > 1:
            for i, s in enumerate(segments[:-1]):
                text_ = s.text
                self.text.append(text_)
                start, end = self.timestamp_offset + s.start, self.timestamp_offset + min(duration, s.end)

                if start >= end:
                    continue
                if s.no_speech_prob > self.no_speech_thresh:
                    continue

                self.transcript.append(self.format_segment(start, end, text_))
                offset = min(duration, s.end)

        # only process the segments if it satisfies the no_speech_thresh
        if segments[-1].no_speech_prob <= self.no_speech_thresh:
            self.current_out += segments[-1].text
            last_segment = self.format_segment(
                self.timestamp_offset + segments[-1].start,
                self.timestamp_offset + min(duration, segments[-1].end),
                self.current_out
            )

        # if same incomplete segment is seen multiple times then update the offset
        # and append the segment to the list
        if self.current_out.strip() == self.prev_out.strip() and self.current_out != '':
            self.same_output_threshold += 1
        else:
            self.same_output_threshold = 0

        if self.same_output_threshold > 5:
            if not len(self.text) or self.text[-1].strip().lower() != self.current_out.strip().lower():
                self.text.append(self.current_out)
                self.transcript.append(self.format_segment(
                    self.timestamp_offset,
                    self.timestamp_offset + duration,
                    self.current_out
                ))
            self.current_out = ''
            offset = duration
            self.same_output_threshold = 0
            last_segment = None
        else:
            self.prev_out = self.current_out

        # update offset
        if offset is not None:
            self.timestamp_offset += offset

        return last_segment

    def handle_transcription_output(self, result, duration):
        """
        Handle the transcription output, updating the transcript and sending data to the client.

        Args:
            result (str): The result from Whisper inference i.e. the list of segments.
            duration (float): Duration of the transcribed audio chunk.
        """
        segments = []
        if len(result):
            self.t_start = None
            last_segment = self.update_segments(result, duration)
            segments = self.prepare_segments(last_segment)
        else:
            # Show previous output if there is pause i.e. no output from Whisper
            segments = self.get_previous_output()

        if len(segments):
            self.send_transcription_to_client(segments)

    def speech_to_text(self):
        """
        Process an audio stream in an infinite loop, continuously transcribing the speech.

        This method continuously receives audio frames, performs real-time transcription, and sends
        transcribed segments to the client via a WebSocket connection.

        It utilizes the Whisper ASR model to transcribe the audio, continuously processing and streaming results. Segments
        are sent to the client in real-time, and a history of segments is maintained to provide context. Pauses in speech
        (no output from Whisper) are handled by showing the previous output for a set duration. A blank segment is added if
        there is no speech for a specified duration to indicate a pause.

        Raises:
            Exception: If there is an issue with audio processing or WebSocket communication.

        """
        while True:
            if self.exit:
                logging.info("Exiting speech to text thread")
                break

            if self.frames_np is None:
                continue

            self.clip_audio_if_no_valid_segment()

            input_bytes, duration = self.get_audio_chunk_for_processing()
            if duration < 1.0:
                time.sleep(0.1)  # Wait for audio chunks to arrive
                continue

            try:
                input_sample = input_bytes.copy()
                result = self.transcribe_audio(input_sample)

                if result is None or self.language is None:
                    self.timestamp_offset += duration
                    time.sleep(0.25)  # Wait for voice activity, result is None when no voice activity
                    continue

                self.handle_transcription_output(result, duration)
            except Exception as e:
                logging.error(f"[ERROR]: Failed to transcribe audio chunk: {e}")
                time.sleep(0.01)