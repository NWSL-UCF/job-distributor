
## Worker Machine Setup

This section explains how to set up a worker machine to run atomic hyperparameter tuning jobs. In this case, we use the [MNIST-parameter-tuning](https://github.com/anonymous/MNIST-parameter-tuning) repository as the task workload. It's a good practice to set up the client in a separate directory from the server when running both on the same machine. This helps avoid confusion and keeps the server and client environments isolated and organized.

---

### 1. Clone the Repository and Install Dependencies

#### For **Linux/macOS**:
```bash
git clone https://github.com/anonymous/job-distributor.git
cd job-distributor/client
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### For **Windows** (Command Prompt or PowerShell):
```cmd
git clone https://github.com/anonymous/job-distributor.git
cd job-distributor\client
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

---

### 2. Clone MNIST-Parameter-Tuning Code Inside the Client Folder

#### For **Linux/macOS**:
```bash
git clone https://github.com/anonymous/MNIST-parameter-tuning.git
cp MNIST-parameter-tuning/* ./
pip install -r requirements.txt
```

#### For **Windows**:
```cmd
git clone https://github.com/anonymous/MNIST-parameter-tuning.git
xcopy MNIST-parameter-tuning\*.* .\ /E /Y
pip install -r requirements.txt
```

---

### 3. Configure the Client

Edit `config.json` inside the `client/` folder. Example:

```json
{
    "expId": "mnist_param_tune",
    "job_server": "https://<your-ngrok-url>.ngrok-free.app",
    "port": 5000,
    "number_of_parallel_process": 3,
    "heartBitInterval": 60,
    "run_command": ["python", "main.py"],
    "machine_type": "desktop", 
    "_comment": "Machine Types: hpc, htc, desktop, laptop"
}
```

#### Key Parameters:
- **`expId`**: Must match the `expId` used in the server’s `config.json`.
- **`job_server`**: Copy the ngrok job server URL shown when you started the server.
- **`number_of_parallel_process`**: Controls how many jobs run in parallel on this machine.  
  Choose based on available CPU cores to avoid overloading the system.
- **`heartBitInterval`**: How often each job sends a "heartbeat" to the server (in seconds).  
  Must be **less than** the server’s `idleTimeout`.
- **`run_command`**: Command to run each job. In this case: `["python", "main.py"]`.
- **`machine_type`**: Label to identify the type of machine (`hpc`, `htc`, `desktop`, or `laptop`).

---

### 4. Run the Worker Machine

#### For **Linux/macOS**:
```bash
python start.py &
```

#### For **Windows**:
```powershell
Start-Process python -ArgumentList "start.py"
```

---

### 5. Logs and Output Results

Job logs and output will be saved in:

#### For **Linux/macOS**:
```bash
~/data/raw/<expId>/
```

#### For **Windows**:
```
%USERPROFILE%\data\raw\<expId>\
```

---

### 6. Dashboard After Running Worker Machine

In the example below, we ran **10 parallel processes** on a desktop machine. The job dashboard displayed the following:

- **After job assignment**  
  ![Jobs Assigned](../img/dashboard_after_job_assign.png)

- **After a job is aborted due to an error**  
  ![Job Aborted](../img/dashboard_after_job_aborted.png)

- **After successful job completion**  
  ![Job Done](../img/dashboard_after_job_done.png)

---
Make sure to check this folder to analyze individual job performance or debug failed jobs.
