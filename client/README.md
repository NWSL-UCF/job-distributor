# Worker client (`jd-worker`)

Workers run **`jd_worker`**, which requests jobs from the job server, executes your **entry script** with parameters as CLI flags, sends heartbeats, and reports **DONE** / **ABORTED**.

Full behaviour, environment variables, local paths (`~/jd_data/…`), and library helpers (`jd_upload`, checkpoints, `jd_job_dir`, …) are documented in **[`docs/jd-worker.md`](../docs/jd-worker.md)**.

---

## Install

From the repo root (editable):

```bash
cd job-distributor/client
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e .
```

Or install from GitHub (see `docs/jd-worker.md` for branch / subdirectory).

---

## Run

```bash
jd_worker expId=<experiment_id> entry_script=<your_script.py>
```

Optional: `server=`, `port=`, `machine_type=`, `once=true`, etc. — see `jd_worker help` or **`docs/jd-worker.md`**.

---

## Example workload

For an end-to-end ML-style example (MNIST tuning), see  
[MNIST-parameter-tuning](https://github.com/NWSL-UCF/MNIST-parameter-tuning). Point **`entry_script`** at that repo’s training script and align **`expId`** with the server.
