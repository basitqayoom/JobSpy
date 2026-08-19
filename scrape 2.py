"""Scrape LinkedIn jobs in India with hour and/or minute recency filters."""

import csv

from jobspy import scrape_jobs

SEARCH_TERM = "frontend"
LOCATION = "India"
RESULTS_WANTED = 100
HOURS_OLD = None  # set to None to use only MINUTES_OLD
MINUTES_OLD = 15  # e.g. 15 or 30; combines with HOURS_OLD
OUTPUT_FILE = "scrape 2.csv"

jobs = scrape_jobs(
    site_name="linkedin",
    search_term=SEARCH_TERM,
    location=LOCATION,
    results_wanted=RESULTS_WANTED,
    hours_old=HOURS_OLD,
    minutes_old=MINUTES_OLD,
    verbose=1,
)

print(f"Found {len(jobs)} jobs")
if not jobs.empty:
    print(jobs[["site", "title", "company", "location", "date_posted", "job_url"]].head(10))

jobs.to_csv(
    OUTPUT_FILE,
    quoting=csv.QUOTE_NONNUMERIC,
    escapechar="\\",
    index=False,
)
print(f"Saved to {OUTPUT_FILE}")
