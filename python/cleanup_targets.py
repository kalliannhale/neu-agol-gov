#!/usr/bin/env python3
"""
Kalli A. Hale | June 2026 | RDS ArcGIS Online Governance

The offboarding half of the cleanup. Reads the reviewed priority list, finds
    everything each graduated user owns, and writes an audit manifest. On a live
    run it backs each item up, makes sure nothing downstream still leans on it,
    and only THEN deletes it. Defaults to a dry run — it touches nothing until you
    flip DRY_RUN to False on purpose.

where this sits in the pipeline:
    identification (build_priority_list.R)  ->  cleanup_priority.csv
        ->  this script, dry run  ->  a human reads the manifest
        ->  this script, live: back up -> dependency-check -> delete -> log

the "who counts as a target" thresholds live UPSTREAM in the R step, not here.
    this script trusts the list it's handed and only layers the safety policy on top.

needs the ArcGIS API for Python (pip install arcgis), and you run it as an org
    admin, since deleting other people's content needs the privileges.

what changed from the first draft (and why each one earns its keep):
    1. public content is never auto-deleted — it goes to review instead.
    2. faculty/staff are a hard keep, even if the spreadsheet missed them.
    3. owners must've been notified + the 15-day grace passed before a live delete.
    4. every item is backed up BEFORE it's deleted (no backup = no delete).
    5. dependencies HALT the delete instead of getting cleared out of the way.
    6. deliberate throttling between calls, plus a deleted-flag write-back so reruns
        don't reprocess the same rows.
"""

import csv
import os
import re
import sys
import time
import getpass
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta

from arcgis.gis import GIS

# ----------------------------------------------------------------------------
# config — edit this block, then run it
# ----------------------------------------------------------------------------

ORG_URL = "https://nu.maps.arcgis.com"   # our AGO org
ADMIN_USERNAME = "RDS_NU"                 # or None to get prompted

# inputs live in data/ (you put them there); everything we generate goes to data/outputs/
INPUT_CSV     = "data/cleanup_priority.csv"     # the reviewed list, back from IT
KEEP_LIST_CSV = "data/keep_list.csv"            # never-delete names/emails (co-ops, etc.)
MANIFEST_CSV = "data/outputs/cleanup_manifest.csv"          # everything we looked at + what we decided
RESULTS_LOG  = "data/outputs/cleanup_results.csv"           # how each item actually turned out
REVIEW_CSV   = "data/outputs/cleanup_review_needed.csv"     # the stuff we held back for a human
UPDATED_CSV  = "data/outputs/cleanup_priority_updated.csv"  # input list + the deleted-flag write-back
BACKUP_DIR   = "data/outputs/backups"           # pre-delete exports land here

# ~~ the single most important switch in this file ~~
# True  = look, but touch nothing. run this first and read the manifest.
# False = actually back up and delete.
DRY_RUN = True

# deletes for supported types sit in the recycle bin for 14 days first.
# flip to True ONLY if you want them gone immediately + unrecoverably (you don't).
PERMANENT_DELETE = False

# a protected item is usually protected for a reason, so we leave it be.
UNPROTECT_BEFORE_DELETE = False

# ~~ safety gates ~~ all default ON; flipping one OFF should be a deliberate call
EXCLUDE_PUBLIC          = True   # public items are never auto-deleted; send to review
GUARD_FACULTY           = True   # faculty/staff are off-limits (pattern below)
HALT_ON_DEPENDENCIES    = True   # if something depends on an item, do NOT delete it
BACKUP_BEFORE_DELETE    = True   # no backup, no delete — full stop
REQUIRE_NOTICE_AND_GRACE = True  # owner must've been emailed + the grace window passed
WRITE_BACK_DELETED      = True   # mark deleted=TRUE on the rows we fully cleaned
REQUIRE_CONFIRMATION    = True   # live run makes you type it out first
                                 # (set False when it runs unattended in a Notebook)

GRACE_DAYS = 15                  # days between the notice and "okay to delete"
THROTTLE_SECONDS = 0.5           # breather between API calls; bump it up if you see 503s

# faculty/staff get caught here by the "first-initial.lastname" email shape,
# e.g. b.sanaiemovahed@northeastern.edu. this is only a BACKSTOP (NOT the real
# signal!) — Role/User Type in the member report is the truth. confirm before trusting.
FACULTY_EMAIL_PATTERN = r"^[a-z]\.[a-z'\-]+@"

# what the columns are called in our CSV
EMAIL_COLUMN  = "final_email"
NAME_COLUMN   = "final_name"
STATUS_COLUMN = "status"
KEEP_COLUMN   = "keep"
DELETED_COLUMN = "deleted"
NOTICE_DATE_COLUMN = "notice_date"   # when the owner was emailed (any normal date format)

# the never-delete list (co-ops, protected staff), loaded from KEEP_LIST_CSV at run time
KEEP_LIST_EMAILS = set()
KEEP_LIST_NAMES = set()

# hosted data has no file to hand us, so we have to EXPORT it to back it up.
# everything else we just download as-is. add to this set if we pick up more
# hosted types (hosted tables, etc.).
EXPORTABLE_TO_FGDB = {"Feature Service", "Feature Collection"}

# relationship types to ask about by name, for older API versions that need it spelled out
REL_TYPES = ["Map2Service", "Service2Data", "Service2Service",
             "Map2FeatureCollection", "WMA2Code", "Service2Layer",
             "Survey2Service", "Survey2Data", "Solution2Item",
             "Area2Package", "Map2Area"]

# ----------------------------------------------------------------------------
# the selection rule — which rows are actually in scope.
# classify_row() hands back "in_scope" or the reason a row dropped out, so the
#     run summary can tell you *why* things were skipped. worth reading closely.
# ----------------------------------------------------------------------------

def is_faculty_email(email: str) -> bool:
    return bool(re.match(FACULTY_EMAIL_PATTERN, (email or "").strip().lower()))


def notice_grace_ok(row: dict) -> bool:
    """did we email them, and has the grace window run out yet?"""
    if not REQUIRE_NOTICE_AND_GRACE:
        return True
    raw = (row.get(NOTICE_DATE_COLUMN) or "").strip()
    if not raw:
        return False
    parsed = None
    # try the date shapes people actually type
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        # last resort: let python have a go at it
        try:
            parsed = datetime.fromisoformat(raw).date()
        except Exception:
            return False
    # eligible once today is past (notice + grace)
    return date.today() >= parsed + timedelta(days=GRACE_DAYS)


def classify_row(row: dict) -> str:
    status  = (row.get(STATUS_COLUMN) or "").strip().upper()
    keep    = (row.get(KEEP_COLUMN) or "").strip().upper()
    deleted = (row.get(DELETED_COLUMN) or "").strip().upper()
    email   = (row.get(EMAIL_COLUMN) or "").strip()
    name    = (row.get(NAME_COLUMN) or "").strip().lower()

    # graduated is the only thing we ever act on
    if status != "DEPARTED":
        return "not_departed"
    # somebody explicitly said hands off
    if keep == "TRUE":
        return "keep_flagged"
    # already handled on an earlier run
    if deleted == "TRUE":
        return "already_deleted"
    # on the never-delete list (co-ops, protected staff)
    if email.lower() in KEEP_LIST_EMAILS or name in KEEP_LIST_NAMES:
        return "keep_listed"
    # faculty/staff backstop, even if 'keep' never got set
    if GUARD_FACULTY and is_faculty_email(email):
        return "faculty_guard"
    # emailed + waited out the grace period?
    if not notice_grace_ok(row):
        return "notice_pending"
    return "in_scope"


# ----------------------------------------------------------------------------
# the machinery — you shouldn't need to touch much below here
# ----------------------------------------------------------------------------

MANIFEST_FIELDS = ["timestamp", "owner_name", "owner_email", "owner_username",
                   "item_id", "title", "type", "size_gb", "is_public",
                   "disposition", "reasons", "blockers", "url"]
RESULTS_FIELDS = MANIFEST_FIELDS + ["result", "detail"]
REVIEW_FIELDS = ["owner_name", "owner_email", "item_id", "title", "type",
                 "size_gb", "is_public", "reasons", "blockers", "url"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def throttle():
    if THROTTLE_SECONDS:
        time.sleep(THROTTLE_SECONDS)


def load_keep_list():
    """pull the never-delete names/emails out of KEEP_LIST_CSV (co-ops, protected
        staff). matches on email or name, whichever a row has. no file = empty list,
        and we just lean on the faculty pattern + the keep column instead."""
    if not os.path.exists(KEEP_LIST_CSV):
        print(f"  (no keep-list at {KEEP_LIST_CSV} — relying on the keep column + faculty guard)")
        return
    with open(KEEP_LIST_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            e = (row.get("email") or "").strip().lower()
            n = (row.get("name") or "").strip().lower()
            if e:
                KEEP_LIST_EMAILS.add(e)
            if n:
                KEEP_LIST_NAMES.add(n)
    print(f"  keep-list: {len(KEEP_LIST_EMAILS)} emails, {len(KEEP_LIST_NAMES)} names protected")


def connect() -> GIS:
    user = ADMIN_USERNAME or input("Admin username: ").strip()
    # password is asked for at runtime and never stored anywhere
    pwd = getpass.getpass(f"Password for {user}: ")
    gis = GIS(ORG_URL, user, pwd)
    me = gis.users.me
    print(f"Connected as {me.username} (role: {me.role})")
    # if this isn't an admin account it can't touch other people's content
    if me.role not in ("org_admin", "admin"):
        print("  WARNING: this account may not have admin rights to delete "
              "other users' content.")
    return gis


def resolve_username(gis: GIS, email: str):
    """email -> AGO username (they're very often not the same thing)."""
    if not email:
        return None
    matches = gis.users.search(query=f'email:"{email}"', max_users=10)
    # keep only exact matches; drop anything fuzzy the search threw in
    matches = [u for u in matches if (u.email or "").lower() == email.lower()]
    throttle()
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # two accounts on one email? don't guess — skip it
        print(f"  AMBIGUOUS: {email} -> {[u.username for u in matches]}; skipping.")
        return None
    return None


def list_owned_items(gis: GIS, username: str):
    """everything this user owns. heads up: search quietly truncates at 10,000 —
        nobody should be anywhere near that, but we shout if someone is."""
    items = gis.content.search(query=f"owner:{username}", max_items=10000)
    throttle()
    if len(items) >= 10000:
        print(f"  WARNING: {username} returned 10,000 items -- result may be "
              "truncated. Chunk this owner's query before a live run.")
    return items


def is_public(item) -> bool:
    """is this shared with everyone? if we genuinely can't tell, we call it public
        on purpose — safer to hold a private item for review by mistake than to
        delete a public one."""
    try:
        sw = item.shared_with
        return bool(sw.get("everyone"))
    except Exception:
        return True


def find_blockers(item):
    """every reason this item should NOT be auto-deleted: anything the platform
        itself refuses to delete, plus anything that still *points at* this item
        (a downstream consumer). empty list == safe to go."""
    blockers = []

    # 1. the easy check: does AGO itself say no?
    try:
        check = item.delete(dry_run=True)
        if isinstance(check, dict) and check.get("can_delete") is False:
            details = check.get("details", {}) or {}
            for off in details.get("offending_items", []) or []:
                oid = getattr(off, "id", None)
                if oid is None and isinstance(off, dict):
                    oid = off.get("itemId") or off.get("id")
                blockers.append(f"blocks_delete:{oid or off}")
    except Exception as e:
        # can't even check? that's reason enough to hold back
        blockers.append(f"dry_run_error:{e}")

    # 2. the harder check: who's still using this? (reverse relationships)
    rels = []
    try:
        rels = item.related_items(direction="reverse")
    except TypeError:
        # older API wants the relationship type named out loud
        for rt in REL_TYPES:
            try:
                rels.extend(item.related_items(rt, "reverse"))
            except Exception:
                pass
    except Exception:
        pass

    # dedupe, and note who owns the thing that depends on us
    seen = set()
    for r in rels:
        rid = getattr(r, "id", None)
        if rid and rid not in seen:
            seen.add(rid)
            blockers.append(f"used_by:{rid}({getattr(r, 'owner', '?')})")

    throttle()
    return blockers


def backup_item(item) -> str:
    """save the item before we delete it — export it if it's hosted, download it
        if it isn't — and hand back the path. raises if it fails, and the caller
        reads that as 'do NOT delete this one'. the temporary export item gets
        cleaned up afterward so the backup doesn't eat org storage (which would
        rather defeat the point)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if item.type in EXPORTABLE_TO_FGDB:
        # hosted: spin up a file-geodatabase export, grab it, then bin the export item
        export_title = f"_backup_{item.id}_{int(time.time())}"
        exported = item.export(export_title, "File Geodatabase")
        try:
            path = exported.download(save_path=BACKUP_DIR)
        finally:
            # clean up the temp export no matter what happened above
            try:
                exported.delete(permanent=True)
            except Exception:
                pass
        if not path:
            raise RuntimeError("export produced no file")
        return path
    # everything else: just pull the file down as-is
    path = item.download(save_path=BACKUP_DIR)
    if not path:
        raise RuntimeError(f"no downloadable data for type '{item.type}'")
    return path


def write_result(results_w, base, result, detail=""):
    # one row in the results log
    row = {k: base.get(k, "") for k in MANIFEST_FIELDS}
    row["result"] = result
    row["detail"] = detail
    results_w.writerow(row)


def process_item(item, owner_email, owner_name, manifest_w, results_w, review_w):
    """look at one item, write it down, and — on a live run — back it up and delete
        it only if it clears every gate. returns the result string so main() tallies."""
    size_gb = (getattr(item, "size", 0) or 0) / (1024 ** 3)  # binary GB, straight off the item
    pub = is_public(item)

    # the row we'll drop into the manifest
    base = {
        "timestamp": now(),
        "owner_name": owner_name,
        "owner_email": owner_email,
        "owner_username": item.owner,
        "item_id": item.id,
        "title": item.title,
        "type": item.type,
        "size_gb": round(size_gb, 4),
        "is_public": "Yes" if pub else "No",
        "url": item.homepage,
        "blockers": "",
        "reasons": "",
        "disposition": "",
    }

    # ~~ work out the disposition ~~ we do this in dry run too, so the preview is honest
    reasons = []
    if EXCLUDE_PUBLIC and pub:
        reasons.append("public")          # public? hands off, goes to review

    blockers = find_blockers(item) if HALT_ON_DEPENDENCIES else []
    if blockers:
        reasons.append("dependencies")    # something leans on it; review, not delete
        base["blockers"] = ";".join(blockers)[:1000]

    base["reasons"] = ",".join(reasons)
    base["disposition"] = "review" if reasons else "delete"
    manifest_w.writerow({k: base.get(k, "") for k in MANIFEST_FIELDS})

    # held back for a human, whatever mode we're in
    if reasons:
        review_w.writerow({k: base.get(k, "") for k in REVIEW_FIELDS})
        write_result(results_w, base, "skipped_" + "_".join(reasons),
                     base.get("blockers", ""))
        return size_gb, "skipped"

    if DRY_RUN:
        # dry run stops here — we looked, we logged, we touched nothing
        write_result(results_w, base, "dry_run", "")
        return size_gb, "dry_run"

    # ~~ live from here ~~ back up first; if that fails we do NOT delete (full stop)
    bpath = ""
    if BACKUP_BEFORE_DELETE:
        try:
            bpath = backup_item(item)
        except Exception as e:
            review_w.writerow({k: base.get(k, "") for k in REVIEW_FIELDS})
            write_result(results_w, base, "backup_failed", str(e))
            return size_gb, "backup_failed"

    try:
        # unprotect only if we were explicitly told it's okay
        if UNPROTECT_BEFORE_DELETE and getattr(item, "protected", False):
            item.protect(enable=False)
        # the actual delete — to the recycle bin unless PERMANENT_DELETE is on
        ok = item.delete(permanent=PERMANENT_DELETE)
        result = "deleted" if ok else "delete_returned_false"
        write_result(results_w, base, result, f"backup={bpath}")
        throttle()
        return size_gb, result
    except Exception as e:
        # something blew up; log it and keep going
        write_result(results_w, base, "error", str(e))
        return size_gb, "error"


def write_back(rows, fieldnames, cleaned_emails):
    """stamp deleted=TRUE on the owners we fully cleaned. writes to a NEW file so a
        bad run can never clobber the source list."""
    fields = list(fieldnames)
    if DELETED_COLUMN not in fields:
        fields.append(DELETED_COLUMN)
    with open(UPDATED_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            out = dict(r)
            if (r.get(EMAIL_COLUMN) or "").strip().lower() in cleaned_emails:
                out[DELETED_COLUMN] = "TRUE"
            w.writerow({k: out.get(k, "") for k in fields})
    print(f"Write-back written: {UPDATED_CSV} "
          f"({len(cleaned_emails)} owners stamped deleted=TRUE)")


def main():
    # read the list; figure out tab vs comma on our own
    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delim)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    # this list comes back from IT, possibly reshaped — so check the columns we
    # actually need are here, and say so plainly if they aren't (rather than
    # quietly classifying everything as out-of-scope)
    needed = [EMAIL_COLUMN, STATUS_COLUMN, KEEP_COLUMN, DELETED_COLUMN, NOTICE_DATE_COLUMN]
    missing = [c for c in needed if c not in fieldnames]
    if missing:
        print(f"  NOTE: the input is missing expected columns: {missing}")
        print( "        it's the sheet IT handed back — reconcile the names in the")
        print( "        CONFIG block above, or wrangle the sheet in R first.")

    # load the never-delete list, then sort every row into a bucket so we can
    # report why things dropped out
    load_keep_list()
    buckets = defaultdict(list)
    for r in rows:
        buckets[classify_row(r)].append(r)
    targets = buckets["in_scope"]

    print(f"\n{len(rows)} rows in file.")
    for reason in ("in_scope", "not_departed", "keep_flagged", "keep_listed",
                   "already_deleted", "faculty_guard", "notice_pending"):
        if buckets[reason]:
            print(f"  {reason:<16}: {len(buckets[reason])}")
    if not targets:
        print("\nNothing in scope. Check classify_row() against your columns.")
        return

    # spell out exactly what mode we're in before doing anything
    mode = ("DRY RUN -- nothing will be deleted" if DRY_RUN else
            ("LIVE -- PERMANENT delete" if PERMANENT_DELETE else
             "LIVE -- delete to 14-day recycle bin"))
    print(f"\nMode: {mode}")
    print(f"Gates: public={'skip' if EXCLUDE_PUBLIC else 'OFF'}, "
          f"faculty={'guard' if GUARD_FACULTY else 'OFF'}, "
          f"deps={'halt' if HALT_ON_DEPENDENCIES else 'OFF'}, "
          f"backup={'on' if BACKUP_BEFORE_DELETE else 'OFF'}, "
          f"notice+grace={'required' if REQUIRE_NOTICE_AND_GRACE else 'OFF'}")
    print("-" * 64)

    # make a human type it out loud before anything goes live
    if not DRY_RUN and REQUIRE_CONFIRMATION:
        try:
            if input('Type "DELETE" to proceed with the live run: ').strip() != "DELETE":
                print("Aborted -- confirmation not given.")
                return
        except EOFError:
            print("Aborted -- no input available (set REQUIRE_CONFIRMATION=False "
                  "for unattended runs).")
            return

    gis = connect()

    # three logs: the manifest, the per-item results, and the review pile
    mf = open(MANIFEST_CSV, "w", newline="", encoding="utf-8")
    rf = open(RESULTS_LOG, "w", newline="", encoding="utf-8")
    vf = open(REVIEW_CSV, "w", newline="", encoding="utf-8")
    manifest_w = csv.DictWriter(mf, fieldnames=MANIFEST_FIELDS)
    results_w = csv.DictWriter(rf, fieldnames=RESULTS_FIELDS)
    review_w = csv.DictWriter(vf, fieldnames=REVIEW_FIELDS)
    manifest_w.writeheader()
    results_w.writeheader()
    review_w.writeheader()

    total_items = 0
    total_gb = 0.0
    unresolved = []
    per_owner = defaultdict(lambda: {"deleted": 0, "skipped": 0, "error": 0,
                                     "backup_failed": 0, "attempted": 0})

    for r in targets:
        email = (r.get(EMAIL_COLUMN) or "").strip()
        name = (r.get(NAME_COLUMN) or "").strip()
        # email -> account; skip cleanly if we can't pin it down
        user = resolve_username(gis, email)
        if user is None:
            unresolved.append((name, email))
            print(f"  UNRESOLVED: {name} <{email}> -- no matching account; skipping.")
            continue

        # walk everything they own
        items = list_owned_items(gis, user.username)
        user_gb = 0.0
        ekey = email.lower()
        for item in items:
            size_gb, result = process_item(item, email, name,
                                           manifest_w, results_w, review_w)
            user_gb += size_gb
            total_items += 1
            # tally up how this one went, per owner
            per_owner[ekey]["attempted"] += 1
            if result in ("deleted", "delete_returned_false"):
                per_owner[ekey]["deleted"] += 1
            elif result == "skipped":
                per_owner[ekey]["skipped"] += 1
            elif result == "backup_failed":
                per_owner[ekey]["backup_failed"] += 1
            elif result == "error":
                per_owner[ekey]["error"] += 1
        total_gb += user_gb
        print(f"  {name:<28} {user.username:<22} "
              f"{len(items):>4} items  {user_gb:>8.3f} GB")

    mf.close(); rf.close(); vf.close()

    # an owner only counts as "fully cleaned" if every item went and nothing
    # got skipped or failed along the way
    cleaned = {e for e, c in per_owner.items()
               if c["deleted"] > 0 and c["skipped"] == 0
               and c["error"] == 0 and c["backup_failed"] == 0}

    print("-" * 64)
    print(f"Users processed : {len(targets) - len(unresolved)}")
    print(f"Unresolved      : {len(unresolved)}")
    print(f"Items considered: {total_items}")
    print(f"Storage in scope: {total_gb:.2f} GB")
    print(f"\nManifest : {MANIFEST_CSV}")
    print(f"Results  : {RESULTS_LOG}")
    print(f"Review   : {REVIEW_CSV}  (public / dependency holds -- handle by hand)")

    if not DRY_RUN and WRITE_BACK_DELETED:
        write_back(rows, fieldnames, cleaned)

    if DRY_RUN:
        print("\nThis was a DRY RUN. Review the manifest and the review file, "
              "then set DRY_RUN = False to delete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")