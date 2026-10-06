# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 600 activities, 1,100-odd dependencies, 386 BIM elements,
two disruptions, one change order. Reproduce with `make demo`, then `make load-test`.

## Reads: the forecast, the critical path and the risk register, in rotation, closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 2017 | 201.6 req/s | 5.7 ms | 6.9 ms | 7.3 ms | 0.00% |
| 8 | 2102 | 209.8 req/s | 35.6 ms | 56.4 ms | 71.3 ms | 0.00% |
| 32 | 1641 | 162.0 req/s | 148.9 ms | 188.1 ms | 208.6 ms | 0.00% |

Target from the blueprint: p95 under 500 ms for reads: met. A thousand-run Monte Carlo takes about a second, so the forecast is
simulated once per state of knowledge (site status, deliveries, announced disruptions, extra days) and served from memory after that;
every schedule import, applied scenario or change-order impact is a new state and pays the second once. The load test is therefore
measuring the cached path plus the status query and the median critical path, which is what a dashboard refreshing would see.
The first forecast after a change is about 1.1 s. `OMP_NUM_THREADS=1` in the compose file: the loop is Python over numpy and
thread pools only fight each other under concurrency.

## Compute (in-process, one core)

| Work | Time |
|---|---|
| Generate twenty past projects (12,000 activity records) | 0.01 s |
| Fit the trade distributions and procurement ratios | under 0.01 s |
| Train the late-activity classifier on 9,600 records | 0.1 s |
| Monte Carlo forecast, 1,000 runs over 600 activities | 1.0 s |
| Critical path on median durations, with float | 1 ms |
| Read a schematic facade photo | 0.04 s |
| Evaluation: three projects, 24 outcomes, 48 forecasts | about 20 s |

Not measured: a portfolio of projects, schedules of 10,000+ activities (the Monte Carlo is linear in runs × activities and would
want vectorising or fewer runs), or real image sizes.
