from src.metrics import SOLVE_DURATION, SOLVE_TOTAL, STRATEGY_TOTAL


def test_counters_exist() -> None:
    SOLVE_TOTAL.labels(outcome="solution").inc()
    SOLVE_TOTAL.labels(outcome="no_solution").inc()
    STRATEGY_TOTAL.labels(name="naive", outcome="solution").inc()


def test_histogram_exists() -> None:
    SOLVE_DURATION.observe(0.5)
    SOLVE_DURATION.observe(1.5)
