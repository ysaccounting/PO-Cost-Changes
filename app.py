"""Flask web app for PO Cost Changes processing (v2 — new DB export).

Drag-and-drop UI, background job thread, polling status endpoint, and
per-company + combined + zip downloads. No Purchase Details uploads: the new
source export carries everything we need, and row exclusion is driven solely
by the manual "Remove" = X flag.
"""
import io
import os
import json
import zipfile
import threading
import uuid
import tempfile
import pickle
import logging

from flask import Flask, request, jsonify, send_file, render_template

from processor import process_files, build_filtered_outputs, convert_to_modified, files_missing_remove_column

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("po-cost-changes")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB

JOBS_DIR = os.path.join(tempfile.gettempdir(), "po_cost_changes_jobs")
os.makedirs(JOBS_DIR, exist_ok=True)


def job_dir(job_id: str) -> str:
    return os.path.join(JOBS_DIR, job_id)


def write_job_status(job_id: str, status: str, message: str | None = None) -> None:
    d = job_dir(job_id)
    os.makedirs(d, exist_ok=True)
    payload = {"status": status}
    if message:
        payload["message"] = message
    with open(os.path.join(d, "status.json"), "w") as f:
        json.dump(payload, f)


def read_job_status(job_id: str) -> dict | None:
    path = os.path.join(job_dir(job_id), "status.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def read_meta(job_id: str) -> dict | None:
    path = os.path.join(job_dir(job_id), "meta.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def write_configure_status(job_id: str, payload: dict) -> None:
    """Progress/result for the file-generation (configure) step."""
    d = job_dir(job_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "configure.json"), "w") as f:
        json.dump(payload, f)


def read_configure_status(job_id: str) -> dict | None:
    path = os.path.join(job_dir(job_id), "configure.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def run_job(job_id: str, file_list: list[tuple[bytes, str]]) -> None:
    """Background worker — process the upload, then pickle the intermediate
    data so build_filtered_outputs() can build the chosen companies' files
    once the user picks them in the selection modal."""
    try:
        result = process_files(file_list)
        d = job_dir(job_id)
        os.makedirs(d, exist_ok=True)

        with open(os.path.join(d, "data.pkl"), "wb") as f:
            pickle.dump({
                "cleaned":       result["_cleaned"],
                "source_view":   result["_source_view"],
                "excluded_view": result["_excluded_view"],
                "date_range":    result["date_range"],
            }, f)

        meta = {
            "date_range": result["date_range"],
            # every QBO company from the master list (drives the selection modal)
            "all_companies": result["all_companies"],
            "stats": result["stats"],
            "dropped": result.get("dropped", {}),
            "excluded": result.get("excluded", {"po_count": 0, "row_count": 0, "total_adjustment": 0.0}),
            "ignored_companies": result.get("ignored_companies", {}),
            # populated by /configure once the user picks companies
            "companies": [],
            "bills_companies": [],
            "selected_companies": [],
        }
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(meta, f)

        write_job_status(job_id, "done")
        log.info(
            "Job %s processed (%d companies available, %d rows excluded)",
            job_id, len(result["all_companies"]),
            result.get("excluded", {}).get("row_count", 0),
        )
    except Exception as e:
        log.exception("Job %s failed", job_id)
        write_job_status(job_id, "error", str(e))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/convert", methods=["POST"])
def convert():
    """Zone 1 (single file): clean one raw PO Cost Changes export into a
    Source Data workbook for review/edit, then upload into Zone 2."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    try:
        cleaned = convert_to_modified(f.read(), f.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.exception("Convert failed")
        return jsonify({"error": f"Conversion failed: {e}"}), 500
    base = os.path.splitext(f.filename)[0]
    return send_file(
        io.BytesIO(cleaned),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"{base} (converted).xlsx",
    )


@app.route("/convert_zip", methods=["POST"])
def convert_zip():
    """Zone 1 (multiple files): clean each raw export independently and return
    them together as a zip."""
    files = request.files.getlist("file")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files provided"}), 400
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if not f.filename:
                continue
            try:
                cleaned = convert_to_modified(f.read(), f.filename)
            except Exception as e:
                return jsonify({"error": f"Failed on {f.filename}: {e}"}), 400
            base = os.path.splitext(f.filename)[0]
            zf.writestr(f"{base} (converted).xlsx", cleaned)
    zip_buf.seek(0)
    return send_file(zip_buf, mimetype="application/zip", as_attachment=True,
                     download_name="PO Cost Changes (converted).zip")


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("file")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No PO Cost Changes files provided"}), 400
    file_list = [(f.read(), f.filename) for f in files if f.filename]

    # Soft block: every uploaded file should contain a "Remove" column. If any
    # are missing it, return the list so the UI can show a dialog. The user can
    # go back and add it, or proceed anyway — which re-submits with force=true
    # and skips this check (every row is then processed as-is).
    force = request.form.get("force", "").strip().lower() in ("1", "true", "yes")
    if not force:
        missing = files_missing_remove_column(file_list)
        if missing:
            return jsonify({
                "error": "remove_column_missing",
                "missing_remove": missing,
            }), 400

    job_id = str(uuid.uuid4())
    write_job_status(job_id, "processing")
    threading.Thread(
        target=run_job,
        args=(job_id, file_list),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = read_job_status(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    if job["status"] in ("error", "processing"):
        return jsonify(job)
    meta = read_meta(job_id)
    if not meta:
        return jsonify({"status": "error", "message": "Result files missing"}), 500
    return jsonify({
        "status": "done",
        "date_range": meta["date_range"],
        "all_companies": meta.get("all_companies", []),
        "stats": meta.get("stats", {}),
        "dropped": meta.get("dropped", {}),
        "excluded": meta.get("excluded", {"po_count": 0, "row_count": 0, "total_adjustment": 0.0}),
        "ignored_companies": meta.get("ignored_companies", {}),
    })


def run_configure(job_id: str, selected: list[str], combined_only: bool = False) -> None:
    """Background worker: build the selected companies' output files, reporting
    per-file progress to configure.json, then record which are downloadable.

    When `combined_only` is set, only the combined workbook is built (no separate
    per-company Expenses/Bills files) — much faster for a full-month run."""
    try:
        write_configure_status(job_id, {"status": "generating", "done": 0, "total": 0})
        meta = read_meta(job_id)
        d = job_dir(job_id)
        with open(os.path.join(d, "data.pkl"), "rb") as f:
            dfs = pickle.load(f)

        def progress(done: int, total: int) -> None:
            write_configure_status(job_id, {"status": "generating", "done": done, "total": total})

        out = build_filtered_outputs(
            dfs["cleaned"], dfs["source_view"], dfs["excluded_view"],
            dfs["date_range"], selected, progress_cb=progress,
            combined_only=combined_only,
        )

        with open(os.path.join(d, "combined.xlsx"), "wb") as f:
            f.write(out["combined"])

        # (Re)write per-company expenses files, clearing any previous selection.
        companies_dir = os.path.join(d, "companies")
        os.makedirs(companies_dir, exist_ok=True)
        for fn in os.listdir(companies_dir):
            os.remove(os.path.join(companies_dir, fn))
        for company, file_bytes in out["companies"].items():
            safe = company.replace("/", "_").replace("\\", "_")
            with open(os.path.join(companies_dir, f"{safe}.xlsx"), "wb") as f:
                f.write(file_bytes)

        # (Re)write per-company bills files.
        bills_dir = os.path.join(d, "bills")
        os.makedirs(bills_dir, exist_ok=True)
        for fn in os.listdir(bills_dir):
            os.remove(os.path.join(bills_dir, fn))
        for company, file_bytes in out["bills_files"].items():
            safe = company.replace("/", "_").replace("\\", "_")
            with open(os.path.join(bills_dir, f"{safe}.xlsx"), "wb") as f:
                f.write(file_bytes)

        meta["selected_companies"] = selected
        meta["combined_only"] = combined_only
        meta["companies"] = list(out["companies"].keys())
        meta["bills_companies"] = list(out["bills_files"].keys())
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(meta, f)

        total = 1 + len(meta["companies"]) + len(meta["bills_companies"])
        write_configure_status(job_id, {
            "status": "ready",
            "done": total,
            "total": total,
            "date_range": meta["date_range"],
            "selected_companies": selected,
            "combined_only": combined_only,
            "companies": meta["companies"],
            "bills_companies": meta["bills_companies"],
            "stats": meta.get("stats", {}),
        })
        log.info("Configure %s ready (%d files)", job_id, total)
    except Exception as e:
        log.exception("Configure %s failed", job_id)
        write_configure_status(job_id, {"status": "error", "message": str(e)})


@app.route("/configure/<job_id>", methods=["POST"])
def configure(job_id):
    """Kick off building the output files for the user's selected companies in a
    background thread. The UI polls /configure_status for per-file progress."""
    meta = read_meta(job_id)
    if not meta:
        return jsonify({"error": "Job not found"}), 404
    if not os.path.exists(os.path.join(job_dir(job_id), "data.pkl")):
        return jsonify({"error": "Job data not found"}), 404

    data = request.get_json(silent=True) or {}
    combined_only = bool(data.get("combined_only", False))
    if combined_only:
        # Combined includes everyone; ignore any per-company selection.
        selected = meta.get("all_companies", [])
    else:
        selected = data.get("selected_companies", meta.get("all_companies", []))

    write_configure_status(job_id, {"status": "generating", "done": 0, "total": 0})
    threading.Thread(target=run_configure, args=(job_id, selected, combined_only), daemon=True).start()
    return jsonify({"status": "generating"})


@app.route("/configure_status/<job_id>")
def configure_status(job_id):
    st = read_configure_status(job_id)
    if not st:
        return jsonify({"status": "not_found"}), 404
    return jsonify(st)


def _combined_download_name(meta: dict) -> str:
    """Combined workbook filename."""
    return f"PO Cost Changes - Combined - {meta['date_range']}.xlsx"


@app.route("/download/<job_id>/combined")
def download_combined(job_id):
    meta = read_meta(job_id)
    if not meta:
        return jsonify({"error": "Job not found"}), 404
    path = os.path.join(job_dir(job_id), "combined.xlsx")
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(
        path, as_attachment=True,
        download_name=_combined_download_name(meta),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/download/<job_id>/company/<company>")
def download_company(job_id, company):
    meta = read_meta(job_id)
    if not meta:
        return jsonify({"error": "Job not found"}), 404
    safe = company.replace("/", "_").replace("\\", "_")
    path = os.path.join(job_dir(job_id), "companies", f"{safe}.xlsx")
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(
        path, as_attachment=True,
        download_name=f"PO Cost Changes - Expenses - {company} - {meta['date_range']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/download/<job_id>/bills/<company>")
def download_bills(job_id, company):
    meta = read_meta(job_id)
    if not meta:
        return jsonify({"error": "Job not found"}), 404
    safe = company.replace("/", "_").replace("\\", "_")
    path = os.path.join(job_dir(job_id), "bills", f"{safe}.xlsx")
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(
        path, as_attachment=True,
        download_name=f"PO Cost Changes - Bills - {company} - {meta['date_range']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/download/<job_id>/all")
def download_all_zip(job_id):
    meta = read_meta(job_id)
    if not meta:
        return jsonify({"error": "Job not found"}), 404
    d = job_dir(job_id)
    dr = meta["date_range"]
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        combined = os.path.join(d, "combined.xlsx")
        if os.path.exists(combined):
            # Combined stays at the top level (not inside Expenses/ or Bills/).
            zf.write(combined, _combined_download_name(meta))
        for company in meta["companies"]:
            safe = company.replace("/", "_").replace("\\", "_")
            cp = os.path.join(d, "companies", f"{safe}.xlsx")
            if os.path.exists(cp):
                zf.write(cp, f"Expenses/PO Cost Changes - Expenses - {company} - {dr}.xlsx")
        for company in meta.get("bills_companies", []):
            safe = company.replace("/", "_").replace("\\", "_")
            bp = os.path.join(d, "bills", f"{safe}.xlsx")
            if os.path.exists(bp):
                zf.write(bp, f"Bills/PO Cost Changes - Bills - {company} - {dr}.xlsx")
    zip_buf.seek(0)
    return send_file(
        zip_buf, mimetype="application/zip", as_attachment=True,
        download_name=f"PO Cost Changes - {dr}.zip",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
