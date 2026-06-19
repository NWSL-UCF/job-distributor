import argparse
import os
import logging
import json
from itertools import product
from database import JobDatabase
from workspace_layout import ensure_exp_layout, exp_meta_dir

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LOG_FILENAME = "create_job_db.log"


def setup_log(args):
    LOG_FILE = os.path.join(exp_meta_dir(BASE_DIR, args.expId), LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def generate_jobs(parameters_dict):
    parameters_keys = list(parameters_dict.keys())
    parameters_values = list(parameters_dict.values())
    parameters_list = [
        json.dumps(dict(zip(parameters_keys, combination)))
        for combination in product(*parameters_values)
    ]

    db = JobDatabase(os.environ["DATABASE_URL"])
    total_jobs = db.create_jobs(parameters_list)

    logging.info(f"Database populated with {total_jobs} jobs.")
    return total_jobs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate the job database")
    parser.add_argument("--expId", type=str, default="sim1", help="Give a unique name")
    parser.add_argument('--parameters', type=str, required=True)
    args = parser.parse_args()

    ensure_exp_layout(BASE_DIR, args.expId)
    setup_log(args)

    parameters_dict = json.loads(args.parameters)
    total = generate_jobs(parameters_dict)

    logging.info("Job database setup complete.")
    print(f"Created {total} jobs.")


# python create_job_db.py --expId=sim1 --parameters='{"param1": [1, 2], "param2": ["a", "b"]}'
