import pandas as pd

tasks = 37720 # p-tasks

first_request_timestamp = {
    "user@ec10": 1746223214,
    "user@ec12": 1746210759,
    "user@ec136": 1746232833,
    "user@ec138": 1746233266,
    "user@ec143": 1746232878,
    "user@ec15": 1746210778,
    "user@ec16": 1746211331,
    "user@ec166": 1746232549,
    "user@ec168": 1746233368,
    "user@ec17": 1746210808,
    "user@ec18": 1746210830,
    "user@ec187": 1746232418,
    "user@ec19": 1746210804,
    "user@ec191": 1746233551,
    "user@ec192": 1746232533,
    "user@ec2": 1746210756,
    "user@ec20": 1746210816,
    "user@ec21": 1746210843,
    "user@ec22": 1746210796,
    "user@ec23": 1746210799,
    "user@ec24": 1746210863,
    "user@ec29": 1746210788,
    "user@ec30": 1746210772,
    "user@ec31": 1746210858,
    "user@ec32": 1746210853,
    "user@ec33": 1746210815,
    "user@ec34": 1746210915,
    "user@ec35": 1746210875,
    "user@ec36": 1746210907,
    "user@ec37": 1746283959,
    "user@ec38": 1746210878,
    "user@ec39": 1746210834,
    "user@ec4": 1746223216,
    "user@ec40": 1746210865,
    "user@ec41": 1746210879,
    "user@ec42": 1746210900,
    "user@ec43": 1746210927,
    "user@ec44": 1746210935,
    "user@ec45": 1746210777,
    "user@ec46": 1746210860,
    "user@ec48": 1746210920,
    "user@ec5": 1746210956,
    "user@ec6": 1746210942,
    "user@ec7": 1746210753,
    "user@ec8": 1746210945,
    "user@ec9": 1746210755,
    "x-user@a007.anvil.rcac.purdue.edu": 1746553374,
    "x-user@a008.anvil.rcac.purdue.edu": 1746552052,
    "x-user@a012.anvil.rcac.purdue.edu": 1746553447,
    "x-user@a013.anvil.rcac.purdue.edu": 1746553415,
    "x-user@a014.anvil.rcac.purdue.edu": 1746552251,
    "x-user@a016.anvil.rcac.purdue.edu": 1746552218,
    "x-user@a031.anvil.rcac.purdue.edu": 1746552152,
    "x-user@a034.anvil.rcac.purdue.edu": 1746552088,
    "x-user@a035.anvil.rcac.purdue.edu": 1746552187,
    "x-user@a036.anvil.rcac.purdue.edu": 1746552120,
    "x-user@a060.anvil.rcac.purdue.edu": 1746219005,
    "x-user@a067.anvil.rcac.purdue.edu": 1746811496,
    "x-user@a074.anvil.rcac.purdue.edu": 1746219101,
    "x-user@a078.anvil.rcac.purdue.edu": 1746219128,
    "x-user@a079.anvil.rcac.purdue.edu": 1746219207,
    "x-user@a080.anvil.rcac.purdue.edu": 1746219156,
    "x-user@a082.anvil.rcac.purdue.edu": 1746219181,
    "x-user@a083.anvil.rcac.purdue.edu": 1746219233,
    "x-user@a106.anvil.rcac.purdue.edu": 1746811389,
    "x-user@a120.anvil.rcac.purdue.edu": 1746812640,
    "x-user@a130.anvil.rcac.purdue.edu": 1746812676,
    "x-user@a239.anvil.rcac.purdue.edu": 1746811316
}
mean_runtime_by_requester = {
    "user@ec10": 10348.47137,
    "user@ec12": 10117.73328,
    "user@ec136": 5130.610291,
    "user@ec138": 5133.67745,
    "user@ec143": 5204.619139,
    "user@ec15": 9971.429024,
    "user@ec16": 10334.2796,
    "user@ec166": 5138.969717,
    "user@ec168": 5137.815493,
    "user@ec17": 10262.95786,
    "user@ec18": 9888.778885,
    "user@ec187": 4122.299398,
    "user@ec19": 10257.71626,
    "user@ec191": 4108.40179,
    "user@ec192": 4103.086524,
    "user@ec2": 10012.33102,
    "user@ec20": 10017.07462,
    "user@ec21": 10345.22643,
    "user@ec22": 10405.88841,
    "user@ec23": 10167.02141,
    "user@ec24": 10006.64446,
    "user@ec29": 9754.052392,
    "user@ec30": 9448.903218,
    "user@ec31": 9403.659049,
    "user@ec32": 9249.969343,
    "user@ec33": 20806.08251,
    "user@ec34": 21312.9297,
    "user@ec35": 21014.34252,
    "user@ec36": 21000.07941,
    "user@ec37": 9474.434346,
    "user@ec38": 10037.82073,
    "user@ec39": 9481.839926,
    "user@ec4": 10528.23927,
    "user@ec40": 9500.306788,
    "user@ec41": 21658.80777,
    "user@ec42": 20899.44017,
    "user@ec43": 20360.90578,
    "user@ec44": 20982.82842,
    "user@ec45": 20295.78698,
    "user@ec46": 19414.51064,
    "user@ec48": 20834.38964,
    "user@ec5": 10421.31686,
    "user@ec6": 10202.23863,
    "user@ec7": 10059.17543,
    "user@ec8": 10289.56561,
    "user@ec9": 10309.95089,
    "x-user@a007.anvil.rcac.purdue.edu": 7889.783132,
    "x-user@a008.anvil.rcac.purdue.edu": 7896.501654,
    "x-user@a012.anvil.rcac.purdue.edu": 7924.037191,
    "x-user@a013.anvil.rcac.purdue.edu": 7883.767387,
    "x-user@a014.anvil.rcac.purdue.edu": 7911.672495,
    "x-user@a016.anvil.rcac.purdue.edu": 7884.4418,
    "x-user@a031.anvil.rcac.purdue.edu": 8014.924415,
    "x-user@a034.anvil.rcac.purdue.edu": 7763.758943,
    "x-user@a035.anvil.rcac.purdue.edu": 7819.869868,
    "x-user@a036.anvil.rcac.purdue.edu": 7718.087898,
    "x-user@a060.anvil.rcac.purdue.edu": 7736.501453,
    "x-user@a067.anvil.rcac.purdue.edu": 8338.892855,
    "x-user@a074.anvil.rcac.purdue.edu": 7853.519879,
    "x-user@a078.anvil.rcac.purdue.edu": 7882.06634,
    "x-user@a079.anvil.rcac.purdue.edu": 7876.65945,
    "x-user@a080.anvil.rcac.purdue.edu": 7803.903272,
    "x-user@a082.anvil.rcac.purdue.edu": 7897.280181,
    "x-user@a083.anvil.rcac.purdue.edu": 7799.544604,
    "x-user@a106.anvil.rcac.purdue.edu": 8170.32394,
    "x-user@a120.anvil.rcac.purdue.edu": 8653.942483,
    "x-user@a130.anvil.rcac.purdue.edu": 8442.410685,
    "x-user@a239.anvil.rcac.purdue.edu": 8589.832236
}
last_completion_timestamp = {
    "user@ec10": 1746664071,
    "user@ec12": 1746641828,
    "user@ec136": 1746478120,
    "user@ec138": 1746478129,
    "user@ec143": 1746477976,
    "user@ec15": 1746713595,
    "user@ec16": 1746641129,
    "user@ec166": 1746478233,
    "user@ec168": 1746477732,
    "user@ec17": 1746641800,
    "user@ec18": 1746642389,
    "user@ec187": 1746900832,
    "user@ec19": 1746640528,
    "user@ec191": 1746898033,
    "user@ec192": 1746900356,
    "user@ec2": 1746636135,
    "user@ec20": 1746639484,
    "user@ec21": 1746642700,
    "user@ec22": 1746665481,
    "user@ec23": 1746641951,
    "user@ec24": 1746639205,
    "user@ec29": 1746642195,
    "user@ec30": 1746642302,
    "user@ec31": 1746640692,
    "user@ec32": 1746641517,
    "user@ec33": 1746664515,
    "user@ec34": 1746640610,
    "user@ec35": 1746664815,
    "user@ec36": 1746665186,
    "user@ec37": 1746633673,
    "user@ec38": 1746640596,
    "user@ec39": 1746641468,
    "user@ec4": 1746664415,
    "user@ec40": 1746653285,
    "user@ec41": 1746654551,
    "user@ec42": 1746642292,
    "user@ec43": 1746665137,
    "user@ec44": 1746664645,
    "user@ec45": 1746641264,
    "user@ec46": 1746641074,
    "user@ec48": 1746640913,
    "user@ec5": 1746641964,
    "user@ec6": 1746639943,
    "user@ec7": 1746640010,
    "user@ec8": 1746642468,
    "user@ec9": 1746642708,
    "x-user@a007.anvil.rcac.purdue.edu": 1746812367,
    "x-user@a008.anvil.rcac.purdue.edu": 1746900774,
    "x-user@a012.anvil.rcac.purdue.edu": 1746642214,
    "x-user@a013.anvil.rcac.purdue.edu": 1746899566,
    "x-user@a014.anvil.rcac.purdue.edu": 1746641313,
    "x-user@a016.anvil.rcac.purdue.edu": 1746812619,
    "x-user@a031.anvil.rcac.purdue.edu": 1746641786,
    "x-user@a034.anvil.rcac.purdue.edu": 1746477988,
    "x-user@a035.anvil.rcac.purdue.edu": 1746900471,
    "x-user@a036.anvil.rcac.purdue.edu": 1746478282,
    "x-user@a060.anvil.rcac.purdue.edu": 1746898550,
    "x-user@a067.anvil.rcac.purdue.edu": 1746642455,
    "x-user@a074.anvil.rcac.purdue.edu": 1746811125,
    "x-user@a078.anvil.rcac.purdue.edu": 1746900315,
    "x-user@a079.anvil.rcac.purdue.edu": 1746898307,
    "x-user@a080.anvil.rcac.purdue.edu": 1746811012,
    "x-user@a082.anvil.rcac.purdue.edu": 1746900450,
    "x-user@a083.anvil.rcac.purdue.edu": 1746810811,
    "x-user@a106.anvil.rcac.purdue.edu": 1746641588,
    "x-user@a120.anvil.rcac.purdue.edu": 1746641796,
    "x-user@a130.anvil.rcac.purdue.edu": 1746642053,
    "x-user@a239.anvil.rcac.purdue.edu": 1746641308
}


df_engagement = pd.read_csv("request_summary.csv")
df_machine_stat = pd.read_csv("machine_completion_times.csv")

for key in first_request_timestamp:
    first_request_timestamp[key] = df_engagement[df_engagement["requested_by"] == key]["first_request_timestamp"].values[0]
    last_completion_timestamp[key] = df_engagement[df_engagement["requested_by"] == key]["last_completion_timestamp"].values[0]
    mean_runtime_by_requester[key] = df_machine_stat[df_machine_stat["requested_by"] == key]["mean"].values[0]


print(mean_runtime_by_requester)

# Use actual data
node_names = list(mean_runtime_by_requester.keys())
nodes = len(node_names)
print(node_names)
# Mean runtime per node (seconds)
average_task_times_on_node = [mean_runtime_by_requester[n] for n in node_names]

# Node availability (start times)
initial_time_when_node_became_available = [first_request_timestamp[n] for n in node_names]

# Track last job end time per node
last_job_completion_time_on_node = initial_time_when_node_became_available.copy()

# Initialize task count
count_of_tasks_on_node = [0] * nodes

# Simultaneous node count
simultanous_nodes = [(8 if "ec" in node_names[i] else 32) for i in range(len(node_names))]
# Greedy task assignment loop
print(simultanous_nodes)

while tasks > 0:
    # Pick node that becomes available the earliest
    node = last_job_completion_time_on_node.index(min(last_job_completion_time_on_node))

    # Assign task
    count_of_tasks_on_node[node] += simultanous_nodes[node]

    # Advance node availability time
    last_job_completion_time_on_node[node] += average_task_times_on_node[node]

    # Decrement remaining tasks
    tasks -= simultanous_nodes[node]
    # print("Node ", node_names[node], simultanous_nodes[node], last_job_completion_time_on_node[node])

# Final stats
print("Assigned Tasks per Node:")
for i, name in enumerate(node_names):
    print(f"{name:50s} | Tasks: {count_of_tasks_on_node[i]} | Final Time: {last_job_completion_time_on_node[i]:.2f}")

result_dict = {
    "machine_name": node_names, 
    "start_timestamp": [first_request_timestamp[node_name] for node_name in node_names],
    "actual_end_timestamp": [last_completion_timestamp[node_name] for node_name in node_names],
    "speculated_end_timestamp": last_job_completion_time_on_node
}  


df = pd.DataFrame(result_dict)

# Save to CSV
df.to_csv("speculative_time.csv", index=False)  
    