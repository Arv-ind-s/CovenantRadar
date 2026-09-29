"""Start an isolated local demo on macOS, Linux or Windows.

Run with the project's Python environment: python scripts/demo_up.py
Each launch uses a fresh temporary database and process-only encryption keys.
"""

from __future__ import annotations

# Console output is the public interface of this standalone launcher.
# ruff: noqa: T201
import argparse
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--without-ml", action="store_true", help="Skip synthetic model training")
    parser.add_argument("--offline-data", action="store_true", help="Disable public data requests")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    with socket.socket() as listener:
        if os.name == "posix":
            # Match Uvicorn: TIME_WAIT connections should not block a restart.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", args.port))
        except OSError:
            parser.error(f"Port {args.port} is already in use; choose another --port")

    workspace = Path(tempfile.mkdtemp(prefix="covenant-radar-demo-"))
    # Do not inherit deployment configuration into a disposable local demo.
    env = {key: value for key, value in os.environ.items() if not key.startswith("COVENANT_RADAR_")}
    # Homebrew's native PDF dependencies are outside macOS's default loader
    # paths. Child processes run Python directly so this setting survives.
    if sys.platform == "darwin":
        native_paths = [
            path
            for path in ("/opt/homebrew/lib", "/usr/local/lib")
            if (Path(path) / "libgobject-2.0.dylib").is_file()
        ]
        if native_paths:
            existing = env.get("DYLD_FALLBACK_LIBRARY_PATH")
            env["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(
                ([existing] if existing else []) + native_paths
            )
    env.update(
        {
            "COVENANT_RADAR_DOTENV": "0",
            "COVENANT_RADAR_ENVIRONMENT": "development",
            "COVENANT_RADAR_DATABASE__URL": f"sqlite:///{workspace / 'demo.db'}",
            "COVENANT_RADAR_DOCUMENTS__STORE": "local",
            "COVENANT_RADAR_DOCUMENTS__LOCAL_PATH": str(workspace / "documents"),
            "COVENANT_RADAR_AI__PROVIDER": "recorded",
            "COVENANT_RADAR_AI__MODEL": "demo-recorded",
            "COVENANT_RADAR_AI__RECORDED_RESPONSES_PATH": str(ROOT / "evaluation/cassettes"),
            "COVENANT_RADAR_FORECAST__ML_ENABLED": "false" if args.without_ml else "true",
            "COVENANT_RADAR_FORECAST__ML_MODE": "shadow",
            "COVENANT_RADAR_INTELLIGENCE__ENABLED": "false" if args.offline_data else "true",
            "COVENANT_RADAR_INTELLIGENCE__CACHE_PATH": str(workspace / "intelligence.json"),
            "COVENANT_RADAR_WEB__HOST": "127.0.0.1",
            "COVENANT_RADAR_WEB__PORT": str(args.port),
            "COVENANT_RADAR_WEB__WORKERS": "1",
            "COVENANT_RADAR_WEB__DEMO_WALKTHROUGH_ENABLED": "true",
            "COVENANT_RADAR_OBSERVABILITY__METRICS_ENABLED": "false",
            "COVENANT_RADAR_OBSERVABILITY__TRACING_ENABLED": "false",
        }
    )
    for name in ("SESSION_SECRET", "FIELD_ENCRYPTION_KEY", "CIN_FINGERPRINT_KEY"):
        env[f"COVENANT_RADAR_SECURITY_{name}"] = base64.b64encode(secrets.token_bytes(32)).decode()
    (workspace / "documents").mkdir()
    (ROOT / "var").mkdir(exist_ok=True)

    def run(*command: str) -> None:
        subprocess.run([sys.executable, *command], cwd=ROOT, env=env, check=True)

    print(f"Preparing synthetic demo in {workspace}", flush=True)
    if not args.without_ml:
        run("-m", "evaluation.ml_reference")
        report = json.loads((ROOT / "var/ml-reference/report.json").read_text())
        artifact = next(name for name in report["models"] if "gradient_boosted" in name)
        env["COVENANT_RADAR_FORECAST__ML_ARTIFACT_PATH"] = str(
            ROOT / "var/ml-reference" / f"{artifact}.pkl"
        )
    run("-m", "radarctl", "migrate", "upgrade")
    run("-m", "radarctl", "seed", "--reference-portfolio", "--signal-days", "1")
    run("create_user.py")
    run("-m", "radarctl", "seed", "--demo-covenants")
    run("-m", "radarctl", "job", "run", "nightly.pipeline")
    print(f"\nOpen http://127.0.0.1:{args.port} · riskhead / CovenantRadar#2026", flush=True)
    print("Synthetic borrowers · ML shadow comparison · recorded AI replay", flush=True)
    print(
        "Public market data: " + ("offline" if args.offline_data else "live on /intelligence"),
        flush=True,
    )
    print(
        "A new launch starts fresh. Keep this process running during your presentation.", flush=True
    )
    run("-m", "radarctl", "serve")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
