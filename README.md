# ArcGIS Online Governance — Research & Data Services

Scheduled, auditable cleanup of the Northeastern AGO organization: identify
graduated users whose content is past its retention window, back it up, check
nothing depends on it, delete it, and log every step. This replaces the manual
"find departed users, email them, delete by hand" cycle.

> ⚠️ **This repository holds code and documentation only — never data.**
> The AGO exports, priority sheets, manifests, and backups contain student and
> faculty emails, login history, and graduation status. They are education
> records and live in institutional storage, not here. See `data/README.md`.
> The `.gitignore` blocks every data pattern, but the rule comes first.

---

## What's here

```
agol-governance/
├── README.md                      # you are here
├── .gitignore                     # blocks all data + credential patterns
├── scripts/
│   ├── build_priority_list.R      # IDENTIFY: builds the priority target list
│   └── cleanup_targets.py         # OFFBOARD: backs up, checks deps, deletes, logs
├── tests/
│   └── test_cleanup_targets.py    # offline gate-logic tests (no live org needed)
├── docs/
│   ├── policy-and-decisions.md    # the governance rules + open decisions
│   ├── runbook-annual-cleanup.md  # step-by-step for the June run
│   ├── 01_annual_cleanup_runbook.pdf      # how-to guides (committed; no data)
│   ├── 02_identification_and_setup.pdf
│   ├── 03_offboarding_safety_guide.pdf
│   └── README.md                  # doc index + where the big reference PDFs live
├── keep_list.example.csv          # template for the never-delete list (committed)
└── data/            (git-ignored) # everything read or generated lives here:
    ├── README.md                  #   (the one tracked file under data/)
    ├── keep_list.csv              #   the live never-delete list (co-ops, staff)
    └── outputs/                   #   manifests, results, priority list, + backups/
```

## The annual sequence (June)

Full steps are in `docs/runbook-annual-cleanup.md`; the shape is:

1. **Download** the AGO item report and member report into `data/`.
2. **Identify** — run `scripts/build_priority_list.R` → `data/outputs/cleanup_priority.csv`.
   Read the three self-checks it prints (storage units, recoverable total,
   faculty values).
3. **IT round-trip** — send that candidate list to IT; they stamp graduation
   status onto it and hand it back. Place the returned sheet at
   `data/cleanup_priority.csv` (the deletion script's input).
4. **Notify** the owners; record the date in the `notice_date` column. The
   15-day grace clock starts here.
5. **Dry run** — run `scripts/cleanup_targets.py` with `DRY_RUN = True`. Read
   `data/outputs/cleanup_manifest.csv` and `data/outputs/cleanup_review_needed.csv`.
6. **Review** — a second person signs off on the manifest. Public items and
   anything with a dependent are held in the review file, by design.
7. **Live run** — set `DRY_RUN = False`. Each item is backed up to
   `data/outputs/backups/`, dependency-checked, then deleted to the recycle bin.
8. **Close out** — `data/outputs/cleanup_priority_updated.csv` carries the
   write-back; archive the manifest and results to the audit trail.

## Setup

- **R** with `tidyverse` and `lubridate` (for `build_priority_list.R`).
- **Python 3** with `arcgis` (`pip install arcgis`) for `cleanup_targets.py`.
- Edit the `CONFIG` block at the top of each script (org URL, admin username,
  thresholds, file paths). These are settings, not secrets.
- **The admin password is never stored.** `cleanup_targets.py` prompts for it at
  runtime via `getpass`. Do not add it to any file.
- Run as an org administrator (deleting other users' content requires it).
- **Keep-list.** Maintain `data/keep_list.csv` (columns: `email,name,reason`) with
  the never-delete accounts — co-ops, sponsored, active research. Both scripts read
  it and protect anyone on it. `keep_list.example.csv` shows the format; the live
  list stays in `data/` and should be kept in institutional storage between co-ops.

## Testing

```
cd tests && python3 test_cleanup_targets.py
```

This stubs the `arcgis` library and verifies the safety gates offline — public
items, dependency holds, and backup failures all prevent deletion. The true
integration test is the dry run against the org.

## Open items (read before a live run)

- **Graduation status from IT.** "Departed" = graduated. IT takes the candidate
  list and hands it back with the graduation `status` stamped on — it's a
  round-trip column, not a separate source to join. `build_priority_list.R` still
  fills `status` from an inactivity proxy; it should instead defer to the value IT
  returns. See the banner in that file and `docs/policy-and-decisions.md`.
- **Backup retention** is currently the 14-day recycle bin; the roadmap argues
  for offsite file-geodatabase backups (the script now produces these, but the
  retention *policy* is unsettled).
- **Records/legal check** before the first live run, for any grant- or
  retention-bound data.
- **Keep-list maintenance** — `data/keep_list.csv` exists, but it's git-ignored,
  so someone owns keeping it current and carrying it between co-ops (likely
  institutional storage). Confirm emails/roles for the seeded co-op rows.
- **Storage units** must be confirmed on first run (see the script's check).

## Ownership

This repository lives in the **library / RDS institutional account**, with
Bahare Sanaie-Movahed as an owner, so access carries from one co-op to the next.
Do not host the working copy on a personal account — that is exactly the failure
this repo exists to prevent.