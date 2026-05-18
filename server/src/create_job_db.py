import argparse
import os
import logging
import json
import shutil
from datetime import datetime
from itertools import product
from database import JobDatabase
from workspace_layout import ensure_exp_layout, exp_meta_dir

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_FILE = ""
LOG_FILENAME = "create_job_db.log"

def createExpBaseDirectory(args):
    ensure_exp_layout(BASE_DIR, args.expId)


def setup_log(args):
    LOG_FILE = os.path.join(exp_meta_dir(BASE_DIR, args.expId), LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    

def backup_existing_db(db_path):
    """Backup existing database if it exists."""
    if os.path.exists(db_path):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.replace('.db', f'_bkp_{timestamp}.db')
        shutil.copy2(db_path, backup_path)
        logging.info(f"Existing database backed up to: {backup_path}")
        return backup_path
    return None

def generate_db(db_path, parameters_dict):
    # Backup existing database if it exists
    backup_path = backup_existing_db(db_path)
    
    parameters_keys = list(parameters_dict.keys())
    parameters_values = list(parameters_dict.values())
    parameters_list = [
        json.dumps(dict(zip(parameters_keys, combination)))
        for combination in product(*parameters_values)
    ]
    
    # Initialize database and create jobs
    db = JobDatabase(db_path)
    total_jobs = db.create_jobs(parameters_list)
    
    logging.info(f"SQLite database '{db_path}' generated with {total_jobs} jobs.")
    if backup_path:
        logging.info(f"Previous database backed up as: {os.path.basename(backup_path)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate job SQLite database")
    parser.add_argument("--jobDB", default="jobs.db",
                        help="SQLite DB filename (stored under <expId>/meta/)")
    parser.add_argument("--expId", type=str, default="sim1", help="Give a unique name")
    parser.add_argument('--parameters', type=str, required=True)
    args = parser.parse_args()

    createExpBaseDirectory(args)
    setup_log(args)

    parameters_dict = json.loads(args.parameters)
    DB_FILE = os.path.join(exp_meta_dir(BASE_DIR, args.expId), args.jobDB)
    generate_db(DB_FILE, parameters_dict)

    logging.info("Job database setup complete.")
    

# python create_job_db.py --expId=sim1 --jobDB=jobs.db --parameters='{"param1": [1, 2], "param2": ["a", "b"]}'