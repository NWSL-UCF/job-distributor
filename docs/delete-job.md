# Delete Job Feature

## Overview

Jobs in `PENDING` state can be soft-deleted from the dashboard. A deleted job is excluded from worker assignment but kept in the database with its full audit history. A deleted job can be restored to `PENDING` at any time.

---

## Status Transitions

```
PENDING  →  DELETED          (delete action — dashboard only)
DELETED  →  PENDING          (restore action — dashboard only)
```

All other transitions involving `DELETED` are blocked:
- `DONE` jobs cannot be deleted (already finished).
- `SERVED` and `ABORTED` jobs cannot be deleted.
- `DELETED` jobs cannot transition to `DONE` or `ABORTED` — only back to `PENDING`.

---

## Rules

| Current Status | Delete | Restore |
|---|---|---|
| `PENDING` | Allowed | N/A |
| `SERVED` | Blocked | N/A |
| `DONE` | Blocked | N/A |
| `ABORTED` | Blocked | N/A |
| `DELETED` | N/A | Allowed (→ PENDING) |

---

## Audit Trail

Every delete and restore action appends a timestamped entry to the job's `message` audit array.

- **On delete:** `"Job Deleted: <reason>. Job will not be assigned to any worker."`
- **On restore:** `"Job Restored to PENDING: <reason>. Job is now available for assignment."`

When restored, all worker-assignment fields are reset: `requested_by`, `request_timestamp`, `completion_timestamp`, `required_time`, `last_ping_timestamp`, `initialization_timestamp`.

---

## Backend API

Both endpoints are served by `dashboard.py` (port 5050), not the job server.

### `POST /delete_job`

Soft-deletes a `PENDING` job.

**Request body:**
```json
{ "job_id": 42, "reason": "Duplicate configuration" }
```

**Responses:**

| Status | Body | Condition |
|---|---|---|
| 200 | `{"success": true, "message": "Job 42 deleted successfully"}` | Job was PENDING and is now DELETED |
| 400 | `{"success": false, "error": "Only PENDING jobs can be deleted. Current status: DONE"}` | Job is not PENDING |
| 400 | `{"success": false, "error": "Missing job_id"}` | `job_id` not provided |
| 404 | `{"success": false, "error": "Job not found"}` | No job with that ID |
| 500 | `{"success": false, "error": "<message>"}` | Unexpected server error |

---

### `POST /restore_job`

Restores a `DELETED` job back to `PENDING`.

**Request body:**
```json
{ "job_id": 42, "reason": "Restored for re-run" }
```

**Responses:**

| Status | Body | Condition |
|---|---|---|
| 200 | `{"success": true, "message": "Job 42 restored to PENDING successfully"}` | Job was DELETED and is now PENDING |
| 400 | `{"success": false, "error": "Only DELETED jobs can be restored. Current status: ABORTED"}` | Job is not DELETED |
| 400 | `{"success": false, "error": "Missing job_id"}` | `job_id` not provided |
| 404 | `{"success": false, "error": "Job not found"}` | No job with that ID |
| 500 | `{"success": false, "error": "<message>"}` | Unexpected server error |

---

## Database Layer

### `JobDatabase.delete_job(job_id, reason="")`
- Acquires the database-wide process lock (`self.lock`) — a single `threading.Lock` shared across all DB write operations, not a per-job lock.
- Selects the job only if `status = 'PENDING'` — returns `False` otherwise.
- Sets `status = 'DELETED'`, appends audit message, commits.

### `JobDatabase.restore_deleted_job(job_id, reason="")`
- Acquires the database-wide process lock (`self.lock`).
- Selects the job only if `status = 'DELETED'` — returns `False` otherwise.
- Sets `status = 'PENDING'`, resets all assignment timestamps and `requested_by` to empty/zero, appends audit message, commits.

### `get_job_counts_by_status()`
Now includes `DELETED` in the returned dict (defaults to 0 if none exist).

---

## Dashboard UI

### DELETED Tab
A new **DELETED** tab appears in the job table alongside SERVED, DONE, ABORTED, and PENDING. It shows the count of deleted jobs and lists them with their full audit history accessible via the details button.

### Stats Card
The Experiment Overview sidebar card now includes a **Deleted** stat item (trash icon, gray). The **Pending** count correctly subtracts deleted jobs.

### Job Details Modal
When opening a job's history modal:

- **PENDING job** — a red "Delete Job" section appears below the status change section. It has an optional reason input and a "Delete Job" button.
- **DELETED job** — a green "Restore Job" section appears. It has an optional reason input and a "Restore to PENDING" button. The status-change section is hidden for DELETED jobs.
- **All other statuses** — neither section is shown.

After a successful delete or restore, a success notification appears and the page reloads after 1.5 seconds.

---

## Impact on Job Assignment

`DELETED` jobs are never served to workers. The `request_job` database method selects only `WHERE status = 'PENDING'`, so deleted jobs are automatically excluded from the assignment pool without any additional logic.
