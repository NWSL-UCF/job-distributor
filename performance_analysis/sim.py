import random
import pandas as pd
tasks = 37720 # p-tasks


mean_runtime_by_requester = {
    "user@ec41": 21658.80777,
    "user@ec34": 21312.9297,
    "user@ec35": 21014.34252,
    "user@ec36": 21000.07941,
    "user@ec44": 20982.82842,
    "user@ec42": 20899.44017,
    "user@ec48": 20834.38964,
    "user@ec33": 20806.08251,
    "user@ec43": 20360.90578,
    "user@ec45": 20295.78698,
    "user@ec46": 19414.51064,
    "user@ec4": 10528.23927,
    "user@ec5": 10421.31686,
    "user@ec22": 10405.88841,
    "user@ec10": 10348.47137,
    "user@ec21": 10345.22643,
    "user@ec16": 10334.2796,
    "user@ec9": 10309.95089,
    "user@ec8": 10289.56561,
    "user@ec17": 10262.95786,
    "user@ec19": 10257.71626,
    "user@ec6": 10202.23863,
    "user@ec23": 10167.02141,
    "user@ec12": 10117.73328,
    "user@ec7": 10059.17543,
    "user@ec38": 10037.82073,
    "user@ec20": 10017.07462,
    "user@ec2": 10012.33102,
    "user@ec24": 10006.64446,
    "user@ec15": 9971.429024,
    "user@ec18": 9888.778885,
    "user@ec29": 9754.052392,
    "user@ec40": 9500.306788,
    "user@ec39": 9481.839926,
    "user@ec37": 9474.434346,
    "user@ec30": 9448.903218,
    "user@ec31": 9403.659049,
    "user@ec32": 9249.969343,
    "x-user@a120.anvil.rcac.purdue.edu": 8653.942483,
    "x-user@a239.anvil.rcac.purdue.edu": 8589.832236,
    "x-user@a130.anvil.rcac.purdue.edu": 8442.410685,
    "x-user@a067.anvil.rcac.purdue.edu": 8338.892855,
    "x-user@a106.anvil.rcac.purdue.edu": 8170.32394,
    "x-user@a031.anvil.rcac.purdue.edu": 8014.924415,
    "x-user@a012.anvil.rcac.purdue.edu": 7924.037191,
    "x-user@a014.anvil.rcac.purdue.edu": 7911.672495,
    "x-user@a082.anvil.rcac.purdue.edu": 7897.280181,
    "x-user@a008.anvil.rcac.purdue.edu": 7896.501654,
    "x-user@a007.anvil.rcac.purdue.edu": 7889.783132,
    "x-user@a016.anvil.rcac.purdue.edu": 7884.4418,
    "x-user@a013.anvil.rcac.purdue.edu": 7883.767387,
    "x-user@a078.anvil.rcac.purdue.edu": 7882.06634,
    "x-user@a079.anvil.rcac.purdue.edu": 7876.65945,
    "x-user@a074.anvil.rcac.purdue.edu": 7853.519879,
    "x-user@a035.anvil.rcac.purdue.edu": 7819.869868,
    "x-user@a080.anvil.rcac.purdue.edu": 7803.903272,
    "x-user@a083.anvil.rcac.purdue.edu": 7799.544604,
    "x-user@a034.anvil.rcac.purdue.edu": 7763.758943,
    "x-user@a060.anvil.rcac.purdue.edu": 7736.501453,
    "x-user@a036.anvil.rcac.purdue.edu": 7718.087898,
    "user@ec143": 5204.619139,
    "user@ec166": 5138.969717,
    "user@ec168": 5137.815493,
    "user@ec138": 5133.67745,
    "user@ec136": 5130.610291,
    "user@ec187": 4122.299398,
    "user@ec191": 4108.40179,
    "user@ec192": 4103.086524
}

nodes = len(mean_runtime_by_requester) # q-nodes
average_task_times_on_node = [mean_runtime_by_requester[key] for key in mean_runtime_by_requester]

# Initial allocation of jobs on nodes
last_job_completion_time_on_node = average_task_times_on_node.copy()

# p-tasks allocated, therefore
tasks = tasks - nodes
count_of_tasks_on_node = [1] * nodes # each node initialized with their first job
simultanous_nodes = [(8 if "ec" in key else 32) for key in mean_runtime_by_requester]
print(simultanous_nodes)
while tasks > 0:
  # Find node that finished job and is available at the earliest
  node_available_for_next_job = last_job_completion_time_on_node.index(min(last_job_completion_time_on_node))

  # Task counter updated
  count_of_tasks_on_node[node_available_for_next_job] += simultanous_nodes[node_available_for_next_job]
  
  # Increment time on node
  last_job_completion_time_on_node[node_available_for_next_job] += average_task_times_on_node[node_available_for_next_job]
  tasks -= simultanous_nodes[node_available_for_next_job]


print(count_of_tasks_on_node)


print(average_task_times_on_node)

print(last_job_completion_time_on_node)

result_dict = {
    "machine_name": [key for key in mean_runtime_by_requester],
    "average_time_on_machine": [mean_runtime_by_requester[key] for key in mean_runtime_by_requester],
    "count": count_of_tasks_on_node,
    "last_timestamp": last_job_completion_time_on_node
}
df = pd.DataFrame(result_dict)

# Save to CSV
df.to_csv("dynamic_simulation.csv", index=False)