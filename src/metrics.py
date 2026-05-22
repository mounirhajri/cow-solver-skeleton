from prometheus_client import Counter, Histogram

SOLVE_TOTAL = Counter(
    "cow_solver_solve_total",
    "Total /solve invocations by outcome.",
    ["outcome"],  # solution | no_solution | error
)

STRATEGY_TOTAL = Counter(
    "cow_solver_strategy_total",
    "Strategy attempts by name and outcome.",
    ["name", "outcome"],  # solution | no_solution | timeout | error
)

SOLVE_DURATION = Histogram(
    "cow_solver_solve_duration_seconds",
    "End-to-end /solve duration.",
    buckets=(0.1, 0.5, 1, 2, 5, 8, 10, 13, 15, 20),
)
