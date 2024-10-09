from live_whisper.client import TranscriptionClient
client = TranscriptionClient(
  "localhost",
  9090,
  lang="en",
  translate=False,
  model="base.en",
  use_vad=False,
  save_output_recording=False,                         # Only used for microphone input, False by Default
  output_recording_filename="./output_recording.wav"  # Only used for microphone input
)

if __name__ == "__main__":
    client()