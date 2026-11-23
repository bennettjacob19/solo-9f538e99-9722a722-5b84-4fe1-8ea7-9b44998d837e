"""Flask 入口：Git 构建回归定位页。"""

import os

from flask import Flask, jsonify, render_template, request

from bisect_engine import BisectEngine, BisectError, STATE_IDLE


def create_app(
    repo_path: str | None = None,
    script_path: str | None = None,
    worktree_root: str | None = None,
    max_concurrent: int | None = None,
) -> Flask:
    app = Flask(__name__)

    repo_path = repo_path or os.environ.get(
        "REPO_PATH", os.path.join(os.getcwd(), "sample-repo")
    )
    script_path = script_path or os.environ.get(
        "CHECK_SCRIPT", os.path.join(os.getcwd(), "scripts", "sample_check.sh")
    )
    worktree_root = worktree_root or os.environ.get(
        "WORKTREE_ROOT", os.path.join(os.getcwd(), "worktrees")
    )
    if max_concurrent is None:
        max_concurrent = int(os.environ.get("MAX_CONCURRENT", "2"))

    engine = BisectEngine(
        repo_path=repo_path,
        script_path=script_path,
        worktree_root=worktree_root,
        max_concurrent=max_concurrent,
    )
    app.extensions["bisect_engine"] = engine

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/commits")
    def api_commits():
        try:
            return jsonify({"commits": engine.list_commits()})
        except BisectError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/start")
    def api_start():
        data = request.get_json(silent=True) or request.form
        good = (data.get("good") or "").strip()
        bad = (data.get("bad") or "").strip()
        if not good or not bad:
            return jsonify({"error": "需要提供 good 和 bad 两个边界提交"}), 400
        try:
            engine.start(good, bad)
        except BisectError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/reset")
    def api_reset():
        engine.request_reset()
        return jsonify({"ok": True})

    @app.get("/api/status")
    def api_status():
        return jsonify(engine.snapshot())

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        threaded=True,
    )
