## ============================================================================
## Kalli A. Hale | June 2026 | RDS ArcGIS Online Governance
##
## The identify half of the cleanup. Rebuilds the priority-target step from the
##     storage-crisis report's method, against the REAL AGO report schemas, and
##     writes cleanup_priority.csv — which is exactly what cleanup_targets.py reads.
##
## the old R for this lived on a laptop that's gone, so this rebuilds the recipe
##     the report lays out:
##       - add up each person's storage from the item report,
##       - read how long they've been gone from Last Login Date,
##       - take the "aggressive" cut (>= 1 GB, gone 1+ year) that freed ~557 GB,
##         ranked by the priority score (storage 35 / inactivity 30 / stale 20 / low-use 15).
##
## heads up: this hasn't actually been run yet. treat the FIRST RUN as the test —
##     the checks it prints (org total vs ~1,151 GB, recoverable vs ~557 GB, the
##     distinct Role/Type lists) are there to confirm it matches the report, not to
##     take any of my assumptions on faith.
## ============================================================================

library(tidyverse)
library(lubridate)

## ###########################################################################
## ~~ OPEN ITEM — don't run this live yet ~~
##   "departed" means GRADUATED, and graduation is IT's data, not ours to guess.
##     the eligibility block below still fakes it from an INACTIVITY PROXY, which
##     is WRONG for this program. it needs to JOIN the IT graduation source
##     (status + graduation date) and gate on:
##       graduated AND past retention (1 yr private / 2 yr public) AND not faculty.
##   storage size + the priority score only RANK the targets — they don't pick them.
##   still need from IT: the join key (email vs username), and whether the source
##     carries a graduation date. see docs/policy-and-decisions.md (open items).
## ###########################################################################

## ---- config — edit, then run -----------------------------------------------
ITEM_REPORT   <- "OrganizationItems.csv"     # item activity report (this one's got the storage)
MEMBER_REPORT <- "OrganizationMembers.csv"   # member report (Last Login Date, Role, etc.)
OUTPUT_CSV    <- "cleanup_priority.csv"       # what we hand off to cleanup_targets.py

## the File/Feature Storage Size columns don't say what unit they're in, and AGO
##     is genuinely inconsistent about it. SET THIS, run, and check the org total
##     it prints lands near ~1,151 GB. off by ~1,000,000x = bytes vs MB; ~1,000x = KB vs MB.
STORAGE_COL_UNIT   <- "MB"      # one of: "bytes", "KB", "MB", "GB"
REFERENCE_TOTAL_GB <- 1151      # from the report; only used for that sanity check

## the report's "aggressive" cut — the one that actually freed the storage
MIN_STORAGE_GB <- 1
INACTIVE_DAYS  <- 365

## faculty/staff are never targeted. the member report's Role / User Type / Member
##     Categories are way better signals than an email shape — run once, read the
##     distinct-values printout further down, drop the right ones in here, re-run.
##     the email regex is only a backstop.
FACULTY_ROLES          <- c()                  # e.g. c("org_admin") — fill from the printout
FACULTY_USER_TYPES     <- c()                  # e.g. c("GIS Professional Advanced")
FACULTY_CATEGORY_REGEX <- "faculty|staff"      # matched against Member Categories
FACULTY_EMAIL_REGEX    <- "^[a-z]\\.[a-z'-]+@"  # backstop: first-initial.lastname

## owner names sometimes drag a department suffix along; strip it before we match
OWNER_SUFFIX_REGEX <- "(_NU|_PPUA|_CSSH|_COE|_CAMD|_COS|_DMSB|_BCHS|_LAW|_Bouve)$"

## ---- helpers ---------------------------------------------------------------

## AGO exports love to staple a title / StartTime / EndTime block on top before
##     the real header. so we hunt down the header line by the columns it has to
##     contain, and read from there. every key in `keys` must show up on that line.
read_ago <- function(path, keys) {
  lines <- read_lines(path)
  is_hdr <- map_lgl(lines, ~ all(str_detect(.x, fixed(keys))))
  hdr <- which(is_hdr)[1]
  if (is.na(hdr)) stop(sprintf("No header containing %s found in %s",
                               paste(keys, collapse = " + "), path))
  read_csv(paste(lines[hdr:length(lines)], collapse = "\n"),
           show_col_types = FALSE, name_repair = "minimal")
}

## storage column -> GB, in binary (1024), using whatever unit we set above
unit_to_gb <- function(x, unit) {
  f <- switch(unit, bytes = 1 / 1024^3, KB = 1 / 1024^2,
              MB = 1 / 1024, GB = 1,
              stop("STORAGE_COL_UNIT must be bytes/KB/MB/GB"))
  x * f
}

## AGO dates turn up in a few shapes; try the usual suspects
parse_when <- function(x) suppressWarnings(
  parse_date_time(x, orders = c("ymd HMS", "ymd HM", "ymd",
                                "mdy HMS", "mdy HM", "mdy"))
)

## squash a column into 0..1 for the score; all-same or all-missing just goes to 0
norm01 <- function(x) {
  x <- ifelse(is.finite(x), x, NA_real_)
  r <- range(x, na.rm = TRUE)
  if (!all(is.finite(r)) || diff(r) == 0) return(rep(0, length(x)))
  out <- (x - r[1]) / diff(r)
  ifelse(is.na(out), 0, out)
}

## ---- 1. items --------------------------------------------------------------
# grab the columns we care about, give them workable names, then derive the
# fields we actually filter and score on
items <- read_ago(ITEM_REPORT, c("Item ID", "Owner")) %>%
  transmute(
    item_id     = `Item ID`,
    title       = Title,
    item_type   = `Item Type`,
    owner_raw   = Owner,
    file_size   = suppressWarnings(as.numeric(`File Storage Size`)),
    feat_size   = suppressWarnings(as.numeric(`Feature Storage Size`)),
    share_level = `Share Level`,
    views       = suppressWarnings(as.numeric(`View Counts`)),
    last_viewed = parse_when(`Date Last Viewed`),
    in_recycle  = tolower(str_trim(as.character(`In Recycle Bin`))) %in%
                    c("yes", "true", "1")
  ) %>%
  mutate(
    storage_gb = unit_to_gb(coalesce(file_size, 0) + coalesce(feat_size, 0),
                            STORAGE_COL_UNIT),
    is_public  = tolower(str_trim(share_level)) %in% c("public", "everyone"),  # shared with the world?
    owner_key  = str_remove(owner_raw, OWNER_SUFFIX_REGEX) %>% str_trim() %>% tolower(),  # join key
    days_since_viewed = as.numeric(difftime(Sys.time(), last_viewed, units = "days"))  # how long since anyone opened it
  )

## ---- units check — this is what stops a 1000x oopsie ----------------------
cat(sprintf("\n[units] STORAGE_COL_UNIT = '%s'\n", STORAGE_COL_UNIT))
cat(sprintf("[units] Org total: %.1f GB (all items), %.1f GB (excl. recycle bin)\n",
            sum(items$storage_gb, na.rm = TRUE),
            sum(items$storage_gb[!items$in_recycle], na.rm = TRUE)))
cat(sprintf("[units] Crisis report total was ~%d GB. If the number above is off\n",
            REFERENCE_TOTAL_GB))
cat("        by orders of magnitude, change STORAGE_COL_UNIT and re-run.\n")

## ---- 2. roll everything up per owner (recycle-bin items left out) ----------
# one row per person, with their totals
by_owner <- items %>%
  filter(!in_recycle) %>%
  group_by(owner_key) %>%
  summarise(
    storage_gb   = sum(storage_gb, na.rm = TRUE),   # their whole footprint
    item_count   = n(),                             # how many things they own
    public_items = sum(is_public, na.rm = TRUE),    # ...of which are public
    total_views  = sum(coalesce(views, 0)),         # eyeballs across all their stuff
    recent_view_days = suppressWarnings(min(days_since_viewed, na.rm = TRUE)),  # most recent view, days ago
    .groups = "drop"
  ) %>%
  # all-NA min() comes back as Inf; turn that into NA
  mutate(recent_view_days = ifelse(is.finite(recent_view_days),
                                   recent_view_days, NA_real_))

## ---- 3. members ------------------------------------------------------------
# same idea for the people behind the content
members <- read_ago(MEMBER_REPORT, c("Username", "Email")) %>%
  transmute(
    username    = Username,
    name        = Name,
    email       = tolower(str_trim(Email)),         # lowercased for the join
    role        = as.character(Role),
    user_type   = as.character(`User Type`),
    categories  = as.character(`Member Categories`),
    acct_status = `Member Account Status`,
    last_login  = parse_when(`Last Login Date`)
  ) %>%
  mutate(
    username_key     = tolower(str_trim(username)),  # join key
    days_since_login = as.numeric(difftime(Sys.time(), last_login, units = "days")),  # days since they signed in
    # faculty if Role / Type / Category says so — or, failing that, the email shape
    is_faculty = (role %in% FACULTY_ROLES) |
                 (user_type %in% FACULTY_USER_TYPES) |
                 (!is.na(categories) & str_detect(tolower(categories), FACULTY_CATEGORY_REGEX)) |
                 str_detect(email, FACULTY_EMAIL_REGEX)
  )

## ---- so... what does our org actually call faculty? print it and see -------
cat("\n[faculty] Distinct Role values:\n");           print(sort(unique(members$role)))
cat("[faculty] Distinct User Type values:\n");        print(sort(unique(members$user_type)))
cat("[faculty] Distinct Member Categories values:\n");print(sort(unique(members$categories)))
cat("-> Fill FACULTY_ROLES / FACULTY_USER_TYPES from these, then re-run.\n")

## ---- 4. join people to their stuff, bucket activity, score -----------------
joined <- by_owner %>%
  left_join(members, by = c("owner_key" = "username_key")) %>%
  mutate(
    # bucket by how long since last login (the report's 30 / 90 / 365 cutoffs)
    activity_status = case_when(
      is.na(days_since_login) ~ "N/A",
      days_since_login <= 30  ~ "Active",
      days_since_login <= 90  ~ "Recent",
      days_since_login <= 365 ~ "Inactive",
      TRUE                    ~ "Stagnant"
    ),
    # the four ingredients of the priority score, each on 0..1
    s_storage  = norm01(storage_gb),
    s_inactive = norm01(coalesce(days_since_login, 0)),
    s_stale    = norm01(coalesce(recent_view_days, 0)),
    s_lowusage = 1 - norm01(coalesce(total_views, 0)),
    # weighted blend -> 0..100
    priority_score = round(100 * (0.35 * s_storage + 0.30 * s_inactive +
                                  0.20 * s_stale + 0.15 * s_lowusage), 1),
    # the aggressive cut: big enough, gone long enough, and not faculty
    eligible = storage_gb >= MIN_STORAGE_GB &
               !is.na(days_since_login) & days_since_login > INACTIVE_DAYS &
               !coalesce(is_faculty, FALSE)
  )

## ---- 5. write it out (columns line up with cleanup_targets.py) -------------
out <- joined %>%
  transmute(
    final_email = email,
    final_name  = name,
    status      = if_else(eligible, "DEPARTED", ""),   # a PROXY for now — see the banner up top
    keep        = if_else(coalesce(is_faculty, FALSE), "TRUE", ""),  # belt-and-suspenders keep for faculty
    deleted     = "",
    notice_date = "",                                   # gets filled in when we email them
    departed_basis   = if_else(eligible, "inactivity_proxy", ""),
    total_storage_gb = round(storage_gb, 3),
    item_count, public_items, total_views,
    days_since_login = round(days_since_login),
    activity_status, priority_score, owner_key
  ) %>%
  arrange(desc(status == "DEPARTED"), desc(priority_score))

write_csv(out, OUTPUT_CSV)

## ---- 6. the summary — does this match the report? --------------------------
elig <- filter(joined, eligible)
cat("\n", strrep("-", 64), "\n", sep = "")
cat(sprintf("[targets] Eligible users (>=%g GB, >%d days inactive, non-faculty): %d\n",
            MIN_STORAGE_GB, INACTIVE_DAYS, nrow(elig)))
cat(sprintf("[targets] Recoverable in target set: %.1f GB  (report 'aggressive' ~= 557 GB)\n",
            sum(elig$storage_gb)))
cat(sprintf("[targets] N/A last-login (surfaced, NOT auto-targeted): %d users\n",
            sum(joined$activity_status == "N/A")))
cat(sprintf("[targets] Owners with no member match (treated as N/A): %d\n",
            sum(is.na(joined$username))))
cat(sprintf("[targets] Faculty/staff held out: %d\n",
            sum(coalesce(joined$is_faculty, FALSE))))
cat(sprintf("[targets] Eligible users still owning public items (script skips those): %d\n",
            sum(elig$public_items > 0)))
cat(sprintf("\nWrote %s (%d rows).\n", OUTPUT_CSV, nrow(out)))