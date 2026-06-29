"""jd — Job Distributor client package."""
__version__ = "1.29.0"

from jd._session import init_session
from jd.files import jd_get_last_checkpoint, jd_update_checkpoint, jd_upload
from jd.job_mgmt import (
    create_jobs,
    data_root,
    download_result,
    exp_path,
    get_job_statuses,
    job_download_dir,
    job_path,
    list_job_uploads,
    list_jobs,
    wait_for_jobs,
)
from jd.paths import jd_exp_dir, jd_job_dir, jd_worker_workspace


def init(env_file=None, *, exp_id=None, hub_url=None):
    """
    Connect to your experiment from your local machine.

    Load credentials from a ``.env`` file (recommended) or environment variables:

    ``JD_API_KEY``, ``JD_EXP_ID``, optional ``JD_WORKSPACE_PATH``, ``JD_HUB_URL``.
    """
    return init_session(env_file, exp_id=exp_id, hub_url=hub_url)


__all__ = [
    "init",
    "create_jobs",
    "get_job_statuses",
    "list_jobs",
    "list_job_uploads",
    "download_result",
    "wait_for_jobs",
    "data_root",
    "exp_path",
    "job_path",
    "job_download_dir",
    "jd_upload",
    "jd_update_checkpoint",
    "jd_get_last_checkpoint",
    "jd_job_dir",
    "jd_worker_workspace",
    "jd_exp_dir",
]
