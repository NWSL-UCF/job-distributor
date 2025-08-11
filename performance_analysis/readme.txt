Successfully loaded CSV with 39366 rows
Columns in CSV: ['id', 'requested_by', 'request_timestamp', 'completion_timestamp', 'required_time', 'status', 'message', 'parameters']

Found 32296 jobs with status 'DONE'
Analyzing completion times for 32296 jobs

=== GLOBAL STATISTICS ===
Total completed jobs: 32296
Total unique machines: 65
Global minimum job duration: 0 days, 0 hours, 20 minutes, 8 seconds
Global maximum job duration: 0 days, 10 hours, 42 minutes, 30 seconds
Global average job duration: 0 days, 2 hours, 19 minutes, 1 seconds

=== TIMELINE INFORMATION ===
First job request:
  ID: 48
  Requested by: user@ec7
  Request timestamp: 2025-05-02 14:32:33

Last job completion:
  ID: 32291
  Requested by: x-user@a012.anvil.rcac.purdue.edu
  Completion timestamp: 2025-05-08 10:30:58

Total elapsed time from first job request to last job completion:
  5 days, 19 hours, 58 minutes, 24 seconds

=== TOP 10 MACHINES BY MAXIMUM COMPLETION TIME ===
   Machine  Jobs  Min (s)  Max (s)  Avg (s)  Min (h)  Max (h)  Avg (h)
user@ec36   160  9288.18 38550.97 21000.08     2.58    10.71     5.83
user@ec45   167  9088.65 38099.09 20295.79     2.52    10.58     5.64
user@ec42   161  9219.96 37546.28 20899.44     2.56    10.43     5.81
user@ec34   159  9270.03 37122.19 21312.93     2.58    10.31     5.92
user@ec35   160  9168.85 37077.99 21014.34     2.55    10.30     5.84
user@ec41   153  9619.52 36829.41 21658.81     2.67    10.23     6.02
user@ec44   161  9292.67 36808.34 20982.83     2.58    10.22     5.83
user@ec33   162  9495.00 36185.73 20806.08     2.64    10.05     5.78
user@ec43   166  9333.12 36147.65 20360.91     2.59    10.04     5.66
user@ec48   162  8699.39 35385.77 20834.39     2.42     9.83     5.79

=== BOTTOM 10 MACHINES BY MAXIMUM COMPLETION TIME ===
               Machine  Jobs  Min (s)  Max (s)  Avg (s)  Min (h)  Max (h)  Avg (h)
            user@apt35   364  1208.90  4972.83  2816.72     0.34     1.38     0.78
           user@ec192   837  1763.18  7274.30  4103.09     0.49     2.02     1.14
           user@ec191   837  1783.50  7289.82  4108.40     0.50     2.02     1.14
ab823254@CECSL78755036  1175  1269.56  7343.58  3446.75     0.35     2.04     0.96
           user@ec187   833  1766.65  7427.64  4122.30     0.49     2.06     1.15
           user@ec143   659  2234.93  8938.81  5204.62     0.62     2.48     1.45
           user@ec138   669  2223.48  9022.72  5133.68     0.62     2.51     1.43
           user@ec168   668  2228.12  9049.48  5137.82     0.62     2.51     1.43
           user@ec136   670  2224.04  9088.60  5130.61     0.62     2.52     1.43
           user@ec166   668  2252.31  9258.51  5138.97     0.63     2.57     1.43

Full results saved to 'machine_completion_times.csv'
