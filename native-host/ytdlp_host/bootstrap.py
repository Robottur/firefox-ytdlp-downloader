"""Keep startup failures framed even when a dependency cannot be imported."""
import os
import sys


def main():
    from .protocol import MessageWriter
    output = sys.stdout.buffer
    sys.stdout = sys.stderr
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(output.fileno(), os.O_BINARY)
    try:
        if "--stop-service" in sys.argv:
            from .service import stop_service
            return stop_service()
        from .main import main as run
        return run()
    except Exception as exc:
        MessageWriter(output).send({"type": "host_error", "code": "startup_failed",
            "message": f"The companion host could not start: {exc}. Re-run the platform installer."})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
