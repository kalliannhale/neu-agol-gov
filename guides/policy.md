# Governance Policy & Decisions

The operating rules for the AGO cleanup program. Every threshold in the scripts
traces back to a decision here. Decisions were made with the supervisor (Bahare
Sanaie-Movahed); open items still need an owner or a sign-off.

## Decided

**1. Definition of "departed" and system of record — CLOSED.**
"Departed" means **graduated**. Graduation is authoritative data held by **IT**,
provided in collaboration with RDS. It is not inferred from inactivity. (This
resolves the question that was open at the supervisor sync.)

**2. Retention and grace.**
- Private content: eligible **one year after graduation**.
- Public content: eligible **two years after graduation**, and never auto-deleted
  (routed to review).
- Notice-to-deletion grace window: **15 days** after the owner is emailed.

**3. Content vs. account removal.**
Default is content-only. Account removal applies to accounts **older than three
years with no sign-in that are not faculty/staff**.

**4. Authorization to execute.**
Deletions are prepared as a **dry-run manifest reviewed by a second person**
before any live run. The co-op does not run live deletions unreviewed.

**7. Communication ownership.**
The **co-op emails** account owners flagged for excessive storage and public
content; owners get a **15-day grace** period to respond. The send date is
recorded in the `notice_date` column, which gates eligibility in the script.

**8. Cadence and infrastructure.**
The identification + cleanup run happens **once a year, in June**
(post-commencement), on an **ArcGIS Online Notebook** (its 15-minute minimum
interval is sufficient).

**Faculty/staff protection.**
Faculty/staff are never auto-deleted. They are identified from the member
report's **Role / User Type / Member Categories** (with the
first-initial.lastname email pattern as a backstop), and written into the
priority list with `keep = TRUE`.

**Public vs. private.**
Private content is safe for automated deletion; **public content is never
auto-deleted** and is held for review.

## Open

**5. Backup retention.** The decision on record is the **14-day** native recycle
bin. The roadmap argues that is not a retention policy and recommends offsite
**file-geodatabase backups** — which `cleanup_targets.py` now produces before
every deletion. The *retention duration and restore authority* still need to be
settled.

**6. Records-retention / research-data obligations.** A check with the relevant
office is needed before the **first live run** for any accounts holding
grant-bound or records-retention-bound data. Do not delete faculty or public
content without review.

**Graduation source integration.** `build_priority_list.R` must join the IT
graduation data. Still needed: the **join key** (email vs username) and whether
the source carries a **graduation date** (required to apply the 1-year / 2-year
retention clock).

**Keep-list ownership.** Who maintains the never-delete list (faculty, sponsored,
active research) after the current co-op leaves.

**Dependency mapping completeness.** The script checks per-item deletion blockers
and reverse relationships; a full org-wide usage-dependency map is a future
enhancement.

**Storage units.** Confirm on the first run of `build_priority_list.R` that the
`File/Feature Storage Size` unit yields an org total near 1,151 GB.

## Co-op tracking

The program is staffed by RDS co-ops. Maintain a roster (name, term, access
granted/revoked) so repository and org-admin access transfers cleanly between
cohorts rather than evaporating with a personal account.