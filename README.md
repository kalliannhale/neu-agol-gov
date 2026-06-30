# neu-agol-gov

# Runbook — Annual Cleanup Run (June)

Step-by-step for one full cycle. Assumes the setup in the main README is done
and the open items in `policy-and-decisions.md` are resolved (in particular, the
IT graduation source is joined into `build_priority_list.R` before any live run).

Run everything from the repository root unless noted. Data files go in `data/`;
script outputs land in `outputs/`.

## 1. Download the reports

From AGO (Organization → Reports), export into `data/`:
- the **item report** (item activity — has Owner, storage sizes, share level,
  view counts, date last viewed, recycle-bin flag),
- the **member report** (has Username, Email, Role, User Type, Member Categories,
  Last Login Date, account status).

Obtain the current **graduation data from IT** and place it where
`build_priority_list.R` expects it.

## 2. Identify targets

```
Rscript scripts/build_priority_list.R
```

Read the printed checks before trusting the output:
- **Units** — the org total should be near 1,151 GB. If it is off by orders of
  magnitude, fix `STORAGE_COL_UNIT` and re-run.
- **Faculty values** — the distinct Role / User Type / Member Categories lists
  tell you what to put in `FACULTY_ROLES` / `FACULTY_USER_TYPES`. Fill them and
  re-run so faculty are correctly held out.
- **Recovery** — the recoverable total is a sanity check against the report's
  scenario.

Output: `outputs/cleanup_priority.csv`.

## 3. Notify owners

Email the flagged owners. Record the send date in each row's `notice_date`
column. This starts the 15-day grace clock; the deletion script will not act on
a row until the grace window has elapsed.

## 4. Dry run

In `scripts/cleanup_targets.py`, confirm `DRY_RUN = True`, then:

```
python3 scripts/cleanup_targets.py
```

Read:
- `cleanup_manifest.csv` — everything considered, with its disposition.
- `cleanup_review_needed.csv` — public items and items with a dependent, held
  back on purpose. These are handled by hand, not by the batch.

## 5. Review and authorize

A second person reviews the manifest and the review file and signs off. Spot-check
that nothing belonging to an active or faculty user is in the delete set.

## 6. Live run

Set `DRY_RUN = False` (leave `PERMANENT_DELETE = False`). For an unattended
Notebook run, also set `REQUIRE_CONFIRMATION = False`; for an interactive run,
leave it on and type the confirmation when prompted.

```
python3 scripts/cleanup_targets.py
```

Each in-scope item is exported to `backups/`, dependency-checked, then deleted to
the 14-day recycle bin. Items whose backup fails are skipped, not deleted.

## 7. Close out

- `outputs/cleanup_priority_updated.csv` has `deleted = TRUE` written back for
  fully-cleaned owners — promote it to the tracking sheet for next year.
- Archive `cleanup_manifest.csv` and `cleanup_results.csv` to the audit trail.
- Move `backups/` to institutional storage; keep per the retention policy.

## If something goes wrong

- A deletion that should not have happened: supported types sit in the recycle
  bin for 14 days; restore from there, or from the `backups/` export.
- A broken downstream map after a run: check `cleanup_results.csv` and the
  review file for the item, and restore from `backups/`.