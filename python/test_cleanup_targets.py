#!/usr/bin/env python3
"""
Kalli A. Hale | June 2026 | RDS ArcGIS Online Governance

Offline tests for cleanup_targets.py. The whole point is to check the SAFETY
    GATES without touching a real org: we stub out `arcgis`, hand process_item a
    pile of fake items — one for each thing that should stop a delete — and make
    sure it does the right thing. it does NOT test the real API calls
    (export / related_items / delete); only the dry run against the org can do that.

run it:  python3 test_cleanup_targets.py
"""

import os
import sys
import types
from datetime import date, timedelta

# fake out the arcgis package so we can import cleanup_targets without it installed
_arc = types.ModuleType("arcgis")
_gis = types.ModuleType("arcgis.gis")
class GIS:  # placeholder — we never actually build one of these
    pass
_gis.GIS = GIS
_arc.gis = _gis
sys.modules["arcgis"] = _arc
sys.modules["arcgis.gis"] = _gis

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import python.cleanup_targets as ct

# no real sleeping, and keep the backups out of the way
ct.THROTTLE_SECONDS = 0
ct.BACKUP_DIR = "/tmp/cleanup_test_backups"


# ~~ the fakes ~~ stand-ins for the AGO objects the script expects
class FakeRelated:
    def __init__(self, iid, owner): self.id = iid; self.owner = owner


class FakeExport:
    def __init__(self, ok): self._ok = ok
    def download(self, save_path=None):
        if not self._ok:
            raise RuntimeError("export download failed")
        return os.path.join(save_path or ".", "export.gdb.zip")
    def delete(self, permanent=False): return True


class FakeItem:
    def __init__(self, iid="i1", title="thing", itype="CSV", size=1 << 30,
                 owner="u1", public=False, can_delete=True, offending=None,
                 reverse=None, backup_ok=True, raise_share=False, protected=False):
        self.id = iid; self.title = title; self.type = itype
        self.size = size; self.owner = owner; self.homepage = "http://x/" + iid
        self.protected = protected
        self._public = public; self._can_delete = can_delete
        self._offending = offending or []
        self._reverse = reverse or []
        self._backup_ok = backup_ok; self._raise_share = raise_share
        self.delete_calls = []

    @property
    def shared_with(self):
        if self._raise_share:
            raise RuntimeError("cannot read sharing")
        return {"everyone": self._public, "org": False, "groups": []}

    def delete(self, permanent=False, dry_run=False):
        if dry_run:
            return {"can_delete": self._can_delete,
                    "details": {"offending_items": self._offending}}
        self.delete_calls.append(permanent)
        return True

    def related_items(self, rel_type=None, direction="forward"):
        return list(self._reverse) if direction == "reverse" else []

    def export(self, title, fmt):
        return FakeExport(self._backup_ok)

    def download(self, save_path=None):
        if not self._backup_ok:
            raise RuntimeError("download failed")
        return os.path.join(save_path or ".", self.id + ".zip")

    def protect(self, enable=True): pass


class ListWriter:
    def __init__(self): self.rows = []
    def writerow(self, d): self.rows.append(dict(d))
    def writeheader(self): pass


# ~~ the runner ~~ tiny pass/fail harness
PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}")


def run_item(item, dry_run):
    ct.DRY_RUN = dry_run
    ct.PERMANENT_DELETE = False
    ct.EXCLUDE_PUBLIC = True
    ct.HALT_ON_DEPENDENCIES = True
    ct.BACKUP_BEFORE_DELETE = True
    mw, rw, vw = ListWriter(), ListWriter(), ListWriter()
    size, result = ct.process_item(item, "u1@x.edu", "U One", mw, rw, vw)
    return result, mw.rows[-1], rw.rows[-1], vw.rows


print("\n== unit: is_faculty_email ==")
check("first-initial.lastname is faculty",
      ct.is_faculty_email("b.sanaiemovahed@northeastern.edu") is True)
check("first-initial j.smith is faculty",
      ct.is_faculty_email("j.smith@northeastern.edu") is True)
check("full firstname.lastname is NOT faculty",
      ct.is_faculty_email("john.smith@northeastern.edu") is False)
check("lastname.initial is NOT faculty",
      ct.is_faculty_email("smith.j@northeastern.edu") is False)

print("\n== unit: notice_grace_ok (GRACE_DAYS=%d) ==" % ct.GRACE_DAYS)
ct.REQUIRE_NOTICE_AND_GRACE = True
old = (date.today() - timedelta(days=ct.GRACE_DAYS + 5)).isoformat()
new = (date.today() - timedelta(days=3)).isoformat()
check("notice older than grace -> eligible",
      ct.notice_grace_ok({ct.NOTICE_DATE_COLUMN: old}) is True)
check("recent notice -> NOT eligible",
      ct.notice_grace_ok({ct.NOTICE_DATE_COLUMN: new}) is False)
check("missing notice -> NOT eligible",
      ct.notice_grace_ok({ct.NOTICE_DATE_COLUMN: ""}) is False)

print("\n== unit: classify_row (uses the configured column names) ==")
def row(**kw):
    base = {ct.STATUS_COLUMN: "DEPARTED", ct.KEEP_COLUMN: "", ct.DELETED_COLUMN: "",
            ct.EMAIL_COLUMN: "student.x@northeastern.edu", ct.NOTICE_DATE_COLUMN: old}
    base.update(kw); return base
check("clean departed -> in_scope", ct.classify_row(row()) == "in_scope")
check("active -> not_departed", ct.classify_row(row(**{ct.STATUS_COLUMN: "ACTIVE"})) == "not_departed")
check("keep flag -> keep_flagged", ct.classify_row(row(**{ct.KEEP_COLUMN: "TRUE"})) == "keep_flagged")
check("faculty email -> faculty_guard",
      ct.classify_row(row(**{ct.EMAIL_COLUMN: "b.sanaiemovahed@northeastern.edu"})) == "faculty_guard")
check("no notice -> notice_pending", ct.classify_row(row(**{ct.NOTICE_DATE_COLUMN: ""})) == "notice_pending")

print("\n== gate: is_public fail-safe ==")
check("public item detected", ct.is_public(FakeItem(public=True)) is True)
check("private item detected", ct.is_public(FakeItem(public=False)) is False)
check("unreadable sharing -> treated public", ct.is_public(FakeItem(raise_share=True)) is True)

print("\n== gate: find_blockers ==")
check("clean item has no blockers", ct.find_blockers(FakeItem()) == [])
check("platform-blocked item is flagged",
      len(ct.find_blockers(FakeItem(can_delete=False,
          offending=[FakeRelated("dep1", "u9")]))) >= 1)
check("downstream consumer is flagged",
      any("used_by" in b for b in ct.find_blockers(
          FakeItem(reverse=[FakeRelated("map1", "active_user")]))))

print("\n== DRY RUN dispositions ==")
res, man, _, rev = run_item(FakeItem(public=True), dry_run=True)
check("public -> review disposition", man["disposition"] == "review" and "public" in man["reasons"])
check("public -> written to review file", len(rev) == 1)
res, man, _, rev = run_item(FakeItem(reverse=[FakeRelated("m1", "active")]), dry_run=True)
check("dependency -> review disposition", man["disposition"] == "review" and "dependencies" in man["reasons"])
res, man, _, _ = run_item(FakeItem(), dry_run=True)
check("clean item -> delete disposition, result dry_run",
      man["disposition"] == "delete" and res == "dry_run")

print("\n== LIVE dispositions (the part that actually deletes) ==")
it = FakeItem()
res, _, _, _ = run_item(it, dry_run=False)
check("clean item -> deleted", res == "deleted")
check("clean item -> delete actually called (recycle bin)", it.delete_calls == [False])

it = FakeItem(backup_ok=False)
res, _, _, rev = run_item(it, dry_run=False)
check("backup failure -> result backup_failed", res == "backup_failed")
check("backup failure -> NOT deleted", it.delete_calls == [])
check("backup failure -> sent to review", len(rev) == 1)

it = FakeItem(public=True)
res, _, _, _ = run_item(it, dry_run=False)
check("public item live -> skipped, NOT deleted", res == "skipped" and it.delete_calls == [])

it = FakeItem(reverse=[FakeRelated("m2", "active")])
res, _, _, _ = run_item(it, dry_run=False)
check("dependency live -> skipped, NOT deleted", res == "skipped" and it.delete_calls == [])

print(f"\n{'='*40}\n{PASS} passed, {FAIL} failed\n{'='*40}")
sys.exit(1 if FAIL else 0)