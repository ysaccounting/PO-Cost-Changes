# PO Cost Changes

Python port of the `PO_Cost_Changes.xlsm` Power Query pipeline, packaged as a
Flask web app with a drag-and-drop UI. Mirrors the structure of the Purchase
Details Processor app — background job thread, polling status endpoint, and
combined + per-company + zip downloads.

## Layout

```
app.py                ← Flask app (routes + background job)
processor.py          ← pipeline (mirrors docs/Section1.m) + process_files()
mapping.py            ← loads Master_Mapping_List.xlsx
templates/
  index.html          ← drag-and-drop UI
data/
  Master_Mapping_List.xlsx
docs/
  Section1.m          ← original Power Query M code
tests/
  test_transform.py
```

## Local dev

```bash
pip install -r requirements.txt
python app.py                # runs on http://localhost:5000/
pytest
```

Or with gunicorn (matches Railway):
```bash
gunicorn app:app --workers 1 --worker-class gthread --threads 4 --timeout 120
```

## How it works

1. User drops one or more `.xlsx` / `.xlsm` / `.csv` files into the UI.
2. `POST /upload` saves the files and kicks off a background thread that runs
   `process_files()` in `processor.py`.
3. UI polls `GET /status/<job_id>` until status is `done` or `error`.
4. On done, UI shows:
   - A date-range badge derived from the data's Adjustment Date column.
   - A summary table: Combined row + one row per QBO company (showing 0/0
     for companies with no data this period).
   - "Download All (.zip)" button.
   - A grid of individual download buttons — Combined + every QBO company.
     Companies with no data are shown faded with "No data".

## Endpoints

| Method | Path                                  | Returns                                              |
|--------|---------------------------------------|------------------------------------------------------|
| GET    | `/`                                   | HTML drag-and-drop UI                                |
| POST   | `/upload`                             | `{job_id}` — kicks off background processing        |
| GET    | `/status/<job_id>`                    | `{status, date_range, companies, all_companies, stats, dropped}` |
| GET    | `/download/<job_id>/combined`         | xlsx of all merged data                              |
| GET    | `/download/<job_id>/company/<name>`   | xlsx for one company                                 |
| GET    | `/download/<job_id>/all`              | zip of combined + all per-company files              |

## Company mapping

The master file `data/Master_Mapping_List.xlsx` is the source of truth. Only
rows whose Company maps to a QBO company (or already uses a canonical QBO
name) are included in the output. Anything else is silently dropped; the
counts are still in the JSON response under `dropped` for debugging if
needed, but the UI doesn't surface them.

To update the mapping, edit the master file and redeploy (or set
`MASTER_MAPPING_PATH` to a different file path). The mapping is loaded once
at startup and cached.

## Deploy to Railway

```bash
git init && git add . && git commit -m "Initial commit"
# Push to GitHub, then in Railway: New Project → Deploy from GitHub
```

`railway.json` pins the start command and healthcheck.

## Known deviations from the original `Section1.m`

- **Non-QBO companies are silently dropped.** The M code did 5 hardcoded
  substring replaces (collapsing YSA 2/3 → YSA, Bearhawk variants → Bearhawk
  Group) but kept all rows. The new pipeline uses the master file's QBO
  Company column as a gating list — only mapped or canonical-named rows
  appear in output. This matches the operational intent that non-QBO
  companies aren't supposed to be in the source files in the first place.
- **Exact match, not substring.** The M code used `Replacer.ReplaceText`;
  we look up exact strings against the master.
- **"Not cancelled" is `""` (empty string)**, not `" "` (single space). The
  M code's literal space was a workaround for PivotTable blank-slicer
  rendering, irrelevant in a web context.
- **VBA `PO_Cost_Changes()` macro is not ported.** It handled disk exports;
  replaced by the download endpoints.

## Required input columns

`Company`, `PO #`, `Adjustment Date`, `Vendor`, `Team/Performer`,
`Ticket Cost Total Start`, `Ticket Cost Total End`, `Total Adjustment`,
`Cancelled`, `User`.

These are dropped before processing if present (kept in the template for
data-entry context): `Opponent/Performer`, `Event Date`, `Seat Section`,
`Seat Row`, `Seats`, `Ticket Cost Start`, `Ticket Cost End`, `Qty Start`,
`Qty End`, `Per Ticket Adjustment`.
