import argparse
import os

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', '-p',
                        type=int,
                        default=9090,
                        help="Websocket port to run the server on.")
    parser.add_argument('--faster_whisper_model_path', '-fw',
                        type=str, default=None,
                        help="Faster Whisper Model")
    parser.add_argument('--omp_num_threads', '-omp',
                        type=int,
                        default=1,
                        help="Number of threads to use for OpenMP")
    parser.add_argument('--no_single_model', '-nsm',
                        action='store_true',
                        help='Set this if every connection should instantiate its own model. Only relevant for custom model, passed using -fw.')
    args = parser.parse_args()

    if "OMP_NUM_THREADS" not in os.environ:
        os.environ["OMP_NUM_THREADS"] = str(args.omp_num_threads)
    
    from live_whisper.server import TranscriptionServer
    server = TranscriptionServer()
    server.run(
        "0.0.0.0",
        port=args.port,
        model_path=args.faster_whisper_model_path,
        single_model=not args.no_single_model,
    )