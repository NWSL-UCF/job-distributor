# job-distributor

**job-distributor** is a lightweight framework for running thousands of parameterized jobs or simulations in parallel. It is designed for research experiments where outcomes depend on specific parameter combinations, and the objective is to identify the best setup based on metrics such as accuracy, performance, or efficiency.

## Demo Video

Watch the demo video to see the job-distributor framework in action:

<div align="center">
  <img src="img/jd_demo.gif" alt="Demo Video">
</div>

## Example Use Case: MNIST Hyperparameter Tuning

To showcase how this framework works, we applied it to a classic machine learning task: hyperparameter tuning for a handwritten digit classifier using the MNIST dataset. The goal is to evaluate various combinations of hyperparameters and find the best-performing configuration. For a clearer understanding of the research task, visit: [MNIST-parameter-tuning](https://github.com/NWSL-UCF/MNIST-parameter-tuning)

## Quick Start with Docker

The server is published as a pre-built multi-arch image on Docker Hub (`linux/amd64` and `linux/arm64`). No local build required.

**1. Create your `.env` file**

```bash
cp .env.example .env
# Then edit .env and fill in your JD_API_KEY and JD_EXP_NAME
```

**2. Start the server**

```bash
docker compose up -d
```

Docker will pull `jobdistributor/jd-server:latest` automatically on first run. Job data is persisted at `./workspace/<JD_EXP_NAME>/` on your host machine.

**3. Stop the server**

```bash
docker compose down
```

---

## Setup Overview

To use the **job-distributor** framework, follow these two main steps:

1. **Start the Server** 

   The server is responsible for managing the job queue and tracking the status of each job (e.g., pending, running, completed). [Learn how to set up the server independently](/server/README.md)

2. **Configure the Clients (Worker Machines)**  
   
   The client-side code runs on each worker machine where the actual jobs will execute. Each client contacts the server to fetch an unassigned job (such as a specific hyperparameter combination), runs the task, and then reports the result back to the server. [Learn how to set up the client (Worker Machine) independently](/client/README.md)

Make sure the server is set up and running before launching any clients.

---

