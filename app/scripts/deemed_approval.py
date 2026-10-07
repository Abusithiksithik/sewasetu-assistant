# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not processed
within the statutory SLA stands approved. This job marks such applications
DEEMED_APPROVED and notifies the applicant by SMS.

Scheduled via cron, 02:00 daily. See deploy/crontab.

Changes (fellowship, Oct 2026):
  * Deemed approval is a remedy for the department's delay, not a waiver of the
    scheme's rules. An application that fails the age rule (under 60 on the day it
    was submitted) is NOT deemed approved; it
    is left PENDING, escalated in the audit log once, and shows up at the top of
    the officer's queue. 264 under-60 applicants were deemed approved this way in
    the 2025-26 data.
  * The audit rows are written for exactly the rows this run updated (RETURNING),
    instead of "anything deemed in the last minute", which missed rows on a slow
    run and double-logged on a quick re-run.
  * Document presence is NOT checked here: no row in the 2026-09-30 snapshot has a
    doc_path (files were not migrated), so that rule would block every application.
  * The job runs at container start against the restored snapshot; DRY_RUN=1
    prints what it would do without changing anything.
"""

import os
import sys
import configparser
from datetime import datetime, timedelta

import psycopg2
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

config = configparser.ConfigParser()
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app.ini")
with open(CONFIG_PATH) as fh:
    config.read_file(fh)

SLA_DAYS = config.getint("pension", "sla_days")
MIN_AGE = config.getint("pension", "min_age")
SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL") or config.get("app", "sms_gateway_url")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# age in whole years on the submission date, computed in SQL
AGE_AT_SUBMISSION = "date_part('year', age(submitted_at::date, dob))"


def main():
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST") or config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=config.get("database", "password"),
    )
    cur = conn.cursor()
    now = datetime.now()
    cutoff = now - timedelta(days=SLA_DAYS)

    # 1. Overdue but ineligible: escalate once, do not approve.
    cur.execute(
        """SELECT a.id, a.application_no FROM applications a
           WHERE a.status = 'PENDING' AND a.submitted_at < %s
             AND (""" + AGE_AT_SUBMISSION + """ < %s)
             AND NOT EXISTS (SELECT 1 FROM audit_log l WHERE l.application_id = a.id
                             AND l.action = 'SLA_ESCALATED')""",
        (cutoff, MIN_AGE))
    escalated = cur.fetchall()

    # 2. Overdue and eligible: deemed approved.
    cur.execute(
        """SELECT id, application_no, mobile FROM applications
           WHERE status = 'PENDING' AND submitted_at < %s
             AND """ + AGE_AT_SUBMISSION + """ >= %s""",
        (cutoff, MIN_AGE))
    to_deem = cur.fetchall()

    if DRY_RUN:
        print("DRY RUN: would deem %d, escalate %d" % (len(to_deem), len(escalated)))
        conn.rollback()
        return 0

    for app_id, _ in escalated:
        cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                    "VALUES (%s, 'SLA_ESCALATED', 'system:rtps-cron', %s, %s)",
                    (app_id, "SLA passed but not deemed: applicant was under 60 on the "
                             "submission date; needs an officer decision", now))
    rows = []
    for app_id, app_no, mobile in to_deem:
        cur.execute("UPDATE applications SET status = 'DEEMED_APPROVED', decided_at = %s, "
                    "decided_by = 'RTPS-AUTO' WHERE id = %s AND status = 'PENDING' RETURNING id",
                    (now, app_id))
        if cur.fetchone():
            cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                        "VALUES (%s, 'DEEMED_APPROVED', 'system:rtps-cron', "
                        "'not processed within SLA', %s)", (app_id, now))
            rows.append((app_no, mobile))
    conn.commit()
    for app_no, mobile in rows:
        try:
            requests.post(SMS_GATEWAY_URL + "/api/send", json={
                "to": mobile,
                "text": "Sewa Setu: your pension application %s stands approved "
                        "under the RTPS Act." % app_no}, timeout=5)
        except Exception:
            pass
    print("%s deemed approval: %d approved, %d escalated (not eligible for deemed approval)"
          % (now.strftime("%Y-%m-%d %H:%M:%S"), len(rows), len(escalated)))
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
