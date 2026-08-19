"""Scrape LinkedIn jobs in India posted within the last hour."""

import csv

from jobspy import scrape_jobs

SEARCH_TERM = "frontend"
LOCATION = "India"
RESULTS_WANTED = 100
HOURS_OLD = 1
OUTPUT_FILE = "scrape.csv"

jobs = scrape_jobs(
    site_name="linkedin",
    search_term=SEARCH_TERM,
    location=LOCATION,
    results_wanted=RESULTS_WANTED,
    hours_old=HOURS_OLD,
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
