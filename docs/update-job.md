# Update Job Parameters Feature

## Overview

Jobs in `PENDING` state can have their parameters updated from the dashboard. Every update is recorded as a timestamped entry in the job's audit history, showing exactly what changed and why. Only `PENDING` jobs are editable — once a job has been served to a worker, its parameters are locked.

---

## Rules

| Current Status | Update Parameters |
|---|---|
| `PENDING` | Allowed |
| `SERVED` | Blocked |
| `DONE` | Blocked |
| `ABORTED` | Blocked |
| `DELETED` | Blocked |

- All keys in `updates` must already exist in the job's `parameters` object. New keys cannot be introduced through this endpoint.
- At least one key must be provided; an empty `updates` object is rejected.

---

## Supported Parameter Types

The endpoint and UI handle all JSON-compatible value types:

| Type | Example | UI input |
|---|---|---|
| Integer | `32` | `<input type="number" step="1">` |
| Float / double | `0.001` | `<input type="number" step="any">` |
| String | `"adam"` | `<input type="text">` |
| JSON array | `[1, 2, 4, 8]` | `<textarea>` (pretty-printed) |
| JSON object | `{"a": 1}` | `<textarea>` (pretty-printed) |

Type is detected automatically from the current stored value. Array and object inputs are validated as JSON before submission; an invalid JSON payload shows an inline error without sending the request.

---

## Audit Trail

Every successful update appends a single timestamped entry to the job's `message` audit array. The entry lists every changed key with its old and new value.

**Format:**

```
Parameters Updated: <key1>: <old> → <new>, <key2>: <old> → <new> | Reason: <reason>
```

**Example** (two keys changed, reason provided):

```
Parameters Updated: epochs: 8 → 16, batch_size: 32 → 64 | Reason: Doubling for longer run
```

When no reason is supplied, the ` | Reason: ...` suffix is omitted:

```
Parameters Updated: optimizer: "adam" → "sgd"
```

Old and new values are serialised with `json.dumps`, so strings are quoted, arrays use brackets, and objects use braces — matching their stored representation.

---

## Backend API

The endpoint is served by `dashboard.py` (port 5050), not the job server.

### `POST /update_job_parameters`

Updates one or more parameter values for a `PENDING` job.

**Request body:**

```json
{
  "job_id": 42,
  "updates": {
    "epochs": 16,
    "optimizer": "sgd",
    "batch_size": 64
  },
  "reason": "Tuning for longer run"
}
```

- `job_id` — required.
- `updates` — required; non-empty object; keys must match existing parameter keys.
- `reason` — optional string; omit or pass `""` to record no reason.

**Responses:**

| HTTP status | Body | Condition |
|---|---|---|
| 200 | `{"success": true, "message": "Job 42 parameters updated successfully"}` | Parameters written and audit entry appended |
| 400 | `{"success": false, "error": "Missing job_id"}` | `job_id` not provided |
| 400 | `{"success": false, "error": "Missing or invalid 'updates' field. Must be a non-empty object."}` | `updates` absent, null, or not an object |
| 400 | `{"success": false, "error": "Only PENDING jobs can have their parameters updated. Current status: SERVED"}` | Job is not `PENDING` |
| 400 | `{"success": false, "error": "Unknown parameter key(s): foo, bar"}` | `updates` contains keys not present in the job |
| 400 | `{"success": false, "error": "Failed to update parameters. Job may no longer be in PENDING state."}` | Job claimed by a worker between validation and the DB write |
| 404 | `{"success": false, "error": "Job not found"}` | No job with that ID |
| 500 | `{"success": false, "error": "<message>"}` | Unexpected server error |

---

## Database Layer

### `JobDatabase.update_job_parameters(job_id, updates, reason="")`

- Acquires the database-wide process lock (`self.lock`).
- Selects the job only if `status = 'PENDING'` — returns `False` otherwise, making the lock window safe against race conditions with `request_job`.
- Merges each key from `updates` into the parsed `parameters` dict.
- Builds a diff string listing `key: <old> → <new>` for every changed key.
- Appends one audit entry to `message` and commits both `parameters` and `message` in a single `UPDATE`.
- Returns `True` on success, `False` if the job was not found or not `PENDING`.

No schema changes are required. `parameters` is already a `TEXT NOT NULL` column storing a JSON object.

---

## Dashboard UI

### Job Details Modal — PENDING jobs

When the details modal is opened for a `PENDING` job, an **Update Parameters** section (yellow border) appears below the Delete section.

- Each parameter is rendered as an editable field:
  - **Numbers** → `<input type="number">` with `step="1"` for integers and `step="any"` for floats.
  - **Strings** → `<input type="text">`.
  - **Arrays / objects** → `<textarea>` pre-filled with pretty-printed JSON.
- An optional **Reason** text field is shown below the parameter fields.
- Clicking **Save Changes**:
  1. Validates all array/object fields as JSON. Shows an inline error notification on parse failure without submitting.
  2. Validates number fields are finite numbers.
  3. `POST /update_job_parameters` with the collected key/value pairs.
  4. On success — shows a success notification and reloads the page after 1.5 s.
  5. On error — shows the server error message; the form stays open for correction.

The section is hidden for all non-`PENDING` statuses (SERVED, DONE, ABORTED, DELETED).

---

## Impact on Job Assignment

Parameter updates only affect `PENDING` jobs. Because `request_job` selects `WHERE status = 'PENDING'` and the DB method holds `self.lock` during the update, there is no window where a worker could receive an old parameter set while an update is in flight.
