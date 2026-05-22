# JobDistributor

**JobDistributor** is a lightweight framework for running large-scale parameterized experiments in parallel across any number of heterogeneous machines — laptops, desktops, multiple HPC clusters, VPS instances, or cloud VMs — without changing a single line of your training code.

## Demo

<div align="center">
  <img src="img/jd_demo.gif" alt="JobDistributor Dashboard Demo">
</div>

## Getting Started

Full documentation, step-by-step setup guide, and library reference are available at:

**[hub.jobdistributor.net/learn/getting-started](https://hub.jobdistributor.net/learn/getting-started)**

## Quick Start

**1.** Create a free account and experiment at [hub.jobdistributor.net](https://hub.jobdistributor.net)

**2.** Start the job server on any machine with Docker:

```bash
JD_API_KEY=jd_xxxx ./run.sh <experiment-name>
```

**3.** Install the worker library and run workers on any machine:

```bash
pip install jd-worker

export JD_API_KEY=jd_xxxx
jd_worker_cli expId=<experiment-name> entry_script=train.py
```

## Links

| | |
|---|---|
| Hub | [hub.jobdistributor.net](https://hub.jobdistributor.net) |
| Documentation | [hub.jobdistributor.net/learn/getting-started](https://hub.jobdistributor.net/learn/getting-started) |
| PyPI (`jd-worker`) | [pypi.org/project/jd-worker](https://pypi.org/project/jd-worker/) |
| Docker Hub | [hub.docker.com/repositories/jobdistributor](https://hub.docker.com/repositories/jobdistributor) |
| Example workload | [NWSL-UCF/MNIST-parameter-tuning](https://github.com/NWSL-UCF/MNIST-parameter-tuning) |

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

Free for personal, academic, and research use. Commercial use is not permitted.

---

*Developed at the [Networking and Wireless Systems Lab (NWSL)](https://www.nwsl.ucf.edu/), University of Central Florida. All rights reserved.*
